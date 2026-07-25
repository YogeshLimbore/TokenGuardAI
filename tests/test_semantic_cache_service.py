"""
Semantic response cache.

A semantic cache answers a request with a *different* request's response, so
the matching rules are both a cost lever and an isolation risk. These tests use
a stub embedding model (the real sentence-transformer download is neither
deterministic enough nor fast enough for CI) and focus on which entries are
considered comparable at all.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.schemas.chat import ChatCompletionRequest, ChatMessage
from app.services import semantic_cache_service as module
from app.services.semantic_cache_service import SemanticCacheService
from tests.conftest import create_project_with_key

pytestmark = pytest.mark.integration

VECTOR_SIZE = 16


class StubEmbeddingModel:
    """
    Deterministic stand-in for ``SentenceTransformer``.

    Identical text always produces an identical unit vector, and unrelated text
    produces near-orthogonal vectors, which is all the service needs.
    """

    def __init__(self) -> None:
        self.encoded: list[str] = []

    def encode(
        self,
        text: str,
        *,
        normalize_embeddings: bool = True,
        convert_to_numpy: bool = True,
    ) -> np.ndarray:
        self.encoded.append(text)

        digest = hashlib.sha256(text.encode("utf-8")).digest()

        # Signed components keep unrelated prompts near-orthogonal; unsigned
        # bytes would make every pair of vectors look similar.
        vector = np.frombuffer(
            digest[:VECTOR_SIZE],
            dtype=np.int8,
        ).astype(np.float32)

        return vector / np.linalg.norm(vector)


@pytest.fixture
def embedding_model(
    monkeypatch: pytest.MonkeyPatch,
) -> StubEmbeddingModel:
    model = StubEmbeddingModel()
    monkeypatch.setattr(module, "get_embedding_model", lambda: model)
    return model


@pytest.fixture(autouse=True)
def _enable_semantic_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        get_settings(),
        "semantic_cache_enabled",
        True,
    )


@pytest.fixture
def service(embedding_model: StubEmbeddingModel) -> SemanticCacheService:
    return SemanticCacheService()


@pytest.fixture
def project_id(database: Session) -> int:
    project, _ = create_project_with_key(
        database,
        name="Semantic Cache Project",
    )
    return project.id


@pytest.fixture
def other_project_id(database: Session) -> int:
    project, _ = create_project_with_key(
        database,
        name="Other Semantic Cache Project",
    )
    return project.id


def _request(
    prompt: str,
    *,
    system: str | None = None,
    **overrides,
) -> ChatCompletionRequest:
    messages = []

    if system is not None:
        messages.append(ChatMessage(role="system", content=system))

    messages.append(ChatMessage(role="user", content=prompt))

    payload = {
        "provider": "ollama",
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 200,
    }
    payload.update(overrides)

    return ChatCompletionRequest(**payload)


def _save(
    service: SemanticCacheService,
    database: Session,
    *,
    project_id: int,
    request: ChatCompletionRequest,
    response: str = "Cached answer.",
    selected_model: str = "llama3.2",
):
    return service.save_response(
        database,
        project_id=project_id,
        request=request,
        selected_model=selected_model,
        response=response,
        input_tokens=10,
        output_tokens=5,
        latency_ms=42.0,
        cost_usd=0.001,
    )


# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------


def test_prompts_are_normalized_before_encoding(
    service: SemanticCacheService,
    embedding_model: StubEmbeddingModel,
) -> None:
    first = service.encode_text("  Explain   CACHING  ")
    second = service.encode_text("explain caching")

    assert np.allclose(first, second)
    assert embedding_model.encoded == ["explain caching", "explain caching"]


def test_the_system_fingerprint_ignores_formatting_but_not_content(
    service: SemanticCacheService,
) -> None:
    baseline = service.create_system_fingerprint(
        _request("Hello.", system="You are a helpful assistant.")
    )
    reformatted = service.create_system_fingerprint(
        _request("Hello.", system="  You are a  HELPFUL assistant.  ")
    )
    different = service.create_system_fingerprint(
        _request("Hello.", system="You are a pirate.")
    )

    assert baseline == reformatted
    assert baseline != different


def test_the_latest_user_message_is_the_cache_subject(
    service: SemanticCacheService,
) -> None:
    request = ChatCompletionRequest(
        provider="ollama",
        messages=[
            ChatMessage(role="user", content="First question."),
            ChatMessage(role="assistant", content="First answer."),
            ChatMessage(role="user", content="Second question."),
        ],
    )

    assert service._extract_latest_user_prompt(request) == "Second question."


def test_a_conversation_without_a_user_message_is_rejected(
    service: SemanticCacheService,
) -> None:
    request = ChatCompletionRequest(
        provider="ollama",
        messages=[
            ChatMessage(role="system", content="You are helpful."),
        ],
    )

    with pytest.raises(ValueError, match="at least one user message"):
        service._extract_latest_user_prompt(request)


def test_embeddings_survive_a_serialization_round_trip(
    service: SemanticCacheService,
) -> None:
    embedding = service.encode_text("Explain semantic caching.")

    restored = service._deserialize_embedding(service._serialize_embedding(embedding))

    assert np.allclose(embedding, restored)


def test_cosine_similarity_handles_a_zero_vector(
    service: SemanticCacheService,
) -> None:
    zeros = np.zeros(VECTOR_SIZE, dtype=np.float32)
    vector = service.encode_text("Anything.")

    assert service._cosine_similarity(zeros, vector) == 0.0
    assert service._cosine_similarity(vector, vector) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_an_identical_prompt_is_served_from_the_cache(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
) -> None:
    request = _request("Explain caching in one sentence.")

    saved = _save(
        service,
        database,
        project_id=project_id,
        request=request,
    )

    match = service.find_match(
        database,
        project_id=project_id,
        request=request,
        selected_model="llama3.2",
    )

    assert match is not None
    assert match.entry.id == saved.id
    assert match.similarity_score == pytest.approx(1.0)


def test_an_unrelated_prompt_does_not_match(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
) -> None:
    _save(
        service,
        database,
        project_id=project_id,
        request=_request("Explain caching in one sentence."),
    )

    match = service.find_match(
        database,
        project_id=project_id,
        request=_request("Write a haiku about the sea."),
        selected_model="llama3.2",
    )

    assert match is None


def test_matching_is_skipped_when_the_feature_is_disabled(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request("Explain caching in one sentence.")

    _save(service, database, project_id=project_id, request=request)

    monkeypatch.setattr(
        get_settings(),
        "semantic_cache_enabled",
        False,
    )

    assert (
        service.find_match(
            database,
            project_id=project_id,
            request=request,
            selected_model="llama3.2",
        )
        is None
    )


def test_an_empty_cache_produces_no_match(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
) -> None:
    assert (
        service.find_match(
            database,
            project_id=project_id,
            request=_request("Explain caching."),
            selected_model="llama3.2",
        )
        is None
    )


def test_a_close_match_below_the_threshold_is_rejected(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request("Explain caching in one sentence.")

    _save(service, database, project_id=project_id, request=request)

    monkeypatch.setattr(
        get_settings(),
        "semantic_cache_threshold",
        1.5,
    )

    assert (
        service.find_match(
            database,
            project_id=project_id,
            request=request,
            selected_model="llama3.2",
        )
        is None
    )


def test_another_projects_cache_entry_is_never_reused(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
    other_project_id: int,
) -> None:
    request = _request("Explain caching in one sentence.")

    _save(
        service,
        database,
        project_id=other_project_id,
        request=request,
        response="Answer that belongs to another tenant.",
    )

    assert (
        service.find_match(
            database,
            project_id=project_id,
            request=request,
            selected_model="llama3.2",
        )
        is None
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "openrouter"),
        ("temperature", 0.9),
        ("max_tokens", 500),
        ("credential_id", 12345),
    ],
)
def test_generation_parameters_partition_the_cache(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
    field: str,
    value: object,
) -> None:
    prompt = "Explain caching in one sentence."

    _save(
        service,
        database,
        project_id=project_id,
        request=_request(prompt),
    )

    assert (
        service.find_match(
            database,
            project_id=project_id,
            request=_request(prompt, **{field: value}),
            selected_model="llama3.2",
        )
        is None
    )


def test_a_different_model_does_not_share_cached_answers(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
) -> None:
    request = _request("Explain caching in one sentence.")

    _save(service, database, project_id=project_id, request=request)

    assert (
        service.find_match(
            database,
            project_id=project_id,
            request=request,
            selected_model="llama3.1",
        )
        is None
    )


def test_a_different_system_prompt_does_not_share_cached_answers(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
) -> None:
    prompt = "Explain caching in one sentence."

    _save(
        service,
        database,
        project_id=project_id,
        request=_request(prompt, system="You are terse."),
    )

    assert (
        service.find_match(
            database,
            project_id=project_id,
            request=_request(prompt, system="You are verbose."),
            selected_model="llama3.2",
        )
        is None
    )


def test_the_best_candidate_wins_when_several_are_comparable(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
) -> None:
    exact = _request("Explain caching in one sentence.")

    _save(
        service,
        database,
        project_id=project_id,
        request=_request("Explain rate limiting in one sentence."),
        response="Rate limiting answer.",
    )
    expected = _save(
        service,
        database,
        project_id=project_id,
        request=exact,
        response="Caching answer.",
    )

    match = service.find_match(
        database,
        project_id=project_id,
        request=exact,
        selected_model="llama3.2",
    )

    assert match is not None
    assert match.entry.id == expected.id
    assert match.entry.response == "Caching answer."


def test_the_candidate_window_is_bounded_by_configuration(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _request("Explain caching in one sentence.")

    _save(
        service,
        database,
        project_id=project_id,
        request=target,
        response="Oldest, and the only real match.",
    )

    for index in range(3):
        _save(
            service,
            database,
            project_id=project_id,
            request=_request(f"Unrelated question number {index}."),
            response=f"Unrelated answer {index}.",
        )

    monkeypatch.setattr(
        get_settings(),
        "semantic_cache_max_candidates",
        2,
    )

    assert (
        service.find_match(
            database,
            project_id=project_id,
            request=target,
            selected_model="llama3.2",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def test_saving_stores_usage_and_provenance(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
) -> None:
    entry = _save(
        service,
        database,
        project_id=project_id,
        request=_request("Explain caching.", system="Be terse."),
        response="Caching stores results for reuse.",
    )

    assert entry.project_id == project_id
    assert entry.provider == "ollama"
    assert entry.model == "llama3.2"
    assert entry.user_prompt == "Explain caching."
    assert entry.response == "Caching stores results for reuse."
    assert entry.total_tokens == 15
    assert entry.original_latency_ms == 42.0
    assert entry.original_cost_usd == 0.001
    assert entry.hit_count == 0
    assert entry.last_hit_at is None


def test_registering_a_hit_updates_the_counters(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
) -> None:
    entry = _save(
        service,
        database,
        project_id=project_id,
        request=_request("Explain caching."),
    )

    service.register_hit(database, entry)
    service.register_hit(database, entry)

    database.refresh(entry)

    assert entry.hit_count == 2
    assert entry.last_hit_at is not None


def test_duplicate_detection_matches_on_the_exact_prompt(
    database: Session,
    service: SemanticCacheService,
    project_id: int,
) -> None:
    request = _request("Explain caching in one sentence.")

    assert (
        service.response_already_stored(
            database,
            project_id=project_id,
            request=request,
            selected_model="llama3.2",
        )
        is False
    )

    _save(service, database, project_id=project_id, request=request)

    assert (
        service.response_already_stored(
            database,
            project_id=project_id,
            request=request,
            selected_model="llama3.2",
        )
        is True
    )

    assert (
        service.response_already_stored(
            database,
            project_id=project_id,
            request=_request("Explain caching in one SENTENCE."),
            selected_model="llama3.2",
        )
        is False
    )
