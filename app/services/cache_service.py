import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import ExactResponseCache
from app.schemas.chat import ChatCompletionRequest


class CacheService:
    """
    Handles deterministic exact-response caching.

    Two requests receive the same cache key only when all important
    generation parameters and messages are identical.
    """

    @staticmethod
    def generate_cache_key(
        request: ChatCompletionRequest,
        selected_model: str,
        project_id: int,
    ) -> str:
        normalized_request = {
            "project_id": project_id,
            "provider": request.provider.lower(),
            "credential_id": request.credential_id,
            "model": selected_model,
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                }
                for message in request.messages
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }

        serialized_request = json.dumps(
            normalized_request,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        return hashlib.sha256(serialized_request.encode("utf-8")).hexdigest()

    @staticmethod
    def get_cached_response(
        database: Session,
        cache_key: str,
        *,
        project_id: int,
    ) -> ExactResponseCache | None:
        statement = select(ExactResponseCache).where(
            ExactResponseCache.cache_key == cache_key,
            ExactResponseCache.project_id == project_id,
        )

        return database.scalar(statement)

    @staticmethod
    def save_response(
        database: Session,
        *,
        cache_key: str,
        project_id: int,
        provider: str,
        model: str,
        prompt: str,
        response: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
    ) -> ExactResponseCache:
        cache_entry = ExactResponseCache(
            cache_key=cache_key,
            project_id=project_id,
            provider=provider,
            model=model,
            prompt=prompt,
            response=response,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            original_latency_ms=latency_ms,
        )

        database.add(cache_entry)
        database.commit()
        database.refresh(cache_entry)

        return cache_entry

    @staticmethod
    def register_cache_hit(
        database: Session,
        cache_entry: ExactResponseCache,
    ) -> None:
        cache_entry.hit_count += 1
        database.commit()
