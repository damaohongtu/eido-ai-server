from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.session_workspace import (
    get_session_workspace_manager,
    validate_session_id,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class CreateSessionRequest(BaseModel):
    pass


@router.get("/")
async def list_sessions():
    mgr = get_session_workspace_manager()
    sessions = []
    for sess_dir in sorted(
        mgr.root.iterdir(),
        key=lambda d: d.stat().st_mtime if d.is_dir() else 0,
        reverse=True,
    ):
        if not sess_dir.is_dir():
            continue
        try:
            validate_session_id(sess_dir.name)
        except ValueError:
            continue
        sessions.append({"id": sess_dir.name})
    return sessions


@router.post("/")
async def create_session(body: CreateSessionRequest):
    sid = uuid.uuid4().hex[:12]
    mgr = get_session_workspace_manager()
    mgr.session_root(sid)
    logger.info(f"创建会话: {sid}")
    return {"id": sid}


@router.get("/{session_id}")
async def get_session_detail(session_id: str):
    try:
        validate_session_id(session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    mgr = get_session_workspace_manager()
    sess_dir = mgr.session_root(session_id, create=False)
    if not sess_dir.exists():
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"id": session_id}


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    try:
        validate_session_id(session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    mgr = get_session_workspace_manager()
    try:
        from app.services.claude_skill_service import get_claude_skill_service
        from app.services.open_code_service import get_open_code_service

        claude_service = get_claude_skill_service()
        if claude_service is not None:
            claude_service.reset_session(session_id)
        open_code_service = get_open_code_service()
        if open_code_service is not None:
            open_code_service.reset_session(session_id)
    except Exception:
        logger.exception("删除会话前回收 agent 状态失败: session=%s", session_id)
    removed = mgr.remove(session_id)
    if not removed:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"deleted": True}
