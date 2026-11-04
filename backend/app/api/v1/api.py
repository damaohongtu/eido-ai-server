from fastapi import APIRouter

from app.api.v1.endpoints import chat, sessions, workspace

api_router = APIRouter()
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
api_router.include_router(workspace.router, prefix="/workspace", tags=["workspace"])
