import asyncio
import json

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

import proxy

FINAL = {"done": True, "prompt_eval_count": 7, "eval_count": 3}
NS = 1e9


async def fake_chat(request):
    body = await request.json()
    if body.get("model") == "boom":
        return web.json_response({"error": "model not found"}, status=404)
    fk = body.get("fake", {})
    final = {**FINAL, **fk.get("final", {})}
    if not body.get("stream", True):
        await asyncio.sleep(fk.get("delay", 0))
        return web.json_response({"message": {"content": "hi"}, **final})
    r = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
    await r.prepare(request)
    # a leading chunk with no content (thinking only) must not count as the first token
    await r.write(json.dumps({"message": {"content": "", "thinking": "hm"}, "done": False}).encode() + b"\n")
    await asyncio.sleep(fk.get("delay", 0))
    first = {"tool_calls": [{"function": {"name": "f"}}]} if fk.get("tool") else {"content": "a"}
    await r.write(json.dumps({"message": first, "done": False}).encode() + b"\n")
    await r.write(json.dumps({"message": {"content": "b"}, "done": False}).encode() + b"\n")
    if body.get("model") == "slow":  # never finishes: client must leave
        await asyncio.sleep(30)
    await r.write(json.dumps({"message": {"content": ""}, **final}).encode() + b"\n")
    return r


@pytest.fixture
async def env(monkeypatch):
    fake = web.Application()
    fake.add_routes([web.post("/api/chat", fake_chat)])
    up = TestServer(fake)
    await up.start_server()
    monkeypatch.setattr(proxy, "UPSTREAM", f"http://127.0.0.1:{up.port}")
    app = proxy.make_app()
    px = TestServer(app)
    await px.start_server()
    async with ClientSession() as c:
        yield app["m"], c, f"http://127.0.0.1:{px.port}"
    await px.close()
    await up.close()


def val(m, k, **lbl):
    c = m[k]
    return (c.labels(**lbl) if lbl else c)._value.get()


async def test_stream_and_tokens(env):
    m, c, u = env
    async with c.post(u + "/api/chat", json={"model": "x"}) as r:
        lines = (await r.read()).splitlines()
    assert len(lines) == 4 and json.loads(lines[-1])["done"]
    assert val(m, "requests", status="200", run="none") == 1
    assert val(m, "prompt") == 7 and val(m, "generated") == 3 and val(m, "in_flight") == 0


async def test_non_stream_body_unchanged(env):
    m, c, u = env
    async with c.post(u + "/api/chat", json={"model": "x", "stream": False}) as r:
        assert (await r.json())["message"]["content"] == "hi"
    assert val(m, "generated") == 3


async def test_upstream_error(env):
    m, c, u = env
    async with c.post(u + "/api/chat", json={"model": "boom"}) as r:
        assert r.status == 404
    assert val(m, "requests", status="404", run="none") == 1 and val(m, "errors") == 1 and val(m, "in_flight") == 0


async def test_upstream_down(env, monkeypatch):
    m, c, u = env
    monkeypatch.setattr(proxy, "UPSTREAM", "http://127.0.0.1:1")
    async with c.post(u + "/api/chat", json={"model": "x"}) as r:
        assert r.status == 502
    assert val(m, "requests", status="502", run="none") == 1 and val(m, "errors") == 1 and val(m, "in_flight") == 0


async def test_client_disconnect_midstream(env):
    m, c, u = env
    r = await c.post(u + "/api/chat", json={"model": "slow"})
    await r.content.readline()
    assert val(m, "in_flight") == 1
    r.close()  # drop the connection while upstream still streams
    for _ in range(50):
        if val(m, "in_flight") == 0:
            break
        await asyncio.sleep(0.1)
    assert val(m, "in_flight") == 0
    assert val(m, "requests", status="client_closed", run="none") == 1


async def test_metrics_endpoint(env):
    _, c, u = env
    async with c.get(u + "/metrics") as r:
        assert "llm_in_flight" in await r.text()


def hist(m, k, run):
    """(count, sum) of one histogram series; (0, 0) when the series does not exist."""
    n = sum_ = 0
    for smp in m[k].collect()[0].samples:
        if smp.labels.get("run") == run:
            if smp.name.endswith("_count"):
                n = smp.value
            elif smp.name.endswith("_sum"):
                sum_ = smp.value
    return n, sum_


def chat(c, u, run=None, **fake):
    h = {"X-Run": run} if run is not None else {}
    stream = fake.pop("stream", True)
    return c.post(u + "/api/chat", json={"model": "x", "stream": stream, "fake": fake}, headers=h)


async def test_ttft_skips_empty_chunk_and_counts_tool_calls(env):
    m, c, u = env
    async with chat(c, u, "t1", delay=0.4) as r:
        await r.read()
    n, s_ = hist(m, "ttft", "t1")
    assert n == 1 and 0.4 <= s_ < 1.0  # the empty first chunk came at about 0
    async with chat(c, u, "t2", delay=0.4, tool=True) as r:
        await r.read()
    n, s_ = hist(m, "ttft", "t2")
    assert n == 1 and 0.4 <= s_ < 1.0


async def test_queue_wait_derived_and_load(env):
    m, c, u = env
    # TTFT about 0.5 s, load 0.1 s + prompt eval 0.1 s -> queue about 0.3 s
    fin = {"load_duration": 0.1 * NS, "prompt_eval_duration": 0.1 * NS, "eval_count": 3, "eval_duration": 0.5 * NS}
    async with chat(c, u, "q1", delay=0.5, final=fin) as r:
        await r.read()
    n, s_ = hist(m, "queue", "q1")
    assert n == 1 and 0.28 <= s_ < 0.6
    assert hist(m, "load", "q1") == (1, pytest.approx(0.1))
    # clamp: Ollama says it worked longer than we waited -> 0, never negative
    fin = {"load_duration": 5 * NS, "prompt_eval_duration": 5 * NS}
    async with chat(c, u, "q2", delay=0.1, final=fin) as r:
        await r.read()
    assert hist(m, "queue", "q2") == (1, 0)


async def test_tokens_per_second(env):
    m, c, u = env
    fin = {"eval_count": 40, "eval_duration": 2 * NS}  # 20 tokens/s
    async with chat(c, u, "tp", final=fin) as r:
        await r.read()
    assert hist(m, "tps", "tp") == (1, pytest.approx(20.0))
    fin = {"eval_count": 0, "eval_duration": 0}  # no division by zero, nothing observed
    async with chat(c, u, "tp0", final=fin) as r:
        await r.read()
    assert hist(m, "tps", "tp0") == (0, 0)


async def test_run_label(env):
    m, c, u = env
    async with chat(c, u, "test") as r:
        await r.read()
    async with chat(c, u) as r:  # no header
        await r.read()
    for bad in ("UPPER", "has space", "a" * 25, "x;y", ""):
        async with chat(c, u, bad) as r:
            await r.read()
    assert val(m, "requests", status="200", run="test") == 1
    assert val(m, "requests", status="200", run="none") == 1
    assert val(m, "requests", status="200", run="invalid") == 5
    assert hist(m, "ttft", "test")[0] == 1 and hist(m, "ttft", "invalid")[0] == 5
    assert run_ok("a" * 24) and not run_ok("a" * 25)


def run_ok(v):
    return proxy.run_label(v) == v


async def test_non_stream_no_ttft(env):
    m, c, u = env
    fin = {"load_duration": 0.2 * NS, "eval_count": 10, "eval_duration": 1 * NS}
    async with chat(c, u, "ns", stream=False, delay=0.2, final=fin) as r:
        assert (await r.json())["message"]["content"] == "hi"
    assert hist(m, "ttft", "ns") == (0, 0) and hist(m, "queue", "ns") == (0, 0)
    assert hist(m, "tps", "ns") == (1, pytest.approx(10.0))
    assert hist(m, "load", "ns") == (1, pytest.approx(0.2))
    assert val(m, "requests", status="200", run="ns") == 1


async def test_disconnect_keeps_run_label(env):
    m, c, u = env
    r = await c.post(u + "/api/chat", json={"model": "slow"}, headers={"X-Run": "dc"})
    await r.content.readline()
    r.close()
    for _ in range(50):
        if val(m, "in_flight") == 0:
            break
        await asyncio.sleep(0.1)
    assert val(m, "in_flight") == 0
    assert val(m, "requests", status="client_closed", run="dc") == 1
