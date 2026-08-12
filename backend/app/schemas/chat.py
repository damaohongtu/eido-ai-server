from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class Message(BaseModel):
    id: Optional[str] = Field(None, description="消息 ID")
    role: Literal["user", "assistant", "system"]
    content: str


class ChatRequest(BaseModel):
    messages: List[Message] = Field(..., description="对话历史")
    context: Optional[str] = Field(
        None, description="流水线上一步输出，附加在 prompt 末尾"
    )
    session_id: str = Field(..., description="会话 ID")
    harness: Optional[str] = Field(
        None,
        description="claude_code | opencode，不传使用 AGENT_HARNESS",
    )
