import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.services.session_workspace import (
    get_session_workspace_manager,
    validate_session_id,
)

router = APIRouter()
logger = logging.getLogger(__name__)

ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


def _resolve_session_path(session_id: str, path_str: str) -> Path:
    try:
        validate_session_id(session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        return get_session_workspace_manager().safe_resolve(session_id, path_str)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get("/file")
async def get_workspace_file(
    path: str = Query(..., description="文件路径"),
    download: bool = Query(False, description="是否以附件形式下载"),
    filename: str | None = Query(None, description="下载时使用的文件名"),
    session_id: str = Query(..., description="会话 ID"),
):
    try:
        resolved = _resolve_session_path(session_id, path)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"无效路径: {path} - {e}")
        raise HTTPException(status_code=400, detail="无效的文件路径")

    if not resolved.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="不是文件")

    ext = resolved.suffix.lower()
    media_type = None
    if ext in ALLOWED_IMAGE_EXT:
        media_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
        }
        media_type = media_types.get(ext)

    download_name = filename or resolved.name
    return FileResponse(
        resolved,
        media_type=media_type,
        filename=download_name,
        content_disposition_type="attachment" if download else "inline",
    )


@router.get("/files")
async def list_workspace_files(
    session_id: str = Query(..., description="会话 ID"),
):
    try:
        validate_session_id(session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    mgr = get_session_workspace_manager()
    nodes = mgr.list_directory(session_id)
    return {"files": [n.to_dict() for n in nodes]}


@router.delete("/file")
async def delete_workspace_file(
    path: str = Query(..., description="文件路径"),
    session_id: str = Query(..., description="会话 ID"),
):
    try:
        validate_session_id(session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        mgr = get_session_workspace_manager()
        deleted = mgr.delete_file(session_id, path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"deleted": True}
