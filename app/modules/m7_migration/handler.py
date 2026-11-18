"""M7 入口：迁移单页与导出物。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app import store, web
from app.modules.m7_migration import service

router = APIRouter()

CSV_HEADERS = {"Content-Disposition": 'attachment; filename="lineageguard.csv"'}


def _csv(body: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/spec", response_class=HTMLResponse)
def spec_page(request: Request):
    run = store.latest_run()
    return web.page_or_404(
        request,
        "spec.html",
        "spec",
        run,
        clauses=service.clauses(run) if run else [],
        ledger=(run or {}).get("ledger") or {},
        aliases=((run or {}).get("mapping") or {}).get("aliases") or [],
        compatibility=(run or {}).get("compatibility") or {},
        warnings=(run or {}).get("warnings") or [],
    )


@router.get("/api/export/evidence.csv")
def export_evidence():
    run = service.latest_or_404()
    return _csv(service.fields_csv_text(run), "lineageguard-evidence.csv")


@router.get("/api/export/capability.csv")
def export_capability():
    run = service.latest_or_404()
    return _csv(service.capability_csv_text(run), "lineageguard-capability.csv")


@router.get("/api/export/spec.md")
def export_spec():
    run = service.latest_or_404()
    return Response(
        content=service.spec_markdown(run),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="lineageguard-spec.md"'},
    )


@router.get("/api/export/fields.csv")
def export_fields():
    run = service.latest_or_404()
    return _csv(service.fields_csv_text(run), "lineageguard-fields.csv")


@router.get("/api/export/refs.csv")
def export_refs():
    run = service.latest_or_404()
    return _csv(service.refs_csv_text(run), "lineageguard-refs.csv")


@router.get("/api/export/ledger.csv")
def export_ledger():
    run = service.latest_or_404()
    return _csv(service.ledger_csv_text(run), "lineageguard-ledger.csv")


@router.get("/api/export/manifest.json")
def export_manifest():
    run = service.latest_or_404()
    return Response(
        content=service.manifest_json_text(run),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="lineageguard-manifest.json"'},
    )
