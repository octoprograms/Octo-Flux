from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

router = APIRouter()
STATIC_DIR = Path(__file__).with_name("static")
_UI_FILE = STATIC_DIR / "index.html"
# Revalidate on every load (ETag keeps it cheap) so an upgrade never runs stale dashboard code.
_NO_CACHE = {"Cache-Control": "no-cache"}


class DashboardStatic(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(_NO_CACHE)
        return response


@router.get("/admin", include_in_schema=False)
@router.get("/admin/", include_in_schema=False)
@router.get("/admin/dashboard", include_in_schema=False)
async def admin_dashboard() -> FileResponse:
    return FileResponse(_UI_FILE, media_type="text/html", headers=_NO_CACHE)
