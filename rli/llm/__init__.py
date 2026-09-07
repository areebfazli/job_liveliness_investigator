"""LLM access for System C: structured calls, prompts, cost, and the replay cache.

`rli.llm.client` owns the transport, the schema-validated response and the
`(model_id, prompt_hash, structured_input_hash)` cache spec.md §2 requires;
`rli.llm.prompts` owns the two prompt templates and the untrusted-data
boundary. See either module's docstring for the design and the judgment
calls.

Importing this package never imports `anthropic`: `AnthropicClient` defers
that to `__init__`, and only when it has to build a real API client. The
package therefore imports (and every non-live test runs) with the SDK
absent and with no API key in the environment.
"""

from __future__ import annotations

from rli.llm.client import (
    AnthropicClient,
    CachedClient,
    CacheStatus,
    LLMClient,
    LLMError,
    LLMResponse,
    LLMSchemaError,
    Prompt,
    ScriptedClient,
    UntrustedBlock,
    compute_cost_usd,
)
from rli.llm.prompts import (
    PROMPT_VERSION,
    UNTRUSTED_TAG,
    build_explanation_prompt,
    build_investigator_prompt,
    sanitize_untrusted,
)

__all__ = [
    "PROMPT_VERSION",
    "UNTRUSTED_TAG",
    "AnthropicClient",
    "CacheStatus",
    "CachedClient",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "LLMSchemaError",
    "Prompt",
    "ScriptedClient",
    "UntrustedBlock",
    "build_explanation_prompt",
    "build_investigator_prompt",
    "compute_cost_usd",
    "sanitize_untrusted",
]
