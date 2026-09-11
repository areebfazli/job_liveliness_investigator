# Independent verification of `reports/security_review.md`

Date: 2026-09-11. This is a **second-opinion pass**, not a new audit: every High and Medium
finding of the prior audit, plus every Low/Info that could be checked quickly, was
re-derived from the code rather than taken on the report's word. Dynamic reproduction was
preferred over reading wherever the claim admitted one.

**Constraints honoured.** No project file was modified other than creating this report.
No git command that changes state was run (HEAD is `c521607`, committed 17:54:15 by the
prior audit session, before this verification began; working tree clean throughout).
`data/rli.db` was never opened — all dynamic work ran against two throwaway copies in the
session scratchpad, deleted afterwards. No live LLM call was made. No API key was printed;
`.env` was never read.

## Baseline (re-run, not inherited)

| Check | Audit claimed | Reproduced |
|---|---|---|
| `uv run pytest -q` | 1233 passed, 1 skipped | **1233 passed, 1 skipped** in 42.04s |
| `uv run ruff check .` | clean | **All checks passed!** |
| `uvx pip-audit -r <exported lock>` | no known vulnerabilities | **No known vulnerabilities found** |
| Lockfile size | "162 pinned packages" | **57** (`grep -c '^\[\[package\]\]' uv.lock`; 56 in the export) |
| Pinned versions cited | fastapi 0.141.1, starlette 1.6.0, httpx 0.28.1, pydantic 2.13.5, uvicorn 0.52.4, bs4 4.15.0, lifelines 0.30.3, numpy 2.5.3, pandas 2.3.3 | **all exact** |

The "162 pinned packages" figure is the one baseline number that does not reproduce; the
vulnerability conclusion it supports is nonetheless correct.

## Verdicts

Counts: **27 CONFIRMED · 3 PARTIAL · 1 NOT REPRODUCED · 0 NOT TESTABLE.**

### High

| id | title | audit sev. | verdict | my sev. | evidence |
|---|---|---|---|---|---|
| H1 | No auth / rate limit on the HTTP API | High | CONFIRMED | **Medium** (High if `RLI_API_HOST` ≠ loopback) | a case-insensitive grep over `rli/api/` for `depends`, `security`, `bearer`, `token`, `auth`, `cors`, `middleware` and `limit` returns **one docstring hit and nothing else**. TestClient against a scratch DB: anonymous `POST /outcomes` persisted a row; anonymous `POST /watch` + `GET /watch` wrote and disclosed the list; one anonymous `POST /investigate` triggered 3 outbound fetches + the LLM gate probe; 60 concurrent anonymous requests serialised in batches of 40, confirming the sync-`def` threadpool claim (`inspect.iscoroutinefunction` false for all routes). `uvicorn.run()` receives only `host`/`port` — no `limit_concurrency` (`rli/api/__main__.py:16`). `127.0.0.1` is an unvalidated env default; `RLI_API_HOST=0.0.0.0` works. |
| H2 | Debug trace route gated by the literal `"1"`, on by default | High | CONFIRMED | **Low** (not separable from H1) | `rli/api/app.py:211` `if not settings.debug_routes or x_rli_debug != "1"`; `rli/api/settings.py:58` `debug_routes=_truthy(..., default=True)`. `GET /runs/{id}` with `X-RLI-Debug: 1` returned raw `runs` + `run_steps` (probe args, prompt hashes, model_id, per-step cost, raw error text); 404 without. No ownership concept exists — a second independent client got a byte-identical response. |
| H3 | `point_in_time` holds a read snapshot across the replay block | High | CONFIRMED | **Medium** | Instrumented the real `point_in_time` on a scratch copy: `in_transaction` False → **True after `_install_postings`** → still True **at the yield**. A second connection then committed to `main`; the PIT connection's next write failed **in 0.000 s** against a 5 s `busy_timeout` with `sqlite3.OperationalError: database is locked`, `sqlite_errorname=SQLITE_BUSY_SNAPSHOT` (517). Counterfactual (monkeypatched wrapper adding `conn.commit()` before the yield, file untouched): `in_transaction` False at yield, **write succeeded**. Temp shadows survive the commit, so the one-line fix is safe. |
| H4 | `team_signal` `data`-blob fallback bypasses the `available_at <= T` gate | High | CONFIRMED | **High** (agree — evaluation integrity) | Reproduced on **real rows** (`dev-300-v2`, posting `archive:crypto.com:07ca…`): at T=2025-12-09 the team_signal claim is correctly gated out (`evidence: none`) yet `corroborating_hiring_signal` = **False**, and the key is absent from `unpopulated`. Control with the blob key stripped at the same T → **UNKNOWN**, proving the blob is the source. Recomputing the probe honestly under `point_in_time(conn, T)` gives **True** — the leaked value is the *inverse* of the at-T truth in 5/5 sampled grid points. Scale: **830 archive-era replay runs** executed `team_signal` with **0** team_signal evidence rows in any of them (729 leaked True, 93 leaked False). `rli replay check` on that dataset reports `evidence_after_t=0`. |
| H5 | Typer pretty tracebacks print frame locals (LLM key) | High | **NOT REPRODUCED** | **Low** (latent) | The premise is false for the locked version. `typer 0.27.2`, `.venv/.../typer/main.py:500`: `pretty_exceptions_show_locals: ... = False`, and its own Doc string says "**(the default)** … to enhance security". Runtime: `typer.Typer().pretty_exceptions_show_locals` → `False`. Forced repro (subprocess CLI, fake key `RLI_FAKE_KEY_SENTINEL_…`, `httpx.Client.send` monkeypatched to raise with the real `Authorization` header as a frame local, fake `base_url` so nothing left the machine): **no sentinel in captured stderr**, and none in `run_steps.error`. A/B control with `pretty_exceptions_show_locals=True` explicitly set **does** print `Authorization: Bearer <FAKE_KEY>` — the Rich mechanism is real, the trigger is not present. Also: `data/logs` contains **zero** key-shaped strings (scanned with `grep -l`, values never printed). |

### Medium

| id | title | audit sev. | verdict | my sev. | evidence |
|---|---|---|---|---|---|
| M1 | json_ld `"*"` + no resolved-IP check → blind SSRF | Medium | CONFIRMED | **Low** | `config.toml:260` `json_ld = ["*"]` (audit said 259). No `getaddrinfo`/socket call anywhere in the guard path. End-to-end: with `getaddrinfo("evil.example.com")` monkeypatched to `127.0.0.1`, a real httpx GET landed on a loopback server — nothing re-validates the resolved IP. Guard correctly rejects IP literals in decimal/octal/hex/IPv6/mapped forms, userinfo, non-public suffixes, and `http://169.254.169.254` **by scheme**. |
| M2 | No response size cap / decompression bomb | Medium | CONFIRMED | **Medium** (agree) | a grep for `max_body`, `content_length`, `Content-Length`, `iter_bytes` and `stream(` over `rli/net/client.py` + `rli/llm/client.py` → **zero hits**. Against the real unmodified `NetClient` (only the transport pointed at loopback; `check_allowed` ran normally) a **64 KB** gzip body decompressed to **67,108,864 bytes**: `NetResult.ok=True`, full body buffered, RSS +196 MB, no error, no truncation, no warning. |
| M3 | LLM `base_url` may be plain `http://` to a remote host | Medium | CONFIRMED | **Low** | `rli/config.py:552-569`: `if parsed.scheme not in ("http","https") or not parsed.hostname: raise` — nothing more. `Llm(base_url="http://api.example.com/v1")` and a full `Config.model_validate()` both pass. `is_local_endpoint()` (`rli/config.py:601-613`) already exists but is wired only into `credentials_configured()`, never into the scheme check. Header confirmed via respx: `Authorization: Bearer <key>` sent regardless of scheme. |
| M4 | Shipped config says "local Ollama, nothing leaves the machine" while pointing at Gemini | Medium | CONFIRMED | **Medium** (agree) | Verbatim, `config.toml:360-362`: `# Default: a local Ollama. Free, no API key, nothing leaves the machine.` / `#   ollama serve && ollama pull qwen3:8b` / `base_url = "https://generativelanguage.googleapis.com/v1beta/openai"`; `:366 api_key_env = "GEMINI_API_KEY"`; `:373 model_id = "gemini-3.5-flash-lite"`. `README.md:117-118`: `**1. A local model with Ollama (the shipped default — free, no API key, no` / `data leaves the machine).**`. Worse than reported: `config.toml:375-382` still frames Gemini as the *opt-in* ("instead of a local model: uncomment") and names `gemini-2.5-flash`, while the live value is `gemini-3.5-flash-lite`. `git show 3d83fbc` confirms the deliberate 3-field swap. **Correction:** the audit's worry that `tests/conftest.py`'s `cfg` fixture would hit a remote endpoint does not hold — every LLM test uses respx, a fake URL, or a scripted fake, and `tests/test_llm_live.py` is gated on `RLI_LLM_LIVE=1`. |
| M5 | No disclosure of what leaves the machine / provider data terms | Medium | CONFIRMED | **Low** (doc gap; fix with M4) | README + `docs/*.md` have zero hits for train/terms/privacy/data-use/retention. Transmitted fields verified from `rli/agent/investigator.py:291-308,444-458`: url/canonical_url, ats/tenant/job_id/posting_id, company_id, title, team, location, every evidence claim (incl. news headline text), all 7 policy inputs, probe catalogue/history, plus `raw_excerpt` capped by `cfg.agent.max_excerpt_chars` (400). Confirmed **notes/outcomes are not sent** — the only free-text `notes` is read solely by `rli/eval/gates.py:628`. |
| M6 | Third-party text inside `<case_state>`, declared "trustworthy" | Medium | CONFIRMED | **Low** | `rli/llm/prompts.py:161-163,213-215` literally say the `<case_state>` block "is machine-generated and trustworthy". `title`, `canonical_url` and evidence `value` (e.g. `rli/probes/company_events.py:221`, `f"{materiality}: {headline}"` — real headline text) are rendered unfenced; only `raw_excerpt` is sanitised into `<untrusted>`. The audit's claimed bounds are real: `extra="forbid"` schemas, probe args rebuilt from the case file (`rli/agent/controller.py:679`, `rli/probes/registry.py:96-116`), citations validated with deterministic fallback. |
| M7 | Delimiter neutralisation misses zero-width Unicode; `<case_state>` not covered | Medium | CONFIRMED | **Low** | Executed against the real `_DELIMITER_RE` (`rli/llm/client.py:125`): `</untrusted>` is neutralised; `<U+200B/untrusted>`, `<U+FEFF/untrusted>`, `<U+2060/untrusted>` (zero-width chars shown as escapes) **all pass through unchanged**; literal `<case_state>` / `</case_state>` never matched at all. |
| M8 | Evidence `source_url` rendered into `href` without scheme validation | Medium | CONFIRMED | **Low** (self-XSS, loopback) | `rli/ui/index.html:325` `'<a href="' + escapeHtml(ev.source_url) + '"'`; `escapeHtml` (`:269-272`) escapes only `& < > " '`. `EvidenceItem(source_url="javascript:alert(1)")` constructs with no error (`rli/models/evidence.py:35`, plain `str`). Path traced: `rli/events/store.py:275-300` loads `source_url` straight from CSV. I separately audited **every** other UI sink — all `innerHTML` interpolations at lines 304, 316-337, 464-482 are `escapeHtml`-wrapped, so the scheme gap is the only UI defect. |
| M9 | Cache key omits `max_tokens` and the `json_object` fallback mode | Medium | CONFIRMED | **Low** (correctness, not security) | `prompt_hash` (`rli/llm/client.py:288-300`) hashes only template_id/version/system/instructions/schema; `max_tokens` is in the request body at `:942`. Two configs differing only in `max_tokens` (500 vs 4000) produce **identical** cache keys. The `json_object` latch (`:831` → `:1110`) changes `response_format` and the system prompt without changing the key. |
| M10 | History batch jobs hold the write lock for the whole run | Medium | CONFIRMED | **Medium** (agree, low end) | `rli/history/closures.py:644` — a single `conn.commit()` after a loop over **every** company; worse than reported, `build_intervals()` (`:630`) does its own per-company scan *inside* the lock window. `rli/history/matching.py:672` — same single terminal commit. `BUSY_TIMEOUT_MS = 5000` (`rli/db/__init__.py:34`). Measured on a scratch DB: an 8 s uncommitted write txn made a second connection fail with `database is locked` after **5.011 s**; a 3 s hold let it through after 1.532 s; WAL readers unaffected. Concurrent writers are real (documented 06:00 cron, the API, a second CLI). **Correction:** the `rli/eval/runner.py` sub-claim is a non-issue — `:142-152` documents committing after every write and `:401,485,532,585` actually do it. |

### Low / Info (quick checks)

| id | verdict | my sev. | evidence |
|---|---|---|---|
| L1 | CONFIRMED | Low | `GET /watch` returns the list unauthenticated. Materially, it is the **run-id oracle** that makes H2 usable: `GET /watch` → run_id → `GET /runs/{id}` with `X-RLI-Debug: 1` → full trace. Chain demonstrated. |
| L2 | CONFIRMED | Low | Request models (`rli/api/app.py:57-71`, not 57-67 — the cited range cuts off `WatchRequest.url`) carry zero constraints; no `maxLength` in any of the three schemas. A **5 MB** `note` was accepted, echoed, and persisted. No body-limit middleware; uvicorn ships none. |
| L3 | CONFIRMED | Low | `outcomes` (`rli/db/schema.sql:254-262`) is insert-only via `POST /outcomes`; no update/delete route, **no `DELETE /watch`**, and purge/retention/ttl greps hit only cache TTLs. |
| L4 | CONFIRMED | Low | No chmod/umask handling in `rli/api/watch_store.py:67-71` (`tmp_path.write_text(...)` → `.replace()`) or `rli/db/__init__.py`. `stat data/rli.db` → **644**, world-readable. |
| L5 | **PARTIAL** | Info | Mechanism real: `_resolve_jsonld` calls `NetClient.get` with no try/except, so `DisallowedHostError` escapes `ProbeRunner.execute`, unlike `rli/probes/requirements_drift.py:244-250` which degrades. **The audit's "API 502" is wrong** — `rli/api/app.py:197-198` catches `DisallowedHostError` ahead of the generic handler and returns **422**. |
| L6 | CONFIRMED | Low | `scripts/replay_c_loop.sh:9` `set -u` only; `:10` unguarded `cd "$(dirname "$0")/.."`; `:33` retries any non-3 failure every 10 min inside an uncapped `while :`; `grep -n flock` → nothing. |
| L7 | CONFIRMED | Low | No rotation in the loop script (`tee -a` only) or `docs/cron.md`; `data/logs` currently 22 files, largest ~370 KB, unbounded by design. |
| L8 | CONFIRMED | Low | `tests/conftest.py` has no autouse deny-fixture; the factory builds a real `httpx.Client(timeout=1.0)`. |
| L9 | CONFIRMED | Low | `rli/db/__init__.py:144` `conn.execute("PRAGMA journal_mode = WAL")` — result discarded, unlike `schema_version`'s `.fetchone()` two lines down. |
| L10 | CONFIRMED | Low | DI/factory substitution in `rli/replay/mode.py`, not a global guard; independent re-run of `grep -rn "import httpx" rli/probes/` → **zero matches**, so nothing bypasses it today. |
| L11 | CONFIRMED | Info | `case_state_at` adopts an installed shadow via `installed_shadows(conn)` (`rli/replay/pit.py:176-184`), which compares **names only**, never T. Repo-wide, `case_state_at(` is called only from `tests/test_replay_run.py` — no production caller, so it is latent. |
| I1 | **PARTIAL** | Info | `rli/db/schema.sql:290-297` documents `tool_cache` as append-only ("rows never overwritten"); `expires_at` gates read-eligibility only; no purge code anywhere. The report's "1090 rows containing third-party key-shaped strings" was **not** re-verified — `data/rli.db` was off-limits for this pass. |
| I2 | CONFIRMED (reframe) | Info | `tenant` is interpolated with no quoting in both scripts. But the host is a fixed literal preceding `{tenant}`, so **no host switch is possible**; real exposure is query/path munging from a locally-authored CSV. "Path traversal / SSRF" overstates it. |
| I3 | CONFIRMED | Info | No `headers=` on `rli/net/client.py:600` or `:745`; the two dev scripts do set a UA. |
| I4 | CONFIRMED | Info | `grep -rn 'import logging' rli/` → **zero**; `traceback.print_exc()` at `rli/api/app.py:201,303`. |
| I5 | **PARTIAL** | Info | `title` / `source_url` / evidence `value` are genuinely uncapped. But `raw_excerpt`'s 400 is `cfg.agent.max_excerpt_chars` (`rli/agent/investigator.py:512`, `rli/config.py:687`), i.e. configurable — the audit's phrasing implies a constant. |

## Disagreements with the audit

**1. H5 is not a live finding — its central premise is factually wrong.** The audit asserts
"Typer's default pretty tracebacks render frame locals". In the locked `typer 0.27.2` the
default is `False`, stated as such in Typer's own parameter documentation, and confirmed at
runtime. A forced exception inside `_send` with the real `Authorization` header as a frame
local produced no key in stderr. The report's own mitigations compound this: `LLMError` is
caught in both commands that touch the LLM client. **Severity High → Low**, and it should be
reframed from "can print the key" to "does not pin a security-relevant library default"
(`pyproject.toml` allows `typer>=0.12`, so a non-locked install could in principle resolve a
version with a different default). The one-line fix is still worth taking as defense in depth.

**2. H2 is not an independent High; it is a symptom of H1.** A "gate" is only a gate if it
separates two trust levels. Since *every* endpoint is unauthenticated (H1), anyone who can
send `X-RLI-Debug: 1` can already run investigations and read the watch list against the same
data. The incremental disclosure is the user's own run trace. The defect worth fixing is
`debug_routes` defaulting to `True`; ranking it High double-counts H1. **High → Low.**

**3. H3 is a robustness bug, not a security one — but its blast radius is larger than reported.**
No attacker, no disclosure, no corruption (the failing write is the *first* of the case, so no
partial run commits and no LLM quota is spent). That argues **High → Medium**. Cutting the other
way, and missed by the audit: the resume path calls `_delete_run_ids` (`rli/replay/run.py:245-247`)
**inside** the `point_in_time` block at `run.py:482`, outside `_replay_one`'s `except`, and
`run_replay` has no outer handler — so a snapshot conflict there **aborts the entire replay run**,
not just one case. `--resume` defaults on for System C (`rli/cli.py:404`), i.e. the in-flight
multi-day run. Also verified: the connection stays wedged for every remaining case at that T
because nothing issues a `ROLLBACK`, and `SQLITE_BUSY_SNAPSHOT` fails instantly rather than
after the 5 s timeout, so the recorded "database is locked" is actively misleading.

**4. H4 is understated, and I would keep it High.** The audit describes the mechanism; the data
shows it has already fired at scale. **830** archive-era replay runs across two datasets
populated `corroborating_hiring_signal` from the build-time blob with **zero** corresponding
evidence rows, and in every sampled case the injected value was the *inverse* of the honest
at-T value. It is silent, untested, invisible to `rli replay check` (which reports
`evidence_after_t=0` over 13,768 evidence rows for the same dataset), and it violates a contract
`rli/replay/build.py:115-127` writes down and even predicts. Only luck limits measured harm:
P4's other two preconditions never co-occurred, so no final action is demonstrably wrong yet.

**5. Four Mediums are over-rated for this threat model (M1, M3, M6–M9); M2, M4 and M10 hold.**
This is a single-user local research tool with a loopback bind and no exfiltration channel.
M1 requires attacker-controlled DNS, yields four parsed JSON-LD fields, and has the highest-value
target (plain-HTTP cloud metadata) already blocked by the https-only rule. M3 requires deliberate
operator misconfiguration. M6–M9 are contained by schema validation and a deterministic policy
that owns the decision. M2 I would keep at Medium — it is unconditional, reachable from any
investigated URL, and I measured 64 KB inflating to 64 MB in one request.

**6. Two factual corrections to specific claims.** (a) L5's "aborting the whole investigation
(API 502)" is wrong: it is a clean **422**. (b) H1's "lets anyone write outcome rows for any
`posting_id`" is wrong: `outcomes.posting_id` is an FK into `postings` with
`PRAGMA foreign_keys=ON`, so an unknown id returns **400**. The real gap is unbounded duplicate
writes with 5 MB free-text notes against *existing* postings. Separately, M10's
`rli/eval/runner.py:732` sub-claim is a non-issue — the commit-after-every-write invariant is
documented at `:142-152` and honoured at `:401,485,532,585`.

**7. Things the audit missed.**
- **`GET /watch/due` is an unauthenticated, side-effecting GET.** It runs System B for every due
  watch (`rli/api/watch_logic.py:59-100`) — outbound HTTPS fetches, DB writes, watch-file writes —
  and is absent from H1's endpoint list. There is **no middleware of any kind** in `rli/` (no CORS),
  and a cross-origin GET needs no preflight, so any page the user visits while the server runs is a
  plausible drive-by trigger (modulo browser private-network restrictions). This is the most
  reachable item on the API surface and deserved naming.
- **H3's resume-path escalation** and **H4's 830 contaminated runs**, per items 3 and 4 above.
- **`rli replay check` on `dev-300-v2` reports 199 `cache_miss` violations** (System C LLM steps),
  observed incidentally while verifying H4. Unrelated to H4, unmentioned by the audit, and worth
  its own look before the next long run.
- The "162 pinned packages" baseline figure does not reproduce (57).

**8. Where the audit is right and deserves credit.** Its "Done well" section survived independent
probing: `grep` for `eval(`/`exec(`/`pickle`/`yaml.load`/`shell=True`/`subprocess.`/`verify=False`
across `rli/` and `scripts/` returns **nothing**; every f-string in a SQL position is a
`?`-placeholder join, a fixed-tuple identifier, or an `int()`-coerced PRAGMA; and every UI
interpolation except the M8 `href` is `escapeHtml`-wrapped. The file:line citations were largely
still accurate — the drifts found were minor (config.toml 259→260, app.py 210→211 and 57-67→57-71,
run.py 601→602, runner.py 732→730, build.py 120-127→115-127) and none changed a conclusion.

## Suggested re-prioritisation

1. **H4** — the only finding I would leave at High. It is silently corrupting the experiment that
   the remaining work depends on, and every further System C case spent at an archive-era T adds to
   the 830.
2. **H3** — one line (`conn.commit()` before `yield`), and it protects the in-flight run; pair it
   with an explicit `rollback()` so a conflict is recoverable rather than wedging the T group.
3. **H1 (incl. L1, L2, and `GET /watch/due`)** — one token dependency closes the whole API surface;
   flip `debug_routes` to `False` in the same change (H2).
4. **M4 + M5** — documentation only, and misleading about data egress today.
5. **M2**, then **M10**, then the remaining Lows.
