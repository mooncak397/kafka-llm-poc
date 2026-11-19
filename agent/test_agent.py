"""Fake Ollama (real HTTP, ndjson stream) + fake MCP session. No cluster needed."""
import json
from types import SimpleNamespace as NS

import anthropic
import pytest
from aiohttp import web

import agent


class FakeMCP:
    def __init__(self, fail=False, text="ok-result"):
        self.calls, self.fail, self.text = [], fail, text

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return NS(content=[NS(text=self.text)], isError=self.fail)


def tool_call(name, **args):
    return {"function": {"name": name, "arguments": args}}


@pytest.fixture
async def env(aiohttp_client, monkeypatch):
    """script = list of replies; each reply = (content, tool_calls). Requests are kept in `seen`."""
    state = NS(script=[], seen=[])

    async def chat(request):
        state.seen.append((await request.json(), request.headers.get("X-Run")))
        content, calls = state.script.pop(0)
        resp = web.StreamResponse()
        await resp.prepare(request)
        # thinking chunk first (must be ignored), then content, then tool_calls in a separate chunk
        for msg in ({"thinking": "hmm"}, {"content": content}, {"tool_calls": calls} if calls else {}):
            await resp.write(json.dumps({"message": msg, "done": False}).encode() + b"\n")
        await resp.write(b'{"done": true, "prompt_eval_count": 300, "eval_count": 20}\n')
        return resp

    app = web.Application()
    app.router.add_post("/api/chat", chat)
    client = await aiohttp_client(app)
    monkeypatch.setattr(agent, "LLM_URL", str(client.make_url("/api/chat")))
    state.session = client.session
    return state


async def run(env, mcp, q="q"):
    return await agent.ask(q, "u1", env.session, mcp, [])


async def test_one_tool_call(env):
    env.script = [("", [tool_call("list-topics")]), ("three topics", [])]
    mcp = FakeMCP()
    r = await run(env, mcp)
    old = {k: r[k] for k in ("answer", "tool_calls", "turns", "error")}  # the pre-cost fields are unchanged
    assert old == {"answer": "three topics", "tool_calls": [{"tool": "list-topics", "args": {}, "status": "ok"}], "turns": 1, "error": None}
    assert r["usage"] == {"input": 600, "cache_read": 0, "cache_write": 0, "output": 40} and r["cost_usd"] == 0.0  # Ollama: $0
    assert env.seen[0][1] == "u1"  # X-Run header
    assert "think" not in env.seen[0][0] and env.seen[0][0]["options"]["num_predict"] == 600
    last = env.seen[1][0]["messages"][-1]  # result went back as a tool message
    assert last == {"role": "tool", "tool_name": "list-topics", "content": "ok-result"}


async def test_two_tool_calls_in_one_turn(env):
    env.script = [("", [tool_call("list-consumer-groups"), tool_call("get-consumer-group-lag", groupId="orders-slow")]), ("done", [])]
    mcp = FakeMCP()
    r = await run(env, mcp)
    assert r["turns"] == 1 and [c["tool"] for c in r["tool_calls"]] == ["list-consumer-groups", "get-consumer-group-lag"]
    assert mcp.calls[1] == ("get-consumer-group-lag", {"groupId": "orders-slow"})
    assert [m["role"] for m in env.seen[1][0]["messages"]].count("tool") == 2


async def test_five_turn_limit(env):
    env.script = [("", [tool_call("list-topics")])] * 6
    r = await run(env, FakeMCP())
    assert r["turns"] == 5 and r["answer"] == "" and "tool turns" in r["error"]


async def test_tool_error_goes_back_to_model(env):
    env.script = [("", [tool_call("list-topics")]), ("could not read", [])]
    r = await run(env, FakeMCP(fail=True))
    assert r["tool_calls"][0]["status"] == "error" and r["answer"] == "could not read" and r["error"] is None


async def test_unknown_tool_is_not_executed(env):
    env.script = [("", [tool_call("delete-topic", topic="x")]), ("no", [])]
    mcp = FakeMCP()
    r = await run(env, mcp)
    assert mcp.calls == [] and r["tool_calls"][0]["status"] == "unknown_tool"


async def test_empty_final_content(env):
    env.script = [("", [])]
    r = await run(env, FakeMCP())
    assert r["answer"] == "" and "empty answer" in r["error"] and r["turns"] == 0


async def test_long_result_is_trimmed(env):
    env.script = [("", [tool_call("list-topics")]), ("ok", [])]
    await run(env, FakeMCP(text="x" * 5000))
    assert len(env.seen[1][0]["messages"][-1]["content"]) == agent.MAX_RESULT_CHARS


async def test_llm_down_is_an_error_not_a_crash(env, monkeypatch):
    monkeypatch.setattr(agent, "LLM_URL", "http://127.0.0.1:1/api/chat")
    r = await run(env, FakeMCP())
    assert r["error"].startswith("LLM call failed")


async def test_log_has_no_args(env, caplog):
    caplog.set_level("INFO", logger="agent")
    env.script = [("", [tool_call("get-consumer-group-lag", groupId="SECRETGROUP")]), ("x", [])]
    await run(env, FakeMCP())
    assert "tool=get-consumer-group-lag" in caplog.text and "SECRETGROUP" not in caplog.text and "args_sha=" in caplog.text


def test_tool_schema_drops_cloud_params():
    t = NS(name="get-consumer-group-lag", description="d" * 500, inputSchema={
        "$schema": "x", "type": "object", "required": ["groupId"],
        "properties": {"groupId": {"type": "string"}, "cluster_id": {"type": "string"}, "environment_id": {"type": "string"}}})
    other = NS(name="create-topics", description="", inputSchema={})
    out = agent.to_ollama_tools([t, other])
    assert [o["function"]["name"] for o in out] == ["get-consumer-group-lag"]  # write tool is filtered out
    f = out[0]["function"]
    assert list(f["parameters"]["properties"]) == ["groupId"] and f["parameters"]["required"] == ["groupId"]
    assert len(f["description"]) == agent.MAX_DESC_CHARS


def test_extra_tools_opt_in(monkeypatch):
    assert agent.allowed_tools("") == agent.READ_TOOLS and "delete-topics" not in agent.ALLOWED  # default closed
    assert agent.allowed_tools("delete-topics") == agent.READ_TOOLS | {"delete-topics"}
    monkeypatch.setattr(agent, "ALLOWED", agent.allowed_tools("delete-topics"))
    out = agent.to_ollama_tools([NS(name="delete-topics", description="", inputSchema={})])
    assert [o["function"]["name"] for o in out] == ["delete-topics"]


# ---- /ask over HTTP: response shape ----
async def test_ask_endpoint_shape(env, aiohttp_client, monkeypatch):
    from contextlib import asynccontextmanager

    class ListingMCP(FakeMCP):
        async def list_tools(self):
            return NS(tools=[NS(name="list-topics", description="", inputSchema={})])

    @asynccontextmanager
    async def fake_session():
        yield ListingMCP()

    monkeypatch.setattr(agent, "mcp_session", fake_session)
    env.script = [("", [tool_call("list-topics")]), ("three topics", [])]
    c = await aiohttp_client(agent.make_app())
    j = await (await c.post("/ask", json={"q": "Which topics exist?", "run": "u1"})).json()
    assert set(j) == {"answer", "tool_calls", "turns", "error", "usage", "cost_usd"}
    assert (j["answer"], j["turns"], j["error"], j["tool_calls"][0]["tool"]) == ("three topics", 1, None, "list-topics")
    assert (await c.post("/ask", json={"q": ""})).status == 400


# ---- Anthropic backend: fake client, responses shaped like the Messages API (no network, no key) ----
from anthropic.types import Message  # noqa: E402

import pricing  # noqa: E402

SONNET = "claude-sonnet-5-5"
ALL_SERVER_TOOLS = [NS(name=n, description="d", inputSchema={"type": "object", "properties": {}}) for n in
                    sorted(agent.READ_TOOLS | {"create-topics", "delete-topics", "get-topic-config"})]


def claude_msg(content, stop="end_turn", usage=None, mid="msg_01"):
    """Usage fixtures follow the raw API: input_tokens is the uncached rest, the cache fields are separate."""
    u = usage or {"input_tokens": 50, "cache_creation_input_tokens": 1500, "cache_read_input_tokens": 0, "output_tokens": 60}
    return Message.model_validate({"id": mid, "type": "message", "role": "assistant", "model": SONNET, "stop_reason": stop,
                                   "stop_sequence": None, "content": content, "usage": u})


UNCACHED = {"input_tokens": 50, "cache_creation_input_tokens": 1500, "cache_read_input_tokens": 0, "output_tokens": 60}
CACHED = {"input_tokens": 40, "cache_creation_input_tokens": 700, "cache_read_input_tokens": 1500, "output_tokens": 30}
THINK = {"type": "thinking", "thinking": "", "signature": "sig-1"}


class FakeClaude:
    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        self.messages = self

    async def create(self, **kw):
        self.requests.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


async def ask_claude(fake, mcp, tools=None):
    return await agent.ask("Which topics exist?", "u1", None, mcp, agent.to_ollama_tools(tools or ALL_SERVER_TOOLS),
                           agent.AnthropicBackend(fake, SONNET))


async def test_anthropic_request_tools_equal_allowed():
    fake = FakeClaude(claude_msg([{"type": "text", "text": "three topics"}]))
    await ask_claude(fake, FakeMCP())
    req = fake.requests[0]
    assert {t["name"] for t in req["tools"]} == agent.ALLOWED  # write tools and get-topic-config never reach the model
    assert req["model"] == SONNET and req["max_tokens"] == pricing.MAX_OUTPUT_TOKENS
    assert req["output_config"] == {"effort": agent.EFFORT} and req["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in req and "stream" not in req
    assert req["system"] == agent.SYSTEM and req["messages"] == [{"role": "user", "content": "Which topics exist?"}]


async def test_anthropic_unknown_tool_is_not_executed():
    fake = FakeClaude(claude_msg([{"type": "tool_use", "id": "toolu_x", "name": "delete-topics", "input": {"topicNames": ["x"]}}], "tool_use"),
                      claude_msg([{"type": "text", "text": "no"}], usage=CACHED, mid="msg_02"))
    mcp = FakeMCP()
    r = await ask_claude(fake, mcp)
    assert mcp.calls == [] and r["tool_calls"][0]["status"] == "unknown_tool"
    result = fake.requests[1]["messages"][-1]["content"][0]  # the model still gets a tool_result for its tool_use id
    assert result["tool_use_id"] == "toolu_x" and result["content"].startswith("error: unknown tool")


async def test_anthropic_loop_round_trip_and_cost(caplog):
    caplog.set_level("INFO", logger="agent")
    before = agent.REGISTRY.get_sample_value("agent_llm_cost_usd_total", {"model": SONNET}) or 0.0
    first = claude_msg([THINK, {"type": "tool_use", "id": "toolu_1", "name": "list-consumer-groups", "input": {}},
                        {"type": "tool_use", "id": "toolu_2", "name": "get-consumer-group-lag", "input": {"groupId": "orders-slow"}}],
                       "tool_use", UNCACHED, "msg_A1")
    fake = FakeClaude(first, claude_msg([{"type": "text", "text": "lag is 14000"}], usage=CACHED, mid="msg_A2"))
    mcp = FakeMCP()
    r = await ask_claude(fake, mcp)
    assert r["answer"] == "lag is 14000" and r["turns"] == 1 and r["error"] is None
    assert mcp.calls == [("list-consumer-groups", {}), ("get-consumer-group-lag", {"groupId": "orders-slow"})]
    sent = fake.requests[1]["messages"]
    assert sent[1] == {"role": "assistant", "content": first.content}  # thinking block goes back unchanged
    assert [b["tool_use_id"] for b in sent[2]["content"]] == ["toolu_1", "toolu_2"] and len(sent) == 3  # one user turn
    # cost: uncached call (50*2 + 1500*2.5 + 60*10) + cached call (40*2 + 1500*0.10 + 700*2.5 + 30*10), per million
    assert r["usage"] == {"input": 90, "cache_read": 1500, "cache_write": 2200, "output": 90}
    assert r["cost_usd"] == round((4450 + 2280) / 1e6, 6)
    after = agent.REGISTRY.get_sample_value("agent_llm_cost_usd_total", {"model": SONNET})
    assert abs(after - before - r["cost_usd"]) < 1e-9
    assert "msg_id=msg_A1" in caplog.text and "msg_id=msg_A2" in caplog.text and "cache_read=1500" in caplog.text
    assert "Which topics" not in caplog.text and "orders-slow" not in caplog.text  # no prompt or argument text


async def test_anthropic_rate_limit_is_counted(caplog):
    import httpx2
    err = anthropic.RateLimitError("429", response=httpx2.Response(429, request=httpx2.Request("POST", "https://x")), body=None)
    before = agent.REGISTRY.get_sample_value("agent_llm_calls_total", {"model": SONNET, "outcome": "rate_limited"}) or 0.0
    r = await ask_claude(FakeClaude(err), FakeMCP())
    assert r["error"] == "LLM call failed: RateLimitError" and r["cost_usd"] == 0.0
    assert agent.REGISTRY.get_sample_value("agent_llm_calls_total", {"model": SONNET, "outcome": "rate_limited"}) == before + 1


async def test_anthropic_refusal_is_its_own_error():
    r = await ask_claude(FakeClaude(claude_msg([], "refusal")), FakeMCP())
    assert r["answer"] == "" and "refused" in r["error"] and r["cost_usd"] > 0  # a refusal is still billed


async def test_daily_budget_stops_before_the_call(monkeypatch):
    monkeypatch.setattr(agent, "DAILY_MAX_USD", 0.0)
    fake = FakeClaude()
    r = await ask_claude(fake, FakeMCP())
    assert fake.requests == [] and "budget" in r["error"]


def test_daily_counter_resets_on_a_new_day(monkeypatch):
    monkeypatch.setattr(agent, "_SPEND", {"day": None, "usd": 0.0})
    agent.spend_today(1.5)
    assert agent.spend_today() == 1.5
    agent._SPEND["day"] = agent._SPEND["day"].replace(year=2000)
    assert agent.spend_today() == 0.0


# ---- cost function ----
def u(i=0, cr=0, cw=0, o=0):
    return {"input": i, "cache_read": cr, "cache_write": cw, "output": o}


def test_cost_from_the_four_fields():
    assert pricing.cost_usd(SONNET, u(i=1_000_000)) == 2.0
    assert pricing.cost_usd(SONNET, u(cr=1_000_000)) == 0.10   # NOT 2.10: cache reads are not inside input
    assert pricing.cost_usd(SONNET, u(cw=1_000_000)) == 2.5
    assert pricing.cost_usd(SONNET, u(o=1_000_000)) == 10.0
    assert abs(pricing.cost_usd("claude-haiku-5-5", u(i=1000, cr=2000, cw=500, o=100)) - (100 + 20 + 62.5 + 50) / 1e6) < 1e-12


def test_cost_edges():
    assert pricing.cost_usd(agent.MODEL, u(i=10**6, o=10**6)) == 0.0          # Ollama model: $0 API cost
    long = pricing.cost_usd("claude-haiku-5-5", u(i=60_000, cr=50_000, o=1000))  # prompt over 100k: higher Haiku prices
    assert abs(long - (60_000 * 0.50 + 50_000 * 0.05 + 1000 * 2.50) / 1e6) < 1e-12
    assert 0.1 < pricing.worst_case_question_usd(SONNET) < 0.2 and pricing.worst_case_question_usd(agent.MODEL) == 0.0
    assert pricing.CALLS_PER_QUESTION == agent.MAX_TOOL_TURNS + 1
    assert pricing.PRICE_SOURCE.startswith("https://") and pricing.PRICE_CHECKED
