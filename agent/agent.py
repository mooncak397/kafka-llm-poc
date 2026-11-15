"""Agent loop (S19): question -> Ollama (through the metrics proxy) -> MCP tools -> answer.

Request recipe (worked out on qwen3:4b, used for the pinned instruct model; see the README, Model choice): never send `think`, terse system prompt,
num_predict 600, num_ctx 4096, stream=true (the proxy measures TTFT only on streams), ignore message.thinking.
Logs: one line per tool call (tool, args hash, status, ms). No arguments, no message text, no secrets.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import timedelta

import aiohttp
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest

LLM_URL = os.environ.get("LLM_URL", "http://llm-proxy.llm.svc.cluster.local:8080/api/chat")
MCP_URL = os.environ.get("MCP_URL", "http://mcp-confluent.kafka:8080/mcp")
MODEL = os.environ.get("MODEL", "qwen3:4b-instruct-2507-q4_K_M")  # deploy sets it (Makefile AGENT_MODEL)
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


# Confluent Cloud ids: useless for our direct connection. Left in, qwen3:4b spent all 600 tokens asking itself for
# them and never called the tool (measured in the S19 test).
DROP_PARAMS = {"cluster_id", "environment_id"}


def to_ollama_tools(mcp_tools) -> list:
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
    """One streamed /api/chat call. Returns {content, tool_calls}. Raises on HTTP error or timeout."""
    body = {"model": MODEL, "stream": True, "options": OPTIONS, "messages": messages, "tools": tools}
    content, calls = "", []
    async with session.post(LLM_URL, json=body, headers={"X-Run": run}, timeout=aiohttp.ClientTimeout(total=LLM_TIMEOUT)) as r:
        r.raise_for_status()
        async for line in r.content:
            if not line.strip():
                continue
            msg = json.loads(line).get("message") or {}  # message.thinking is ignored on purpose
            content += msg.get("content") or ""
            calls += msg.get("tool_calls") or []
    return {"content": content, "tool_calls": calls}


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


async def ask(q: str, run: str, session, mcp, tools: list) -> dict:
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": q}]
    used, turns, answer, error, outcome = [], 0, "", None, "answered"
    try:
        while True:
            reply = await llm_chat(session, messages, tools, run)
            if not reply["tool_calls"]:
                answer = reply["content"].strip()
                if not answer:  # cap hit while thinking: a failed answer, not a crash
                    error, outcome = "empty answer (model hit the token cap while thinking)", "empty"
                break
            if turns == MAX_TOOL_TURNS:
                error, outcome = f"stopped: more than {MAX_TOOL_TURNS} tool turns", "turn_limit"
                break
            turns += 1
            messages.append({"role": "assistant", "content": reply["content"], "tool_calls": reply["tool_calls"]})
            for c in reply["tool_calls"]:
                fn = c.get("function") or {}
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                status, text = await run_tool(mcp, name, args if isinstance(args, dict) else {})
                used.append({"tool": name, "args": args, "status": status})
                messages.append({"role": "tool", "tool_name": name, "content": text})
    except asyncio.TimeoutError:
        error, outcome = f"LLM call timed out after {LLM_TIMEOUT}s", "llm_error"
    except (aiohttp.ClientError, ValueError) as e:
        error, outcome = f"LLM call failed: {type(e).__name__}", "llm_error"
    QUESTIONS.labels(outcome).inc()
    TURNS.observe(turns)
    return {"answer": answer, "tool_calls": used, "turns": turns, "error": error}


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
            return web.json_response(await ask(q, run, request.app["session"], mcp, tools))
    except Exception as e:  # MCP unreachable etc.
        QUESTIONS.labels("mcp_error").inc()
        return web.json_response({"answer": "", "tool_calls": [], "turns": 0, "error": f"MCP failed: {type(e).__name__}"}, status=502)


async def metrics(request): return web.Response(body=generate_latest(REGISTRY), headers={"Content-Type": CONTENT_TYPE_LATEST})
async def healthz(request): return web.Response(text="ok")


def make_app() -> web.Application:
    app = web.Application()

    async def session_ctx(app):
        app["session"] = aiohttp.ClientSession()
        yield
        await app["session"].close()

    app.cleanup_ctx.append(session_ctx)
    app.add_routes([web.post("/ask", handle_ask), web.get("/metrics", metrics), web.get("/healthz", healthz)])
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    log.setLevel(logging.INFO)  # only our tool lines; httpx/mcp/aiohttp access logs stay quiet
    web.run_app(make_app(), port=int(os.environ.get("PORT", "8080")), print=None, access_log=None)
