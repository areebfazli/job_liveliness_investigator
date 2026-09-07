"""`rli.llm.client`: cost accounting, the Anthropic adapter, and the replay cache.

Nothing here touches the network or needs an API key. `AnthropicClient` is
exercised through an injected fake that mimics the SDK surface the adapter
actually uses (`messages.parse(...)` -> `.parsed_output`, `.usage`,
`.content`), and `test_anthropic_client_does_not_import_the_sdk_when_a_client_
is_injected` pins the lazy-import guarantee that makes that possible.

`CachedClient` is exercised against a real SQLite database (the `conn`
fixture's tmp_path file, never `data/rli.db`), because the behaviour under
test — a hit, a miss, an overwrite, a corrupt row — is behaviour of the
`llm_cache` table and its composite primary key.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from rli.config import Config
from rli.llm.client import (
    AnthropicClient,
    CachedClient,
    LLMError,
    LLMResponse,
    LLMSchemaError,
    Prompt,
    ScriptedClient,
    UntrustedBlock,
    compute_cost_usd,
)
from rli.llm.prompts import build_investigator_prompt


class Answer(BaseModel):
    verdict: str
    score: int = 0


class Alt(BaseModel):
    other: str


def make_prompt(**structured: Any) -> Prompt:
    return build_investigator_prompt(
        structured_input=structured or {"posting_id": "p1"},
        untrusted=[UntrustedBlock(source="e1", content="excerpt")],
    )


# ---------------------------------------------------------------------------
# A fake mimicking the SDK surface `AnthropicClient` uses
# ---------------------------------------------------------------------------


class FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeParsed:
    def __init__(self, parsed_output: Any, *, usage: Any, content: Any) -> None:
        self.parsed_output = parsed_output
        self.usage = usage
        self.content = content


class FakeMessages:
    def __init__(self, result: Any, *, raises: BaseException | None = None) -> None:
        self._result = result
        self._raises = raises
        self.kwargs: dict[str, Any] | None = None
        self.call_count = 0

    def parse(self, **kwargs: Any) -> Any:
        self.call_count += 1
        self.kwargs = kwargs
        if self._raises is not None:
            raise self._raises
        return self._result


class FakeAnthropic:
    def __init__(self, result: Any = None, *, raises: BaseException | None = None) -> None:
        self.messages = FakeMessages(result, raises=raises)


def fake_ok(
    parsed: Any = None,
    *,
    input_tokens: int = 1000,
    output_tokens: int = 500,
    text: str = '{"verdict":"live","score":7}',
) -> FakeAnthropic:
    if parsed is None:
        parsed = Answer(verdict="live", score=7)
    return FakeAnthropic(
        FakeParsed(
            parsed,
            usage=FakeUsage(input_tokens, output_tokens),
            content=[FakeTextBlock(text)],
        )
    )


# ---------------------------------------------------------------------------
# compute_cost_usd
# ---------------------------------------------------------------------------


def test_cost_uses_the_config_price_table(cfg: Config) -> None:
    price = cfg.llm.price_for("claude-sonnet-5")
    assert price is not None and (price.input_usd_per_mtok, price.output_usd_per_mtok) == (
        2.0,
        10.0,
    )
    # 1M in @ $2 + 0.5M out @ $10.
    assert compute_cost_usd(cfg, "claude-sonnet-5", 1_000_000, 500_000) == pytest.approx(7.0)
    assert compute_cost_usd(cfg, "claude-sonnet-5", 1000, 500) == pytest.approx(0.007)


def test_cost_differs_per_model(cfg: Config) -> None:
    opus = compute_cost_usd(cfg, "claude-opus-5", 1_000_000, 0)
    haiku = compute_cost_usd(cfg, "claude-haiku-4-5", 1_000_000, 0)
    assert opus == pytest.approx(5.0)
    assert haiku == pytest.approx(1.0)


def test_unknown_model_costs_zero_and_does_not_raise(cfg: Config) -> None:
    # Documented deliberately: the price table is accounting, not a safety
    # control, so an unpriced model must under-report rather than abort.
    assert cfg.llm.price_for("no-such-model") is None
    assert compute_cost_usd(cfg, "no-such-model", 10_000_000, 10_000_000) == 0.0


def test_zero_tokens_cost_zero(cfg: Config) -> None:
    assert compute_cost_usd(cfg, "claude-sonnet-5", 0, 0) == 0.0


# ---------------------------------------------------------------------------
# AnthropicClient (injected fake — no network, no API key)
# ---------------------------------------------------------------------------


def test_anthropic_client_sends_the_expected_request(cfg: Config) -> None:
    fake = fake_ok()
    client = AnthropicClient(cfg, client=fake)
    prompt = make_prompt(posting_id="p1")

    client.complete_structured(prompt, Answer)

    kwargs = fake.messages.kwargs
    assert kwargs is not None
    assert kwargs["model"] == cfg.llm.model_id
    assert kwargs["max_tokens"] == cfg.llm.max_tokens
    assert kwargs["output_format"] is Answer
    assert kwargs["system"] == [
        {
            "type": "text",
            "text": prompt.system,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert kwargs["messages"] == [{"role": "user", "content": prompt.render_user()}]
    # The untrusted excerpt travels inside the user message's block, never in
    # the system prompt.
    assert "excerpt" not in kwargs["system"][0]["text"]
    assert "excerpt" in kwargs["messages"][0]["content"]


def test_anthropic_client_returns_a_priced_validated_response(cfg: Config) -> None:
    client = AnthropicClient(cfg, client=fake_ok(input_tokens=1000, output_tokens=500))

    response = client.complete_structured(make_prompt(), Answer)

    assert isinstance(response, LLMResponse)
    assert isinstance(response.parsed, Answer)
    assert response.parsed.verdict == "live"
    assert response.model_id == cfg.llm.model_id
    assert (response.input_tokens, response.output_tokens) == (1000, 500)
    assert response.cost_usd == pytest.approx(0.007)
    assert response.cache_status == "n/a"
    assert response.latency_ms >= 0.0
    assert response.raw_text == '{"verdict":"live","score":7}'


def test_anthropic_client_model_id_override_is_used_for_call_and_price(cfg: Config) -> None:
    fake = fake_ok(input_tokens=1_000_000, output_tokens=0)
    client = AnthropicClient(cfg, model_id="claude-opus-5", client=fake)

    response = client.complete_structured(make_prompt(), Answer)

    assert client.model_id == "claude-opus-5"
    assert fake.messages.kwargs is not None
    assert fake.messages.kwargs["model"] == "claude-opus-5"
    assert response.model_id == "claude-opus-5"
    assert response.cost_usd == pytest.approx(5.0)


def test_anthropic_client_unpriced_model_still_answers(cfg: Config) -> None:
    client = AnthropicClient(cfg, model_id="some-future-model", client=fake_ok())
    response = client.complete_structured(make_prompt(), Answer)
    assert response.cost_usd == 0.0


def test_anthropic_client_falls_back_to_parsed_json_for_raw_text(cfg: Config) -> None:
    fake = FakeAnthropic(
        FakeParsed(Answer(verdict="live", score=1), usage=FakeUsage(1, 1), content=None)
    )
    response = AnthropicClient(cfg, client=fake).complete_structured(make_prompt(), Answer)
    assert response.raw_text == Answer(verdict="live", score=1).model_dump_json()


def test_anthropic_client_tolerates_missing_usage(cfg: Config) -> None:
    fake = FakeAnthropic(FakeParsed(Answer(verdict="live"), usage=None, content=[]))
    response = AnthropicClient(cfg, client=fake).complete_structured(make_prompt(), Answer)
    assert (response.input_tokens, response.output_tokens, response.cost_usd) == (0, 0, 0.0)


def test_anthropic_client_coerces_a_dict_parsed_output(cfg: Config) -> None:
    fake = fake_ok({"verdict": "closed", "score": 2})
    response = AnthropicClient(cfg, client=fake).complete_structured(make_prompt(), Answer)
    assert isinstance(response.parsed, Answer)
    assert response.parsed.verdict == "closed"


def test_anthropic_client_wraps_transport_failures(cfg: Config) -> None:
    fake = FakeAnthropic(None, raises=RuntimeError("connection reset"))
    with pytest.raises(LLMError) as excinfo:
        AnthropicClient(cfg, client=fake).complete_structured(make_prompt(), Answer)
    assert "connection reset" in str(excinfo.value)
    assert not isinstance(excinfo.value, LLMSchemaError)


def test_anthropic_client_does_not_swallow_keyboard_interrupt(cfg: Config) -> None:
    fake = FakeAnthropic(None, raises=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        AnthropicClient(cfg, client=fake).complete_structured(make_prompt(), Answer)


def test_anthropic_client_raises_schema_error_on_missing_output(cfg: Config) -> None:
    fake = FakeAnthropic(FakeParsed(None, usage=FakeUsage(1, 1), content=[]))
    with pytest.raises(LLMSchemaError):
        AnthropicClient(cfg, client=fake).complete_structured(make_prompt(), Answer)


def test_anthropic_client_raises_schema_error_on_wrong_shape(cfg: Config) -> None:
    fake = fake_ok({"nope": 1})
    with pytest.raises(LLMSchemaError) as excinfo:
        AnthropicClient(cfg, client=fake).complete_structured(make_prompt(), Answer)
    # LLMSchemaError is an LLMError, so the agent loop's single handler works.
    assert isinstance(excinfo.value, LLMError)


def test_anthropic_client_schema_error_on_a_different_model_class(cfg: Config) -> None:
    fake = fake_ok(Alt(other="x"))
    with pytest.raises(LLMSchemaError):
        AnthropicClient(cfg, client=fake).complete_structured(make_prompt(), Answer)


def test_anthropic_client_does_not_import_the_sdk_when_a_client_is_injected(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `None` in sys.modules makes `import anthropic` raise ImportError, which
    # simulates the package being absent. Constructing with an injected
    # client must still work: that is what keeps `rli.llm` importable (and
    # every non-live test runnable) without the dependency installed.
    monkeypatch.setitem(sys.modules, "anthropic", None)

    client = AnthropicClient(cfg, client=fake_ok())
    assert client.complete_structured(make_prompt(), Answer).parsed.verdict == "live"


def test_anthropic_client_reports_a_missing_sdk_as_an_llm_error(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)

    with pytest.raises(LLMError) as excinfo:
        AnthropicClient(cfg)
    assert "anthropic" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ScriptedClient
# ---------------------------------------------------------------------------


def test_scripted_client_replays_in_order_and_records_calls() -> None:
    client = ScriptedClient([Answer(verdict="a"), Answer(verdict="b")])
    prompt = make_prompt()

    first = client.complete_structured(prompt, Answer)
    second = client.complete_structured(prompt, Answer)

    assert [r.parsed.verdict for r in (first, second)] == ["a", "b"]  # type: ignore[attr-defined]
    assert client.calls == [(prompt, Answer), (prompt, Answer)]
    assert first.cache_status == "n/a"
    assert first.model_id == "scripted-model"
    assert first.cost_usd == 0.0


def test_scripted_client_raises_scripted_exceptions() -> None:
    client = ScriptedClient([LLMError("boom"), Answer(verdict="a")])
    with pytest.raises(LLMError):
        client.complete_structured(make_prompt(), Answer)
    assert client.complete_structured(make_prompt(), Answer).parsed.verdict == "a"  # type: ignore[attr-defined]


def test_scripted_client_reports_exhaustion() -> None:
    client = ScriptedClient([])
    with pytest.raises(LLMError, match="exhausted"):
        client.complete_structured(make_prompt(), Answer)


def test_scripted_client_accepts_a_callable() -> None:
    def answer(prompt: Prompt, schema: type[BaseModel]) -> BaseModel:
        return Answer(verdict=prompt.template_id)

    client = ScriptedClient(answer)
    assert client.complete_structured(make_prompt(), Answer).parsed.verdict == "investigator"  # type: ignore[attr-defined]


def test_scripted_client_rejects_a_mismatched_schema() -> None:
    client = ScriptedClient([Alt(other="x")])
    with pytest.raises(LLMSchemaError):
        client.complete_structured(make_prompt(), Answer)


def test_scripted_client_prices_against_config_when_given_one(cfg: Config) -> None:
    client = ScriptedClient(
        [Answer(verdict="a")],
        model_id="claude-sonnet-5",
        input_tokens=1000,
        output_tokens=500,
        cfg=cfg,
    )
    assert client.complete_structured(make_prompt(), Answer).cost_usd == pytest.approx(0.007)


# ---------------------------------------------------------------------------
# CachedClient (real SQLite, via the `conn` fixture's tmp_path database)
# ---------------------------------------------------------------------------


def cache_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT model_id, prompt_hash, structured_input_hash, response, created_at "
            "FROM llm_cache ORDER BY structured_input_hash"
        ).fetchall()
    ]


def test_miss_calls_inner_and_writes_the_row(conn: sqlite3.Connection) -> None:
    inner = ScriptedClient([Answer(verdict="live", score=3)])
    cached = CachedClient(inner, conn, now=lambda: datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC))
    prompt = make_prompt(posting_id="p1")

    response = cached.complete_structured(prompt, Answer)

    assert response.cache_status == "miss"
    assert response.parsed.score == 3  # type: ignore[attr-defined]
    assert len(inner.calls) == 1

    rows = cache_rows(conn)
    assert len(rows) == 1
    model_id, prompt_hash, structured_hash, stored, created_at = rows[0]
    assert model_id == "scripted-model"
    assert prompt_hash == prompt.prompt_hash(Answer)
    assert structured_hash == prompt.structured_input_hash()
    assert Answer.model_validate_json(stored) == Answer(verdict="live", score=3)
    assert created_at == "2026-03-04T05:06:07.000000Z"


def test_second_identical_call_is_a_free_hit_and_does_not_call_inner(
    conn: sqlite3.Connection,
) -> None:
    inner = ScriptedClient([Answer(verdict="live", score=3)])
    cached = CachedClient(inner, conn)
    prompt = make_prompt(posting_id="p1")

    first = cached.complete_structured(prompt, Answer)
    second = cached.complete_structured(prompt, Answer)

    assert first.cache_status == "miss"
    assert second.cache_status == "hit"
    # The inner client was called exactly once: a second call would also have
    # raised, since only one response was scripted.
    assert len(inner.calls) == 1

    assert second.cost_usd == 0.0
    assert (second.input_tokens, second.output_tokens) == (0, 0)
    assert second.model_id == "scripted-model"
    assert second.raw_text == Answer(verdict="live", score=3).model_dump_json()
    assert second.parsed == first.parsed
    assert second.latency_ms >= 0.0
    assert len(cache_rows(conn)) == 1


def test_a_hit_survives_a_new_client_instance(conn: sqlite3.Connection) -> None:
    prompt = make_prompt(posting_id="p1")
    CachedClient(ScriptedClient([Answer(verdict="live")]), conn).complete_structured(
        prompt, Answer
    )

    # A fresh process would build a fresh wrapper over a fresh inner client;
    # the row must still serve it (this is what replay depends on).
    fresh_inner = ScriptedClient([])
    response = CachedClient(fresh_inner, conn).complete_structured(prompt, Answer)

    assert response.cache_status == "hit"
    assert fresh_inner.calls == []


def test_a_different_structured_input_misses(conn: sqlite3.Connection) -> None:
    inner = ScriptedClient([Answer(verdict="a"), Answer(verdict="b")])
    cached = CachedClient(inner, conn)

    first = cached.complete_structured(make_prompt(posting_id="p1"), Answer)
    second = cached.complete_structured(make_prompt(posting_id="p2"), Answer)

    assert (first.cache_status, second.cache_status) == ("miss", "miss")
    assert [r.parsed.verdict for r in (first, second)] == ["a", "b"]  # type: ignore[attr-defined]
    assert len(inner.calls) == 2
    assert len(cache_rows(conn)) == 2


def test_a_different_untrusted_block_misses(conn: sqlite3.Connection) -> None:
    inner = ScriptedClient([Answer(verdict="a"), Answer(verdict="b")])
    cached = CachedClient(inner, conn)
    base = {"posting_id": "p1"}

    cached.complete_structured(
        build_investigator_prompt(
            structured_input=base, untrusted=[UntrustedBlock(source="e1", content="one")]
        ),
        Answer,
    )
    second = cached.complete_structured(
        build_investigator_prompt(
            structured_input=base, untrusted=[UntrustedBlock(source="e1", content="two")]
        ),
        Answer,
    )

    assert second.cache_status == "miss"
    assert len(cache_rows(conn)) == 2


def test_a_different_schema_misses(conn: sqlite3.Connection) -> None:
    # The schema is folded into `prompt_hash`, so asking the same case for a
    # different output type must not reuse the other type's row.
    prompt = make_prompt(posting_id="p1")
    CachedClient(ScriptedClient([Answer(verdict="a")]), conn).complete_structured(prompt, Answer)

    inner = ScriptedClient([Alt(other="x")])
    response = CachedClient(inner, conn).complete_structured(prompt, Alt)

    assert response.cache_status == "miss"
    assert len(inner.calls) == 1
    assert len(cache_rows(conn)) == 2


def test_a_different_model_id_misses(conn: sqlite3.Connection) -> None:
    prompt = make_prompt(posting_id="p1")
    CachedClient(ScriptedClient([Answer(verdict="a")], model_id="m1"), conn).complete_structured(
        prompt, Answer
    )

    inner = ScriptedClient([Answer(verdict="b")], model_id="m2")
    response = CachedClient(inner, conn).complete_structured(prompt, Answer)

    assert response.cache_status == "miss"
    assert response.parsed.verdict == "b"  # type: ignore[attr-defined]
    assert {row[0] for row in cache_rows(conn)} == {"m1", "m2"}


@pytest.mark.parametrize(
    "corrupt",
    [
        "not json at all",
        "{}",  # valid JSON, missing the required field
        '{"verdict": []}',  # wrong type
        "",
    ],
)
def test_a_corrupt_row_is_treated_as_a_miss_and_overwritten(
    conn: sqlite3.Connection, corrupt: str
) -> None:
    prompt = make_prompt(posting_id="p1")
    conn.execute(
        "INSERT INTO llm_cache (model_id, prompt_hash, structured_input_hash, response, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (
            "scripted-model",
            prompt.prompt_hash(Answer),
            prompt.structured_input_hash(),
            corrupt,
            "2026-01-01T00:00:00.000000Z",
        ),
    )
    conn.commit()
    with pytest.raises(ValidationError):
        Answer.model_validate_json(corrupt)

    inner = ScriptedClient([Answer(verdict="repaired", score=9)])
    response = CachedClient(inner, conn).complete_structured(prompt, Answer)

    # A corrupt row must degrade to a re-fetch, never wedge the run.
    assert response.cache_status == "miss"
    assert len(inner.calls) == 1

    rows = cache_rows(conn)
    assert len(rows) == 1
    assert Answer.model_validate_json(rows[0][3]) == Answer(verdict="repaired", score=9)

    # ...and the repaired row now serves a hit.
    assert (
        CachedClient(ScriptedClient([]), conn).complete_structured(prompt, Answer).cache_status
        == "hit"
    )


def test_an_inner_failure_writes_nothing(conn: sqlite3.Connection) -> None:
    cached = CachedClient(ScriptedClient([LLMError("boom")]), conn)
    with pytest.raises(LLMError):
        cached.complete_structured(make_prompt(), Answer)
    assert cache_rows(conn) == []


def test_model_id_delegates_to_the_inner_client(conn: sqlite3.Connection) -> None:
    inner = ScriptedClient([], model_id="claude-opus-5")
    assert CachedClient(inner, conn).model_id == "claude-opus-5"


def test_cached_client_wraps_the_anthropic_adapter(
    cfg: Config, conn: sqlite3.Connection
) -> None:
    # The wiring the CLI uses: CachedClient(AnthropicClient(...), conn).
    fake = fake_ok(input_tokens=1000, output_tokens=500)
    cached = CachedClient(AnthropicClient(cfg, client=fake), conn)
    prompt = make_prompt(posting_id="p1")

    first = cached.complete_structured(prompt, Answer)
    second = cached.complete_structured(prompt, Answer)

    assert (first.cache_status, second.cache_status) == ("miss", "hit")
    assert fake.messages.call_count == 1
    assert first.cost_usd == pytest.approx(0.007)
    assert second.cost_usd == 0.0
    assert cache_rows(conn)[0][0] == cfg.llm.model_id
