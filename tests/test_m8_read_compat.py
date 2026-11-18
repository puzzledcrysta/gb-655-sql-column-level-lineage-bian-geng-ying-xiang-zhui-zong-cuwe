"""M8 扩容读写兼容：值域依据、报表读取能力、未登记结论、能力更新重算。"""

from __future__ import annotations

import json

from app import config, store
from app.modules.m1_intake import service as intake
from app.modules.m3_compat import capability, reports, service as compat
from app.modules.m6_batches import service as batch_service
from tests.fixtures import expected


# ---------- 纯规则：读取能力比较 ----------

def _consumer(capability_value, name="dw.report"):
    return {
        "name": name,
        "kind": "view",
        "read_capability": capability_value,
        "read_capability_error": None,
    }


def test_capability_scale_is_the_deciding_factor():
    """decimal(20,4) 的值域比 decimal(18,2) 大，但旧报表按 2 位小数读会丢精度。"""
    lossy = capability.evaluate(_consumer({"*": "decimal(18,2)"}),
                                "dim_order", "order_amt", "decimal(20,4)")
    assert lossy["status"] == capability.STATUS_LOSSY
    assert lossy["detail"] == "scale_exceeds_capability"
    assert lossy["reason"] == "READ_CAPABILITY_LOSSY"

    ok = capability.evaluate(_consumer({"*": "decimal(20,4)"}),
                             "dim_order", "order_amt", "decimal(20,4)")
    assert ok["status"] == capability.STATUS_OK


def test_capability_range_and_exactness_are_also_checked():
    narrow_range = capability.evaluate(_consumer({"*": "decimal(12,4)"}),
                                       "dim_order", "order_amt", "decimal(20,4)")
    assert narrow_range["status"] == capability.STATUS_LOSSY
    assert narrow_range["detail"] == "range_exceeds_capability"

    exactness = capability.evaluate(_consumer({"*": "double"}),
                                    "fct_pay", "pay_id", "bigint")
    assert exactness["status"] == capability.STATUS_LOSSY
    assert exactness["detail"] == "exactness_exceeds_capability"


def test_capability_family_mismatch_is_lossy():
    verdict = capability.evaluate(_consumer({"*": "bigint"}),
                                  "dim_user", "user_name", "varchar(64)")
    assert verdict["status"] == capability.STATUS_LOSSY
    assert verdict["detail"] == "family_mismatch"


def test_text_and_temporal_capability():
    widen = capability.evaluate(_consumer({"*": "varchar(64)"}),
                                "dim_user", "user_name", "varchar(64)")
    assert widen["status"] == capability.STATUS_OK

    short = capability.evaluate(_consumer({"*": "varchar(32)"}),
                                "dim_user", "user_name", "varchar(64)")
    assert short["status"] == capability.STATUS_LOSSY
    assert short["detail"] == "length_exceeds_capability"

    date_only = capability.evaluate(_consumer({"*": "date"}),
                                    "dim_user", "created_at", "timestamp")
    assert date_only["status"] == capability.STATUS_LOSSY
    assert date_only["detail"] == "temporal_exceeds_capability"


def test_unregistered_report_has_explicit_conclusion():
    """没登记读取能力必须有明确结论，不能默认放行。"""
    verdict = capability.evaluate(_consumer(None), "dim_order", "order_amt", "decimal(20,4)")
    assert verdict["status"] == capability.STATUS_UNREGISTERED
    assert verdict["reason"] == "READ_CAPABILITY_MISSING"
    assert verdict["detail"] == "report_unregistered"
    assert verdict["repair"] == "register_capability"

    partial = capability.evaluate(_consumer({"dim_order.order_id": "bigint"}),
                                  "dim_order", "order_amt", "decimal(20,4)")
    assert partial["status"] == capability.STATUS_UNREGISTERED
    assert partial["detail"] == "family_unregistered"

    invalid = capability.evaluate(_consumer({"*": "not_a_type(,,,)"}),
                                  "dim_order", "order_amt", "decimal(20,4)")
    assert invalid["status"] == capability.STATUS_INVALID


def test_registration_parsing_forms():
    assert capability.normalize_registration("decimal(18,2)") == ({"*": "decimal(18,2)"}, None)
    assert capability.normalize_registration(None) == (None, None)
    assert capability.normalize_registration(12)[1] is not None
    normalized, error = capability.normalize_registration(
        {"Dim_Order.Order_Amt": "decimal(18,2)", "*": "varchar(8)"})
    assert error is None
    assert normalized["dim_order.order_amt"] == "decimal(18,2)"
    assert capability.lookup(normalized, "dim_order", "order_amt") == "decimal(18,2)"
    assert capability.lookup(normalized, "dim_order", "order_no") == "varchar(8)"
    assert capability.lookup(normalized, "dim_user", "user_name") == "varchar(8)"


# ---------- 批次级：写端 + 读端合成 ----------

def _diff(rows):
    buckets: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    for row in rows:
        buckets[row["bucket"]] = buckets.get(row["bucket"], 0) + 1
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
    return {"rows": rows, "buckets": buckets, "verdicts": verdicts,
            "counts": {"old_total": len(rows), "new_total": len(rows)}}


def _row(**overrides):
    row = {
        "field_id": "1", "table": "dim_order", "old_name": "order_amt",
        "new_name": "order_amt", "old_type": "decimal(18,2)",
        "new_type": "decimal(20,4)", "change": "widen", "type_detail": None,
        "bucket": "retyped", "verdict": "COMPATIBLE", "primary": "TYPE_WIDEN",
        "required_change": "same", "ref_count": 1,
        "reader_ok": True, "writer_ok": True,
        "reader": {"ok": True, "reason": "COMPATIBLE", "repair": None},
        "writer": {"ok": True, "reason": "COMPATIBLE", "repair": None},
        "type_evidence": {
            "old": {"lo": "-9999999999999999.99", "hi": "9999999999999999.99",
                    "quantum": "0.01", "exact": "9999999999999999.99"},
            "new": {"lo": "-9999999999999999.9999", "hi": "9999999999999999.9999",
                    "quantum": "0.0001", "exact": "9999999999999999.9999"},
        },
    }
    row.update(overrides)
    return row


def _resolution(field_id="1", consumer="dw.report", ref="dim_order.order_amt"):
    table, _, column = ref.rpartition(".")
    return {
        "resolved": [{"consumer": consumer, "field_id": field_id, "ref": ref,
                      "table": table, "column": column, "via": "direct"}],
        "broken": [],
        "reachable": 1,
        "broken_count": 0,
    }


def _consumers(capability_value):
    return [{"name": "dw.report", "kind": "view", "refs": ["dim_order.order_amt"],
             "read_capability": capability_value, "read_capability_error": None}]


def test_batch_compatible_when_reader_covers_new_range():
    built = reports.build(_diff([_row()]), _consumers({"*": "decimal(20,4)"}),
                          _resolution())
    assert built["verdict"] == "BATCH_COMPATIBLE"
    assert built["write"]["allowed"] is True
    assert built["read"]["summary"]["ok_reports"] == 1


def test_batch_partial_keeps_write_open_but_flags_reader():
    built = reports.build(_diff([_row()]), _consumers({"*": "decimal(18,2)"}),
                          _resolution())
    assert built["verdict"] == "BATCH_READ_PARTIAL"
    assert built["write"]["allowed"] is True
    report = built["read"]["reports"][0]
    assert report["status"] == capability.STATUS_LOSSY
    assert report["fields"][0]["detail"] == "scale_exceeds_capability"
    assert any("刻度" in line for line in built["basis"])


def test_batch_partial_when_capability_missing():
    built = reports.build(_diff([_row()]), _consumers(None), _resolution())
    assert built["verdict"] == "BATCH_READ_PARTIAL"
    report = built["read"]["reports"][0]
    assert report["status"] == "unresolved"
    assert report["reason"] == "READ_CAPABILITY_MISSING"
    assert report["repair"] == "register_capability"


def test_batch_breaking_when_writer_blocked():
    """放宽同时收紧必填：桶仍是 retyped，但写端被拦下，整批扩容不能放行。"""
    blocked = _row(required_change="tightened", verdict="BREAKING", writer_ok=False,
                   writer={"ok": False, "reason": "REQUIRED_TIGHTENED",
                           "repair": "default_value"})
    built = reports.build(_diff([blocked]), _consumers({"*": "decimal(20,4)"}),
                          _resolution())
    assert built["verdict"] == "BATCH_BREAKING"
    assert built["write"]["allowed"] is False
    assert built["write"]["blocked"][0]["writer_reason"] == "REQUIRED_TIGHTENED"


def test_batch_without_widen_is_explicitly_not_applicable():
    """没有扩容字段时不能默默算作兼容，要给出「本批不适用」的明确结论。"""
    built = reports.build(_diff([]), _consumers(None), {"resolved": [], "broken": []})
    assert built["verdict"] == "BATCH_NOT_APPLICABLE"
    assert built["reason"] == "BATCH_NO_WIDEN"
    assert built["scope"]["widened_fields"] == 0


def test_widened_field_with_broken_reference_is_reported():
    """引用了扩容字段但引用已断开时，该报表要给断链结论，不能只看能力。"""
    diff = _diff([_row()])
    resolution = {
        "resolved": [],
        "broken": [{"consumer": "dw.report", "kind": "view", "ref": "dim_order.order_amt",
                    "table": "dim_order", "column": "order_amt", "via": "deleted",
                    "detail": "字段已删除"}],
        "reachable": 0,
        "broken_count": 1,
    }
    built = reports.build(diff, _consumers({"*": "decimal(20,4)"}), resolution)
    report = built["read"]["reports"][0]
    assert report["status"] == "broken"
    assert report["reason"] == "REF_BROKEN"
    assert report["repair"] == "fix_reference"
    assert report["broken_links"][0]["detail"] == "字段已删除"
    assert built["verdict"] == "BATCH_READ_PARTIAL"


def test_invalid_registration_is_unresolved_not_silently_ok():
    """登记结构不合法时不能当成无损；要落到 READ_CAPABILITY_MISSING。"""
    consumer = {"name": "dw.report", "kind": "view", "refs": ["dim_order.order_amt"],
                "read_capability": None,
                "read_capability_error": "read_capability.* 必须是类型写法"}
    built = reports.build(_diff([_row()]), [consumer], _resolution())
    report = built["read"]["reports"][0]
    assert report["status"] == "unresolved"
    assert report["reason"] == "READ_CAPABILITY_MISSING"
    assert report["capability_text"] == "登记无法解析"
    assert report["fields"][0]["detail"] == "capability_registration_invalid"
    assert built["verdict"] == "BATCH_READ_PARTIAL"


def test_all_reports_unregistered_blocks_blanket_cutover():
    """存量报表全都没登记读取能力时，逐条给出未登记结论，不整批判兼容。"""
    diff = _diff([_row(), _row(field_id="2", table="fct_pay",
                               old_name="pay_amt", new_name="pay_amt")])
    consumers = [
        {"name": "dw.a", "kind": "view", "refs": ["dim_order.order_amt"],
         "read_capability": None, "read_capability_error": None},
        {"name": "dw.b", "kind": "view", "refs": ["fct_pay.pay_amt"],
         "read_capability": None, "read_capability_error": None},
    ]
    resolution = {
        "resolved": [
            {"consumer": "dw.a", "field_id": "1", "ref": "dim_order.order_amt",
             "table": "dim_order", "column": "order_amt", "via": "direct"},
            {"consumer": "dw.b", "field_id": "2", "ref": "fct_pay.pay_amt",
             "table": "fct_pay", "column": "pay_amt", "via": "direct"},
        ],
        "broken": [], "reachable": 2, "broken_count": 0,
    }
    built = reports.build(diff, consumers, resolution)
    assert built["verdict"] == "BATCH_READ_PARTIAL"
    assert built["read"]["summary"]["unresolved_reports"] == 2
    assert built["read"]["summary"]["ok_reports"] == 0
    assert all(report["reason"] == "READ_CAPABILITY_MISSING"
               for report in built["read"]["reports"])


def test_reports_not_referencing_widened_field_are_not_affected():
    consumers = _consumers({"*": "decimal(18,2)"})
    consumers.append({"name": "dw.other", "kind": "view", "refs": [],
                      "read_capability": None, "read_capability_error": None})
    built = reports.build(_diff([_row()]), consumers, _resolution())
    other = next(item for item in built["read"]["reports"]
                 if item["consumer"] == "dw.other")
    assert other["status"] == "not_affected"
    assert other["reason"] == "READ_CAPABILITY_NOT_REQUIRED"
    assert built["read"]["summary"]["not_affected_reports"] == 1


# ---------- 样例批次：结论与幂等 ----------

def test_sample_batch_verdict(run):
    compatibility = run["compatibility"]
    assert compatibility["verdict"] == "BATCH_READ_PARTIAL"
    assert compatibility["write"]["allowed"] is True
    scope = compatibility["scope"]
    assert scope["widened_fields"] == expected.WIDENED_FIELDS
    assert scope["referenced_fields"] == expected.WIDENED_REFERENCED

    summary = compatibility["read"]["summary"]
    assert summary["affected_reports"] == expected.READ_AFFECTED
    assert summary["lossy_reports"] == expected.READ_LOSSY
    assert summary["unresolved_reports"] == expected.READ_UNRESOLVED

    reports_by_name = {item["consumer"]: item for item in compatibility["read"]["reports"]}
    assert "dw.mv_user_dim" in reports_by_name
    assert reports_by_name["dw.mv_user_dim"]["reason"] == "READ_CAPABILITY_MISSING"
    assert reports_by_name["dw.mv_order_daily"]["reason"] == "READ_CAPABILITY_LOSSY"
    assert reports_by_name["dw.task_settle"]["status"] == "ok"


def test_every_report_has_a_verdict(run):
    """每个登记报表都要有结论：无损 / 丢精度 / 未登记 / 断链 / 不受影响。"""
    compatibility = run["compatibility"]
    assert len(compatibility["read"]["reports"]) == expected.CONSUMERS
    for report in compatibility["read"]["reports"]:
        assert report["status"] in {"ok", "lossy", "unresolved", "broken", "not_affected"}
        assert report["reason"]
        assert report["status_label"]
        assert report["capability_text"]


def test_capability_update_creates_new_batch_and_rerun_is_stable(client, monkeypatch):
    """上游读取能力更新后重算产生新批次；同输入重跑不新增批次。"""
    first = store.latest_run()
    before = store.count_runs()

    refs_path = config.resolve_sample("refs_downstream.json")
    backup = refs_path.read_bytes()
    data = json.loads(backup)
    for consumer in data["consumers"]:
        if consumer["name"] == "dw.mv_order_daily":
            consumer["read_capability"]["dim_order.order_amt"] = "decimal(20,4)"
        if consumer["name"] == "dw.mv_user_dim":
            consumer["read_capability"] = {"dim_user.user_name": "varchar(64)"}
    try:
        refs_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        second = batch_service.execute()
        assert second["run_id"] != first["run_id"]
        assert second["compatibility"]["verdict"] == "BATCH_COMPATIBLE"
        assert store.count_runs() == before + 1

        again = batch_service.execute()
        assert again["run_id"] == second["run_id"]
        assert again["reused"] is True
        assert store.count_runs() == before + 1
    finally:
        refs_path.write_bytes(backup)


def test_upstream_field_update_creates_new_batch(client):
    """上游字段扩容取值更新后重算：产生新批次，读端结论随之变化。"""
    first = store.latest_run()
    before = store.count_runs()
    read_before = {item["consumer"]: item for item in
                   first["compatibility"]["read"]["reports"]}
    assert read_before["dw.task_settle"]["status"] == "ok"

    new_path = config.resolve_sample("schema_v2026_09.sql")
    backup = new_path.read_bytes()
    try:
        # 把 user_name 继续放大到 varchar(96)：仍是扩容，但超出 task_settle 登记的 64。
        updated = backup.replace(b"VARCHAR(64)   NOT NULL,  -- fid=3002",
                                 b"VARCHAR(96)   NOT NULL,  -- fid=3002")
        assert updated != backup
        new_path.write_bytes(updated)

        second = batch_service.execute()
        assert second["run_id"] != first["run_id"]
        assert store.count_runs() == before + 1
        read_after = {item["consumer"]: item for item in
                      second["compatibility"]["read"]["reports"]}
        assert read_after["dw.task_settle"]["status"] == "lossy"

        again = batch_service.execute()
        assert again["run_id"] == second["run_id"]
        assert again["reused"] is True
        assert store.count_runs() == before + 1
    finally:
        new_path.write_bytes(backup)


def test_sample_widened_and_narrowed_fields():
    """样例里三个字段是放宽、pay_amt 是整数位变少的收窄，不能混为一谈。"""
    from app.modules.m2_types import service as types

    snap_old = types.prepare(intake.ingest("schema_v2026_06.sql"))
    snap_new = types.prepare(intake.ingest("schema_v2026_09.sql"))
    diff = compat.diff_snapshots(snap_old, snap_new)
    widened = [row for row in diff["rows"] if row["change"] == "widen"]
    assert {row["old_name"] for row in widened} == {"order_amt", "user_name", "created_at"}
    pay_amt = next(row for row in diff["rows"] if row["old_name"] == "pay_amt")
    assert pay_amt["change"] == "narrow"
    assert pay_amt["type_detail"] == "int_digits_drop"
