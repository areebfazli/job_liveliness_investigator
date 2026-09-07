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
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from rli.config import Config
from rli.models.time import now_utc, to_utc_z

__all__ = [
    "AnthropicClient",
    "CachedClient",
    "CacheStatus",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "LLMSchemaError",
    "Prompt",
    "ScriptedClient",
    "UntrustedBlock",
    "compute_cost_usd",
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


def compute_cost_usd(cfg: Config, model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Dollar cost of one call from `[llm.prices]`; 0.0 for an unpriced model.

    An unknown `model_id` yields `0.0` and does NOT raise. The price table is
    cost ACCOUNTING, not a safety control (spec.md §2's hard budgets are
    enforced by the controller against whatever number lands here), so an
    unpriced model must under-report its cost rather than abort a run — the
    alternative trades a wrong number for no answer at all. The consequence
    is worth stating plainly: pointing `[llm].model_id` at a model missing
    from `[llm.prices]` makes that model look FREE to the budget ledger.
    """
    price = cfg.llm.price_for(model_id)
    if price is None:
        return 0.0
    return (
        input_tokens / 1_000_000.0 * price.input_usd_per_mtok
        + output_tokens / 1_000_000.0 * price.output_usd_per_mtok
    )


def _text_from_content(content: Any) -> str:
    """Join the text blocks of an Anthropic `response.content`, tolerantly.

    The parsed object is the contract; `raw_text` is for the trace, so this
    never raises: an unexpected content shape degrades to an empty string
    and the caller substitutes the parsed JSON.
    """
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return ""
    chunks: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


class AnthropicClient:
    """Live structured-output client over `client.messages.parse` (spec.md §7).

    Two constructor knobs matter:

    * `model_id` overrides `[llm].model_id` for this client only (the CLI's
      `--model` flag), and is what the cache is keyed on.
    * `client` injects an already-built API client. This is what makes the
      class unit-testable: a fake exposing `messages.parse(...)` -> object
      with `.parsed_output`, `.usage.input_tokens`, `.usage.output_tokens`
      and `.content` exercises every line below without a network or an API
      key.

    **The `anthropic` import is lazy, inside `__init__`, and only on the
    path that actually builds a real client.** `rli.llm` must import (and
    `tests/test_smoke.py` must pass) in an environment where the package is
    not installed, and no test may require an API key. The corollary is that
    the SDK's exception classes are not available at call time either, which
    is why `complete_structured` wraps `Exception` from the API call rather
    than enumerating `anthropic.APIError` / `APIConnectionError` / …: an SDK
    error class we failed to enumerate must not escape as a raw exception
    into the agent loop, which is written to handle `LLMError`.
    `BaseException` (KeyboardInterrupt, SystemExit) still propagates.
    """

    def __init__(
        self,
        cfg: Config,
        *,
        model_id: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._cfg = cfg
        self.model_id = model_id if model_id is not None else cfg.llm.model_id

        if client is not None:
            self._client = client
            return

        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise LLMError(
                "the 'anthropic' package is required for live LLM calls; "
                "install it (`uv add anthropic`) or inject a client with "
                "AnthropicClient(cfg, client=...)"
            ) from exc

        # Transport retries and the per-request timeout are applied at client
        # construction, not per call, so `complete_structured` can keep the
        # exact `messages.parse` signature an injected fake has to satisfy.
        self._client = anthropic.Anthropic(
            timeout=cfg.llm.timeout_s,
            max_retries=cfg.llm.max_retries,
        )

    def complete_structured(self, prompt: Prompt, schema: type[BaseModel]) -> LLMResponse:
        """One structured call. Returns a validated `schema` instance or raises."""
        started = time.perf_counter()
        try:
            raw = self._client.messages.parse(
                model=self.model_id,
                max_tokens=self._cfg.llm.max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": prompt.system,
                        # The system prompt is static per template, so it is
                        # the one part of the request worth caching across
                        # the many cases a benchmark run investigates.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt.render_user()}],
                output_format=schema,
            )
        except Exception as exc:  # see the class docstring for why this is broad
            raise LLMError(
                f"anthropic call failed for model {self.model_id!r} "
                f"(template {prompt.template_id!r}): {exc}"
            ) from exc
        latency_ms = (time.perf_counter() - started) * 1000.0

        parsed = _validate_parsed(getattr(raw, "parsed_output", None), schema)

        usage = getattr(raw, "usage", None)
        input_tokens = _int_or_zero(getattr(usage, "input_tokens", 0))
        output_tokens = _int_or_zero(getattr(usage, "output_tokens", 0))

        raw_text = _text_from_content(getattr(raw, "content", None))
        if not raw_text:
            raw_text = parsed.model_dump_json()

        return LLMResponse(
            parsed=parsed,
            raw_text=raw_text,
            model_id=self.model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=compute_cost_usd(self._cfg, self.model_id, input_tokens, output_tokens),
            latency_ms=latency_ms,
            cache_status="n/a",
        )


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
        responses: Sequence[BaseModel | Exception]
        | Callable[[Prompt, type[BaseModel]], BaseModel],
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
            cost = compute_cost_usd(
                self.cfg, self.model_id, self.input_tokens, self.output_tokens
            )

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
