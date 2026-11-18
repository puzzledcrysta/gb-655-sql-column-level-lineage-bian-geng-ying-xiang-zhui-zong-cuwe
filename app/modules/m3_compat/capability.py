"""报表读取能力：把下游登记里写的类型当成「读者能无损装下的值域」，逐字段核对。

类型格回答的是「新类型能否装下旧类型的全部取值」；这里回答的是另一个问题：
「某个报表按它登记的读取能力，能否无损读出扩容后的新取值」。两个方向都用
值域四件套（lo / hi / quantum / exact）比较，不靠类型名的直觉。

未登记读取能力的报表不猜：直接给出 READ_CAPABILITY_MISSING，结论是「不能判定
为无损」，既不悄悄放行，也不冒充破坏。
"""

from __future__ import annotations

from app import reasons
from app.modules.m2_types import coerce, range as value_range

STATUS_OK = "ok"
STATUS_LOSSY = "lossy"
STATUS_UNREGISTERED = "unregistered"
STATUS_INVALID = "invalid"

STATUS_REASON = {
    STATUS_OK: "READ_CAPABILITY_OK",
    STATUS_LOSSY: "READ_CAPABILITY_LOSSY",
    STATUS_UNREGISTERED: "READ_CAPABILITY_MISSING",
    STATUS_INVALID: "READ_CAPABILITY_MISSING",
}

STATUS_REPAIR = {
    STATUS_OK: None,
    STATUS_LOSSY: "widen_reader",
    STATUS_UNREGISTERED: "register_capability",
    STATUS_INVALID: "register_capability",
}


def normalize_registration(raw) -> tuple[dict | None, str | None]:
    """把消费者上的 read_capability 归一成 {key: 类型写法}。

    支持两种写法：
      "decimal(18,2)"                     —— 该报表所有引用都用这个能力
      {"dim_order.order_amt": "...", "*": "..."} —— 按 表.列 登记，* 作缺省

    返回 (归一结果, 错误说明)。错误说明非空时整份登记不采信该消费者的能力。
    """
    if raw is None:
        return None, None
    if isinstance(raw, str):
        text = raw.strip()
        return ({"*": text} if text else None), None
    if not isinstance(raw, dict):
        return None, "read_capability 必须是类型写法或对象"
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            return None, f"read_capability.{key} 必须是类型写法"
        text = value.strip()
        if text:
            out[str(key).strip().lower()] = text
    return (out or None), None


def lookup(registration: dict | None, table: str, column: str) -> str | None:
    """按 表.列 取能力，取不到再看 * 缺省；都没有就是未登记。"""
    if not registration:
        return None
    key = f"{table.lower()}.{column.lower()}"
    if key in registration:
        return registration[key]
    return registration.get("*")


def describe(consumer_or_registration) -> str:
    """把归一后的登记压成一行，页面与导出共用。

    接受消费者字典或已归一的能力字典；登记结构不合法时如实标出来。
    """
    if isinstance(consumer_or_registration, dict) and "read_capability" in consumer_or_registration:
        if consumer_or_registration.get("read_capability_error"):
            return "登记无法解析"
        registration = consumer_or_registration.get("read_capability")
    else:
        registration = consumer_or_registration
    if not registration:
        return "未登记"
    if set(registration) == {"*"}:
        return registration["*"]
    return "；".join(f"{key}={value}" for key, value in sorted(registration.items()))


def _numeric_detail(cap: value_range.ValueRange, field: value_range.ValueRange) -> str | None:
    if cap.quantum > field.quantum:
        return "scale_exceeds_capability"
    if cap.lo > field.lo or cap.hi < field.hi:
        return "range_exceeds_capability"
    if cap.exact < field.exact:
        return "exactness_exceeds_capability"
    return None


def _evidence(cap_spec: dict, field_spec: dict) -> dict:
    cap_range = value_range.value_range(cap_spec)
    field_range = value_range.value_range(field_spec)
    if cap_range is not None and field_range is not None:
        return {
            "capability_type": coerce.type_text(cap_spec),
            "field_type": coerce.type_text(field_spec),
            "capability": cap_range.as_dict(),
            "field": field_range.as_dict(),
        }
    return {
        "capability_type": coerce.type_text(cap_spec),
        "field_type": coerce.type_text(field_spec),
        "capability_length": value_range.text_length(cap_spec),
        "field_length": value_range.text_length(field_spec),
        "capability_rank": value_range.temporal_rank(cap_spec["base"]),
        "field_rank": value_range.temporal_rank(field_spec["base"]),
    }


def evaluate(consumer: dict, table: str, column: str, field_type_raw: str,
             via: str = "direct") -> dict:
    """核对一个报表能否无损读取一个字段的新类型。

    field_type_raw 用新版本里的类型写法；不能解析的一律按不可判定处理。
    """
    field_spec = coerce.parse_type(field_type_raw)
    registration = consumer.get("read_capability")
    registration_error = consumer.get("read_capability_error")
    if registration is None and registration_error:
        return {
            "consumer": consumer["name"],
            "kind": consumer.get("kind") or "task",
            "via": via,
            "capability": None,
            "capability_text": "登记无法解析",
            "status": STATUS_INVALID,
            "reason": "READ_CAPABILITY_MISSING",
            "repair": STATUS_REPAIR[STATUS_INVALID],
            "detail": "capability_registration_invalid",
            "detail_label": reasons.narrow_label("capability_registration_invalid"),
            "status_label": reasons.label("READ_CAPABILITY_MISSING"),
            "evidence": {"field_type": coerce.type_text(field_spec),
                         "registration_error": registration_error},
        }
    if registration is None:
        return {
            "consumer": consumer["name"],
            "kind": consumer.get("kind") or "task",
            "via": via,
            "capability": None,
            "capability_text": describe(None),
            "status": STATUS_UNREGISTERED,
            "reason": "READ_CAPABILITY_MISSING",
            "repair": STATUS_REPAIR[STATUS_UNREGISTERED],
            "detail": "report_unregistered",
            "detail_label": reasons.narrow_label("report_unregistered"),
            "status_label": reasons.label("READ_CAPABILITY_MISSING"),
            "evidence": {"field_type": coerce.type_text(field_spec)},
        }

    raw_cap = lookup(registration, table, column)
    if raw_cap is None:
        return {
            "consumer": consumer["name"],
            "kind": consumer.get("kind") or "task",
            "via": via,
            "capability": None,
            "capability_text": describe(registration),
            "status": STATUS_UNREGISTERED,
            "reason": "READ_CAPABILITY_MISSING",
            "repair": STATUS_REPAIR[STATUS_UNREGISTERED],
            "detail": "family_unregistered",
            "detail_label": reasons.narrow_label("family_unregistered"),
            "status_label": reasons.label("READ_CAPABILITY_MISSING"),
            "evidence": {"field_type": coerce.type_text(field_spec),
                         "registered_keys": sorted(registration)},
        }

    cap_spec = coerce.parse_type(raw_cap)
    detail = _coverage_detail(cap_spec, field_spec)
    if detail is None:
        status = STATUS_OK
    elif detail in ("capability_type_unknown", "field_type_unknown"):
        status = STATUS_INVALID
    else:
        status = STATUS_LOSSY
    return {
        "consumer": consumer["name"],
        "kind": consumer.get("kind") or "task",
        "via": via,
        "capability": coerce.type_text(cap_spec),
        "capability_text": describe({f"{table.lower()}.{column.lower()}": raw_cap}),
        "status": status,
        "reason": STATUS_REASON[status],
        "repair": STATUS_REPAIR[status],
        "detail": detail,
        "detail_label": reasons.narrow_label(detail),
        "status_label": reasons.label(STATUS_REASON[status]),
        "evidence": _evidence(cap_spec, field_spec),
    }


def _coverage_detail(cap_spec: dict, field_spec: dict) -> str | None:
    if not field_spec.get("known"):
        return "field_type_unknown"
    if not cap_spec.get("known"):
        return "capability_type_unknown"
    if cap_spec["super"] != field_spec["super"]:
        return "family_mismatch"

    super_name = cap_spec["super"]
    if super_name == "numeric":
        cap_range = value_range.value_range(cap_spec)
        field_range = value_range.value_range(field_spec)
        if cap_range is None or field_range is None:
            return "capability_type_unknown"
        return _numeric_detail(cap_range, field_range)

    if super_name == "text":
        cap_len = value_range.text_length(cap_spec)
        field_len = value_range.text_length(field_spec)
        if cap_len is None:
            return None
        if field_len is None or cap_len < field_len:
            return "length_exceeds_capability"
        return None

    if super_name == "temporal":
        if value_range.temporal_rank(cap_spec["base"]) < value_range.temporal_rank(
                field_spec["base"]):
            return "temporal_exceeds_capability"
        return None

    return None


def summary(checks: list[dict]) -> dict:
    """字段级汇总：一处引用算一条，供账目与页面展示。"""
    counts = {STATUS_OK: 0, STATUS_LOSSY: 0, STATUS_UNREGISTERED: 0, STATUS_INVALID: 0}
    for item in checks:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    total = len(checks)
    return {
        "checks": total,
        "ok": counts[STATUS_OK],
        "lossy": counts[STATUS_LOSSY],
        "unregistered": counts[STATUS_UNREGISTERED],
        "invalid": counts[STATUS_INVALID],
        "unresolved": counts[STATUS_UNREGISTERED] + counts[STATUS_INVALID],
        "all_ok": bool(checks) and counts[STATUS_OK] == total,
    }
