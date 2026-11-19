# Decisions

Decisions for the hosted Claude backend and the cost layer, written 2026-11-19 from the architecture review of the extension plan. Each entry gives the decision, the reason, the rejected alternative and why it was rejected.

## 1. Anthropic SDK behind a backend seam in the existing loop

Decision: Claude runs through the official `anthropic` Python SDK (pinned to 2.10.0 in `agent/uv.lock`) behind `chat(messages, tools) -> {content, tool_calls, usage}` in `agent/agent.py`. The existing `ask()` loop stays as it is.

Reason: the loop already carries the turn cap, the `unknown_tool` status, the per-call log lines, result truncation and the error outcomes. Keeping it means a difference between models comes from the model, not from the harness around it.

Rejected: LangChain with `langchain.mcp`. The old `langchain-mcp-adapters` package is archived, its successor is in beta and needs `mcp` 2.x while the agent pins `mcp==1.27.1`, and a framework agent loop next to our own would mix model differences with harness differences. A small LangChain demo outside the measured path may come later as a separate task.

## 2. One loop, one allow-list, checked at listing and at execution, for every backend

Decision: `to_ollama_tools()` is the only place that lists tools for the model, and it filters by `ALLOWED`. `run_tool()` checks `ALLOWED` again before any tool call reaches MCP. The Anthropic backend translates the already filtered list and its tool calls go through the same `run_tool()`. `ALLOWED` comes from `allowed_tools()`, so `AGENT_EXTRA_TOOLS` and `make unsafe-demo` work the same on both backends.

Reason: a second path must not be able to show the model a tool the first path hides, or run a tool name the model invents.

Rejected: a separate agent module with its own copy of `READ_TOOLS`. Two lists drift apart, and `scripts/check-profile.sh` and the negative test would no longer describe the new path.

## 3. Usage as Prometheus counters plus a per-call log ledger keyed by the Anthropic message id

Decision: the agent counts tokens, cost and calls on its existing `/metrics` and writes one log line per LLM call with the message id, model, the four usage fields and the cost, and no prompt text.

Reason: the message id is the billed unit, so it is the natural key for reconciling against the Console. Counters and a log line give the totals with no extra service.

Rejected: a Kafka topic `ai.cost.events` with a consumer. It adds delivery semantics, deduplication and a second place where totals can disagree, for one producer and one reader.

## 4. Budget enforced by the turn cap, max_tokens, a pre-run estimate, an in-process daily counter and the Console limit

Decision: each question is bounded by 6 LLM calls and `max_tokens` 1024 per call. `score.py` and `load.py` estimate the worst case for the deployed model and refuse to start above `--max-usd`. The agent stops new calls once its in-memory daily total reaches `AGENT_DAILY_MAX_USD`. The workspace spend limit in the Anthropic Console is the outer cap.

Reason: worst-case spend per question can be computed (about $0.15 on Sonnet 5.5), so a simple estimate before a run is enough. With one replica, the daily counter can overshoot by at most the number of concurrent questions times that amount.

Rejected: a Redis budget guard with a switch to Haiku at 70% of the budget. Check-then-act races let concurrent requests pass at the cap, the cost of a call is only known after it, and routing by spend would mix two models inside one comparison run.

## 5. No optimizer agent; a person writes the analysis from measured rows

Decision: cost recommendations are written by hand from the measured results table.

Reason: a written analysis that cites measured rows is repeatable and checkable.

Rejected: a LangChain optimizer agent writing `finops-report.md`. Writing a file is a write tool in a project whose point is having none, its output is not repeatable, and it would read Kafka names a producer controls, which opens a prompt-injection path into a file writer.

## 6. Pinned models `claude-haiku-5-5` and `claude-sonnet-5-5`

Decision: the price table knows exactly these two ids, and the agent refuses to start on the Claude backend with any model that has no price.

Reason: "Haiku" or "Sonnet" in a results table means nothing without the exact id, and an unpriced model would silently count as $0.

Rejected: Haiku 4.5, at 10 times the price of Haiku 5.5 and unable to cache a prefix this short (its minimum is 4,096 tokens). Sonnet 4.6, which is older and more expensive.

## 7. Eval of at least 48 questions with Wilson intervals

Decision: before any quality comparison is published, the question set grows to at least 48 questions (12 per kind), with 3 runs per model and a 95% Wilson interval on every rate. Claims are made only where intervals do not overlap. This is phase 3 and not built yet.

Reason: at 12 questions, 10 correct has an interval of about 55% to 95%, so most differences between models are noise.

Rejected: publishing comparisons on the current 12 questions.

## 8. Ollama reported as $0 API cost plus latency and hardware

Decision: Ollama calls count as $0 in every cost metric and file. Results state the hardware and the measured seconds per answer next to it.

Reason: there is no API bill for the local model, and any per-question price for it would be an assumption.

Rejected: an invented GPU-hour price.

## 9. Automatic prompt caching on, measured once as an A/B row

Decision: every Anthropic call sends top-level `cache_control: {"type": "ephemeral"}` (5-minute TTL). The effect will be shown once as a measured pair of rows (cache on and off) in phase 4.

Reason: the tool definitions plus the system prompt are above the 512-token minimum on the 5.5 models, and calls 2 and later inside one question reuse the conversation so far.

Rejected: a cache-hit-ratio metric and dashboard. At a few cents per run it would be a vanity metric, and it can be derived from `agent_llm_tokens_total` if ever needed.

## 10. Same Deployment, backend switched by a Makefile variable

Decision: `AGENT_BACKEND` and `AGENT_MODEL` in the `Makefile` are filled into `k8s/12-agent.yaml` at `make agent-deploy`. Any plain `agent-deploy` goes back to Ollama.

Reason: `score.py`, `load.py`, `make ask` and `check-profile.sh` all talk to `deploy/agent`, so they keep working with no change.

Rejected: a second Deployment for Claude, which none of those scripts or checks would see.

## 11. Non-streaming Anthropic calls; total seconds per answer compared

Decision: the Anthropic backend calls `messages.create` without streaming.

Reason: with `max_tokens` 1024 the calls are short, and non-streaming code is simpler. The comparison uses total seconds per answer, which both paths have.

Rejected: streaming with a client-side TTFT, which is more code for a number the comparison does not need.

## 12. Request options differ per backend, and the README says so

Decision: Claude calls send no temperature, set effort explicitly to `medium` (`AGENT_EFFORT`) and leave thinking at the model default. The Ollama path keeps temperature 0.6, `num_predict` 600 and `num_ctx` 4096.

Reason: current Claude models reject a temperature value, and the default effort differs between Haiku 5.5 and Sonnet 5.5, so setting it explicitly keeps the two Claude models on the same setting.

Rejected: claiming that both backends run with identical settings.

## 13. No server-side refusal fallback on the Claude path

Decision: a refusal (`stop_reason: refusal`) is recorded as its own outcome with its cost. The agent does not send `fallbacks`, so another model never answers in place of the pinned one.

Reason: a fallback model answering some questions would mix two models inside one scored run, the same problem as decision 4's rejected Haiku switch.

Rejected: Anthropic's server-side fallback, which is the SDK guidance for Sonnet 5.5 in general but would make the per-model results ambiguous here.
