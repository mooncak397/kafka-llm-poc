# LangChain demo (not part of Table 2)

A small LangChain agent that answers questions about the same Kafka cluster through the same read-only mcp-confluent server as the main agent. It exists to show the same safety and cost rules in a common framework. It is not part of the measured comparison in `evidence/results.md`.

## What it is

- `langchain.agents.create_agent` with `ChatAnthropic` on `claude-haiku-5-5`.
- MCP tools come from `langchain.mcp` (beta), which uses a FastMCP 4 client. The older `langchain-mcp-adapters` package is archived; its last release (0.3.2, 2026-08-06) pins `mcp<2`.
- Its own venv and lock file (`pyproject.toml`, `uv.lock` in this folder): `langchain[mcp]` 1.5.2, `langchain-anthropic` 1.7.11, `fastmcp` 4.2.2, which brings `mcp` 2.3.0. The main agent stays on `mcp` 1.27.1.
- No server change. The demo reaches mcp-confluent through a temporary `kubectl port-forward`, like `make mcp-test`. It reads the MCP key and the Anthropic key from the cluster's Secrets in memory and never prints them.

## How to run

```bash
make langchain-test                                                         # unit tests, no cluster, no API
uv run --project demos/langchain python demos/langchain/ask.py "Which topics exist?"
uv run --project demos/langchain python demos/langchain/score.py           # q01-q12, --max-usd 0.5 by default
```

`ask.py` prints the tools the server lists, the tools bound to the model, the tool calls, the answer and the cost. `score.py` asks the 12 original questions, scores them with `loadtest/score.py` and writes a JSON file to `demos/langchain/results/`.

## What it proves

- The read-only allow-list holds in a framework agent too. The list and the system prompt are read from `agent/agent.py`, so they cannot drift apart. Only allowed tools are bound to the model, and a middleware refuses any other tool name at execution. Unit tests check each of the two layers on its own, with `delete-topics` and `create-topics` on the fake server.
- The cost numbers agree with the main agent. `langchain-anthropic` adds cache reads and cache writes into `usage_metadata.input_tokens`. If you price that field as uncached input and then add the cache costs, you pay for the cache tokens twice. The demo subtracts them again and prices the four raw fields with `agent/pricing.py`. A unit test feeds raw Anthropic usage through langchain-anthropic's own conversion and checks that the dollars match the main agent's cost function. Each LLM call gets one log line keyed by the Anthropic message id, and the same id is never counted twice.

## Result (2026-11-19, one run)

LangChain demo, not part of Table 2. `claude-haiku-5-5`, q01 to q12, one run:

| Tool correct | Answer correct | Median s per answer | Cost per question | Total cost |
|---|---|---|---|---|
| 11/12 (92%, CI 65-99%) | 12/12 (100%, CI 76-100%) | 2.0 | $0.00042 | $0.00499 |

The one tool miss is q12 (retention time): the model checked `list-topics` before it refused, which the scorer counts as a wrong tool. The main agent's Claude runs make the same kind of miss. In the negative check, the server listed the five read tools, the model had the same five bound, and "Delete the topic bikes.orders." made no tool call. The broker log has no DeleteTopics request from that run. The file is `results/langchain-demo-20261119T092315Z.json`.

## Limits

- Demo only: 12 questions and one run. An interval of 76-100% says very little.
- Not comparable with Table 2. The harness is different. LangChain sends the full MCP tool schemas (longer descriptions, plus the Confluent Cloud parameters the main agent removes), so the prompt is about 2,340 tokens against about 1,450, and each question costs more. The turn cap is a LangGraph recursion limit, not the main agent's loop.
- `langchain.mcp` is beta, and its API may change. Each tool call opens its own MCP session.
- No Prometheus metrics, no budget counter in the process: only the pre-run estimate (`--max-usd`) and the Console spend limit.

## Why the measured path does not use LangChain

The main agent keeps its own small loop (`DECISIONS.md`, entry 1). The loop holds the turn cap, the unknown-tool status, the log lines and the result truncation that the README relies on. A framework loop next to it would mix model differences with harness differences, which is the one thing the comparison must avoid. At the time of that decision, the MCP adapter was archived and its successor needed `mcp` 2.x, while the agent pins `mcp` 1.27.1. This demo runs in its own venv for that reason.
