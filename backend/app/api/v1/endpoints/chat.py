import logging
import uuid
from contextlib import aclosing
from pathlib import Path

from anyio import CancelScope
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from starlette.types import Send

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


class _ChatStreamResponse(StreamingResponse):
    async def stream_response(self, send: Send) -> None:
        # send() 被取消时，Starlette 的迭代器可能仍停在上一条 yield。
        try:
            await super().stream_response(send)
        finally:
            with CancelScope(shield=True):
                await self.body_iterator.aclose()


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

        from app.services.agent_service import get_agent_service

        svc = get_agent_service()
        if svc is None:
            raise HTTPException(status_code=503, detail="Agent 服务未初始化")

        get_session_workspace_manager().session_root(request.session_id)

        async def stream():
            stream_session_token = set_session_id(request.session_id)
            try:
                events = svc.execute_stream(
                    request.messages,
                    request.context,
                    session_id=request.session_id,
                )
                async with aclosing(events):
                    async for event in events:
                        yield event
            finally:
                reset_session_id(stream_session_token)

        return _ChatStreamResponse(stream(), media_type="text/event-stream")

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
