"""LangChain demo agent over the SAME read-only mcp-confluent server. Not part of the measured path (DECISIONS.md, entry 1).

Stack: langchain.agents.create_agent + ChatAnthropic (claude-haiku-5-5) + langchain.mcp (beta, FastMCP 4 client) in its
own venv, so the main agent keeps mcp==1.27.1.
Read-only: the allow-list is applied before the model sees any tool (allowed_tools) and again when a tool call runs
(AllowListMiddleware). The list and the system prompt are read from agent/agent.py, so the demo cannot drift from it.
Cost: langchain-anthropic folds cache reads and cache writes INTO usage_metadata.input_tokens. raw_usage() takes them
out again, then agent/pricing.py prices the four raw fields. One log line per LLM call, keyed by the Anthropic
message id (a repeated id is not counted twice). No prompt text in the logs.
"""
import ast
import asyncio
import base64
import logging
import re
import subprocess
import sys
import warnings
from contextlib import asynccontextmanager
from pathlib import Path

from langchain.agents import create_agent  # noqa: E402
from langchain.agents.middleware import AgentMiddleware  # noqa: E402
from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "loadtest"))
import pricing  # noqa: E402  the agent's price table, not a copy

# After the langchain imports (langchain_core resets warning filters). The README says that langchain.mcp is beta.
warnings.filterwarnings("ignore", message=".*langchain.mcp.*is in beta")

CTX = "kind-kafka-poc"
MODEL = "claude-haiku-5-5"
MAX_TOOL_TURNS = 5  # same cap as the agent: at most 6 model calls per question
log = logging.getLogger("langchain-demo")


def agent_constants(path=ROOT / "agent" / "agent.py") -> dict:
    """READ_TOOLS and SYSTEM from agent/agent.py, read with ast (importing it would need the agent's own venv)."""
    out = {}
    for node in ast.parse(Path(path).read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in ("READ_TOOLS", "SYSTEM"):
            out[node.targets[0].id] = ast.literal_eval(node.value)
    return out


_C = agent_constants()
ALLOWED, SYSTEM = frozenset(_C["READ_TOOLS"]), _C["SYSTEM"]  # no AGENT_EXTRA_TOOLS here: the demo has no unsafe profile


def allowed_tools(tools: list) -> list:
    """Check 1: only allow-listed tools are ever given to the model."""
    return [t for t in tools if t.name in ALLOWED]


class AllowListMiddleware(AgentMiddleware):
    """Check 2: a tool call outside the allow-list is answered with an error and never executed."""

    @staticmethod
    def _deny(request):
        name = request.tool_call["name"]
        if name not in ALLOWED:
            return ToolMessage(content=f"error: unknown tool {name}", tool_call_id=request.tool_call["id"], name=name, status="error")
        return None

    def wrap_tool_call(self, request, handler):
        return self._deny(request) or handler(request)

    async def awrap_tool_call(self, request, handler):
        return self._deny(request) or await handler(request)


def raw_usage(um: dict) -> dict:
    """usage_metadata -> the four raw Anthropic fields. langchain-anthropic adds cache_read and cache writes into
    input_tokens; pricing input_tokens at the base rate as well would charge cache tokens twice."""
    d = um.get("input_token_details") or {}
    cache_read = d.get("cache_read") or 0
    # cache_creation is set to 0 when the TTL split is present, so the sum counts each write once.
    # shortcut: 1-hour writes are priced like 5-minute ones (pricing.py has one write price); the demo uses 5-minute caching only.
    cache_write = (d.get("cache_creation") or 0) + (d.get("ephemeral_5m_input_tokens") or 0) + (d.get("ephemeral_1h_input_tokens") or 0)
    return {"input": um["input_tokens"] - cache_read - cache_write, "cache_read": cache_read, "cache_write": cache_write,
            "output": um["output_tokens"]}


class CostHandler(BaseCallbackHandler):
    """One ledger line per LLM call, keyed by the Anthropic message id; totals for the current question."""
    run_inline = True

    def __init__(self, model=MODEL, run="demo"):
        self.model, self.run, self.seen = model, run, set()
        self.reset()

    def reset(self):
        self.usd, self.calls = 0.0, 0
        self.usage = dict.fromkeys(("input", "cache_read", "cache_write", "output"), 0)

    def on_llm_end(self, response, **kwargs):
        for gens in response.generations:
            for g in gens:
                m = getattr(g, "message", None)
                if not isinstance(m, AIMessage) or not m.usage_metadata:
                    continue
                mid = m.response_metadata.get("id") or m.id
                if mid in self.seen:  # the same billed message reported twice
                    continue
                self.seen.add(mid)
                u = raw_usage(m.usage_metadata)
                usd = pricing.cost_usd(self.model, u)
                self.usd += usd
                self.calls += 1
                for k in u:
                    self.usage[k] += u[k]
                log.info("llm model=%s msg_id=%s run=%s input=%d cache_read=%d cache_write=%d output=%d usd=%.6f",
                         self.model, mid, self.run, u["input"], u["cache_read"], u["cache_write"], u["output"], usd)


def secret(ns: str, name: str) -> str:
    """A k8s Secret value (key `key`), read in-process. Never printed, never written to a file."""
    raw = subprocess.run(["kubectl", "--context", CTX, "-n", ns, "get", "secret", name, "-o", "jsonpath={.data.key}"],
                         capture_output=True, text=True, check=True).stdout
    return base64.b64decode(raw).decode()


def chat_model():
    import os
    from langchain_anthropic import ChatAnthropic
    # Same request options as the main agent's Claude path: not streamed, max_tokens 1024, effort medium,
    # automatic prompt caching, no temperature. Key: ANTHROPIC_API_KEY, else the agent's Secret (never logged).
    key = os.environ.get("ANTHROPIC_API_KEY") or secret("llm", "anthropic-api-key")
    return ChatAnthropic(model=MODEL, max_tokens=pricing.MAX_OUTPUT_TOKENS, output_config={"effort": "medium"},
                         model_kwargs={"cache_control": {"type": "ephemeral"}}, max_retries=2, api_key=key)


def build_agent(tools: list, model=None):
    bound = allowed_tools(tools)
    return create_agent(model or chat_model(), bound, system_prompt=SYSTEM, middleware=[AllowListMiddleware()]), [t.name for t in bound]


async def ask(agent, q: str, handler: CostHandler) -> dict:
    """Same answer shape as the main agent's /ask, so loadtest/score.py can score it."""
    handler.reset()
    used, answer, error, turns = [], "", None, 0
    try:
        out = await agent.ainvoke({"messages": [{"role": "user", "content": q}]},
                                  config={"callbacks": [handler], "recursion_limit": 2 * MAX_TOOL_TURNS + 2})
        msgs = out["messages"]
        status = {m.tool_call_id: ("error" if m.status == "error" else "ok") for m in msgs if isinstance(m, ToolMessage)}
        for m in msgs:
            if isinstance(m, AIMessage) and m.tool_calls:
                turns += 1
                used += [{"tool": c["name"], "args": c["args"], "status": status.get(c["id"], "?")} for c in m.tool_calls]
        answer = msgs[-1].text.strip() if isinstance(msgs[-1], AIMessage) else ""
        if not answer:
            error = "empty answer"
    except Exception as e:  # turn cap (GraphRecursionError), API or MCP failure: a failed answer, not a crash
        error = f"{type(e).__name__}"
    return {"answer": answer, "tool_calls": used, "turns": turns, "error": error, "usage": dict(handler.usage),
            "cost_usd": round(handler.usd, 6), "llm_calls": handler.calls}


@asynccontextmanager
async def mcp_tools():
    """All tools the server lists, through a temporary port-forward (127.0.0.1). The MCP key is read from the k8s
    Secret in-process and only sent as a header: never printed, never written to a file."""
    from fastmcp.client import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from langchain.mcp import MCPAdapter
    key = secret("kafka", "mcp-api-key")
    p = await asyncio.create_subprocess_exec("kubectl", "--context", CTX, "-n", "kafka", "port-forward", "--address", "127.0.0.1",
                                             "svc/mcp-confluent", ":8080", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        async with asyncio.timeout(20):
            while not (m := re.search(rb"127\.0\.0\.1:(\d+)", await p.stdout.readline())):
                pass
        drain = asyncio.create_task(p.stdout.read())  # keep the pipe empty
        async with MCPAdapter(Client(StreamableHttpTransport(f"http://127.0.0.1:{m[1].decode()}/mcp", headers={"cflt-mcp-api-key": key}))) as a:
            yield await a.list_tools()
        drain.cancel()
    finally:
        p.terminate()
        await p.wait()


def setup_logging():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    log.setLevel(logging.INFO)
    # mcp-confluent answers the client's session DELETE with 400 after every session; harmless, the main agent logs it too.
    logging.getLogger("mcp.client.streamable_http").setLevel(logging.ERROR)
