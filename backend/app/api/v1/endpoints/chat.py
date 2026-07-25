import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.core.logging_context import reset_session_id, set_session_id
from app.schemas.chat import ChatRequest
from app.services.session_workspace import (
    get_session_workspace_manager,
    validate_session_id,
)

router = APIRouter()
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".md", ".pdf", ".csv", ".xls", ".xlsx"}
MAX_FILE_SIZE = 20 * 1024 * 1024


def _parse_sse_payload(event: str) -> dict[str, Any] | None:
    for line in event.splitlines():
        if not line.startswith("data: "):
            continue
        data_str = line.removeprefix("data: ").strip()
        if not data_str or data_str == "[DONE]":
            return None
        try:
            return json.loads(data_str)
        except json.JSONDecodeError:
            logger.debug("忽略无法解析的 SSE: %s", data_str)
            return None
    return None


@router.post("/upload")
async def upload_chat_file(
    raw_request: Request,
    file: UploadFile = File(...),
    session_id: str = Form(..., description="会话 ID，文件将隔离写入 session 工作区"),
):
    session_token = set_session_id(session_id)
    raw_request.state.session_id = session_id
    try:
        try:
            validate_session_id(session_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        ext = Path(file.filename or "").suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"仅支持 .md/.pdf/.csv/.xls/.xlsx 格式，当前: {ext or '无扩展名'}",
            )
        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="文件大小超过 20 MB 限制")

        ws = get_session_workspace_manager()
        upload_dir = ws.uploads_dir(session_id)
        safe_name = f"{uuid.uuid4().hex[:8]}_{Path(file.filename or 'file').name}"
        out_path = upload_dir / safe_name
        out_path.write_bytes(content)
        abs_path = str(out_path.resolve())
        logger.info("上传文件: %s -> %s", file.filename, abs_path)
        return {"path": abs_path, "name": file.filename or safe_name}
    finally:
        reset_session_id(session_token)


@router.post("/chat")
async def chat_completion(request: ChatRequest, raw_request: Request):
    request_session_token = None
    try:
        if not request.messages:
            raise HTTPException(status_code=400, detail="消息列表为空")
        if not request.session_id:
            raise HTTPException(status_code=400, detail="缺少 session_id")
        try:
            validate_session_id(request.session_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        request_session_token = set_session_id(request.session_id)
        raw_request.state.session_id = request.session_id

        from app.core.config import settings

        harness_type = (
            request.harness or ""
        ).strip().lower() or settings.AGENT_HARNESS.strip().lower()

        if harness_type == "open_harness":
            from app.services.open_harness_service import get_open_harness_service

            svc = get_open_harness_service()
        elif harness_type == "opencode":
            from app.services.open_code_service import get_open_code_service

            svc = get_open_code_service()
        elif harness_type == "claude_code":
            from app.services.claude_skill_service import get_claude_skill_service

            svc = get_claude_skill_service()
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"不支持的 AI 后端: {harness_type}"
                    "（可选: claude_code, open_harness, opencode）"
                ),
            )

        if svc is None:
            raise HTTPException(status_code=503, detail="技能服务未初始化")

        logger.info(
            f"[session={request.session_id}] 聊天请求 - harness={harness_type} - 消息数: {len(request.messages)}"
            + (
                f" [含流水线上下文 {len(request.context)} 字符]"
                if request.context
                else ""
            )
        )

        get_session_workspace_manager().session_root(request.session_id)

        async def stream():
            stream_session_token = set_session_id(request.session_id)
            try:
                async for event in svc.execute_stream(
                    request.messages,
                    request.context,
                    session_id=request.session_id,
                ):
                    yield event
            except Exception as e:
                logger.error(f"流式执行异常: {e}", exc_info=True)
                raise
            finally:
                reset_session_id(stream_session_token)

        return StreamingResponse(stream(), media_type="text/event-stream")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"聊天处理异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        if request_session_token is not None:
            reset_session_id(request_session_token)


@router.get("/health")
async def health_check():
    return {"status": "healthy", "service": "chat"}
