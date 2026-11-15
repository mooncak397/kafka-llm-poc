"""Fake Ollama (real HTTP, ndjson stream) + fake MCP session. No cluster needed."""
import json
from types import SimpleNamespace as NS

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
        await resp.write(b'{"done": true}\n')
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
    assert r == {"answer": "three topics", "tool_calls": [{"tool": "list-topics", "args": {}, "status": "ok"}], "turns": 1, "error": None}
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
