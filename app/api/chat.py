from fastapi import APIRouter

from app.providers.ollama import OllamaProvider
from app.schemas.chat import ChatMessage, ChatRequest, ChatResponse
from app.services.token_service import count_messages_tokens, count_tokens

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    provider = OllamaProvider()
    messages = [message.model_dump() for message in request.messages]

    prompt_tokens = count_messages_tokens(messages)
    result = await provider.chat(request.model or provider.default_model, messages)

    reply = result.get("message", {})
    completion_tokens = count_tokens(reply.get("content", ""))

    return ChatResponse(
        model=result.get("model", request.model or provider.default_model),
        message=ChatMessage(role=reply.get("role", "assistant"), content=reply.get("content", "")),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
