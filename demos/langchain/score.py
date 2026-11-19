#!/usr/bin/env python3
"""LangChain demo scorer: the 12 ORIGINAL questions (q01-q12) through the demo agent, scored with loadtest/score.py.
LangChain demo, NOT part of Table 2: different harness, 12 questions, one run. Output: demos/langchain/results/."""
import argparse
import asyncio
import json
import time
from datetime import datetime, timezone

import demo
import score  # loadtest/score.py (demo.py puts loadtest/ on sys.path): same checks, same live Prometheus values

ORIGINAL = [f"q{i:02d}" for i in range(1, 13)]


async def main(a):
    demo.setup_logging()
    qs = [q for q in score.load_questions() if q["id"] in ORIGINAL]
    assert [q["id"] for q in qs] == ORIGINAL
    score.budget_gate(len(qs), demo.MODEL, a.max_usd)
    rows, handler = [], demo.CostHandler(run="lc-demo")
    async with demo.mcp_tools() as tools:
        agent, bound = demo.build_agent(tools)
        print("bound to model:", ", ".join(sorted(bound)))
        for q in qs:
            t0 = time.monotonic()
            resp = await demo.ask(agent, q["question"], handler)
            row = score.score_one(q, resp, await asyncio.to_thread(score.read_live), time.monotonic() - t0)
            rows.append(row)
            print(f"{q['id']} tool={'ok' if row['tool_ok'] else 'NO'} args={'ok' if row['args_ok'] else 'NO'} answer={'ok' if row['answer_ok'] else 'NO'} "
                  f"{row['seconds']:5.1f}s ${row['cost_usd']:.5f} tools={','.join(row['tools']) or '-'} err={row['error'] or '-'}", flush=True)
    s, n = score.summarize(rows), len(rows)
    print(f"\nLangChain demo, not part of Table 2 | {demo.MODEL} | n = {n} (q01-q12, one run)\n"
          f"tool correct   {score.rate(s['tool_correct'], n)}\nanswer correct {score.rate(s['answer_correct'], n)}\n"
          f"seconds/answer median {s['median_s']}\nAPI cost       ${s['cost_usd']:.5f} total, ${s['cost_usd'] / n:.5f} per question")
    out = demo.Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    f = out / f"langchain-demo-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    f.write_text(json.dumps({"label": "LangChain demo, not part of Table 2", "model": demo.MODEL, "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "bound_tools": sorted(bound), "summary": s, "rows": rows}, indent=1) + "\n")
    print("wrote", f.relative_to(demo.ROOT))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-usd", type=float, default=0.5, help="refuse to start above this worst-case cost (default 0.5)")
    asyncio.run(main(ap.parse_args()))
