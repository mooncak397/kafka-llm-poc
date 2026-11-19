"""Token prices and the cost of one LLM call. Stdlib only: agent.py and loadtest/score.py both import it.

usage = the four raw Anthropic usage fields, renamed: input (input_tokens), cache_read (cache_read_input_tokens),
cache_write (cache_creation_input_tokens), output (output_tokens). With the raw Anthropic SDK they are SEPARATE:
input_tokens is only the uncached remainder, so summing all four priced fields never counts a cache token twice.
A model not in PRICES (every Ollama model) costs $0 API cost.
"""
PRICE_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
PRICE_CHECKED = "2026-11-19"
# USD per million tokens. cache_write = the 5-minute TTL price (the default of automatic caching).
PRICES = {
    "claude-haiku-5-5": {"input": 0.10, "cache_write": 0.125, "cache_read": 0.01, "output": 0.50},
    "claude-sonnet-5-5": {"input": 2.00, "cache_write": 2.50, "cache_read": 0.10, "output": 10.00},
}
# Haiku 5.5 only: a request whose prompt (input + cache_read + cache_write) is over 100,000 tokens pays these prices.
LONG_PROMPT = {"claude-haiku-5-5": (100_000, {"input": 0.50, "cache_write": 0.625, "cache_read": 0.05, "output": 2.50})}

# Budget bounds. agent.py uses both (a unit test ties CALLS_PER_QUESTION to its MAX_TOOL_TURNS + 1).
CALLS_PER_QUESTION = 6
MAX_OUTPUT_TOKENS = 1024  # max_tokens of every Anthropic call
# shortcut: assumes about one tool result per turn (prefix ~1,800 tokens + up to 6 results of ~650); parallel tool
# calls can go above it. The agent's daily counter and the Console spend limit are the hard stops behind this estimate.
WORST_INPUT_PER_CALL = 1_800 + 6 * 650


def cost_usd(model: str, usage: dict) -> float:
    p = PRICES.get(model)
    if p is None:
        return 0.0
    if model in LONG_PROMPT and usage["input"] + usage["cache_read"] + usage["cache_write"] > LONG_PROMPT[model][0]:
        p = LONG_PROMPT[model][1]
    return sum(usage[k] * p[k] for k in p) / 1e6


def worst_case_question_usd(model: str) -> float:
    """Upper estimate for one question: every call at max_tokens, all input priced as cache writes (the dearest kind)."""
    one_call = {"input": 0, "cache_read": 0, "cache_write": WORST_INPUT_PER_CALL, "output": MAX_OUTPUT_TOKENS}
    return CALLS_PER_QUESTION * cost_usd(model, one_call)
