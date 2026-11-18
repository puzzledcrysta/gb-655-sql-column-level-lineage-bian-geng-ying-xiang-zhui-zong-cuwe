"""批次读写兼容：把「写端放行」和「读端按报表分批」合成一个结论。

字段扩容（widen）意味着新取值可能超出旧值域。写端要回答「新类型能否无损容纳
旧值」，类型格已经算过；读端要回答「每个报表按自己登记的读取能力能否无损读出
扩容后的取值」，这一步必须逐报表核对，不能用一个整批结论代替。

结论只有四档：
  BATCH_COMPATIBLE     写端放行，且所有受影响报表都能无损读取；
  BATCH_READ_PARTIAL   写端放行，部分报表读取能力不足或未登记，需要分批改造；
  BATCH_BREAKING       写端被拦下，扩容本身不可放行；
  BATCH_NOT_APPLICABLE 本批没有扩容字段，读写分向核对不适用。

未登记读取能力的报表给 READ_CAPABILITY_MISSING：既不当作兼容悄悄放行，也不冒
充破坏整批拦下，而是明确标成「不能证明无损」，并给出补登记的类型写法建议。
"""

from __future__ import annotations

from app import reasons
from app.modules.m3_compat import capability

# 只有「换类型」桶里的放宽字段才进入批次读写核对：改名冲突、删除、新增各有
# 自己的结论，不混进扩容这一批。判桶顺序决定了放宽字段必然落在 retyped。
WIDENED_BUCKETS = ("retyped",)

STATUS_NOT_AFFECTED = "not_affected"
STATUS_UNRESOLVED = "unresolved"
STATUS_BROKEN = "broken"

STATUS_ORDER = {
    capability.STATUS_LOSSY: 0,
    STATUS_BROKEN: 1,
    capability.STATUS_UNREGISTERED: 2,
    capability.STATUS_INVALID: 2,
    capability.STATUS_OK: 3,
}


def _write_section(all_widened: list[dict]) -> dict:
    """写端结论：扩容字段里只要有一个写端不通过，整批扩容都不能放行。"""
    fields = [
        {
            "field_id": row["field_id"],
            "table": row["table"],
            "old_name": row["old_name"],
            "new_name": row["new_name"],
            "old_type": row["old_type"],
            "new_type": row["new_type"],
            "type_detail": row["type_detail"],
            "writer_ok": bool(row["writer_ok"]),
            "writer_reason": row["writer"]["reason"],
            "required_change": row["required_change"],
            "ref_count": row["ref_count"],
            "evidence": row.get("type_evidence") or {},
        }
        for row in all_widened
    ]
    blocked = [
        {
            "field_id": field["field_id"],
            "table": field["table"],
            "old_name": field["old_name"],
            "new_name": field["new_name"],
            "old_type": field["old_type"],
            "new_type": field["new_type"],
            "writer_reason": field["writer_reason"],
            "required_change": field["required_change"],
        }
        for field in fields if not field["writer_ok"]
    ]
    return {
        "allowed": not blocked,
        "reason": "BATCH_WRITE_ALLOWED" if not blocked else "BATCH_WRITE_BLOCKED",
        "fields": fields,
        "blocked": blocked,
    }


def _report_status(checks: list[dict], links: list[dict]) -> str:
    statuses = {item["status"] for item in checks}
    if capability.STATUS_LOSSY in statuses:
        return capability.STATUS_LOSSY
    if links:
        return STATUS_BROKEN
    if capability.STATUS_UNREGISTERED in statuses or capability.STATUS_INVALID in statuses:
        return STATUS_UNRESOLVED
    return capability.STATUS_OK


def _report_reason(status: str) -> str:
    return {
        capability.STATUS_OK: "READ_CAPABILITY_OK",
        capability.STATUS_LOSSY: "READ_CAPABILITY_LOSSY",
        STATUS_UNRESOLVED: "READ_CAPABILITY_MISSING",
        STATUS_BROKEN: "REF_BROKEN",
    }.get(status, "READ_CAPABILITY_MISSING")


def _report_repair(status: str) -> str | None:
    return {
        capability.STATUS_OK: None,
        capability.STATUS_LOSSY: "widen_reader",
        STATUS_UNRESOLVED: "register_capability",
        STATUS_BROKEN: "fix_reference",
    }.get(status)


def _widened_broken_links(consumers: list[dict], resolution: dict,
                          widened_old: dict[tuple[str, str], dict]) -> dict[str, list[dict]]:
    """引用了扩容字段、但解析已经断开的引用，也要逐条给结论。"""
    out: dict[str, list[dict]] = {}
    for item in resolution.get("broken") or []:
        key = (item["table"].lower(), item["column"].lower())
        row = widened_old.get(key)
        if row is None:
            continue
        out.setdefault(item["consumer"], []).append({
            "ref": item["ref"],
            "table": item["table"],
            "column": item["column"],
            "via": item.get("via") or "broken",
            "detail": item.get("detail") or "",
            "field_id": row["field_id"],
            "old_name": row["old_name"],
            "new_name": row["new_name"],
            "reason": "REF_BROKEN",
            "status_label": reasons.label("REF_BROKEN"),
        })
    return out


def _read_section(consumers: list[dict], resolution: dict,
                  by_id: dict[str, dict], widened_old: dict[tuple[str, str], dict]) -> dict:
    affected: dict[str, list[dict]] = {}
    for item in resolution.get("resolved") or []:
        if item.get("field_id") in by_id:
            affected.setdefault(item["consumer"], []).append(item)
    broken_links = _widened_broken_links(consumers, resolution, widened_old)

    reports: list[dict] = []
    for consumer in sorted(consumers, key=lambda item: item["name"]):
        name = consumer["name"]
        items = sorted(affected.get(name) or [],
                       key=lambda item: (item["table"], item["column"], item["field_id"]))
        links = broken_links.get(name) or []
        if not items and not links:
            reports.append({
                "consumer": name,
                "kind": consumer.get("kind") or "task",
                "status": STATUS_NOT_AFFECTED,
                "reason": "READ_CAPABILITY_NOT_REQUIRED",
                "repair": None,
                "detail": None,
                "detail_label": "—",
                "status_label": reasons.label("READ_CAPABILITY_NOT_REQUIRED"),
                "capability": consumer.get("read_capability"),
                "capability_text": capability.describe(consumer),
                "capability_error": consumer.get("read_capability_error"),
                "fields": [],
                "broken_links": [],
            })
            continue

        checks: list[dict] = []
        for item in items:
            row = by_id[item["field_id"]]
            verdict = capability.evaluate(consumer, item["table"], item["column"],
                                          row.get("new_type") or "unknown",
                                          via=item.get("via") or "direct")
            verdict.update({
                "field_id": item["field_id"],
                "table": item["table"],
                "column": item["column"],
                "old_name": row.get("old_name"),
                "new_name": row.get("new_name"),
                "old_type": row.get("old_type"),
                "new_type": row.get("new_type"),
            })
            checks.append(verdict)

        status = _report_status(checks, links)
        # 代表性一条：lossy 优先，其次断链/未登记
        worst = min(checks, key=lambda check: (STATUS_ORDER.get(check["status"], 9),
                                               check["table"], check["column"])) \
            if checks else None
        detail = worst.get("detail") if worst else ("ref_broken" if links else None)
        detail_label = worst.get("detail_label") if worst else (
            reasons.label("REF_BROKEN") if links else "—")
        reports.append({
            "consumer": name,
            "kind": consumer.get("kind") or "task",
            "status": status,
            "reason": _report_reason(status),
            "repair": _report_repair(status),
            "detail": detail,
            "detail_label": detail_label or "—",
            "status_label": reasons.label(_report_reason(status)),
            "capability": consumer.get("read_capability"),
            "capability_text": capability.describe(consumer),
            "capability_error": consumer.get("read_capability_error"),
            "fields": checks,
            "broken_links": links,
        })

    affected_reports = [report for report in reports
                        if report["status"] != STATUS_NOT_AFFECTED]
    field_summary = capability.summary(
        [check for report in affected_reports for check in report["fields"]]
    )
    summary = {
        "reports": len(reports),
        "affected_reports": len(affected_reports),
        "not_affected_reports": len(reports) - len(affected_reports),
        "ok_reports": sum(1 for report in affected_reports
                          if report["status"] == capability.STATUS_OK),
        "lossy_reports": sum(1 for report in affected_reports
                             if report["status"] == capability.STATUS_LOSSY),
        "unresolved_reports": sum(1 for report in affected_reports
                                  if report["status"] == STATUS_UNRESOLVED),
        "broken_reports": sum(1 for report in affected_reports
                              if report["status"] == STATUS_BROKEN),
        "checks": field_summary["checks"],
        "ok_checks": field_summary["ok"],
        "lossy_checks": field_summary["lossy"],
        "unregistered_checks": field_summary["unregistered"],
        "invalid_checks": field_summary["invalid"],
    }

    if not affected_reports:
        reason = "READ_CAPABILITY_NOT_REQUIRED"
    elif summary["lossy_reports"]:
        reason = "READ_CAPABILITY_LOSSY"
    elif summary["broken_reports"]:
        reason = "REF_BROKEN"
    elif summary["unresolved_reports"]:
        reason = "READ_CAPABILITY_MISSING"
    else:
        reason = "READ_CAPABILITY_OK"

    return {"reports": reports, "summary": summary, "reason": reason}


def build(diff: dict, consumers: list[dict], resolution: dict) -> dict:
    all_widened = [
        row for row in diff.get("rows") or []
        if row.get("change") == "widen" and row.get("bucket") in WIDENED_BUCKETS
    ]
    # 读写分别算：写端被拦下时读端结论照给，但不改变「扩容不可放行」这个总判。
    by_id = {row["field_id"]: row for row in all_widened}
    widened_old = {
        (row["table"].lower(), (row["old_name"] or "").lower()): row
        for row in all_widened
    }
    write = _write_section(all_widened)
    read = _read_section(consumers, resolution, by_id, widened_old)

    other_breaking = [
        row for row in (diff.get("rows") or [])
        if row.get("verdict") == "BREAKING"
        and row.get("bucket") != "added"
        and row["field_id"] not in by_id
    ]

    if not all_widened:
        # 本批没有扩容字段：读写分向核对不适用，明确标出来，而不是当成兼容。
        verdict, reason = "BATCH_NOT_APPLICABLE", "BATCH_NO_WIDEN"
    elif not write["allowed"]:
        verdict, reason = "BATCH_BREAKING", "BATCH_WRITE_BLOCKED"
    elif (read["summary"]["lossy_reports"] or read["summary"]["unresolved_reports"]
          or read["summary"]["broken_reports"]):
        verdict, reason = "BATCH_READ_PARTIAL", "BATCH_READ_PARTIAL"
    else:
        verdict, reason = "BATCH_COMPATIBLE", "BATCH_COMPATIBLE"

    return {
        "scope": {
            "widened_fields": len(all_widened),
            "write_allowed_fields": sum(1 for row in all_widened if row.get("writer_ok")),
            "referenced_fields": len({item["field_id"]
                                      for item in resolution.get("resolved") or []
                                      if item["field_id"] in by_id}),
            "other_breaking": len(other_breaking),
        },
        "write": write,
        "read": read,
        "verdict": verdict,
        "reason": reason,
        "verdict_label": reasons.label(reason),
        "basis": _basis(write, read, other_breaking),
    }


def _basis(write: dict, read: dict, other_breaking: list[dict]) -> list[str]:
    """给人看的依据：值域比较 + 读取能力，逐条可复核。"""
    lines: list[str] = []
    for field in write["fields"]:
        old = (field.get("evidence") or {}).get("old") or {}
        new = (field.get("evidence") or {}).get("new") or {}
        if old or new:
            detail = (
                f"值域 {old.get('lo', '—')}~{old.get('hi', '—')} ⇒ "
                f"{new.get('lo', '—')}~{new.get('hi', '—')}，刻度 "
                f"{old.get('quantum', '—')} ⇒ {new.get('quantum', '—')}"
            )
        else:
            evidence = field.get("evidence") or {}
            detail = (
                f"长度 {evidence.get('old_length', '—')} ⇒ "
                f"{evidence.get('new_length', '—')}"
                if "old_length" in evidence else
                f"精度 {evidence.get('old_rank', '—')} ⇒ {evidence.get('new_rank', '—')}"
            )
        lines.append(
            f"{field['table']}.{field['old_name']} {field['old_type']} → "
            f"{field['new_type']}：写端{'放行' if field['writer_ok'] else '拦下'}（{detail}）"
        )
    for report in read["reports"]:
        if report["status"] == STATUS_NOT_AFFECTED:
            continue
        if report["status"] == capability.STATUS_OK:
            lines.append(f"{report['consumer']}：读取能力覆盖新值域")
            continue
        for check in report["fields"]:
            if check["status"] == capability.STATUS_OK:
                continue
            cap = check.get("capability_text") or "未登记"
            lines.append(
                f"{report['consumer']} 读 {check['table']}.{check['column']}"
                f"（新类型 {check['evidence'].get('field_type', '—')}）：能力 {cap}，"
                f"{check.get('detail_label') or '—'}"
            )
        for link in report["broken_links"]:
            lines.append(
                f"{report['consumer']} 引用 {link['ref']} 已断链（{link['detail'] or '无法解析'}），"
                "需先恢复引用再谈读取能力"
            )
    if other_breaking:
        lines.append(f"另有序号外破坏字段 {len(other_breaking)} 个，不属扩容范围，"
                     "仍按字段判定逐条处理")
    return lines
