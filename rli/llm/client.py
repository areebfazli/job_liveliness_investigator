"""Structured-output LLM client, cost accounting, and the replay cache (spec.md §2/§6).

This module is the single doorway between System C and a language model.
Everything it exports exists to satisfy four spec requirements at once:

* spec.md §2 — "Treat job pages/news as untrusted data; delimit them in
  prompts." Untrusted text may only travel as an `UntrustedBlock`, and a
  block cannot hold a live delimiter (see `UntrustedBlock` below).
* spec.md §2 — "Schema-validate model outputs." `complete_structured` never
  returns free text; it returns an instance of the caller's Pydantic model
  or raises `LLMSchemaError`.
* spec.md §2 — "Cache LLM outputs by `(model_id, prompt_hash,
  structured_input_hash)` for exact benchmark replay; do not assume
  temperature 0 makes APIs deterministic." `CachedClient` is that cache, and
  the sentence after the semicolon is why it exists: we do not get
  reproducibility from the API, so we buy it from the cache.
* spec.md §6 — in replay, "live **LLM** calls are allowed on cache miss
  (e.g. a changed investigator prompt) and are recorded, so the cache is
  complete for subsequent runs." A miss is a normal event, not an error.

--------------------------------------------------------------------------
JUDGMENT CALL: why the prompt is split in half, and hashed in two pieces
--------------------------------------------------------------------------

`Prompt` deliberately keeps the TEMPLATE and the DATA apart, and hashes each
half on its own:

* `prompt_hash(schema)` covers `template_id`, `version`, `system`,
  `instructions` and the canonical JSON of `schema.model_json_schema()` —
  and NOTHING about the case under investigation.
* `structured_input_hash()` covers `structured_input` and `untrusted` — and
  NOTHING about the template.

The obvious alternative (hash the whole rendered user message into
`prompt_hash`) is what the spec's cache key would degenerate into: both
components would then vary together per case, `structured_input_hash` would
be a redundant function of `prompt_hash`, and the composite primary key on
`llm_cache` would carry one bit of information instead of two. Splitting it
buys two properties that the agent actually depends on:

1. **A changed template invalidates the cache, globally and immediately.**
   Editing one word of `rli.llm.prompts.INVESTIGATOR_INSTRUCTIONS`, bumping
   `PROMPT_VERSION`, or changing a field on the output schema moves
   `prompt_hash` for every case at once. spec.md §6 names exactly this case
   ("a changed investigator prompt") as the legitimate reason for a live
   call during replay.
2. **The same case under two templates is two distinguishable rows.** Cache
   rows stay attributable: you can see which template produced which answer
   for a given case, rather than an opaque per-(case, template) blob.

The output schema is folded into the TEMPLATE half rather than the data half
because it is part of the request contract, not part of the case: two
different `schema` types asked about one case must not share a cache row —
the stored JSON would not validate against the other model. `CachedClient`
still treats a row that fails to validate as a miss (see there), which is
the belt to this braces.

--------------------------------------------------------------------------
Canonical JSON
--------------------------------------------------------------------------

Both hashes go through `_canonical_json`, which follows the
`rli.net.client.hash_args` convention (`sort_keys=True`, tight separators,
`default=str`) and adds one thing on top: sets, frozensets and tuples are
normalized to sorted/ordered lists, and non-string dict keys are coerced to
strings, BEFORE serialization. `hash_args` can skip that because probe
arguments are flat scalars; a `structured_input` is a nested document
assembled by `rli.agent.investigator`, and a stray `set` in it would hash
differently on every process (PYTHONHASHSEED), silently turning the cache
into a permanent miss — cost and unreproducibility with no error anywhere.
Normalizing is a few lines; detecting that failure in production is not.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from rli.config import Config, ModelPrice
from rli.models.time import now_utc, to_utc_z

__all__ = [
    "CachedClient",
    "CacheStatus",
    "LLMClient",
    "LLMError",
    "LLMRequestError",
    "LLMResponse",
    "LLMSchemaError",
    "LLMTransportError",
    "OpenAICompatibleClient",
    "Prompt",
    "ScriptedClient",
    "UntrustedBlock",
    "close_llm_client",
    "compute_cost_usd",
    "endpoint_unavailable_reason",
    "neutralize_delimiters",
]

CacheStatus = Literal["hit", "miss", "n/a"]

# The delimiter that fences attacker-controllable text. Kept here rather than
# imported from `rli.llm.prompts` because `prompts` imports this module (it
# builds `Prompt`s), and the neutralization below must run inside
# `UntrustedBlock` itself — see the class docstring.
_UNTRUSTED_TAG = "untrusted"

# Matches any attempt to open or close an untrusted block: `<untrusted`,
# `</untrusted`, `< /UNTRUSTED`, `</ untrusted`, and any whitespace variant
# (including newlines and tabs) of those. Anchored on the `<`, because that
# character is the only way to start a tag — neutralizing it is sufficient
# and, unlike escaping the tag NAME, cannot be re-assembled by the attacker.
_DELIMITER_RE = re.compile(rf"<\s*/?\s*{_UNTRUSTED_TAG}", re.IGNORECASE)

# Characters kept verbatim in a rendered `source="..."` attribute. Everything
# else becomes `_`. Evidence ids (`e3`) and source labels (`json_ld`) are
# already inside this set, so the mapping is the identity in practice; it
# exists so that a source string reaching us from anywhere at all cannot
# close the attribute, close the tag, or inject a second attribute.
_SOURCE_SAFE_RE = re.compile(r"[^A-Za-z0-9 ._:@/-]")


def neutralize_delimiters(text: str) -> str:
    """Rewrite every `<untrusted` / `</untrusted` so it cannot fence a block.

    The leading `<` is replaced by `&lt;`, leaving the rest of the match
    intact: `</untrusted>` becomes `&lt;/untrusted>`, which reads the same to
    a human, carries no `<`, and therefore cannot terminate the block it sits
    in. The function is IDEMPOTENT (a second pass finds no `<` to rewrite),
    so it is safe to apply defensively at several layers — which this module
    does.

    `rli.llm.prompts.sanitize_untrusted` is the public, documented name for
    this operation; it lives there because sanitizing untrusted input is a
    prompt-construction concern. The implementation lives here because
    `UntrustedBlock` must apply it during validation and cannot import
    `prompts` without a cycle.
    """
    return _DELIMITER_RE.sub(lambda m: "&lt;" + m.group(0)[1:], text)


def _canonical_json(value: Any) -> str:
    """Deterministic JSON for hashing (see the module docstring)."""
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _normalize(value: Any) -> Any:
    """Make `value` order-stable and JSON-shaped without ever raising.

    Sets and frozensets become lists sorted by their own canonical JSON
    (so ordering does not depend on Python's element ordering or on the
    elements being mutually comparable); tuples become lists; dict keys
    become strings so `sort_keys=True` cannot trip over a mixed-key mapping.
    Anything else is handed to `json.dumps`, whose `default=str` catches the
    remainder (notably datetimes).
    """
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, (set, frozenset)):
        items = [_normalize(v) for v in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return value


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class UntrustedBlock(BaseModel):
    """One block of attacker-controllable text (job page / news / `raw_excerpt`).

    spec.md §2 requires untrusted data to be delimited in prompts. A
    delimiter is only a boundary if the fenced text cannot forge it, so this
    model neutralizes the delimiter *during validation* rather than trusting
    callers to have called `sanitize_untrusted` first:

    * `content` is passed through `neutralize_delimiters`;
    * `source` is additionally reduced to attribute-safe characters, since
      it is rendered inside `source="..."`.

    That is a deliberate strengthening of the interface contract, which only
    requires callers to sanitize. Making it impossible to CONSTRUCT a block
    carrying a live `</untrusted>` means a future caller who forgets is
    wrong-but-safe instead of exploitable, and because the transformation is
    idempotent the extra pass costs nothing when the caller did sanitize.

    Sanitizing `source` in the validator (rather than at render time) also
    keeps `structured_input_hash` honest: what is hashed is exactly what is
    rendered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    content: str

    @field_validator("source")
    @classmethod
    def _safe_source(cls, value: str) -> str:
        return _SOURCE_SAFE_RE.sub("_", value)

    @field_validator("content")
    @classmethod
    def _safe_content(cls, value: str) -> str:
        return neutralize_delimiters(value)


class Prompt(BaseModel):
    """A rendered prompt, split so the composite cache key is non-degenerate.

    `system` + `instructions` + the output `schema` are the TEMPLATE half
    (`prompt_hash`); `structured_input` + `untrusted` are the DATA half
    (`structured_input_hash`). The module docstring explains why that split
    is load-bearing rather than cosmetic.

    `structured_input` is the JSON-rendered case summary and MUST NOT contain
    raw excerpt text: excerpts are attacker-controlled, so they belong in an
    `UntrustedBlock` (the case summary refers to them by evidence id
    instead). `render_user` enforces the separation structurally — it emits
    `structured_input` and the untrusted blocks into different, clearly
    labelled regions of the message — but it cannot detect an excerpt that a
    caller smuggled INTO `structured_input`, so `rli.agent.investigator` is
    responsible for passing only `raw_excerpt_ref` ids there.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    version: str
    system: str
    instructions: str
    structured_input: dict[str, Any]
    untrusted: tuple[UntrustedBlock, ...] = ()

    def render_user(self) -> str:
        """Render the user message: instructions, then case state, then untrusted blocks.

        Layout, in order:

        1. `instructions` — static template text, no case data.
        2. `<case_state>` … `</case_state>` — the canonical JSON of
           `structured_input`. Canonical (sorted keys, tight separators) so
           the bytes the model sees are as stable as the hash.
        3. One `<untrusted source="…">` … `</untrusted>` block per entry of
           `untrusted`, each on its own lines so a block's first and last
           line are unambiguous.

        Untrusted text appears in region 3 and nowhere else. The blocks come
        LAST so the operator instructions in regions 1-2 are never bracketed
        by attacker-controlled text, and so a partial read of the message
        always sees the rules before the data they govern.
        """
        parts = [
            self.instructions.strip(),
            "<case_state>\n" + _canonical_json(self.structured_input) + "\n</case_state>",
        ]
        for block in self.untrusted:
            parts.append(
                f'<{_UNTRUSTED_TAG} source="{block.source}">\n'
                # Belt and braces: the field validator already neutralized
                # this, and the call is idempotent. Repeating it here means
                # the rendering path is safe on its own terms, without
                # depending on a validator someone might later relax.
                f"{neutralize_delimiters(block.content)}\n"
                f"</{_UNTRUSTED_TAG}>"
            )
        return "\n\n".join(parts)

    def prompt_hash(self, schema: type[BaseModel]) -> str:
        """sha256 over the TEMPLATE half only: id, version, system, instructions, schema."""
        return _sha256(
            _canonical_json(
                {
                    "template_id": self.template_id,
                    "version": self.version,
                    "system": self.system,
                    "instructions": self.instructions,
                    "schema": schema.model_json_schema(),
                }
            )
        )

    def structured_input_hash(self) -> str:
        """sha256 over the DATA half only: structured input plus untrusted blocks."""
        return _sha256(
            _canonical_json(
                {
                    "structured": self.structured_input,
                    "untrusted": [[b.source, b.content] for b in self.untrusted],
                }
            )
        )


class LLMResponse(BaseModel):
    """One structured completion plus everything the run trace needs about it.

    `input_tokens` / `output_tokens` are carried even though `run_steps` has
    no token column (they go into the step's `decision_type` label), and
    `cost_usd` / `latency_ms` feed the agent's budget ledger.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    parsed: BaseModel
    raw_text: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    cache_status: CacheStatus


class LLMClient(Protocol):
    """The one call System C makes. `CachedClient` wraps any implementation."""

    model_id: str

    def complete_structured(self, prompt: Prompt, schema: type[BaseModel]) -> LLMResponse: ...


class LLMError(RuntimeError):
    """Transport or API failure. The agent loop treats this as a structured failure."""


class LLMSchemaError(LLMError):
    """The model returned something that does not validate against the requested schema.

    A subclass of `LLMError` on purpose: to the agent loop, "the API broke"
    and "the model answered nonsense" have the same consequence (this step
    produced no usable investigator output), and a caller that wants to tell
    them apart can still catch the subclass first.
    """


class LLMTransportError(LLMError):
    """A RETRYABLE failure that survived every retry: 429, 5xx, or no answer.

    Raised only after `max_retries` retries have been spent, so catching it
    means "the endpoint is unwell right now", not "try again immediately".
    `status_code` is the last HTTP status seen, or `None` for a connection
    failure that never produced one.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMRequestError(LLMError):
    """A NON-retryable HTTP failure: 4xx other than 429 (bad key, bad body, 404).

    Never retried — the same request would fail identically — so this is
    raised on the first response. `status_code` is load-bearing:
    `OpenAICompatibleClient` reads it to recognize the one 400 that means
    "this server does not support `response_format: json_schema`" and to
    fall back to `json_object` (see the class docstring).
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _price_cost_usd(
    prices: Mapping[str, ModelPrice],
    model_id: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Dollars for one call from an explicit price table; 0.0 when unpriced."""
    price = prices.get(model_id)
    if price is None:
        return 0.0
    return (
        input_tokens / 1_000_000.0 * price.input_usd_per_mtok
        + output_tokens / 1_000_000.0 * price.output_usd_per_mtok
    )


def compute_cost_usd(cfg: Config, model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Dollar cost of one call from `[llm.prices]`; 0.0 for an unpriced model.

    An unknown `model_id` yields `0.0` and does NOT raise. The price table is
    cost ACCOUNTING, not a safety control (spec.md §2's hard budgets are
    enforced by the controller against whatever number lands here), so an
    unpriced model must under-report its cost rather than abort a run — the
    alternative trades a wrong number for no answer at all. The consequence
    is worth stating plainly: pointing `[llm].model_id` at a model missing
    from `[llm.prices]` makes that model look FREE to the budget ledger.
    That is the normal, intended state for a local Ollama model (it IS
    free), and the reason `config.toml` lists the local ids at 0.0 anyway is
    so the ledger's zero is a recorded fact rather than a missing row.
    """
    return _price_cost_usd(cfg.llm.prices, model_id, input_tokens, output_tokens)


# Statuses worth a second attempt. 429 is rate limiting and 5xx is the
# server admitting fault; everything else in 4xx describes a request that
# will fail identically forever. This is deliberately broader than
# `rli.net.client.RETRYABLE_STATUS_CODES` (which excludes 501/505): an
# OpenAI-compatible LLM gateway in front of a loading model answers 501/503
# interchangeably while it warms up, and a wrongly-retried 501 costs one
# extra request, while a wrongly-abandoned one costs the whole run.
_RETRYABLE_STATUS_FLOOR = 500
_RETRY_ALWAYS = frozenset({429})

# Backoff between 5xx retries: min(_MAX_BACKOFF_S, base * 2**attempt). No
# jitter, unlike `rli.net` — that client fans out across many hosts from many
# probes and needs de-correlated retries, whereas this one is a single
# serialized conversation with one endpoint, where a deterministic (and
# therefore testable) delay is strictly more useful. 429 does NOT use this
# schedule — see `_RATE_LIMIT_BACKOFF_BASE_S` and `_rate_limit_wait_s` below,
# and the class docstring for why the two are split.
_BACKOFF_BASE_S = 0.5
_MAX_BACKOFF_S = 8.0

# Backoff floor for a 429 with no usable server hint: min(rate_limit_max_wait_s,
# base * 2**attempt). Deliberately much larger than `_BACKOFF_BASE_S`: a 5xx
# is a transient server fault where sub-second retries are reasonable, but a
# 429 against a PER-MINUTE quota cannot possibly clear in under a minute, so
# retrying on the 5xx schedule is guaranteed to exhaust `max_retries` before
# the window resets. 15s doubling (15/30/60/...) reaches a full minute within
# three attempts instead of sixteen.
_RATE_LIMIT_BACKOFF_BASE_S = 15.0

# A server can ask for an arbitrarily long `Retry-After`. We honour it, but
# not past this: a multi-hour quota reset must fail the run so the operator
# sees it, not silently freeze the agent inside one step's budget. Applies to
# the header on BOTH 429 and 5xx (same parsing helper); the per-call ceiling
# for 429 specifically is `[llm].rate_limit_max_wait_s` (see
# `_rate_limit_wait_s`), which is independently configurable and defaults
# lower than this.
_MAX_RETRY_AFTER_S = 120.0

# Bytes of an error response body quoted in an exception message. Enough to
# carry the provider's error JSON, short enough not to paste a whole HTML
# error page into a `run_steps.error` column. Raised from 500 to make room
# for Gemini's quota message, which can run long before the actionable
# "Quota exceeded for metric: ..." sentence even begins — though that
# sentence is also extracted and placed FIRST regardless (see
# `_extract_quota_lines`), so truncation no longer risks losing it.
_ERROR_BODY_CHARS = 800

# Extracts a Gemini-style `Quota exceeded for metric: ..., limit: ... per
# ...` fragment out of a longer wrapping sentence (e.g. "You exceeded your
# current quota, please check your plan and billing details. Quota exceeded
# for metric: generate_content_free_tier_requests, limit: 15 per minute.").
# Anchored on the fixed "Quota exceeded for metric:"/"limit:"/"per" shape
# Gemini actually emits, rather than an open-ended run to the next quote or
# period — the metric name and unit are free text, but this structure is
# stable, so matching it precisely (instead of a greedy `.*?`) cannot
# accidentally swallow unrelated trailing prose in the same JSON string.
_QUOTA_LINE_RE = re.compile(
    r"Quota exceeded for metric:\s*[^,]+,\s*limit:\s*\d+(?:\.\d+)?\s*per\s*\w+",
    re.IGNORECASE,
)

# A 429 body's own suggested wait, in the two shapes Gemini emits depending
# on which layer generated the error: a human-readable "retry in 22.5s"
# sentence, or a structured `"retryDelay": "45s"` field inside
# `error.details`. Checked only when there is no `Retry-After` HEADER (see
# `_rate_limit_wait_s`) — the header is the more standard signal when both
# are present.
_RETRY_IN_BODY_RE = re.compile(r"retry in (\d+(?:\.\d+)?)s", re.IGNORECASE)
_RETRY_DELAY_BODY_RE = re.compile(r'retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', re.IGNORECASE)

# `response_format.json_schema.name` must match this (OpenAI's rule, and the
# strictest of the compatible servers); anything else is replaced with `_`.
_SCHEMA_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")

# Timeout for the liveness probe in `endpoint_unavailable_reason`. Short on
# purpose: it runs on a request path (`/investigate`) and its only job is to
# distinguish "something is listening" from "nothing is".
DEFAULT_PROBE_TIMEOUT_S = 1.5


def _schema_name(schema: type[BaseModel]) -> str:
    """A `response_format` name derived from the schema class name."""
    return _SCHEMA_NAME_RE.sub("_", schema.__name__)[:64] or "output"


def _message_content(payload: Mapping[str, Any]) -> str:
    """Pull `choices[0].message.content` out of a chat-completions body.

    Tolerates the two content shapes servers actually emit — a plain string,
    and OpenAI's newer list of `{"type": "text", "text": ...}` parts — and
    reports anything else as a schema failure rather than crashing on an
    attribute that is not there. A `refusal` is named explicitly because it
    is the one "successful" HTTP 200 that carries no answer at all.
    """
    choices = payload.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise LLMSchemaError("chat-completions response carried no `choices`")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    if not isinstance(message, Mapping):
        raise LLMSchemaError("chat-completions response carried no `choices[0].message`")

    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        raise LLMSchemaError(f"model refused to answer: {refusal.strip()[:200]}")

    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts = [
            part["text"]
            for part in content
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        ]
        if parts:
            return "".join(parts)
    raise LLMSchemaError(
        "chat-completions response carried no usable `choices[0].message.content` "
        f"(got {type(content).__name__})"
    )


def _json_object_from_text(text: str) -> Any:
    """Parse `text` as JSON, tolerating the wrappers small models add.

    Strictly, `response_format` obliges the server to return bare JSON. In
    practice a local 4-8B model asked for `json_object` will sometimes fence
    it in ```json ... ``` or prefix a sentence. Recovering from that is worth
    a few lines: the alternative is a failed agent step (and a wasted local
    inference) over punctuation the model added outside the object.

    Only two recoveries are attempted, both of which preserve the "it must be
    ONE JSON document" contract: strip a markdown fence, then take the span
    from the first `{` to the last `}`. Anything still unparseable is an
    `LLMSchemaError` — this never guesses at the content.
    """
    candidates = [text.strip()]

    fenced = re.match(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", text, re.DOTALL | re.IGNORECASE)
    if fenced is not None:
        candidates.append(fenced.group(1).strip())

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise LLMSchemaError(
        f"model output is not JSON (first {_ERROR_BODY_CHARS} chars): {text[:_ERROR_BODY_CHARS]!r}"
    )


def _retry_after_seconds(value: str | None) -> float | None:
    """Seconds to wait per a `Retry-After` header, or None if unusable.

    RFC 9110 allows both forms and providers use both: `Retry-After: 20`
    (Ollama/OpenAI) and `Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`
    (Gemini's gateway). A malformed header is ignored rather than fatal —
    it only costs us the default backoff — and the result is clamped into
    `[0, _MAX_RETRY_AFTER_S]` so a hostile or buggy header cannot park the
    agent for an hour.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return max(0.0, min(_MAX_RETRY_AFTER_S, float(raw)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = (when - datetime.now(UTC)).total_seconds()
    return max(0.0, min(_MAX_RETRY_AFTER_S, delta))


def _body_retry_hint_seconds(text: str) -> float | None:
    """A provider-suggested retry delay parsed out of a 429 error BODY.

    Only consulted when there is no usable `Retry-After` header (see
    `_rate_limit_wait_s`): the header is the standard channel for this, the
    body is Gemini's fallback for the cases where its gateway does not set
    one. Unlike the header, this is NOT clamped here — `_rate_limit_wait_s`
    applies `rate_limit_max_wait_s` uniformly to whatever hint it ends up
    with, header or body.
    """
    for pattern in (_RETRY_IN_BODY_RE, _RETRY_DELAY_BODY_RE):
        match = pattern.search(text)
        if match is not None:
            try:
                return float(match.group(1))
            except ValueError:  # pragma: no cover - regex only captures digits
                continue
    return None


def _extract_quota_lines(text: str) -> str:
    """Pull `Quota exceeded for metric: ..., limit: ...` fragment(s) out of `text`.

    Called on the FULL, pre-truncation error body: the quota sentence is
    usually preceded by a generic "you exceeded your quota" preamble, so on a
    verbose response it can land past `_ERROR_BODY_CHARS` and be silently cut
    off by the truncation applied everywhere else in this module. Scanning
    the whole body once, here, and surfacing what it finds up front (see
    `OpenAICompatibleClient._transport_error_message`) means the one
    actionable fact — which metric, what limit — survives truncation even
    though the raw body around it does not.

    Multiple matches (a multi-quota response) are joined so none are lost.
    Returns `""` when nothing matches, which callers treat as "no quota line
    to prepend", not as an error.
    """
    return "; ".join(_QUOTA_LINE_RE.findall(text))


class _MinIntervalThrottle:
    """Client-side pacing: at most one attempt START per `60 / requests_per_minute`
    seconds (spec.md §2 concern: a free-tier per-minute quota cannot be
    survived by retrying after the fact, per `_RATE_LIMIT_BACKOFF_BASE_S`
    above — this throttle exists to avoid tripping it in the first place).

    Modeled on `rli.net.client.TokenBucket`'s lock discipline: accounting
    happens under a lock, but the lock is NEVER held across the `sleep`
    call — holding it would serialize concurrent callers into a queue whose
    total wait is the SUM of their deficits rather than each simply waiting
    its own turn against a shared timeline. `monotonic` and `sleep` are
    injectable for the same reason `TokenBucket`'s are: deterministic tests
    with no real delay.

    Deliberately simpler than a token bucket: there is no burst allowance,
    because this throttles ONE client's calls to ONE endpoint in a strictly
    serialized agent loop (one call in flight at a time), not many
    concurrent probes fanning out across hosts. "Last start time plus the
    remainder of the interval" is the whole algorithm.

    `requests_per_minute <= 0` makes `wait()` a true no-op: `_interval_s` is
    `0.0`, the `<= 0.0` check short-circuits before the lock or a `monotonic`
    call, and no `sleep` is ever invoked. This matters because the default
    is 0 (unthrottled) and must cost nothing — not a lock acquisition, not a
    clock read — for every caller who never opts in.
    """

    def __init__(
        self,
        requests_per_minute: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval_s = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_start: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block, if needed, so this call starts no sooner than the interval allows."""
        if self._interval_s <= 0.0:
            return
        while True:
            with self._lock:
                now = self._monotonic()
                deficit = (
                    0.0 if self._last_start is None else self._interval_s - (now - self._last_start)
                )
                if deficit <= 0.0:
                    self._last_start = now
                    return
            # Sleep the deficit, then re-check under the lock: another
            # thread may have claimed the slot this sleep was aimed at,
            # exactly the `TokenBucket.acquire` re-check.
            self._sleep(deficit)


class OpenAICompatibleClient:
    """Live structured-output client over `POST {base_url}/chat/completions`.

    One transport, many providers: this speaks the OpenAI chat-completions
    wire format and nothing else, so a local Ollama
    (`http://localhost:11434/v1`, free, no key), Google Gemini's
    compatibility layer
    (`https://generativelanguage.googleapis.com/v1beta/openai/`), vLLM,
    llama.cpp or OpenAI itself are all the same code path and differ only by
    `[llm].base_url` / `[llm].model_id`. There is no per-vendor branch to
    keep in sync, which is the whole point of choosing this API as the
    target (spec.md §7 asks for "one structured-output-capable LLM API",
    not for one vendor).

    --------------------------------------------------------------------
    JUDGMENT CALL: httpx directly, and NOT through `rli.net`
    --------------------------------------------------------------------

    `rli.net.NetClient` is the audited path for PROBE traffic: per-probe
    domain allowlists, per-host rate limiting, an append-only tool cache,
    https-only, no loopback targets. LLM calls have never gone through it,
    and deliberately still do not:

    * `rli.net.check_allowed` rejects non-https and non-publicly-routable
      hosts, which would make the default local endpoint
      (`http://localhost:11434/v1`) unreachable by construction. Loosening
      that check would weaken the SSRF hardening that exists because the
      `json_ld` probe runs with a `"*"` allowlist.
    * The allowlist is a control on where UNTRUSTED, model-or-page-directed
      traffic may go. The LLM endpoint is operator configuration, fixed
      before the run starts, and is the one host the agent must talk to;
      an allowlist entry for it would authorize exactly what config already
      authorizes.
    * `ToolCache` would be the wrong cache anyway — `CachedClient` and the
      `llm_cache` table already provide replay-exact LLM caching keyed the
      way spec.md §2 requires.

    What replaces the allowlist here is `rli.config.Llm._check_base_url`
    (http(s) + a real host, validated at config load) plus the fact that the
    URL is never derived from remote input. The residual risk is stated
    plainly: a `base_url` typo sends the prompt — including untrusted page
    excerpts — to whatever host was typed. That is a config-review concern,
    the same class as `[allowlists]` itself.

    --------------------------------------------------------------------
    Structured output, and the one-way fallback
    --------------------------------------------------------------------

    First choice is `response_format={"type": "json_schema", ...,
    "strict": true}` with the Pydantic model's own JSON Schema, because it
    makes the server responsible for shape. Servers that do not implement it
    answer HTTP 400. On that 400 the client falls back ONCE to
    `{"type": "json_object"}` with the schema described in the system
    message, and LATCHES that decision for the rest of its lifetime
    (`_json_object_mode`): re-probing `json_schema` on every subsequent call
    would burn one wasted request per call for the entire run against a
    server whose answer will not change.

    The latch is per client INSTANCE, not global, so a process talking to two
    endpoints does not let one server's limitation degrade the other. Note
    the deliberate over-trigger: ANY 400 on the first json_schema attempt
    flips the latch, because the error bodies are not standardized enough to
    classify reliably. A 400 that was really about something else simply
    fails again on the retry and raises — one extra request, no wrong answer.

    Validation is unchanged in spirit from the client this replaced:
    `choices[0].message.content` is parsed as JSON and validated into the
    caller's Pydantic model, and anything that does not fit raises
    `LLMSchemaError`.

    --------------------------------------------------------------------
    429 vs 5xx: two different retry schedules
    --------------------------------------------------------------------

    A 5xx is a transient server fault, so it keeps the original
    `_backoff_s` schedule (sub-second, doubling, capped at
    `_MAX_BACKOFF_S`). A 429 is a RATE limit, most often per-MINUTE on a
    free tier, and no sub-16-second retry schedule can ever outlast one —
    it just spends `max_retries` faster. So 429 gets its own, much longer
    schedule (`_rate_limit_wait_s`, floor `_RATE_LIMIT_BACKOFF_BASE_S`
    doubling, capped at `[llm].rate_limit_max_wait_s`), and `requests_per_minute`
    (`_MinIntervalThrottle`) exists to make hitting 429 at all the
    exception rather than the norm.

    JUDGMENT CALL, preserved deliberately: the 429 wait is
    `max(server_hint, exponential_floor)`, not `server_hint` alone. That
    means a SMALL server-supplied hint can be overridden UPWARD by the
    exponential floor (a `Retry-After: 7` on the first attempt becomes 15,
    since `min(rate_limit_max_wait_s, 15) == 15 > 7`), while a LARGE hint —
    a real quota-reset delay — is honoured because it exceeds the floor.
    This is intentional, not a bug to "fix" into always trusting the
    header: a rate-limited endpoint that undersells its own reset time
    (or omits `Retry-After` on some responses but not others) should not
    cause a tighter retry than the schedule would otherwise use.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_id: str,
        *,
        timeout_s: float = 60.0,
        max_tokens: int = 4096,
        prices: Mapping[str, ModelPrice] | None = None,
        max_retries: int = 2,
        requests_per_minute: int = 0,
        rate_limit_max_wait_s: float = 90.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self._api_key = api_key or ""
        self._timeout_s = timeout_s
        self._max_tokens = max_tokens
        self._prices: Mapping[str, ModelPrice] = prices if prices is not None else {}
        self._max_retries = max(0, max_retries)
        self._rate_limit_max_wait_s = rate_limit_max_wait_s
        self._sleep = sleep
        self._throttle = _MinIntervalThrottle(requests_per_minute, monotonic=monotonic, sleep=sleep)
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(timeout=timeout_s)
        # Latched by the first 400 on a json_schema request. See the class
        # docstring for why it never resets.
        self._json_object_mode = False

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        *,
        model_id: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> OpenAICompatibleClient:
        """Build from `[llm]`. `model_id` overrides `[llm].model_id` (CLI `--model`).

        The API key is read from the environment variable NAMED by
        `[llm].api_key_env`, here and nowhere else; an unset variable yields
        `""`, which means "send no Authorization header" — the local-Ollama
        case, not an error.
        """
        return cls(
            cfg.llm.base_url,
            cfg.llm.api_key(),
            model_id if model_id is not None else cfg.llm.model_id,
            timeout_s=cfg.llm.timeout_s,
            max_tokens=cfg.llm.max_tokens,
            prices=cfg.llm.prices,
            max_retries=cfg.llm.max_retries,
            requests_per_minute=cfg.llm.requests_per_minute,
            rate_limit_max_wait_s=cfg.llm.rate_limit_max_wait_s,
            client=client,
            sleep=sleep,
            monotonic=monotonic,
        )

    # -- request construction ------------------------------------------------

    @property
    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        """`Authorization: Bearer …` only when a key was configured.

        Sending `Bearer ` with an empty key is worse than sending nothing:
        several OpenAI-compatible servers (Ollama included) accept an absent
        header and reject a malformed one.
        """
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _scrub(self, text: str) -> str:
        """Remove the API key from anything that may be shown to a human.

        Every exception message, and every string this module would ever log,
        goes through here. Providers do echo credentials back in error
        bodies, and `run_steps.error` is persisted to the database and
        printed by `rli agent trace`, so a leaked key would not merely be
        transient.
        """
        if not self._api_key:
            return text
        return text.replace(self._api_key, "***")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"OpenAICompatibleClient(base_url={self.base_url!r}, "
            f"model_id={self.model_id!r}, api_key={'***' if self._api_key else ''!r})"
        )

    def _build_body(self, prompt: Prompt, schema: type[BaseModel]) -> dict[str, Any]:
        """The request body, in whichever structured-output mode is latched.

        `temperature=0` is a request for determinism, not a guarantee of it —
        spec.md §2 says as much ("do not assume temperature 0 makes APIs
        deterministic"), which is why `CachedClient` exists.

        Note where the schema goes in fallback mode: into the SYSTEM message,
        never the user message. Untrusted blocks live in the user message
        (`Prompt.render_user`), so the two never share a region and the
        schema text cannot be confused for case data.
        """
        json_schema = schema.model_json_schema()
        system = prompt.system

        if self._json_object_mode:
            response_format: dict[str, Any] = {"type": "json_object"}
            system = (
                f"{prompt.system}\n\n"
                "Respond with a single JSON object and nothing else: no prose, no "
                "explanation, no markdown code fence. The object must validate "
                "against this JSON Schema:\n"
                f"{json.dumps(json_schema, sort_keys=True)}"
            )
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": _schema_name(schema),
                    "schema": json_schema,
                    "strict": True,
                },
            }

        return {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt.render_user()},
            ],
            "max_tokens": self._max_tokens,
            "temperature": 0,
            "response_format": response_format,
        }

    # -- transport -----------------------------------------------------------

    def _backoff_s(self, attempt: int) -> float:
        """5xx backoff schedule. See the class docstring for why 429 does not share it."""
        return min(_MAX_BACKOFF_S, _BACKOFF_BASE_S * (2.0**attempt))

    def _rate_limit_wait_s(self, attempt: int, response: httpx.Response) -> float:
        """429 backoff schedule: `max(server_hint, exponential_floor)`.

        `server_hint` prefers the `Retry-After` HEADER (via
        `_retry_after_seconds`, clamped to `_MAX_RETRY_AFTER_S`); when that
        header is absent or unparseable, it falls back to a hint parsed out
        of the response BODY (`_body_retry_hint_seconds`) — Gemini's gateway
        does not always set the header. `None` when neither is present or
        parseable.

        The exponential floor doubles from `_RATE_LIMIT_BACKOFF_BASE_S`,
        capped by the configured `[llm].rate_limit_max_wait_s`. See the class
        docstring's JUDGMENT CALL note for why the floor can override a small
        hint but never a large one.
        """
        header_hint = _retry_after_seconds(response.headers.get("Retry-After"))
        server_hint = (
            header_hint if header_hint is not None else _body_retry_hint_seconds(response.text)
        )
        floor = min(self._rate_limit_max_wait_s, _RATE_LIMIT_BACKOFF_BASE_S * (2.0**attempt))
        if server_hint is None:
            return floor
        return max(server_hint, floor)

    def _transport_error_message(self, status: int, attempt: int, raw_text: str) -> str:
        """Message for an `LLMTransportError` raised once retries are exhausted.

        `raw_text` is the FULL response body (not yet truncated): scanned
        for a quota-metric fragment (`_extract_quota_lines`) before the
        truncation happens, so a long preamble ahead of the actionable
        sentence cannot push it past `_ERROR_BODY_CHARS` and lose it. When
        found, the quota line is placed FIRST, ahead of the (still-included,
        now-truncated) raw body; when not found, the message is exactly the
        truncated body, unchanged from before this method existed.
        """
        prefix = (
            f"LLM endpoint returned HTTP {status} after {attempt + 1} "
            f"attempt(s) for model {self.model_id!r}: "
        )
        detail = self._scrub(raw_text[:_ERROR_BODY_CHARS])
        quota = self._scrub(_extract_quota_lines(raw_text))
        if quota:
            return f"{prefix}{quota} | body: {detail}"
        return f"{prefix}{detail}"

    def _send(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """POST once, retrying only 429/5xx and connection failures.

        Total attempts = `max_retries + 1` (spec.md §2: "no uncontrolled
        retry loops" — the bound is a configured number, not a duration).
        A non-retryable status raises immediately: retrying a 401 or a 404
        cannot change the answer and only delays the operator's error.

        `self._throttle.wait()` runs before EVERY attempt, including
        retries of this same logical call: the throttle paces the START of
        each attempt against `[llm].requests_per_minute`, independent of
        whatever wait a previous attempt's 429/5xx already served.
        """
        attempt = 0
        while True:
            self._throttle.wait()
            try:
                response = self._client.post(self._url, json=body, headers=self._headers())
            except httpx.HTTPError as exc:
                if attempt >= self._max_retries:
                    raise LLMTransportError(
                        f"LLM request to {self._scrub(self._url)} failed after "
                        f"{attempt + 1} attempt(s): {self._scrub(str(exc))}"
                    ) from exc
                self._sleep(self._backoff_s(attempt))
                attempt += 1
                continue
            except Exception as exc:
                # NOT retried, and NOT allowed to escape unwrapped. httpx
                # raises a handful of things that are not `HTTPError` at all —
                # `httpx.InvalidURL` for a host that cannot be IDNA-encoded is
                # the reachable one, since `[llm].base_url` only has to parse
                # to pass config validation. Retrying cannot fix any of them,
                # but letting one out as a raw exception would break the
                # guarantee the agent loop is built on: `_call_investigator`
                # catches `LLMError`, and spec.md §4 requires the run to reach
                # a valid `Decision` regardless of what the model layer did.
                # `BaseException` (KeyboardInterrupt, SystemExit) still
                # propagates.
                raise LLMError(
                    f"LLM request to {self._scrub(self._url)} could not be sent: "
                    f"{type(exc).__name__}: {self._scrub(str(exc))}"
                ) from exc

            status = response.status_code
            if status < 300:
                return self._decode(response)

            # Redirects are NOT followed (httpx's default, kept deliberately):
            # this request carries an Authorization header, and following a
            # 3xx would hand that credential to whatever host the redirect
            # names. A 3xx therefore falls through to the non-retryable branch
            # below and surfaces as an error naming the status, which is the
            # right diagnosis for a mistyped `base_url`.
            # 429 and 5xx are both retryable but on DIFFERENT schedules — see
            # the class docstring for why a per-minute rate limit cannot
            # share a sub-second server-error backoff.
            if status in _RETRY_ALWAYS:
                if attempt >= self._max_retries:
                    raise LLMTransportError(
                        self._transport_error_message(status, attempt, response.text),
                        status_code=status,
                    )
                self._sleep(self._rate_limit_wait_s(attempt, response))
                attempt += 1
                continue

            if status >= _RETRYABLE_STATUS_FLOOR:
                if attempt >= self._max_retries:
                    raise LLMTransportError(
                        self._transport_error_message(status, attempt, response.text),
                        status_code=status,
                    )
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                self._sleep(retry_after if retry_after is not None else self._backoff_s(attempt))
                attempt += 1
                continue

            detail = self._scrub(response.text[:_ERROR_BODY_CHARS])
            raise LLMRequestError(
                f"LLM endpoint returned HTTP {status} for model {self.model_id!r}: {detail}",
                status_code=status,
            )

    def _decode(self, response: httpx.Response) -> Mapping[str, Any]:
        """A 2xx whose body is not a JSON object is a schema failure, not a 200."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMSchemaError(
                f"LLM endpoint returned a non-JSON body for model {self.model_id!r}: "
                f"{self._scrub(response.text[:_ERROR_BODY_CHARS])!r}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise LLMSchemaError(
                f"LLM endpoint returned {type(payload).__name__}, expected a JSON object"
            )
        return payload

    # -- the one call --------------------------------------------------------

    def complete_structured(self, prompt: Prompt, schema: type[BaseModel]) -> LLMResponse:
        """One structured call. Returns a validated `schema` instance or raises."""
        started = time.perf_counter()
        try:
            payload = self._send(self._build_body(prompt, schema))
        except LLMRequestError as exc:
            # The one recoverable 4xx: this server does not implement
            # `response_format: json_schema`. Latch json_object mode and
            # retry exactly once; a second failure propagates.
            if exc.status_code != 400 or self._json_object_mode:
                raise
            self._json_object_mode = True
            payload = self._send(self._build_body(prompt, schema))
        latency_ms = (time.perf_counter() - started) * 1000.0

        raw_text = _message_content(payload)
        parsed = _validate_parsed(_json_object_from_text(raw_text), schema)

        usage = payload.get("usage")
        usage_map: Mapping[str, Any] = usage if isinstance(usage, Mapping) else {}
        input_tokens = _int_or_zero(usage_map.get("prompt_tokens", 0))
        output_tokens = _int_or_zero(usage_map.get("completion_tokens", 0))

        reported = payload.get("model")
        model_id = reported if isinstance(reported, str) and reported else self.model_id

        return LLMResponse(
            parsed=parsed,
            raw_text=raw_text,
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self._cost_usd(model_id, input_tokens, output_tokens),
            latency_ms=latency_ms,
            cache_status="n/a",
        )

    def _cost_usd(self, reported_model_id: str, input_tokens: int, output_tokens: int) -> float:
        """Price the call, preferring the REPORTED model id over the requested one.

        The server is the authority on what actually served the request (a
        gateway may resolve an alias), so its id is tried first. When that id
        is absent from the table the CONFIGURED id is tried before giving up:
        providers decorate their ids (`models/gemini-2.5-flash`,
        `gemini-2.5-flash-001`), and silently pricing a paid model at zero
        because of a suffix is the one failure this fallback prevents.
        """
        if reported_model_id in self._prices:
            return _price_cost_usd(self._prices, reported_model_id, input_tokens, output_tokens)
        return _price_cost_usd(self._prices, self.model_id, input_tokens, output_tokens)

    def close(self) -> None:
        """Close the underlying httpx client when this object created it."""
        if self._owns_client:
            self._client.close()


def close_llm_client(client: object) -> None:
    """Release the HTTP resources behind `client`, wrappers included.

    `CachedClient` is a wrapper, not a client, so the thing holding a socket
    is usually `client.inner`; this walks that chain and calls `close()` on
    whatever exposes one (`ScriptedClient` and `CachedClient` expose none, and
    are silently left alone). Callers that BUILD a live client — the CLI, the
    API request handler — use this in a `finally` so the connection pool is
    released when the run ends rather than whenever the object is collected.

    Never raises: teardown must not turn a completed run into a failed one.
    """
    seen = 0
    node: Any = client
    while node is not None and seen < 8:  # bounded: a cycle must not hang
        close = getattr(node, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - teardown is best-effort
                pass
        node = getattr(node, "inner", None)
        seen += 1


def endpoint_unavailable_reason(
    cfg: Config,
    *,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    client: httpx.Client | None = None,
) -> str | None:
    """Why the configured LLM endpoint cannot be used, or `None` if it can.

    Two checks, cheapest first:

    1. **Config only, no network.** A missing API key is fatal for a REMOTE
       endpoint and irrelevant for a local one (`Llm.credentials_configured`),
       so `http://localhost:11434/v1` with no key is fine and
       `https://generativelanguage.googleapis.com/...` with no key is not.
    2. **A bounded `GET {base_url}/models`.** No model is run, no tokens are
       spent and no money changes hands — this only distinguishes "something
       is listening and will talk to us" from "nothing is". Any HTTP status
       other than 401/403 counts as reachable, including 404: several
       compatible servers do not implement `/models`, and a 404 still proves
       a server answered.

    Never raises. Any exception from the probe — connection refused, DNS
    failure, timeout, a test's mock transport refusing an unexpected call —
    is reported as unreachable, because from the caller's point of view those
    are one thing: System C cannot run right now.
    """
    if not cfg.llm.credentials_configured():
        return (
            f"no LLM API key: ${cfg.llm.api_key_env} is unset and "
            f"[llm].base_url ({cfg.llm.base_url}) is not local"
        )

    headers = {}
    api_key = cfg.llm.api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    owned = client is None
    probe = client if client is not None else httpx.Client(timeout=timeout_s)
    try:
        response = probe.get(cfg.llm.models_url, headers=headers, timeout=timeout_s)
    except Exception as exc:  # see the docstring: unreachable is unreachable
        detail = str(exc).replace(api_key, "***") if api_key else str(exc)
        return f"LLM endpoint {cfg.llm.base_url} is not reachable: {detail[:200]}"
    else:
        if response.status_code in (401, 403):
            return (
                f"LLM endpoint {cfg.llm.base_url} rejected our credentials "
                f"(HTTP {response.status_code}; check ${cfg.llm.api_key_env})"
            )
        return None
    finally:
        if owned:
            probe.close()


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _validate_parsed(parsed: Any, schema: type[BaseModel]) -> BaseModel:
    """Coerce whatever the provider handed back into a `schema` instance."""
    if parsed is None:
        raise LLMSchemaError(f"model returned no parsed output for schema {schema.__name__}")
    if isinstance(parsed, schema):
        return parsed
    payload: Any = parsed.model_dump() if isinstance(parsed, BaseModel) else parsed
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise LLMSchemaError(
            f"model output does not validate against {schema.__name__}: {exc}"
        ) from exc


class ScriptedClient:
    """A deterministic `LLMClient` for tests: replays canned parsed outputs.

    `responses` is either a sequence consumed in order — a `BaseModel` is
    returned, an `Exception` instance is raised, which is how a test scripts
    an `LLMError` — or a callable `(prompt, schema) -> BaseModel` for tests
    whose answer depends on what was asked.

    Every call is appended to `calls`, so a test can assert on the prompts
    the agent built (that the excerpt never reached `structured_input`, that
    the candidate catalogue was present, and so on) rather than only on the
    decisions that followed.

    A scripted object that is not already an instance of the requested
    schema is validated into one, and a failure raises `LLMSchemaError`.
    That turns "the test scripted the wrong model class" into a clear
    failure at the call site instead of an attribute error three layers up.
    """

    def __init__(
        self,
        responses: Sequence[BaseModel | Exception] | Callable[[Prompt, type[BaseModel]], BaseModel],
        *,
        model_id: str = "scripted-model",
        input_tokens: int = 100,
        output_tokens: int = 50,
        latency_ms: float = 1.0,
        cfg: Config | None = None,
    ) -> None:
        self._responses = responses
        self._index = 0
        self.model_id = model_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.cfg = cfg
        self.calls: list[tuple[Prompt, type[BaseModel]]] = []

    def complete_structured(self, prompt: Prompt, schema: type[BaseModel]) -> LLMResponse:
        self.calls.append((prompt, schema))
        obj = self._next(prompt, schema)
        parsed = _validate_parsed(obj, schema)

        cost = 0.0
        if self.cfg is not None:
            cost = compute_cost_usd(self.cfg, self.model_id, self.input_tokens, self.output_tokens)

        return LLMResponse(
            parsed=parsed,
            raw_text=parsed.model_dump_json(),
            model_id=self.model_id,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=cost,
            latency_ms=self.latency_ms,
            cache_status="n/a",
        )

    def _next(self, prompt: Prompt, schema: type[BaseModel]) -> Any:
        if callable(self._responses):
            return self._responses(prompt, schema)
        if self._index >= len(self._responses):
            raise LLMError(
                f"ScriptedClient exhausted after {self._index} call(s); "
                f"no scripted response for template {prompt.template_id!r}"
            )
        item = self._responses[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        return item


class CachedClient:
    """`llm_cache`-backed wrapper giving exact benchmark replay (spec.md §2/§6).

    Key: `(inner.model_id, prompt.prompt_hash(schema),
    prompt.structured_input_hash())` — the composite primary key of
    `llm_cache`, read at call time from `inner` so a swapped inner client
    cannot serve another model's answers.

    * **HIT** — the stored JSON is validated back into `schema`. The
      response reports `cost_usd=0.0` and zero tokens, because nothing was
      spent: the agent's ledger must charge a replayed run nothing, or a
      replay would appear to cost as much as the original.
      `latency_ms` is the measured LOOKUP time (real, small), not the
      original call's — the trace should say how long THIS run took.
    * **MISS** — `inner` is called, `response.parsed.model_dump_json()` is
      stored with `INSERT OR REPLACE`, and the inner response is returned
      with `cache_status="miss"`. spec.md §6 explicitly allows a live LLM
      call on a miss during replay and requires it to be recorded; that is
      this branch.
    * **A row that no longer validates against `schema` is a MISS** and is
      overwritten. `prompt_hash` folds in the schema, so this should not
      happen — but "should not happen" is exactly the state that wedges a
      replay: without this rule, one corrupt or hand-edited row would make
      every future run of that case raise instead of re-fetching. Degrading
      to a re-fetch keeps the failure to one paid call.

    Note what is stored: the PARSED model re-serialized, not the provider's
    raw text. That is what makes a hit reconstructable and schema-checked;
    the cost is that a hit's `raw_text` is the canonical JSON rather than the
    exact bytes the model emitted.

    Connection ownership: this class calls `commit()`, exactly like
    `rli.net.ToolCache`, so it must not be handed a connection with an
    in-flight caller transaction.
    """

    def __init__(
        self,
        inner: LLMClient,
        conn: sqlite3.Connection,
        *,
        now: Callable[[], datetime] = now_utc,
    ) -> None:
        self.inner = inner
        self._conn = conn
        self._now = now

    @property
    def model_id(self) -> str:
        return self.inner.model_id

    def complete_structured(self, prompt: Prompt, schema: type[BaseModel]) -> LLMResponse:
        started = time.perf_counter()
        model_id = self.inner.model_id
        prompt_hash = prompt.prompt_hash(schema)
        structured_input_hash = prompt.structured_input_hash()

        row = self._conn.execute(
            """
            SELECT response
            FROM llm_cache
            WHERE model_id = ? AND prompt_hash = ? AND structured_input_hash = ?
            """,
            (model_id, prompt_hash, structured_input_hash),
        ).fetchone()

        if row is not None:
            # Positional access so this works whether or not the caller's
            # connection sets `row_factory = sqlite3.Row` (the ToolCache
            # convention).
            stored = row[0]
            try:
                parsed = schema.model_validate_json(stored)
            except (ValidationError, ValueError, TypeError):
                # Corrupt/stale row: fall through to a live call, which will
                # overwrite it. See the class docstring.
                parsed = None
            if parsed is not None:
                return LLMResponse(
                    parsed=parsed,
                    raw_text=stored,
                    model_id=model_id,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0.0,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    cache_status="hit",
                )

        response = self.inner.complete_structured(prompt, schema)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO llm_cache
                (model_id, prompt_hash, structured_input_hash, response, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                model_id,
                prompt_hash,
                structured_input_hash,
                response.parsed.model_dump_json(),
                to_utc_z(self._now()),
            ),
        )
        self._conn.commit()
        return response.model_copy(update={"cache_status": "miss"})
