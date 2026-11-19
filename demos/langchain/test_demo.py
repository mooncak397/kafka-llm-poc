"""Unit tests of the LangChain demo: no cluster, no API (`make langchain-test`)."""
import asyncio

from anthropic.types import Usage
from langchain_anthropic.chat_models import _create_usage_metadata
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from langchain_core.tools import StructuredTool

import demo
import score  # loadtest/score.py, on sys.path through demo


def fake_tool(name, calls):
    def run(**kw):
        calls.append((name, kw))
        return f"{name}-result"
    return StructuredTool.from_function(run, name=name, description=name, args_schema={"type": "object", "properties": {}})


SERVER = ["list-topics", "list-consumer-groups", "describe-consumer-group", "get-consumer-group-lag", "get-partition-offsets",
          "delete-topics", "create-topics", "get-topic-config"]


def usage_md(input_tokens, cache_read, cache_write, output):
    """usage_metadata exactly as langchain-anthropic builds it from a raw Anthropic Usage."""
    return _create_usage_metadata(Usage(input_tokens=input_tokens, cache_read_input_tokens=cache_read,
                                        cache_creation_input_tokens=cache_write, output_tokens=output))


class FakeChat(BaseChatModel):
    script: list = []
    bound: list = []

    @property
    def _llm_type(self):
        return "fake"

    def bind_tools(self, tools, **kw):
        self.bound.append(sorted(t.name for t in tools))
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


def ai(text="", calls=(), mid="msg_01", u=(50, 0, 1500, 60)):
    return AIMessage(content=text, tool_calls=[{"name": n, "args": a, "id": f"toolu_{i}"} for i, (n, a) in enumerate(calls)],
                     usage_metadata=usage_md(*u), response_metadata={"id": mid})


def run_agent(script, tools):
    model = FakeChat(script=list(script), bound=[])
    agent, bound = demo.build_agent(tools, model)
    return asyncio.run(demo.ask(agent, "Which topics exist?", demo.CostHandler())), model, bound


def test_allow_list_comes_from_the_agent():
    assert demo.ALLOWED == score.TOOLS == {"list-topics", "list-consumer-groups", "describe-consumer-group", "get-consumer-group-lag", "get-partition-offsets"}
    assert demo.SYSTEM.startswith("Answer in one short sentence.")


def test_bound_tools_equal_allow_list_even_with_write_tools_on_the_server():
    calls = []
    r, model, bound = run_agent([ai("three topics")], [fake_tool(n, calls) for n in SERVER])
    assert set(bound) == demo.ALLOWED and set(model.bound[0]) == demo.ALLOWED  # what the model actually got
    assert r["answer"] == "three topics" and r["tool_calls"] == []


def test_unknown_tool_is_not_executed():
    calls = []
    r, _, _ = run_agent([ai(calls=[("delete-topics", {"topicNames": ["bikes.orders"]})]), ai("no", mid="msg_02")],
                        [fake_tool(n, calls) for n in SERVER])
    assert calls == [] and r["tool_calls"] == [{"tool": "delete-topics", "args": {"topicNames": ["bikes.orders"]}, "status": "error"}]


def test_allowed_tool_runs_and_result_shape():
    calls = []
    r, _, _ = run_agent([ai(calls=[("list-topics", {})]), ai("three topics", mid="msg_02", u=(40, 1500, 700, 30))],
                        [fake_tool(n, calls) for n in SERVER])
    assert calls == [("list-topics", {})] and r["turns"] == 1 and r["error"] is None
    assert set(r) >= {"answer", "tool_calls", "turns", "error", "usage", "cost_usd"}
    assert r["usage"] == {"input": 90, "cache_read": 1500, "cache_write": 2200, "output": 90}
    # same raw numbers as the main agent's round-trip test, so the same dollars
    assert r["cost_usd"] == round(pricing_cost(50, 0, 1500, 60) + pricing_cost(40, 1500, 700, 30), 6)


def pricing_cost(i, cr, cw, o):
    return demo.pricing.cost_usd(demo.MODEL, {"input": i, "cache_read": cr, "cache_write": cw, "output": o})


def test_cost_matches_the_agent_and_avoids_the_double_count():
    for raw in [(50, 0, 1500, 60), (40, 1500, 700, 30), (12, 0, 0, 7)]:  # uncached first call, cached call, no cache
        um = usage_md(*raw)
        assert um["input_tokens"] == raw[0] + raw[1] + raw[2]  # the trap: langchain-anthropic folds cache tokens into input
        u = demo.raw_usage(um)
        assert (u["input"], u["cache_read"], u["cache_write"], u["output"]) == raw
        assert demo.pricing.cost_usd(demo.MODEL, u) == pricing_cost(*raw)
    naive = demo.pricing.cost_usd(demo.MODEL, {"input": 1540, "cache_read": 1500, "cache_write": 700, "output": 30})
    assert naive > pricing_cost(40, 1500, 700, 30)  # pricing usage_metadata.input_tokens as uncached charges cache tokens twice


def test_ttl_split_counts_each_write_once():
    um = _create_usage_metadata(Usage.model_validate({"input_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 900,
                                                      "cache_creation": {"ephemeral_5m_input_tokens": 900, "ephemeral_1h_input_tokens": 0},
                                                      "output_tokens": 5}))
    assert demo.raw_usage(um) == {"input": 10, "cache_read": 0, "cache_write": 900, "output": 5}


def test_message_id_is_the_idempotency_key(caplog):
    caplog.set_level("INFO", logger="langchain-demo")
    h = demo.CostHandler()
    res = LLMResult(generations=[[ChatGeneration(message=ai("x", mid="msg_same"))]])
    h.on_llm_end(res)
    h.on_llm_end(res)  # the same billed message reported twice
    assert h.calls == 1 and caplog.text.count("msg_id=msg_same") == 1 and "Which topics" not in caplog.text


def test_middleware_alone_blocks_a_write_tool():
    """Check 2 on its own: give the agent ALL server tools (check 1 bypassed on purpose); delete-topics must still not run."""
    from langchain.agents import create_agent
    calls = []
    model = FakeChat(script=[ai(calls=[("delete-topics", {"topicNames": ["bikes.orders"]})]), ai("no", mid="msg_02")], bound=[])
    agent = create_agent(model, [fake_tool(n, calls) for n in SERVER], system_prompt=demo.SYSTEM, middleware=[demo.AllowListMiddleware()])
    r = asyncio.run(demo.ask(agent, "Delete the topic bikes.orders.", demo.CostHandler()))
    assert "delete-topics" in model.bound[0] and calls == [] and r["tool_calls"][0]["status"] == "error"
