"""Agent loop (S19): question -> LLM backend -> MCP tools -> answer.

Backends (LLM_BACKEND, one per deploy): `ollama` = local Ollama through the metrics proxy; `anthropic` = hosted Claude
through the anthropic SDK. Both sit behind chat(messages, tools) -> {content, tool_calls, usage}; the loop, the turn cap
and the ALLOWED check at listing and at execution are the same for both.
Ollama request recipe (worked out on qwen3:4b, used for the pinned instruct model; see the README, Model choice): never send `think`, terse system prompt,
num_predict 600, num_ctx 4096, stream=true (the proxy measures TTFT only on streams), ignore message.thinking.
Anthropic request: non-streaming, fixed max_tokens, effort set explicitly, automatic prompt caching, no temperature
(current Claude models reject it).
Logs: one line per tool call (tool, args hash, status, ms) and one per LLM call (the cost ledger: model, message id,
the four usage fields, cost). No arguments, no message text, no secrets.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import aiohttp
import anthropic
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest

import pricing

LLM_URL = os.environ.get("LLM_URL", "http://llm-proxy.llm.svc.cluster.local:8080/api/chat")
MCP_URL = os.environ.get("MCP_URL", "http://mcp-confluent.kafka:8080/mcp")
MODEL = os.environ.get("MODEL", "qwen3:4b-instruct-2507-q4_K_M")  # deploy sets it (Makefile AGENT_MODEL)
BACKEND = os.environ.get("LLM_BACKEND", "ollama")  # deploy sets it (Makefile AGENT_BACKEND)
EFFORT = os.environ.get("AGENT_EFFORT", "medium")  # Anthropic only; explicit, because the default differs per model
DAILY_MAX_USD = float(os.environ.get("AGENT_DAILY_MAX_USD", "5"))  # in-process cap; the Console limit is the outer one
MAX_TOOL_TURNS = 5
LLM_TIMEOUT, TOOL_TIMEOUT = 180, 30
# num_ctx is 4096 tokens: about 2000 chars (roughly 500 tokens) per tool result keeps system + tools + 5 results inside it.
MAX_RESULT_CHARS = 2000
MAX_DESC_CHARS = 160  # tool descriptions cost context too
# Read-only allow-list on the client side as well (the server allow-list is layer 1). No write tool, ever.
READ_TOOLS = {"list-topics", "list-consumer-groups", "describe-consumer-group", "get-consumer-group-lag", "get-partition-offsets"}


def allowed_tools(extra: str) -> set:
    return READ_TOOLS | {t.strip() for t in extra.split(",") if t.strip()}


# AGENT_EXTRA_TOOLS exists ONLY for the negative test (`make unsafe-demo` sets it to delete-topics). Default empty = closed.
ALLOWED = allowed_tools(os.environ.get("AGENT_EXTRA_TOOLS", ""))
RUN_RE = re.compile(r"[a-z0-9-]{1,24}")
SYSTEM = "Answer in one short sentence. Think as little as possible: do not plan or draft. If a tool fits, call it at once with no other text. If the question needs several steps, call the first tool now; do not plan the rest."
OPTIONS = {"num_ctx": 4096, "num_predict": 600, "temperature": 0.6}

log = logging.getLogger("agent")
REGISTRY = CollectorRegistry()
TOOL_CALLS = Counter("agent_tool_calls_total", "Tool calls by tool and status", ["tool", "status"], registry=REGISTRY)
QUESTIONS = Counter("agent_questions_total", "Questions by outcome", ["outcome"], registry=REGISTRY)
TURNS = Histogram("agent_tool_turns", "Tool turns per question", buckets=(0, 1, 2, 3, 4, 5), registry=REGISTRY)
LLM_TOKENS = Counter("agent_llm_tokens_total", "LLM tokens by model and kind (input, cache_read, cache_write, output)", ["model", "kind"], registry=REGISTRY)
LLM_COST = Counter("agent_llm_cost_usd_total", "LLM API cost in USD (Ollama: 0)", ["model"], registry=REGISTRY)
LLM_CALLS = Counter("agent_llm_calls_total", "LLM calls by model and outcome", ["model", "outcome"], registry=REGISTRY)
QUESTION_COST = Histogram("agent_question_cost_usd", "LLM API cost per question in USD", ["model"], registry=REGISTRY,
                          buckets=(0, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25))
USAGE_KINDS = ("input", "cache_read", "cache_write", "output")


# Confluent Cloud ids: useless for our direct connection. Left in, qwen3:4b spent all 600 tokens asking itself for
# them and never called the tool (measured in the S19 test).
DROP_PARAMS = {"cluster_id", "environment_id"}


def to_ollama_tools(mcp_tools) -> list:
    """The ONE place tools are listed for the model, for every backend (AnthropicBackend translates this list)."""
    out = []
    for t in mcp_tools:
        if t.name in ALLOWED:
            sch = t.inputSchema or {}
            props = {k: v for k, v in sch.get("properties", {}).items() if k not in DROP_PARAMS}
            params = {"type": "object", "properties": props, "required": sch.get("required", [])}
            out.append({"type": "function", "function": {
                "name": t.name, "description": (t.description or "")[:MAX_DESC_CHARS], "parameters": params}})
    return out


async def llm_chat(session: aiohttp.ClientSession, messages: list, tools: list, run: str) -> dict:
    """One streamed /api/chat call. Returns {content, tool_calls, usage}. Raises on HTTP error or timeout."""
    body = {"model": MODEL, "stream": True, "options": OPTIONS, "messages": messages, "tools": tools}
    content, calls, usage = "", [], dict.fromkeys(USAGE_KINDS, 0)
    async with session.post(LLM_URL, json=body, headers={"X-Run": run}, timeout=aiohttp.ClientTimeout(total=LLM_TIMEOUT)) as r:
        r.raise_for_status()
        async for line in r.content:
            if not line.strip():
                continue
            d = json.loads(line)
            msg = d.get("message") or {}  # message.thinking is ignored on purpose
            content += msg.get("content") or ""
            calls += msg.get("tool_calls") or []
            if d.get("done"):  # Ollama's own counts; no cache split
                usage.update(input=d.get("prompt_eval_count") or 0, output=d.get("eval_count") or 0)
    return {"content": content, "tool_calls": calls, "usage": usage}


class OllamaBackend:
    def __init__(self, session, run):
        self.session, self.run, self.model = session, run, MODEL

    async def chat(self, messages: list, tools: list) -> dict:
        return await llm_chat(self.session, messages, tools, self.run)


def to_anthropic(messages: list) -> tuple[str, list]:
    """Loop messages (Ollama shape) -> (system, Messages API messages). Assistant turns already hold the raw Claude
    blocks (thinking blocks must go back unchanged); tool results pair with the tool_use ids in order, one user turn."""
    system, out, ids = "", [], []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        elif m["role"] == "assistant":
            out.append({"role": "assistant", "content": m["content"]})
            ids = [b.id for b in m["content"] if b.type == "tool_use"]
        elif m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": ids.pop(0), "content": m["content"]}
            if out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return system, out


class AnthropicBackend:
    def __init__(self, client, model=None):
        self.client, self.model = client, model or MODEL

    async def chat(self, messages: list, tools: list) -> dict:
        system, msgs = to_anthropic(messages)
        kw = {"tools": [{"name": f["function"]["name"], "description": f["function"]["description"],
                         "input_schema": f["function"]["parameters"]} for f in tools]} if tools else {}
        r = await self.client.messages.create(
            model=self.model, max_tokens=pricing.MAX_OUTPUT_TOKENS, system=system, messages=msgs,
            output_config={"effort": EFFORT}, cache_control={"type": "ephemeral"}, **kw)
        u = r.usage
        return {"content": "".join(b.text for b in r.content if b.type == "text"),
                "tool_calls": [{"function": {"name": b.name, "arguments": b.input}} for b in r.content if b.type == "tool_use"],
                "usage": {"input": u.input_tokens, "cache_read": u.cache_read_input_tokens or 0,
                          "cache_write": u.cache_creation_input_tokens or 0, "output": u.output_tokens},
                "id": r.id, "outcome": "ok" if r.stop_reason in ("end_turn", "tool_use") else r.stop_reason,
                "message": {"role": "assistant", "content": r.content}}


class BudgetExceeded(Exception):
    pass


_SPEND = {"day": None, "usd": 0.0}


def spend_today(add: float = 0.0) -> float:
    """In-process daily spend (UTC day). One replica, so overshoot is at most concurrency x one question's cost."""
    day = datetime.now(timezone.utc).date()
    if _SPEND["day"] != day:
        _SPEND.update(day=day, usd=0.0)
    _SPEND["usd"] += add
    return _SPEND["usd"]


async def chat_accounted(backend, messages: list, tools: list, run: str) -> tuple[dict, float]:
    """One LLM call with budget check, metrics and one ledger log line. Returns (reply, cost in USD)."""
    if spend_today() >= DAILY_MAX_USD:
        raise BudgetExceeded
    t0 = time.monotonic()
    try:
        reply = await backend.chat(messages, tools)
    except Exception as e:  # counted and logged here, handled by ask()
        outcome = "rate_limited" if isinstance(e, anthropic.RateLimitError) else "error"
        LLM_CALLS.labels(backend.model, outcome).inc()
        log.info("llm model=%s msg_id=- run=%s outcome=%s ms=%d", backend.model, run, outcome, (time.monotonic() - t0) * 1000)
        raise
    u, usd = reply["usage"], pricing.cost_usd(backend.model, reply["usage"])
    for k in USAGE_KINDS:
        LLM_TOKENS.labels(backend.model, k).inc(u[k])
    LLM_COST.labels(backend.model).inc(usd)
    LLM_CALLS.labels(backend.model, reply.get("outcome", "ok")).inc()
    spend_today(usd)
    log.info("llm model=%s msg_id=%s run=%s outcome=%s input=%d cache_read=%d cache_write=%d output=%d usd=%.6f ms=%d",
             backend.model, reply.get("id") or "-", run, reply.get("outcome", "ok"), u["input"], u["cache_read"],
             u["cache_write"], u["output"], usd, (time.monotonic() - t0) * 1000)
    return reply, usd


async def run_tool(mcp, name: str, args: dict) -> tuple[str, str]:
    """Execute one tool. Returns (status, text). Never raises: the model gets the error text back."""
    label = name if name in ALLOWED else "unknown"
    t0 = time.monotonic()
    if name not in ALLOWED:
        status, text = "unknown_tool", f"error: unknown tool {name}"
    else:
        try:
            res = await asyncio.wait_for(mcp.call_tool(name, args), TOOL_TIMEOUT)
            text = "".join(getattr(c, "text", "") for c in res.content)
            status = "error" if res.isError else "ok"
        except asyncio.TimeoutError:
            status, text = "timeout", f"error: tool timed out after {TOOL_TIMEOUT}s"
        except Exception as e:  # MCP or network failure: tell the model, keep the loop alive
            status, text = "error", f"error: {type(e).__name__}"
    TOOL_CALLS.labels(label, status).inc()
    h = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:12]
    log.info("tool=%s args_sha=%s status=%s ms=%d", label, h, status, (time.monotonic() - t0) * 1000)
    return status, text[:MAX_RESULT_CHARS]


async def ask(q: str, run: str, session, mcp, tools: list, backend=None) -> dict:
    backend = backend or OllamaBackend(session, run)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": q}]
    used, turns, answer, error, outcome = [], 0, "", None, "answered"
    usage, cost = dict.fromkeys(USAGE_KINDS, 0), 0.0
    try:
        while True:
            reply, usd = await chat_accounted(backend, messages, tools, run)
            cost += usd
            for k in USAGE_KINDS:
                usage[k] += reply["usage"][k]
            if not reply["tool_calls"]:
                answer = reply["content"].strip()
                if not answer and reply.get("outcome") == "refusal":
                    error, outcome = "model refused (stop_reason refusal)", "refusal"
                elif not answer:  # cap hit while thinking: a failed answer, not a crash
                    error, outcome = "empty answer (model hit the token cap while thinking)", "empty"
                break
            if turns == MAX_TOOL_TURNS:
                error, outcome = f"stopped: more than {MAX_TOOL_TURNS} tool turns", "turn_limit"
                break
            turns += 1
            # Anthropic replies carry their raw blocks in "message"; Ollama replies use the Ollama shape as before.
            messages.append(reply.get("message") or {"role": "assistant", "content": reply["content"], "tool_calls": reply["tool_calls"]})
            for c in reply["tool_calls"]:
                fn = c.get("function") or {}
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                status, text = await run_tool(mcp, name, args if isinstance(args, dict) else {})
                used.append({"tool": name, "args": args, "status": status})
                messages.append({"role": "tool", "tool_name": name, "content": text})
    except BudgetExceeded:
        error, outcome = f"daily LLM budget reached (AGENT_DAILY_MAX_USD={DAILY_MAX_USD})", "budget"
    except asyncio.TimeoutError:
        error, outcome = f"LLM call timed out after {LLM_TIMEOUT}s", "llm_error"
    except (aiohttp.ClientError, ValueError, anthropic.APIError) as e:
        error, outcome = f"LLM call failed: {type(e).__name__}", "llm_error"
    QUESTIONS.labels(outcome).inc()
    TURNS.observe(turns)
    QUESTION_COST.labels(backend.model).observe(cost)
    return {"answer": answer, "tool_calls": used, "turns": turns, "error": error, "usage": usage, "cost_usd": round(cost, 6)}


@asynccontextmanager
async def mcp_session():
    """Fresh MCP session per question (simple; the server is local). Key comes from env, never logged."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {"cflt-mcp-api-key": os.environ["MCP_API_KEY"]}
    async with streamablehttp_client(MCP_URL, headers=headers, timeout=TOOL_TIMEOUT) as (r, w, _):
        async with ClientSession(r, w, read_timeout_seconds=timedelta(seconds=TOOL_TIMEOUT)) as s:
            await s.initialize()
            yield s


async def handle_ask(request: web.Request) -> web.Response:
    try:
        j = await request.json()
        q, run = j["q"], j.get("run", "none")
        if not isinstance(q, str) or not q.strip() or len(q) > 2000 or not RUN_RE.fullmatch(str(run)):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": 'bad request: need {"q": "...", "run": "[a-z0-9-]{1,24}"}'}, status=400)
    try:
        async with mcp_session() as mcp:
            tools = to_ollama_tools((await mcp.list_tools()).tools)
            return web.json_response(await ask(q, run, request.app["session"], mcp, tools, request.app.get("claude")))
    except Exception as e:  # MCP unreachable etc.
        QUESTIONS.labels("mcp_error").inc()
        return web.json_response({"answer": "", "tool_calls": [], "turns": 0, "error": f"MCP failed: {type(e).__name__}",
                                  "usage": dict.fromkeys(USAGE_KINDS, 0), "cost_usd": 0.0}, status=502)


async def metrics(request): return web.Response(body=generate_latest(REGISTRY), headers={"Content-Type": CONTENT_TYPE_LATEST})
async def healthz(request): return web.Response(text="ok")


def make_app() -> web.Application:
    app = web.Application()
    # Export this model's cost series at 0 from pod start, so increase() in Grafana also counts the first question after a restart.
    for k in USAGE_KINDS:
        LLM_TOKENS.labels(MODEL, k)
    LLM_COST.labels(MODEL)
    QUESTION_COST.labels(MODEL)
    if BACKEND not in ("ollama", "anthropic"):
        sys.exit(f"LLM_BACKEND must be ollama or anthropic, got {BACKEND!r}")
    if BACKEND == "anthropic":
        if MODEL not in pricing.PRICES:  # an unpriced Claude model would count as $0: refuse to start
            sys.exit(f"MODEL {MODEL!r} has no price in pricing.py")
        # Key from env ANTHROPIC_API_KEY (optional k8s Secret anthropic-api-key). The SDK would only fail at the first
        # call, so a missing key stops the pod here (CrashLoop is visible in `make check`). The key is never logged.
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY is not set: run make anthropic-key, then agent-deploy again")
        app["claude"] = AnthropicBackend(anthropic.AsyncAnthropic(timeout=LLM_TIMEOUT))

    async def session_ctx(app):
        app["session"] = aiohttp.ClientSession()
        yield
        await app["session"].close()

    app.cleanup_ctx.append(session_ctx)
    app.add_routes([web.post("/ask", handle_ask), web.get("/metrics", metrics), web.get("/healthz", healthz)])
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    log.setLevel(logging.INFO)  # only our tool and llm lines; httpx/mcp/aiohttp access logs stay quiet
    web.run_app(make_app(), port=int(os.environ.get("PORT", "8080")), print=None, access_log=None)
