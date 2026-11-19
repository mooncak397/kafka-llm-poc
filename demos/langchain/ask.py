#!/usr/bin/env python3
"""python demos/langchain/ask.py "Which topics exist?" : one question through the LangChain demo agent (claude-haiku-5-5).
Prints the tools bound to the model, the tool calls, the answer and the cost. Needs ANTHROPIC_API_KEY and the cluster."""
import asyncio
import sys

import demo


async def main(q):
    demo.setup_logging()
    async with demo.mcp_tools() as tools:
        agent, bound = demo.build_agent(tools)
        print("server lists :", ", ".join(sorted(t.name for t in tools)))
        print("bound to model:", ", ".join(sorted(bound)))
        r = await demo.ask(agent, q, demo.CostHandler(run="ask"))
    print("answer:", r["answer"] or "(none)")
    print("tools :", ", ".join(f"{c['tool']}({c['args']})={c['status']}" for c in r["tool_calls"]) or "(none)")
    print(f"turns : {r['turns']} | llm calls: {r['llm_calls']} | cost: ${r['cost_usd']:.6f} | error: {r['error']}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('usage: ask.py "question"')
    asyncio.run(main(sys.argv[1]))
