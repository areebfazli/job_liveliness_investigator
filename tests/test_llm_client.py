"""`rli.llm.client`: cost accounting, the OpenAI-compatible adapter, and the cache.

Nothing here touches the network or needs an API key. `OpenAICompatibleClient`
speaks plain HTTP, so it is exercised through `respx` — the same mocking layer
`tests/test_net.py` uses for probe traffic — against a fake endpoint at
`http://llm.test/v1`. That is a strictly better test than an injected SDK
double: it pins the actual bytes on the wire (the `response_format` block, the
`Authorization` header, the retry behaviour on a 429), which is where every
provider incompatibility will show up.

`CachedClient` is exercised against a real SQLite database (the `conn`
fixture's tmp_path file, never `data/rli.db`), because the behaviour under
test — a hit, a miss, an overwrite, a corrupt row — is behaviour of the
`llm_cache` table and its composite primary key.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel, ValidationError

from rli.config import Config, ModelPrice
from rli.llm.client import (
    CachedClient,
    LLMError,
    LLMRequestError,
    LLMResponse,
    LLMSchemaError,
    LLMTransportError,
    OpenAICompatibleClient,
    Prompt,
    ScriptedClient,
    UntrustedBlock,
    close_llm_client,
    compute_cost_usd,
    endpoint_unavailable_reason,
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
# A fake OpenAI-compatible endpoint
# ---------------------------------------------------------------------------

BASE_URL = "http://llm.test/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"

# A price table that exists only in this file, so the assertions below are
# about the arithmetic rather than about whatever `config.toml` charges today.
TEST_PRICES = {
    "test-model": ModelPrice(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0),
}


def chat_body(
    content: str = '{"verdict":"live","score":7}',
    *,
    model: str = "test-model",
    prompt_tokens: int = 1000,
    completion_tokens: int = 500,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One OpenAI-shaped chat-completions response body."""
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    body["usage"] = (
        usage
        if usage is not None
        else {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    )
    return body


def make_client(
    *,
    api_key: str = "",
    model_id: str = "test-model",
    max_retries: int = 2,
    sleeps: list[float] | None = None,
) -> OpenAICompatibleClient:
    """A client pointed at the fake endpoint, whose retries never really sleep."""
    return OpenAICompatibleClient(
        BASE_URL,
        api_key,
        model_id,
        timeout_s=1.0,
        max_tokens=256,
        prices=TEST_PRICES,
        max_retries=max_retries,
        sleep=(sleeps.append if sleeps is not None else (lambda _seconds: None)),
    )


def sent_body(route: respx.Route, index: int = 0) -> dict[str, Any]:
    return json.loads(route.calls[index].request.content)


# ---------------------------------------------------------------------------
# compute_cost_usd
# ---------------------------------------------------------------------------


def test_cost_uses_the_config_price_table(cfg: Config) -> None:
    price = cfg.llm.price_for("gemini-2.5-flash")
    assert price is not None and (price.input_usd_per_mtok, price.output_usd_per_mtok) == (
        0.30,
        2.50,
    )
    # 1M in @ $0.30 + 0.5M out @ $2.50.
    assert compute_cost_usd(cfg, "gemini-2.5-flash", 1_000_000, 500_000) == pytest.approx(1.55)
    assert compute_cost_usd(cfg, "gemini-2.5-flash", 1000, 500) == pytest.approx(0.00155)


def test_cost_differs_per_model(cfg: Config) -> None:
    pro = compute_cost_usd(cfg, "gemini-2.5-pro", 1_000_000, 0)
    flash = compute_cost_usd(cfg, "gemini-2.5-flash", 1_000_000, 0)
    assert pro == pytest.approx(1.25)
    assert flash == pytest.approx(0.30)


def test_a_local_model_is_priced_at_zero_explicitly(cfg: Config) -> None:
    # Not the unpriced-model fallback: the row exists and says 0.0, so the
    # ledger's zero for a local run is a recorded fact.
    assert cfg.llm.price_for(cfg.llm.model_id) is not None
    assert compute_cost_usd(cfg, cfg.llm.model_id, 10_000_000, 10_000_000) == 0.0


def test_unknown_model_costs_zero_and_does_not_raise(cfg: Config) -> None:
    # Documented deliberately: the price table is accounting, not a safety
    # control, so an unpriced model must under-report rather than abort.
    assert cfg.llm.price_for("no-such-model") is None
    assert compute_cost_usd(cfg, "no-such-model", 10_000_000, 10_000_000) == 0.0


def test_zero_tokens_cost_zero(cfg: Config) -> None:
    assert compute_cost_usd(cfg, "gemini-2.5-flash", 0, 0) == 0.0


# ---------------------------------------------------------------------------
# OpenAICompatibleClient — the json_schema happy path
# ---------------------------------------------------------------------------


@respx.mock
def test_client_posts_an_openai_shaped_json_schema_request() -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=chat_body()))
    prompt = make_prompt(posting_id="p1")

    make_client().complete_structured(prompt, Answer)

    assert route.called
    body = sent_body(route)
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 256
    assert body["messages"] == [
        {"role": "system", "content": prompt.system},
        {"role": "user", "content": prompt.render_user()},
    ]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "Answer",
            "schema": Answer.model_json_schema(),
            "strict": True,
        },
    }
    # The untrusted excerpt travels inside the user message's block, never in
    # the system prompt.
    assert "excerpt" not in body["messages"][0]["content"]
    assert "excerpt" in body["messages"][1]["content"]


@respx.mock
def test_client_returns_a_priced_validated_response() -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=chat_body()))

    response = make_client().complete_structured(make_prompt(), Answer)

    assert isinstance(response, LLMResponse)
    assert isinstance(response.parsed, Answer)
    assert response.parsed.verdict == "live"
    assert response.model_id == "test-model"
    assert (response.input_tokens, response.output_tokens) == (1000, 500)
    # 1000/1e6*$3 + 500/1e6*$15.
    assert response.cost_usd == pytest.approx(0.0105)
    assert response.cache_status == "n/a"
    assert response.latency_ms >= 0.0
    assert response.raw_text == '{"verdict":"live","score":7}'


@respx.mock
def test_model_id_comes_from_the_response_body() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=chat_body(model="test-model-0925"))
    )
    response = make_client().complete_structured(make_prompt(), Answer)
    assert response.model_id == "test-model-0925"
    # The reported id is unpriced, so the CONFIGURED id's price is used
    # rather than silently charging zero for a decorated alias.
    assert response.cost_usd == pytest.approx(0.0105)


@respx.mock
def test_an_unpriced_model_costs_zero_and_still_answers() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=chat_body(model="llama3.2:3b"))
    )
    response = make_client(model_id="llama3.2:3b").complete_structured(make_prompt(), Answer)
    assert response.cost_usd == 0.0
    assert response.parsed.verdict == "live"  # type: ignore[attr-defined]


@respx.mock
def test_missing_usage_reports_zero_tokens() -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=chat_body(usage={})))
    response = make_client().complete_structured(make_prompt(), Answer)
    assert (response.input_tokens, response.output_tokens, response.cost_usd) == (0, 0, 0.0)


@respx.mock
def test_content_delivered_as_parts_is_joined() -> None:
    body = chat_body()
    body["choices"][0]["message"]["content"] = [
        {"type": "text", "text": '{"verdict":"clo'},
        {"type": "text", "text": 'sed","score":2}'},
    ]
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))

    response = make_client().complete_structured(make_prompt(), Answer)
    assert response.parsed.verdict == "closed"  # type: ignore[attr-defined]


@respx.mock
def test_a_fenced_json_object_is_still_parsed() -> None:
    # Small local models fence their output even when told not to; recovering
    # is cheaper than throwing away a completed inference.
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json=chat_body(content='```json\n{"verdict":"live","score":1}\n```')
        )
    )
    response = make_client().complete_structured(make_prompt(), Answer)
    assert response.parsed.score == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Authorization header
# ---------------------------------------------------------------------------


@respx.mock
def test_authorization_header_is_sent_when_a_key_is_configured() -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=chat_body()))
    make_client(api_key="secret-key-123").complete_structured(make_prompt(), Answer)
    assert route.calls[0].request.headers["Authorization"] == "Bearer secret-key-123"


@respx.mock
def test_no_authorization_header_without_a_key() -> None:
    # The local-Ollama case: several compatible servers reject a malformed
    # `Bearer ` header while accepting an absent one.
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=chat_body()))
    make_client().complete_structured(make_prompt(), Answer)
    assert "authorization" not in route.calls[0].request.headers


@respx.mock
def test_the_api_key_never_appears_in_an_error_message() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(401, text="invalid api key: secret-key-123")
    )
    with pytest.raises(LLMError) as excinfo:
        make_client(api_key="secret-key-123").complete_structured(make_prompt(), Answer)
    assert "secret-key-123" not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_repr_does_not_carry_the_api_key() -> None:
    assert "secret-key-123" not in repr(make_client(api_key="secret-key-123"))


# ---------------------------------------------------------------------------
# json_object fallback (and its latch)
# ---------------------------------------------------------------------------


@respx.mock
def test_a_400_on_json_schema_falls_back_to_json_object_once() -> None:
    route = respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "response_format not supported"}}),
            httpx.Response(200, json=chat_body()),
        ]
    )

    response = make_client().complete_structured(make_prompt(), Answer)

    assert response.parsed.verdict == "live"  # type: ignore[attr-defined]
    assert route.call_count == 2
    first, second = sent_body(route, 0), sent_body(route, 1)
    assert first["response_format"]["type"] == "json_schema"
    assert second["response_format"] == {"type": "json_object"}
    # The schema has to reach the model somehow: it moves into the SYSTEM
    # message, never the user message where untrusted text lives.
    assert "verdict" in second["messages"][0]["content"]
    assert second["messages"][1]["content"] == first["messages"][1]["content"]


@respx.mock
def test_the_json_object_fallback_sticks_for_the_rest_of_the_client_life() -> None:
    route = respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": "unsupported"}),
            httpx.Response(200, json=chat_body()),
            httpx.Response(200, json=chat_body()),
            httpx.Response(200, json=chat_body()),
        ]
    )
    client = make_client()

    client.complete_structured(make_prompt(posting_id="p1"), Answer)
    client.complete_structured(make_prompt(posting_id="p2"), Answer)
    client.complete_structured(make_prompt(posting_id="p3"), Answer)

    # 2 for the first call (probe + fallback), then exactly 1 each: the
    # client never re-probes json_schema.
    assert route.call_count == 4
    assert [sent_body(route, i)["response_format"]["type"] for i in range(4)] == [
        "json_schema",
        "json_object",
        "json_object",
        "json_object",
    ]


@respx.mock
def test_a_second_400_in_json_object_mode_raises() -> None:
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(400, text="genuinely bad request")
    )
    with pytest.raises(LLMRequestError) as excinfo:
        make_client().complete_structured(make_prompt(), Answer)
    assert excinfo.value.status_code == 400
    # One probe + one fallback attempt, and no retry loop beyond that.
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# Retries: 429, 5xx, Retry-After, transport
# ---------------------------------------------------------------------------


@respx.mock
def test_a_429_is_retried_and_honours_retry_after_seconds() -> None:
    sleeps: list[float] = []
    route = respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}, text="slow down"),
            httpx.Response(200, json=chat_body()),
        ]
    )

    response = make_client(sleeps=sleeps).complete_structured(make_prompt(), Answer)

    assert response.parsed.verdict == "live"  # type: ignore[attr-defined]
    assert route.call_count == 2
    # The header wins over the 0.5s exponential backoff that would otherwise
    # apply to the first retry.
    assert sleeps == [7.0]


@respx.mock
def test_retry_after_accepts_an_http_date() -> None:
    sleeps: list[float] = []
    when = datetime.now(UTC) + timedelta(seconds=10)
    respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(
                503,
                headers={"Retry-After": when.strftime("%a, %d %b %Y %H:%M:%S GMT")},
            ),
            httpx.Response(200, json=chat_body()),
        ]
    )

    make_client(sleeps=sleeps).complete_structured(make_prompt(), Answer)

    assert len(sleeps) == 1
    assert 5.0 <= sleeps[0] <= 11.0


@respx.mock
def test_an_absurd_retry_after_is_clamped() -> None:
    sleeps: list[float] = []
    respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "86400"}),
            httpx.Response(200, json=chat_body()),
        ]
    )
    make_client(sleeps=sleeps).complete_structured(make_prompt(), Answer)
    # A quota that resets tomorrow must fail the run, not freeze the agent.
    assert sleeps == [60.0]


@respx.mock
def test_a_malformed_retry_after_falls_back_to_exponential_backoff() -> None:
    sleeps: list[float] = []
    respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "soon"}),
            httpx.Response(200, json=chat_body()),
        ]
    )
    make_client(sleeps=sleeps).complete_structured(make_prompt(), Answer)
    assert sleeps == [0.5]


@respx.mock
def test_5xx_retries_are_bounded_and_then_raise_the_retryable_error() -> None:
    sleeps: list[float] = []
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(503, text="overloaded"))

    with pytest.raises(LLMTransportError) as excinfo:
        make_client(max_retries=2, sleeps=sleeps).complete_structured(make_prompt(), Answer)

    assert excinfo.value.status_code == 503
    assert isinstance(excinfo.value, LLMError)
    # max_retries=2 means three attempts total, and exponential backoff
    # between them.
    assert route.call_count == 3
    assert sleeps == [0.5, 1.0]
    assert "overloaded" in str(excinfo.value)


@respx.mock
def test_max_retries_zero_attempts_once() -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(LLMTransportError):
        make_client(max_retries=0).complete_structured(make_prompt(), Answer)
    assert route.call_count == 1


@respx.mock
def test_a_connection_failure_is_retried_then_wrapped() -> None:
    sleeps: list[float] = []
    route = respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(LLMTransportError) as excinfo:
        make_client(max_retries=1, sleeps=sleeps).complete_structured(make_prompt(), Answer)

    assert route.call_count == 2
    assert sleeps == [0.5]
    assert "connection refused" in str(excinfo.value)


@respx.mock
def test_a_connection_failure_that_recovers_is_not_an_error() -> None:
    respx.post(CHAT_URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json=chat_body())]
    )
    assert make_client().complete_structured(make_prompt(), Answer).parsed.verdict == "live"  # type: ignore[attr-defined]


@respx.mock
@pytest.mark.parametrize("status", [401, 403, 404, 422])
def test_non_retryable_statuses_are_not_retried(status: int) -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(status, text="nope"))

    with pytest.raises(LLMRequestError) as excinfo:
        make_client().complete_structured(make_prompt(), Answer)

    assert excinfo.value.status_code == status
    assert not isinstance(excinfo.value, LLMTransportError)
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Schema failures
# ---------------------------------------------------------------------------


@respx.mock
def test_non_json_model_output_raises_a_schema_error() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=chat_body(content="I cannot answer that."))
    )
    with pytest.raises(LLMSchemaError) as excinfo:
        make_client().complete_structured(make_prompt(), Answer)
    # LLMSchemaError is an LLMError, so the agent loop's single handler works.
    assert isinstance(excinfo.value, LLMError)


@respx.mock
def test_a_non_json_http_body_raises_a_schema_error() -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, text="<html>hello</html>"))
    with pytest.raises(LLMSchemaError):
        make_client().complete_structured(make_prompt(), Answer)


@respx.mock
def test_json_that_does_not_fit_the_schema_raises_a_schema_error() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=chat_body(content='{"nope": 1}'))
    )
    with pytest.raises(LLMSchemaError):
        make_client().complete_structured(make_prompt(), Answer)


@respx.mock
def test_a_response_without_choices_raises_a_schema_error() -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json={"model": "test-model"}))
    with pytest.raises(LLMSchemaError):
        make_client().complete_structured(make_prompt(), Answer)


@respx.mock
def test_a_refusal_raises_a_schema_error() -> None:
    body = chat_body()
    body["choices"][0]["message"] = {"role": "assistant", "content": None, "refusal": "no"}
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(LLMSchemaError, match="refused"):
        make_client().complete_structured(make_prompt(), Answer)


@respx.mock
def test_output_matching_a_different_schema_raises() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=chat_body(content='{"other":"x"}'))
    )
    with pytest.raises(LLMSchemaError):
        make_client().complete_structured(make_prompt(), Answer)


# ---------------------------------------------------------------------------
# Nothing escapes as a non-LLMError
# ---------------------------------------------------------------------------


def test_a_non_http_error_from_httpx_is_still_an_llm_error() -> None:
    """`httpx.InvalidURL` is NOT an `httpx.HTTPError`, and must not escape.

    `[llm].base_url` only has to PARSE to pass config validation, so a host
    that httpx cannot IDNA-encode reaches the transport and raises something
    outside the `HTTPError` tree. The agent loop catches `LLMError` and
    spec.md §4 requires the run to reach a valid `Decision` regardless of
    what the model layer did, so an unwrapped exception here would break that
    guarantee rather than merely producing a worse message.
    """
    client = OpenAICompatibleClient("http://\u2603.com/v1", "", "test-model", max_retries=0)
    with pytest.raises(LLMError) as excinfo:
        client.complete_structured(make_prompt(), Answer)
    assert "could not be sent" in str(excinfo.value)


def test_a_keyboard_interrupt_is_not_swallowed() -> None:
    """The broad transport catch is `Exception`, so Ctrl-C still stops the run.

    Injected rather than mocked through respx, which refuses to raise a
    non-`Exception` side effect at all.
    """

    class Interrupting:
        def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            raise KeyboardInterrupt

    client = OpenAICompatibleClient(
        BASE_URL, "", "test-model", client=Interrupting()  # type: ignore[arg-type]
    )
    with pytest.raises(KeyboardInterrupt):
        client.complete_structured(make_prompt(), Answer)


# ---------------------------------------------------------------------------
# close_llm_client
# ---------------------------------------------------------------------------


def test_close_llm_client_closes_through_a_wrapper(conn: sqlite3.Connection) -> None:
    inner = make_client()
    close_llm_client(CachedClient(inner, conn))
    assert inner._client.is_closed


def test_close_llm_client_tolerates_a_client_with_nothing_to_close() -> None:
    # `ScriptedClient` holds no resources and exposes no `close`; teardown
    # must not care.
    close_llm_client(ScriptedClient([]))


def test_close_llm_client_does_not_close_an_injected_client() -> None:
    # The caller that passed the httpx client in still owns it.
    injected = httpx.Client()
    close_llm_client(make_client_with(injected))
    assert not injected.is_closed
    injected.close()


def make_client_with(client: httpx.Client) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(BASE_URL, "", "test-model", client=client)


# ---------------------------------------------------------------------------
# from_config
# ---------------------------------------------------------------------------


def test_from_config_reads_the_endpoint_model_and_key_env(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cfg.llm.api_key_env, "from-the-environment")
    client = OpenAICompatibleClient.from_config(cfg)

    assert client.base_url == cfg.llm.base_url
    assert client.model_id == cfg.llm.model_id
    assert client._headers()["Authorization"] == "Bearer from-the-environment"
    client.close()


def test_from_config_sends_no_auth_header_when_the_env_var_is_unset(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(cfg.llm.api_key_env, raising=False)
    client = OpenAICompatibleClient.from_config(cfg)
    assert "Authorization" not in client._headers()
    client.close()


def test_from_config_model_id_override_is_used_for_the_call_and_the_price(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(cfg.llm.api_key_env, raising=False)
    client = OpenAICompatibleClient.from_config(cfg, model_id="gemini-2.5-pro")
    try:
        assert client.model_id == "gemini-2.5-pro"
        assert client._cost_usd("gemini-2.5-pro", 1_000_000, 0) == pytest.approx(1.25)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# endpoint_unavailable_reason
# ---------------------------------------------------------------------------


@respx.mock
def test_a_reachable_endpoint_has_no_unavailable_reason(cfg: Config) -> None:
    respx.get(f"{cfg.llm.base_url}/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    assert endpoint_unavailable_reason(cfg) is None


@respx.mock
def test_an_unreachable_endpoint_reports_why(cfg: Config) -> None:
    respx.get(f"{cfg.llm.base_url}/models").mock(side_effect=httpx.ConnectError("refused"))
    reason = endpoint_unavailable_reason(cfg)
    assert reason is not None and "not reachable" in reason


@respx.mock
def test_a_404_on_models_still_counts_as_reachable(cfg: Config) -> None:
    # Not every compatible server implements /models; a 404 still proves a
    # server answered.
    respx.get(f"{cfg.llm.base_url}/models").mock(return_value=httpx.Response(404))
    assert endpoint_unavailable_reason(cfg) is None


@respx.mock
def test_rejected_credentials_are_reported(cfg: Config) -> None:
    respx.get(f"{cfg.llm.base_url}/models").mock(return_value=httpx.Response(401))
    reason = endpoint_unavailable_reason(cfg)
    assert reason is not None and "credentials" in reason


def test_a_remote_endpoint_without_a_key_is_unavailable_without_any_request(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No respx mock is installed: reaching the network at all would raise,
    # so this also proves the config check short-circuits the probe.
    remote = cfg.model_copy(
        update={
            "llm": cfg.llm.model_copy(
                update={"base_url": "https://generativelanguage.googleapis.com/v1beta/openai"}
            )
        }
    )
    monkeypatch.delenv(remote.llm.api_key_env, raising=False)
    reason = endpoint_unavailable_reason(remote)
    assert reason is not None and remote.llm.api_key_env in reason


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
        model_id="gemini-2.5-flash",
        input_tokens=1000,
        output_tokens=500,
        cfg=cfg,
    )
    assert client.complete_structured(make_prompt(), Answer).cost_usd == pytest.approx(
        0.00155
    )


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
    inner = ScriptedClient([], model_id="gemini-2.5-pro")
    assert CachedClient(inner, conn).model_id == "gemini-2.5-pro"


@respx.mock
def test_cached_client_wraps_the_live_adapter(conn: sqlite3.Connection) -> None:
    # The wiring the CLI uses: CachedClient(OpenAICompatibleClient(...), conn).
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=chat_body()))
    inner = make_client()
    cached = CachedClient(inner, conn)
    prompt = make_prompt(posting_id="p1")

    first = cached.complete_structured(prompt, Answer)
    second = cached.complete_structured(prompt, Answer)

    assert (first.cache_status, second.cache_status) == ("miss", "hit")
    # The hit never reached the endpoint — that is what makes replay exact.
    assert route.call_count == 1
    assert first.cost_usd == pytest.approx(0.0105)
    assert second.cost_usd == 0.0
    assert cache_rows(conn)[0][0] == inner.model_id
