from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()
_UI_FILE = Path(__file__).with_name("static") / "index.html"


@router.get("/admin", include_in_schema=False)
@router.get("/admin/", include_in_schema=False)
@router.get("/admin/dashboard", include_in_schema=False)
async def admin_dashboard() -> FileResponse:
    return FileResponse(_UI_FILE, media_type="text/html")
