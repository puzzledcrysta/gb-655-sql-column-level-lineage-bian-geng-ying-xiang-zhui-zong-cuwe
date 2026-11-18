"""迁移单：把一次判定的结论整理成带条款号与脚注的 Markdown。"""

from __future__ import annotations

from app import config, reasons

FOOTNOTES = [
    "字段身份按 field_id 对齐，改名不算新字段。",
    "类型放宽指新类型能无损容纳旧类型的全部取值。",
    f"生效区间按 {config.WINDOW_SEMANTICS} 处理。",
    "账目差额应为 0；不为 0 说明判定漏项。",
    "扩容批次结论：写端放行才算放行；读端按报表登记的读取能力逐条核对，未登记的不当作无损。",
]


def clauses(run: dict) -> list[dict]:
    """按优先级列出破坏项，每条一个条款号。"""
    rows = [row for row in (run.get("diff") or []) if row["verdict"] == "BREAKING"]
    rows.sort(key=lambda row: (row["table"], row["old_name"] or row["new_name"] or ""))
    out: list[dict] = []
    for index, row in enumerate(rows, start=1):
        out.append({
            "no": f"3.{index}",
            "field_id": row["field_id"],
            "table": row["table"],
            "old_name": row["old_name"],
            "new_name": row["new_name"],
            "old_type": row["old_type"],
            "new_type": row["new_type"],
            "change": row["change"],
            "type_detail": row["type_detail"],
            "primary": row["primary"],
            "primary_label": reasons.label(row["primary"]),
            "reader_ok": row["reader_ok"],
            "writer_ok": row["writer_ok"],
            "ref_count": row["ref_count"],
            "reasons": row["reasons"],
        })
    return out


def _md_table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def render(run: dict) -> str:
    ledger = run.get("ledger") or {}
    fields = ledger.get("fields") or {}
    refs = ledger.get("refs") or {}
    buckets = fields.get("buckets") or {}
    digests = run.get("digests") or {}
    parts = [
        f"# 迁移单 {run['run_id']}",
        "",
        f"- 版本：{run.get('version_label_old')} → {run.get('version_label_new')}",
        f"- 规则集：{run.get('ruleset')}",
        f"- 生成时间：{config.to_local_text(_created(run))}",
        f"- 生效区间：{run.get('effective_from')} 起，半开区间[^3]",
        "",
        "## 1 输入",
        "",
        _md_table(
            ["角色", "文件", "sha256"],
            [[role, name, digest] for role, (name, digest) in sorted(_inputs(run).items())],
        ),
        "",
        "## 2 字段账",
        "",
        _md_table(
            ["项", "数"],
            [["旧版本字段", fields.get("old_total", 0)],
             ["未变", buckets.get("unchanged", 0)],
             ["改名", buckets.get("renamed", 0)],
             ["换类型", buckets.get("retyped", 0)],
             ["改必填", buckets.get("required", 0)],
             ["冲突", buckets.get("conflict", 0)],
             ["删除", buckets.get("deleted", 0)],
             ["新增", buckets.get("added", 0)],
             ["新版本字段", fields.get("new_total", 0)],
             ["旧侧差额", fields.get("old_gap", 0)],
             ["新侧差额", fields.get("new_gap", 0)]],
        ),
        "",
        f"字段身份按 field_id 对齐[^1]，类型放宽按值域与刻度判定[^2]，账目差额应为 0[^4]。",
        "",
        "### 2.1 扩容读写兼容",
        "",
    ]
    parts.extend(_compatibility_lines(run))
    parts.extend([
        "",
        "## 3 破坏项",
        "",
    ])

    items = clauses(run)
    if not items:
        parts.append("无破坏项。")
        parts.append("")
    for item in items:
        parts.append(f"### {item['no']} {item['table']}.{item['old_name'] or '—'}"
                     f" → {item['new_name'] or '—'}")
        parts.append("")
        parts.append(f"- 原因码：{item['primary']}（{item['primary_label']}）")
        parts.append(f"- 类型：{item['old_type'] or '—'} → {item['new_type'] or '—'}"
                     f"，变化 {item['change']}")
        parts.append(f"- 读者：{'通过' if item['reader_ok'] else '不通过'}"
                     f"；写者：{'通过' if item['writer_ok'] else '不通过'}")
        parts.append(f"- 下游引用：{item['ref_count']} 处")
        parts.append("")

    parts.extend([
        "## 4 映射",
        "",
        "```sql",
    ])
    aliases = (run.get("mapping") or {}).get("aliases") or []
    if aliases:
        for item in aliases:
            parts.append(f"{item['sql']}  -- {item['table']}")
    else:
        parts.append("-- 无改名，无需别名")
    parts.extend([
        "```",
        "",
        "## 5 影响面",
        "",
        _md_table(
            ["项", "数"],
            [["下游节点", (run.get("impact") or {}).get("summary", {}).get("consumers", 0)],
             ["登记引用", refs.get("refs_total", 0)],
             ["可达", refs.get("reachable", 0)],
             ["断链", refs.get("broken", 0)],
             ["受影响节点", (run.get("impact") or {}).get("impact", {}).get("total", 0)],
             ["环", (run.get("impact") or {}).get("cycles", {}).get("cycle_count", 0)],
             ["自引用", (run.get("impact") or {}).get("cycles", {}).get("self_loop_count", 0)],
             ["单边登记", len((run.get("impact") or {}).get("one_sided") or [])]],
        ),
        "",
        "## 6 回滚",
        "",
        "1. 按第 4 节建兼容视图，旧名字继续可用。",
        "2. 破坏项逐个补默认值或改类型，再重跑判定。",
        "3. 版本切回上一版，生效区间端点保持半开。",
        "",
    ])
    for index, note in enumerate(FOOTNOTES, start=1):
        parts.append(f"[^{index}]: {note}")
    parts.append("")
    return "\n".join(parts)


def _compatibility_lines(run: dict) -> list[str]:
    """第 2.1 节：批次结论 + 写端值域 + 逐报表读取能力。"""
    compatibility = run.get("compatibility") or {}
    if not compatibility:
        return ["本次批次没有扩容字段，无需读写分向核对。"]
    scope = compatibility.get("scope") or {}
    write = compatibility.get("write") or {}
    read = compatibility.get("read") or {}
    summary = read.get("summary") or {}
    lines = [
        f"- 结论：{compatibility.get('verdict')}（{compatibility.get('verdict_label')}）[^5]",
        f"- 扩容字段：{scope.get('widened_fields', 0)} 个；写端"
        f"{'放行' if write.get('allowed') else '拦下'}；"
        f"受影响报表 {summary.get('affected_reports', 0)} 个"
        f"（无损 {summary.get('ok_reports', 0)}、丢精度 {summary.get('lossy_reports', 0)}、"
        f"未登记 {summary.get('unresolved_reports', 0)}、"
        f"断链 {summary.get('broken_reports', 0)}）",
        "",
        "写端：",
        "",
    ]
    write_rows = [
        [field["table"], field["old_name"], field["old_type"], field["new_type"],
         "放行" if field["writer_ok"] else "拦下",
         _range_text(field.get("evidence") or {})]
        for field in write.get("fields") or []
    ]
    lines.append(_md_table(["表", "字段", "旧类型", "新类型", "写端", "值域 / 刻度"], write_rows)
                 if write_rows else "没有扩容字段。")
    lines.extend(["", "读端：", ""])
    read_rows = []
    for report in read.get("reports") or []:
        if report.get("status") == "not_affected":
            continue
        read_rows.append([
            report["consumer"], report.get("capability_text") or "未登记",
            report.get("reason") or "", report.get("status_label") or "",
            report.get("detail_label") or "—", report.get("repair") or "—",
        ])
    lines.append(_md_table(["报表", "读取能力", "原因码", "结论", "依据", "处置"], read_rows)
                 if read_rows else "没有报表引用扩容字段。")
    return lines


def _range_text(evidence: dict) -> str:
    old = evidence.get("old")
    new = evidence.get("new")
    if old and new:
        return (f"值域 {old.get('lo')}~{old.get('hi')} ⇒ {new.get('lo')}~{new.get('hi')}，"
                f"刻度 {old.get('quantum')} ⇒ {new.get('quantum')}")
    if "old_length" in evidence:
        return f"长度 {evidence.get('old_length')} ⇒ {evidence.get('new_length')}"
    return f"精度 {evidence.get('old_rank', '—')} ⇒ {evidence.get('new_rank', '—')}"


def _created(run: dict):
    from datetime import datetime

    raw = run.get("created_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _inputs(run: dict) -> dict[str, tuple[str, str]]:
    names = run.get("input_names") or {}
    digests = run.get("digests") or {}
    return {role: (names.get(role, "—"), digests.get(role, "—")) for role in digests}
