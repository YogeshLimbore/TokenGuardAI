from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]


class ChatResponse(BaseModel):
    model: str
    message: ChatMessage
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
