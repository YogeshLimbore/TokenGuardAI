from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

import numpy as np
from sentence_transformers import SentenceTransformer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.models import SemanticResponseCache
from app.schemas.chat import ChatCompletionRequest


@dataclass
class SemanticCacheMatch:
    entry: SemanticResponseCache
    similarity_score: float


@lru_cache(maxsize=1)
def get_embedding_model() -> SentenceTransformer:
    settings = get_settings()

    return SentenceTransformer(settings.semantic_cache_model)


class SemanticCacheService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.model = get_embedding_model()

    @staticmethod
    def _normalize_text(text: str) -> str:
        return " ".join(text.strip().lower().split())

    @staticmethod
    def _extract_system_text(
        request: ChatCompletionRequest,
    ) -> str:
        system_messages = [
            message.content for message in request.messages if message.role == "system"
        ]

        return "\n".join(system_messages)

    @staticmethod
    def _extract_latest_user_prompt(
        request: ChatCompletionRequest,
    ) -> str:
        for message in reversed(request.messages):
            if message.role == "user":
                return message.content

        raise ValueError("Semantic caching requires at least one user message.")

    def create_system_fingerprint(
        self,
        request: ChatCompletionRequest,
    ) -> str:
        system_text = self._extract_system_text(request)

        normalized_system_text = self._normalize_text(system_text)

        return hashlib.sha256(normalized_system_text.encode("utf-8")).hexdigest()

    def encode_text(self, text: str) -> np.ndarray:
        embedding = self.model.encode(
            self._normalize_text(text),
            normalize_embeddings=True,
            convert_to_numpy=True,
        )

        return np.asarray(
            embedding,
            dtype=np.float32,
        )

    @staticmethod
    def _serialize_embedding(
        embedding: np.ndarray,
    ) -> str:
        return json.dumps(
            embedding.tolist(),
            separators=(",", ":"),
        )

    @staticmethod
    def _deserialize_embedding(
        value: str,
    ) -> np.ndarray:
        return np.asarray(
            json.loads(value),
            dtype=np.float32,
        )

    @staticmethod
    def _cosine_similarity(
        left: np.ndarray,
        right: np.ndarray,
    ) -> float:
        denominator = np.linalg.norm(left) * np.linalg.norm(right)

        if denominator == 0:
            return 0.0

        similarity = np.dot(left, right) / denominator

        return float(similarity)

    def find_match(
        self,
        database: Session,
        *,
        project_id: int,
        request: ChatCompletionRequest,
        selected_model: str,
    ) -> SemanticCacheMatch | None:
        if not self.settings.semantic_cache_enabled:
            return None

        user_prompt = self._extract_latest_user_prompt(request)

        query_embedding = self.encode_text(user_prompt)

        system_fingerprint = self.create_system_fingerprint(request)

        statement = (
            select(SemanticResponseCache)
            .where(
                SemanticResponseCache.project_id == project_id,
                SemanticResponseCache.provider == request.provider,
                SemanticResponseCache.model == selected_model,
                SemanticResponseCache.credential_id == request.credential_id,
                SemanticResponseCache.system_fingerprint == system_fingerprint,
                SemanticResponseCache.temperature == request.temperature,
                SemanticResponseCache.max_tokens == request.max_tokens,
            )
            .order_by(SemanticResponseCache.created_at.desc())
            .limit(self.settings.semantic_cache_max_candidates)
        )

        candidates = list(database.scalars(statement).all())

        best_match: SemanticCacheMatch | None = None

        for candidate in candidates:
            candidate_embedding = self._deserialize_embedding(candidate.embedding_json)

            similarity = self._cosine_similarity(
                query_embedding,
                candidate_embedding,
            )

            if best_match is None or similarity > best_match.similarity_score:
                best_match = SemanticCacheMatch(
                    entry=candidate,
                    similarity_score=similarity,
                )

        if best_match is None:
            return None

        if best_match.similarity_score < self.settings.semantic_cache_threshold:
            return None

        return best_match

    def save_response(
        self,
        database: Session,
        *,
        project_id: int,
        request: ChatCompletionRequest,
        selected_model: str,
        response: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        cost_usd: float,
    ) -> SemanticResponseCache:
        user_prompt = self._extract_latest_user_prompt(request)

        embedding = self.encode_text(user_prompt)

        entry = SemanticResponseCache(
            project_id=project_id,
            provider=request.provider,
            credential_id=request.credential_id,
            model=selected_model,
            system_fingerprint=(self.create_system_fingerprint(request)),
            user_prompt=user_prompt,
            embedding_json=(self._serialize_embedding(embedding)),
            response=response,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=(input_tokens + output_tokens),
            original_latency_ms=latency_ms,
            original_cost_usd=cost_usd,
        )

        database.add(entry)
        database.commit()
        database.refresh(entry)

        return entry

    @staticmethod
    def register_hit(
        database: Session,
        entry: SemanticResponseCache,
    ) -> None:
        entry.hit_count += 1
        entry.last_hit_at = datetime.now(UTC)

        database.commit()

    def response_already_stored(
        self,
        database: Session,
        *,
        project_id: int,
        request: ChatCompletionRequest,
        selected_model: str,
    ) -> bool:
        user_prompt = self._extract_latest_user_prompt(request)

        statement = select(SemanticResponseCache.id).where(
            SemanticResponseCache.project_id == project_id,
            SemanticResponseCache.provider == request.provider,
            SemanticResponseCache.credential_id == request.credential_id,
            SemanticResponseCache.model == selected_model,
            SemanticResponseCache.system_fingerprint
            == self.create_system_fingerprint(request),
            SemanticResponseCache.user_prompt == user_prompt,
            SemanticResponseCache.temperature == request.temperature,
            SemanticResponseCache.max_tokens == request.max_tokens,
        )

        return database.scalar(statement) is not None
