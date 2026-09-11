# Security and robustness review — `rli`

Date: 2026-09-11. Read-only audit of `rli/`, `tests/`, `scripts/`, `docs/`, `config.toml`
against spec.md §2 ("Safety and reproducibility invariants") and §9. No project file was
modified other than creating this report; no git state changed; `data/rli.db` was opened
read-only; no live LLM calls were made.

Baseline at audit time: `uv run pytest -q` → 1233 passed, 1 skipped; `uv run ruff check .` →
clean; `pip-audit` over the exported `uv.lock` (162 pinned packages) → no known
vulnerabilities (fastapi 0.141.1, starlette 1.6.0, httpx 0.28.1, pydantic 2.13.5,
uvicorn 0.52.4, beautifulsoup4 4.15.0, lifelines 0.30.3, numpy 2.5.3, pandas 2.3.3).

Every finding below was verified by reading the cited code path. Where a worker's dynamic
reproduction is relied on, that is stated.

## Summary

| Severity | Count | Findings |
|---|---|---|
| Critical | 0 | — |
| High | 5 | H1 no API auth / open LLM-spend and fetch surface; H2 debug trace gate is a public literal; H3 PIT read-snapshot held across replay block ("database is locked" root cause); H4 `team_signal` fallback bypasses the point-in-time gate; H5 Typer tracebacks can print the LLM key into disk logs |
| Medium | 10 | M1 json_ld `"*"` + unresolved DNS (blind SSRF to https-only internal hosts); M2 no response byte cap / decompression bomb; M3 LLM `base_url` accepts `http://` to non-local hosts; M4 shipped config sends data to Gemini while comment + README say "nothing leaves the machine"; M5 no data-flow / training-terms disclosure; M6 `<case_state>` labelled "trustworthy" carries third-party text; M7 zero-width chars survive delimiter neutralisation; M8 UI evidence `href` allows `javascript:`; M9 LLM cache key omits `max_tokens` and JSON-mode fallback; M10 long-held write locks in history batch jobs |
| Low | 11 | L1 `GET /watch` discloses the watch list; L2 no `max_length` / body-size limits on API inputs; L3 no retention/purge for outcomes, notes, watches; L4 `watches.json` and DB written with default perms; L5 `absolute_url` from ATS adopted unvalidated (run aborts instead of degrading); L6 `replay_c_loop.sh` lacks `set -e`, `cd` guard, `flock`, failure cap; L7 no log rotation; L8 tests have no default-deny network transport; L9 `journal_mode=WAL` result unchecked; L10 replay network ban is per-object, not global; L11 shadow-T not verified on reuse in `case_state_at` |
| Info | 5 | I1 `tool_cache` retains third-party page bodies (incl. embedded third-party keys) forever; I2 two dev scripts bypass `rli.net`; I3 no `User-Agent`; I4 no `logging` usage (stderr only); I5 uncapped `title`/`source_url`/`value` in prompts |

## Findings

### High

**H1 — `POST /investigate`, `/outcomes`, `/watch`, `GET /watch` have no authentication or rate limit.**
`rli/api/app.py:134-202, 235-285, 290-322`. Each `/investigate` (default `system: "C"`) spends
LLM quota (Gemini free tier: 15 RPM / 500 req/day) and makes outbound HTTPS fetches; `/watch`
adds entries forever, each of which triggers a System B run; `/outcomes` lets anyone write
outcome rows for any `posting_id`. Handlers are sync `def` (correct for sqlite3), so each slow
request also occupies an unbounded threadpool worker. Mitigated today only by the default
`127.0.0.1` bind (`rli/api/__main__.py:16`).
Fix: add a `Depends(require_token)` that compares `Authorization: Bearer` against
`RLI_API_TOKEN` via `secrets.compare_digest` (refuse to start with an empty token when
`RLI_API_HOST != 127.0.0.1`); wrap `/investigate` and `/watch` in a `threading.BoundedSemaphore`
(e.g. 2) and return 429 when full; pass `limit_concurrency=` to `uvicorn.run`.

**H2 — Debug trace route is gated by the public literal `"1"` and enabled by default.**
`rli/api/app.py:210` (`x_rli_debug != "1"`), `rli/api/settings.py:58` (`debug_routes`
default `True`). `GET /runs/{id}` with `X-RLI-Debug: 1` returns raw `runs` + `run_steps` rows
for any run id: probe args, prompt hashes, error text, model id, costs.
Fix: `debug_routes` default `False`; require `x_rli_debug` to equal an env-configured
`RLI_API_DEBUG_TOKEN` via `secrets.compare_digest`; keep the 404 on failure.

**H3 — `point_in_time` leaves an implicit read transaction open across the whole replay block.**
`rli/replay/pit.py:369-374`: `_install_postings` runs `UPDATE postings ...` (lines 274, 314)
and `_install_repost_links` inserts, which under Python's sqlite3 legacy transaction control
open an implicit `BEGIN` and take a WAL read snapshot of `main`; `yield conn` then hands the
connection back with that transaction still open, and `_drop_shadows` (DDL only) never commits
it. The first write inside the block (e.g. `INSERT INTO runs`) must upgrade read→write; if any
other connection committed to `main` meanwhile, SQLite returns `SQLITE_BUSY_SNAPSHOT`, which
`busy_timeout` cannot retry. The worker reproduced this in a scratch DB: one concurrent commit
wedges the connection for every later case, and `rli/replay/run.py:601-609` records each as a
generic per-case error rather than failing loudly. This matches the PROGRESS note that two
replays cannot run concurrently. `connect()` (`rli/db/__init__.py:135-148`) already sets WAL,
`busy_timeout=5000` and `foreign_keys=ON`, so that is not the cause.
Fix: `conn.commit()` immediately before `yield conn`, and `conn.commit()` in the `finally`
after `_drop_shadows`; add a test that opens a second connection, commits a row to `main`
inside a `point_in_time` block, and asserts a write on the first connection still succeeds.

**H4 — `team_signal` boolean fallback reads the probe `data` blob and bypasses the `available_at <= T` gate.**
`rli/eval/case.py:1088-1093` sets `team_value` from `team.data["corroborating_hiring_signal"]`;
`rli/policy/inputs.py:548` uses that keyword whenever the gated claim path returns UNKNOWN.
In replay the `team_signal` result is the build-time record (its `TeamSignalArgs`,
`rli/probes/team_signal.py:185-187`, has no `as_of`, so one record serves every T), its claims
are stamped `available_at=now` at build time (`team_signal.py:386-425`) and are correctly
dropped at an archive-era T — at which point the ungated build-time boolean fills the input.
`rli/replay/build.py:120-127` explicitly states policy inputs must never come from a `data` blob
and that doing so "would reintroduce leakage that no test would catch"; `rli/replay/leakage.py`
only audits `evidence` / `run_steps`, so `replay check` reports 0 violations while the `skip`
branch (P4, repeated_unchanged + long_lived + corroborating False) may be decided from
post-T board activity. Reproducibility/§9 "point-in-time safe" finding, not a security one.
Fix: delete the fallback at `case.py:1088-1093` (leave `team_value = UNKNOWN`); if the
`data`-only licensed-adapter path is still wanted, add `as_of` to `TeamSignalArgs` and re-run
the probe per T like `company_events`; add a leakage-check assertion that no policy input
differs between "claims only" and "claims + data" derivations.

**H5 — Typer's default pretty tracebacks render frame locals, which can include the live `Authorization` header.**
`rli/cli.py:25`, `rli/agent/cli.py:40`, `rli/archive/cli.py:28`, `rli/history/cli.py:55` — no
`pretty_exceptions_show_locals=False` anywhere (`grep` confirmed). `rli/llm/client.py` scrubs
the key from every exception *message* (`_scrub`, 883-894; call sites 992-1039, 1223), but an
unhandled exception (or the chained `__cause__` from httpx) reaching Typer is rendered by Rich
with locals, and the header dict is a local in the httpx frames. Those tracebacks land in
`data/logs/replay-c.log` (`scripts/replay_c_loop.sh:28`, `>> "$LOG" 2>&1`) and cron logs.
Currently mitigated by the replay runner's per-case `except Exception`; `rli agent run` and
future commands are not. Checked read-only: 0 of 3499 `run_steps.error` rows and no
`data/logs` file currently contain a key pattern.
Fix: pass `pretty_exceptions_show_locals=False` (or `pretty_exceptions_enable=False`) to every
`typer.Typer(...)` constructor; optionally also `rich_markup_mode=None`.

### Medium

**M1 — json_ld `"*"` allowlist + no resolved-IP check: blind SSRF to any https host, including attacker-DNS-to-private-IP.**
`rli/net/client.py:182-278` is a URL-level guard (scheme, userinfo, IP literals in every
encoding, non-public suffixes) and deliberately does not resolve the host (docstring 206-211).
Any user URL with an unknown ATS reaches `_resolve_jsonld` (`rli/probes/resolve_posting.py:277`)
via `ctx.net_client("json_ld")` with allowlist `"*"` (`config.toml:259`); an attacker can point
their own A record at `127.0.0.1` / `10.x` / `169.254.169.254` with no race needed. Two strong
mitigations the worker did not weigh: https-only is enforced at every hop, so plaintext
metadata/admin services on :80 cannot be reached, and the read-back is four parsed JSON-LD
`JobPosting` fields (`rli/resolvers/jsonld.py:31-39,114-125`), not the body. Net: blind GET to
internal TLS services only; reachable unauthenticated (H1).
Fix: in `NetClient._fetch_one_hop` resolve with `socket.getaddrinfo` right before the request and
return `NetResult(ok=False, retryable=False, error="non-public address")` if any answer is
private/loopback/link-local/reserved (`ipaddress.ip_address(x).is_global`); keep the documented
residual for true mid-flight rebinding.

**M2 — No response size cap; decompression bomb / memory exhaustion.**
`rli/net/client.py:600` builds `httpx.Client(timeout=..., follow_redirects=False)` with no
limit; `:745` `self._client.get(...)` reads the whole body and `:775, :839` use `response.text`
after httpx's automatic gzip/br decompression. Reachable from any json_ld target (M1/H1) and
from archive.org responses. Same in `rli/llm/client.py` for LLM responses (lower risk).
Fix: use `self._client.stream("GET", ...)`, accumulate `iter_bytes()` up to `cfg.net.max_body_bytes`
(e.g. 5 MiB), abort with a structured `NetResult(ok=False, error="response too large")`;
also refuse `Content-Length` above the cap before reading.

**M3 — LLM `base_url` may be plain `http://` for a remote host, sending the Bearer key in cleartext.**
`rli/config.py:554-569` `_check_base_url` only requires scheme in `{http, https}` and a hostname;
`rli/llm/client.py:719-747` documents bypassing `rli.net` for localhost Ollama. A typo such as
`http://api.example.com/v1` leaks the key and every prompt in cleartext.
Fix: in `_check_base_url`, raise unless scheme is `https` or hostname is in
`{"localhost", "127.0.0.1", "::1"}`.

**M4 — Shipped `config.toml` points at Google Gemini while its own comment and the README say "local Ollama, nothing leaves the machine".**
`config.toml:360-362, 366, 373` (`base_url = ".../generativelanguage.googleapis.com/..."`,
`api_key_env = "GEMINI_API_KEY"`, `model_id = "gemini-3.5-flash-lite"` directly under
"# Default: a local Ollama. Free, no API key, nothing leaves the machine."); `README.md:117-118`
("the shipped default — free, no API key, no data leaves the machine"). Committed in `3d83fbc`,
so this is a deliberate switch with stale documentation; anyone with `GEMINI_API_KEY` set sends
data to Google believing otherwise. Also makes `tests/conftest.py:26-27`'s `cfg` fixture load a
real remote endpoint (current LLM tests override `base_url`, verified).
Fix: either restore the localhost default and keep Gemini as a documented opt-in, or rewrite the
comment and README paragraph to say Gemini is the default and add the M5 disclosure.

**M5 — No disclosure of what leaves the machine or of provider data-use terms.**
`README.md:137-150` explains obtaining a Gemini key but never lists transmitted fields; `grep`
for train/terms/data-use across README and docs/ finds nothing. What is sent (verified in
`rli/agent/investigator.py:291-308, 444-458`, `rli/llm/prompts.py`): posting URL and title,
company name/id, evidence claim values (including news headline text from `company_events`),
policy inputs, and `raw_excerpt` text truncated to 400 chars (`config.toml:492`). No user notes
or outcomes are sent. Free tiers (Gemini, Mistral "Experiment" per PROGRESS) may use prompts
for training.
Fix: add a "What is sent to the LLM endpoint" paragraph enumerating the fields above and
linking the provider's free-tier data terms; mention the Ollama path as the no-egress option.

**M6 — Third-party text enters the prompt inside `<case_state>`, which the system prompt declares "machine-generated and trustworthy".**
`rli/llm/prompts.py:161-163, 213-215`; content assembled in `rli/agent/investigator.py:291-308,
444-458`: `title` (from Greenhouse/Ashby/Lever JSON or JSON-LD), `canonical_url`, evidence
`value` strings (news headlines). These bypass the `<untrusted>` fence that `raw_excerpt` gets.
Impact is bounded: model output is schema-validated, probe args are rebuilt from the case file
(`rli/agent/controller.py:655-662`, `rli/probes/registry.py:96-116`), and citations are
validated with a deterministic fallback (`rli/agent/explanation.py:473-548, 800-813`) — so the
worst case is a worse probe choice or a misleading explanation sentence, not an action change.
Fix: render `title`, evidence `value` and free-text fields as `UntrustedBlock`s (or escape
`<`/`>` and cap length) and soften the "trustworthy" wording to cover only the structural
fields.

**M7 — Delimiter neutralisation misses zero-width / non-`\s` Unicode inside the tag.**
`rli/llm/client.py:125` `_DELIMITER_RE = <\s*/?\s*untrusted` — `</​untrusted>` or
`<​/untrusted>` is not matched but many tokenizers/models read it as the closing tag;
`<case_state>` is not in the neutralised set at all (`:271-285`).
Fix: strip Cf-category characters (`unicodedata.category(c) == "Cf"`) before matching, and/or
use a per-render random nonce in the tag name (`<untrusted-7f3a>`), which also covers
`case_state`.

**M8 — Evidence `source_url` is rendered into `href` without scheme validation (`javascript:` XSS).**
`rli/ui/index.html:325` `'<a href="' + escapeHtml(ev.source_url) + '"'` — `escapeHtml` stops
attribute breakout, not the scheme. `EvidenceItem.source_url` (`rli/models/evidence.py:36`) has
no validator; the ATS/net path is https-only, but `company_events` rows come from the events CSV
loader. Requires a hostile CSV row or DB write; self-XSS scale today.
Fix: in the UI, only emit the anchor when `/^https?:\/\//i.test(ev.source_url)`; add a
`field_validator` on `source_url` requiring `https://` (or `http(s)`).

**M9 — LLM cache key omits `max_tokens` and the latched `json_object` fallback mode.**
`rli/llm/client.py:288-300` hashes template id/version/system/instructions/schema;
`max_tokens` is sent (`:942`) but not hashed, and the `json_object` fallback (`:831`, `:915-923`)
mutates the request without changing the key. Raising `max_tokens` to fix truncation keeps
serving the truncated cached answer; the same key can map to two different requests. The
`version` field exists, so render-layout edits are covered only if someone remembers to bump it.
Also `schema.model_json_schema()` output is Pydantic-version dependent (low: cache-wide miss,
not a wrong hit).
Fix: add `"max_tokens": self._max_tokens`, `"response_format_mode": mode`, and a
`RENDER_VERSION` constant to the hashed dict.

**M10 — Two history batch jobs hold the write lock for their whole run.**
`rli/history/closures.py:608-645` (`apply_to_postings`) and `rli/history/matching.py:~565-672`
(`link_reposts`) commit once at the end of a loop over every company; with a 1.5 GB DB this can
exceed the 5 s `busy_timeout` for any concurrent writer (snapshot cron, API). Second independent
source of "database is locked". (Related: `rli/eval/runner.py:732` hands the run's own
connection to `ToolCache`/`CachedClient`, which document that they own commits —
benign now, latent partial-commit bug.)
Fix: `conn.commit()` per company inside the loops; raise `BUSY_TIMEOUT_MS`
(`rli/db/__init__.py:34`) to 30000; assert `not conn.in_transaction` in the cache constructors.

### Low

**L1 — `GET /watch` returns the full watch list (URLs, states, run ids) unauthenticated.** `rli/api/app.py:308-312`. Fix: covered by H1's token dependency.

**L2 — No length bounds on `url` / `note` / `notes`, no request body cap.** `rli/api/app.py:57-67`; Starlette/uvicorn have no default body limit. Fix: `Field(max_length=2048)` on `url`, `Field(max_length=4000)` on note fields; a small middleware rejecting `Content-Length > 64 KiB`.

**L3 — No retention or deletion path for `outcomes` (free-text `notes`), `evidence`, or `data/watches.json`.** `rli/db/schema.sql:254-262`, `rli/api/app.py:261-267`; no purge command exists (grep purge/retention/ttl → cache TTLs only). Fix: `rli outcomes delete <id>` and `rli db purge --older-than N` covering outcomes/evidence/runs; `DELETE /watch` endpoint.

**L4 — `data/watches.json` and `data/rli.db` are created with default permissions; they are a person's job-search history.** `rli/api/watch_store.py:66-70`, `rli/db/__init__.py`. Fix: `os.chmod(tmp_path, 0o600)` before `replace`; note "sensitive data" in README ops section.

**L5 — ATS-supplied `absolute_url` is adopted as `canonical_url` unvalidated; a disallowed host then raises `DisallowedHostError` out of the run.** `rli/probes/resolve_posting.py:86` → `:277` `_resolve_jsonld` → `check_allowed` raises (by design "misconfiguration"), aborting the whole investigation (API 502) instead of degrading like `rli/probes/requirements_drift.py:246-250`. Fix: `try: check_allowed(job.absolute_url, ["*"]) except DisallowedHostError: keep the input URL`.

**L6 — `scripts/replay_c_loop.sh` robustness.** Lines 9-10: `set -u` only, `cd "$(dirname "$0")/.."` unguarded (a failed `cd` then sources nothing and writes `data/` in the wrong tree); line 33 retries a non-quota failure every 10 min forever; no `flock`, so two invocations double-spend the daily quota (WAL prevents corruption). Fix: `set -euo pipefail`, `cd ... || exit 1`, `exec 9>data/replay-c.lock; flock -n 9 || exit 0`, a `fails` counter that exits after 5 consecutive non-3 failures.

**L7 — No log rotation.** `data/logs/replay-c.log` grows across restarts; `docs/cron.md` per-day snapshot logs accumulate indefinitely. Fix: `find data/logs -name '*.log' -mtime +90 -delete` in the cron line, or `logrotate` stanza.

**L8 — Tests rely on per-test `@respx.mock`; no default-deny transport.** `tests/conftest.py:48-62` builds a real `httpx.Client(timeout=1.0)`; every network test today is wrapped (verified), but a future test would hit the network silently. Fix: autouse fixture `with respx.mock(assert_all_mocked=True, assert_all_called=False): yield`, or `httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(RuntimeError("network in tests"))))` as the default.

**L9 — `PRAGMA journal_mode=WAL` return value is not checked.** `rli/db/__init__.py:144`. On a filesystem where WAL fails (network mounts) everything silently reverts to rollback-journal locking. Fix: `mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]; if mode != "wal": raise RuntimeError(...)`.

**L10 — Replay network ban is per-object substitution, not global.** `rli/replay/mode.py:284-346` swaps the net client factory; no probe imports httpx directly today (verified), but nothing stops one. Fix: a module-level `rli.net.client.REPLAY_LOCK = threading.local()` flag set by `replay_mode()` and checked in `_fetch_one_hop`.

**L11 — `case_state_at` adopts whichever PIT shadow is already installed without checking its T.** `rli/replay/build.py:607-611`; no in-tree caller misuses it. Fix: store T in a temp view (`CREATE TEMP VIEW pit_meta AS SELECT '…' AS t`) and compare on reuse.

### Info

**I1 — `tool_cache.response` retains full third-party page bodies forever (append-only, no TTL purge).** Read-only scan found 1090 rows whose bodies contain third-party API-key-shaped strings embedded by the *scraped* careers pages (Maps/reCAPTCHA-style public keys), not this project's credential. Exporting or sharing `data/rli.db` (1.49 GB) ships them. Fix: document; add an export scrub or exclude `tool_cache` from any shared copy.

**I2 — `scripts/audit_targets.py:79-160` and `scripts/wayback_spotcheck.py:91-135` use raw `httpx.Client()`.** Hosts are hardcoded literals (only `tenant` from the CSV is interpolated into paths), so no SSRF; but no allowlist, rate limiter, or size cap. Fix: route through `NetClient` or URL-encode `tenant` and cap bodies.

**I3 — No `User-Agent` on the production probe path** (`rli/net/client.py:600, 745`); the dev scripts do set one. Fix: `headers={"User-Agent": "rli/0.1 (+contact)"}` on the shared client.

**I4 — No `logging` usage; API failures go to stderr via `traceback.print_exc()`** (`rli/api/app.py:201, 303`). Fix: `logging.getLogger(__name__).exception("investigate failed")`.

**I5 — `title`, `source_url`, evidence `value` are not length-capped before prompt rendering** (`rli/agent/investigator.py:293-308, 451`; `raw_excerpt` is capped at 400). Prompt/cache bloat only. Fix: cap at e.g. 300 chars in the case-state builder.

## Done well (leave alone)

- **Secrets.** `Llm.api_key_env` stores only the env-var *name*; `api_key()` reads at call time (`rli/config.py:591-598`), so `Config.model_dump()` / `config_hash` cannot leak it. Key travels only in the `Authorization` header, never a query string; LLM redirects are not followed precisely to avoid header leakage (`rli/llm/client.py:1046-1051`); `_scrub` is applied at every raise site and in `__repr__`; `llm_cache` stores only the validated parsed output. `.env` is gitignored and untracked; the DB has 0 key hits across `run_steps.error`.
- **Allowlist / SSRF guard** (`rli/net/client.py:114-278`): https-only, userinfo rejected, exact case-insensitive host match with trailing-dot normalisation, no subdomain wildcards, IP literals rejected in v4/v6/bracketed/mapped/decimal/octal/hex forms, non-public suffixes rejected, `follow_redirects=False` forced even on an injected client, every hop re-validated, `max_redirects` bounded, retries bounded with jittered backoff and clamped `Retry-After`, per-host token bucket, no `verify=False` anywhere, no XML parser (bs4 `html.parser` + `json.loads` only).
- **SQL.** Every query is parameterised; the one unavoidable inlining (PIT `<= T` cutoff in `CREATE TEMP VIEW`, `rli/replay/pit.py:216-262`) is regex-validated to a strict UTC timestamp and identifiers come from fixed tuples. No `eval`/`exec`/`pickle`/`yaml.load`/`shell=True` anywhere.
- **Agent containment.** Model outputs are strict Pydantic (`extra="forbid"`), probe names checked against the registry, model-proposed args validated then discarded and rebuilt from the case file, duplicate calls refused, step/cost/latency caps checked before the model's `stop`, bounded `for` loop (`rli/agent/loop.py:1158`), invalid JSON stops rather than retries, citations validated with deterministic-stub fallback, post-explanation invariant is a `RuntimeError` not an `assert`.
- **Untrusted delimiting.** Enforced by a `field_validator` on `UntrustedBlock`, re-applied at render, untrusted blocks rendered last, truncate-after-sanitise ordering documented; raw excerpts never enter the hashed structured input.
- **API basics.** Default bind `127.0.0.1`; sync handlers (right for sqlite3); broad exceptions mapped to generic 502 with no internals; no `StaticFiles` mount (single hardcoded `FileResponse`); watch store path is fixed (no traversal), atomic tmp+`replace`, lock-guarded.
- **Config.** All 14 models `ConfigDict(frozen=True, extra="forbid")`, ranges via `Field(gt/ge)`, cross-field `model_validator`s, no `Any` fields.
- **Time.** Single `now_utc()`, `ensure_aware()`/`parse_utc()` reject naive datetimes at every boundary; no naive/aware mixing found in PIT, closures, or features.
- **Cache keys.** sha256 over canonical JSON (`sort_keys`, fixed separators) everywhere; LLM key includes model id, template version, schema and the PIT clock.
- **Tests.** Fresh `tmp_path` SQLite per test, no module-level mutable state, no bare `datetime.now()`, live LLM test opt-in via `RLI_LLM_LIVE=1` and skips otherwise, real hostnames only inside `@respx.mock`.
- **Dependencies.** Lockfile clean under pip-audit; no `verify=False`, no vendor SDKs.

## Recommended fix order

1. **H3** `rli/replay/pit.py` — `conn.commit()` before `yield` and in `finally` (unblocks concurrent replays; two lines).
2. **H4** `rli/eval/case.py:1088-1093` — remove the `team_signal` data-blob fallback (restores the §9 point-in-time guarantee before more System C replay is spent).
3. **H5** — `pretty_exceptions_show_locals=False` on all four `typer.Typer(...)` calls (one line each).
4. **H1 + H2 + L1** `rli/api` — bearer token dependency, `debug_routes=False` default, `compare_digest` on the debug token, concurrency semaphore, `limit_concurrency`.
5. **M4 + M5** — fix the config comment / README default statement and add the data-egress disclosure (documentation only; do before sharing the repo).
6. **M2** then **M1** `rli/net/client.py` — streamed body cap, then pre-connect resolved-IP check.
7. **M3** `rli/config.py` — https unless loopback.
8. **M9** — fold `max_tokens`, response-format mode, render version into `prompt_hash` (invalidates the current `llm_cache`; do it before the next long System C run, not during).
9. **M6/M7/M8** prompt and UI hardening; **M10** per-batch commits.
10. Lows and infos as convenient; L6 (`flock`, `set -e`) and L8 (default-deny test transport) are cheap and worth doing first.
