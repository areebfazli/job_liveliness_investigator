"""LLM access for System C: structured calls, prompts, cost, and the replay cache.

`rli.llm.client` owns the transport, the schema-validated response and the
`(model_id, prompt_hash, structured_input_hash)` cache spec.md §2 requires;
`rli.llm.prompts` owns the two prompt templates and the untrusted-data
boundary. See either module's docstring for the design and the judgment
calls.

There is exactly one live transport, `OpenAICompatibleClient`: a direct
httpx POST to `{[llm].base_url}/chat/completions` with an OpenAI-shaped
body. No vendor SDK is imported anywhere in this package, so it imports
(and every non-live test runs) with no API key and no network available.
"""

from __future__ import annotations

from rli.llm.client import (
    CachedClient,
    CacheStatus,
    LLMClient,
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
    "CacheStatus",
    "CachedClient",
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
    "build_explanation_prompt",
    "build_investigator_prompt",
    "close_llm_client",
    "compute_cost_usd",
    "endpoint_unavailable_reason",
    "sanitize_untrusted",
]
