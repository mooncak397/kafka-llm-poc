#!/usr/bin/env python3
"""Writes evidence/results.md from loadtest/results/load-*.json and score-*.json. No number is typed by hand.

Per (mode, label) the LAST 2 runs are shown, one row each (plan A14: report both). Label `dry` is skipped.
Table 2 (model comparison) uses score files labelled `<model>-r<N>` with 48 questions, the last 3 per model.
Table 3 is copied from evidence/negative-test.txt as it is.
"""
import json, re, statistics, sys
from pathlib import Path

from score import wilson

HERE = Path(__file__).parent
ROOT = HERE.parent
RUNS_PER_LEVEL = 2
SKIP_LABELS = {"dry"}
HARDWARE = "GTX 1060 6 GB"  # label only; the GPU memory column comes from nvidia-smi samples
RUN_LABEL = re.compile(r"^(?P<model>.+)-r(?P<run>\d+)$")
QUESTIONS_PER_RUN, RUNS_PER_MODEL = 48, 3


def load_results(folder):
    """-> (kept runs sorted by date, number of skipped dry files)."""
    runs, skipped = [], 0
    for f in sorted(Path(folder).glob("load-*.json")):
        r = json.loads(f.read_text())
        if r["run"] in SKIP_LABELS:
            skipped += 1
            continue
        r["file"] = f.name
        runs.append(r)
    return sorted(runs, key=lambda r: r["date"]), skipped


def latest_per_level(runs, mode):
    """Last RUNS_PER_LEVEL runs per label of a mode, ordered by users then date."""
    by = {}
    for r in runs:
        if r["mode"] == mode:
            by.setdefault(r["run"], []).append(r)
    kept = [x for v in by.values() for x in v[-RUNS_PER_LEVEL:]]
    return sorted(kept, key=lambda r: (r["users"], r["run"], r["date"]))


def f(x, nd=2):
    return "-" if x is None else f"{x:.{nd}f}"


def pair(a, b, nd=2):
    return f"{f(a, nd)} / {f(b, nd)}"


def meta(rows):
    dates = sorted({r["date"][:10] for r in rows})
    models = sorted({r["model"] for r in rows})
    return f"Run date: {', '.join(dates)}. Model: `{', '.join(models)}`. N = users and requests per run, see the columns. One laptop, one broker, fake data."


def number_runs(rows):
    n = {}
    for r in rows:
        n[r["run"]] = n.get(r["run"], 0) + 1
    seen, out = {}, []
    for r in rows:
        seen[r["run"]] = seen.get(r["run"], 0) + 1
        out.append(f"{seen[r['run']]} of {n[r['run']]}")
    return out


def table1(rows):
    out = [meta(rows), "", "| Users | Run | Requests ok / total | Error rate | TTFT p50 / p95 (s) | Tokens/s per user (median) | Queue wait p50 / p95 (s, derived) | GPU memory peak (MiB) |",
           "|---|---|---|---|---|---|---|---|"]
    for r, n in zip(rows, number_runs(rows)):
        s = r["summary"]
        out.append(f"| {r['users']} | {n} | {s['requests_ok']} / {s['requests_total']} | {f(s['error_rate'])} | {pair(s['ttft_p50'], s['ttft_p95'])} | "
                   f"{f(s['tokens_per_s_median'], 1)} | {pair(s['queue_wait_derived_p50'], s['queue_wait_derived_p95'])} | {r['gpu']['gpu_peak_mib'] if r['gpu']['gpu_peak_mib'] is not None else '-'} |")
    return out


def table2(rows):
    out = [meta(rows), "", "| Users | Run | Questions | Tool correct | Args correct | Answer correct | Timeouts | Seconds per question p50 / p95 |", "|---|---|---|---|---|---|---|---|"]
    for r, n in zip(rows, number_runs(rows)):
        s, t = r["summary"], r["summary"]["requests_total"]
        out.append(f"| {r['users']} | {n} | {t} | {s['tool_correct']} / {t} | {s['args_correct']} / {t} | {s['answer_correct']} / {t} | {s['timeouts']} | {pair(s['total_p50'], s['total_p95'], 1)} |")
    return out


def load_scores(folder):
    """Comparison runs: score files labelled <model>-r<N> over the 48-question set. -> {label model: last 3 runs by date}."""
    by = {}
    for f in sorted(Path(folder).glob("score-*.json")):
        r = json.loads(f.read_text())
        m = RUN_LABEL.match(r["model"])
        if m and r["summary"]["n"] == QUESTIONS_PER_RUN:
            r["file"] = f.name
            by.setdefault(m["model"], []).append(r)
    return {k: sorted(v, key=lambda r: r["date"])[-RUNS_PER_MODEL:] for k, v in sorted(by.items())}


def model_stats(runs):
    rows = [x for r in runs for x in r["rows"]]
    n, k, t = len(rows), sum(x["answer_ok"] for x in rows), sum(x["tool_ok"] for x in rows)
    per_run = [r["summary"]["answer_correct"] for r in runs]
    tool_mean = sum(sum(x["tool_ok"] for x in r["rows"]) for r in runs) / len(runs)
    missed = {}
    for x in rows:
        if not x["answer_ok"] or not x["tool_ok"]:
            missed[x["id"]] = missed.get(x["id"], 0) + 1
    cost = sum(x.get("cost_usd") or 0.0 for x in rows)
    return {"model": runs[-1].get("agent_model") or runs[-1]["model"], "runs": len(runs), "n": n, "per_run": per_run,
            "mean": sum(per_run) / len(per_run), "answer": k, "answer_ci": wilson(k, n), "tool": t, "tool_ci": wilson(t, n),
            "answer_run_ci": wilson(sum(per_run) / len(per_run), QUESTIONS_PER_RUN), "tool_run_ci": wilson(tool_mean, QUESTIONS_PER_RUN),
            "missed": sorted(q for q, c in missed.items() if c * 2 > len(runs)),
            "median_s": statistics.median(x["seconds"] for x in rows), "cost": cost, "cost_q": cost / n, "cost_ok": cost / k if k else None,
            "dates": sorted({r["date"][:10] for r in runs})}


def pct(k, n):
    lo, hi = wilson(k, n)
    return f"{k:g}/{n} ({100 * k / n:.0f}%, CI {100 * lo:.0f}-{100 * hi:.0f}%)"


def usd(x, free):
    return "$0 API" if free else ("-" if x is None else f"${x:.5f}")


def separated(a, b):
    return a[0] > b[1] or b[0] > a[1]


def table2_models(scores):
    st = [model_stats(v) for v in scores.values()]
    out = [f"Run dates: {', '.join(sorted({d for s in st for d in s['dates']}))}. One question at a time (`loadtest/score.py`), {QUESTIONS_PER_RUN} questions per run, "
           f"{RUNS_PER_MODEL} runs per model. CI = 95% Wilson interval.", "",
           "| Model | Runs | Answer correct per run | Mean per run | Pooled answer correct | Pooled tool correct | Median s per answer | Cost per question | Cost per correct answer | Total cost |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for s in st:
        free = s["cost"] == 0
        out.append(f"| `{s['model']}` | {s['runs']} | {', '.join(map(str, s['per_run']))} | {pct(round(s['mean'], 1), QUESTIONS_PER_RUN)} | {pct(s['answer'], s['n'])} | "
                   f"{pct(s['tool'], s['n'])} | {s['median_s']:.1f} | {usd(s['cost_q'], free)} | {usd(s['cost_ok'], free)} | {usd(s['cost'], free)} |")
    out += ["", "What the intervals allow. A difference is claimed only where the intervals do not overlap. The pooled interval is the narrow one "
            "(see the notes), so a gap counts as proven only when the per-run intervals separate too:", ""]
    for i, a in enumerate(st):
        for b in st[i + 1:]:
            for key, what in (("answer", "answer correctness"), ("tool", "tool choice")):
                hi, lo = (a, b) if a[key] > b[key] else (b, a)
                if separated(a[f"{key}_run_ci"], b[f"{key}_run_ci"]):
                    out.append(f"- {what}: `{hi['model']}` is higher than `{lo['model']}`; the pooled and the per-run intervals both separate.")
                elif separated(a[f"{key}_ci"], b[f"{key}_ci"]):
                    r1, r2 = hi[f"{key}_run_ci"], lo[f"{key}_run_ci"]
                    out.append(f"- {what}: `{hi['model']}` is higher than `{lo['model']}` on the pooled intervals, but the per-run intervals still overlap "
                               f"({100 * r1[0]:.0f}-{100 * r1[1]:.0f}% and {100 * r2[0]:.0f}-{100 * r2[1]:.0f}%). Read it as a strong hint, not a proven gap.")
                else:
                    out.append(f"- {what}: `{a['model']}` and `{b['model']}` overlap, so these runs show no difference.")
    out += ["", "Questions with a wrong tool choice or a wrong answer in most runs of a model:", ""]
    out += [f"- `{s['model']}`: {', '.join(s['missed']) or 'none'}" for s in st]
    out += ["", "Notes:", "",
            "- Questions q01 to q12 are the original set. q13 to q48 were added on 2026-11-19 and reviewed by the owner. After the first Haiku check run "
            "the wording of q16 and q18 and the answer check of q21 changed; every run in this table uses the final version.",
            "- Runs of one model repeat the same 48 questions, so the pooled interval treats correlated answers as independent and is narrower than it should be. "
            "The per-run mean is the more careful number.",
            "- Cost comes from the usage fields the Anthropic API returns (uncached input, cache read, cache write, output), priced with `agent/pricing.py`. "
            "The local model has no API bill; electricity and hardware are not priced. Hardware: " + HARDWARE + ".",
            "- The runs of one model follow each other within the 5-minute prompt cache life, so the first run pays most of the cache writes "
            "and later runs read the shared prefix from the cache. The cost columns are the total over all runs.",
            "- Seconds per answer are measured by the scorer around one `kubectl exec` call, so they include that overhead for every model.",
            "- Request options differ per backend. Claude: not streamed, `max_tokens` 1024, effort `medium`, automatic prompt caching, no temperature "
            "(current Claude models reject it), no server-side refusal fallback (a refusal is scored as a wrong answer). "
            "Ollama: streamed, `temperature` 0.6, `num_predict` 600, `num_ctx` 4096.",
            "- All models get the same system prompt, the same five read-only tools with the same trimmed descriptions, and the same 5-turn cap."]
    return out


def table3(path):
    """The Table 3 block of negative-test.txt: from the 'Table 3' line up to the first line that is not a table row."""
    lines = Path(path).read_text().splitlines()
    i = next(i for i, l in enumerate(lines) if l.startswith("Table 3"))
    block = [lines[i]]
    for l in lines[i + 1:]:
        if l.startswith("|"):
            block.append(l)
        elif block[-1].startswith("|"):
            break
    neg = next((l for l in lines if l.startswith("Date:")), "")
    return [f"_{neg}_", "", f"**{block[0]}**", ""] + block[1:]


def render(runs, skipped, negative_path, scores=None):
    raw, agent = latest_per_level(runs, "raw"), latest_per_level(runs, "agent")
    allrows = raw + agent
    ver = sorted({r["ollama_version"] for r in allrows}) or ["-"]
    par = sorted({str(r["ollama_num_parallel"]) for r in allrows}) or ["-"]
    ctx = sorted({str(r["num_ctx"]) for r in allrows}) or ["-"]
    o = ["# Results (written by loadtest/make_results.py, do not edit by hand)", "",
         "## Environment", "", f"- Ollama {', '.join(ver)}", f"- `OLLAMA_NUM_PARALLEL={', '.join(par)}`: 10 and 20 users queue behind one slot. This is the expected result.",
         f"- `num_ctx` {', '.join(ctx)}", f"- Hardware: {HARDWARE}",
         "- Closed loop: think time 1 s, 180 s timeout per request, one warm-up request per run (not counted). Percentiles use linear interpolation.",
         "- TTFT is measured on the client (first chunk with content or tool calls). Queue wait is DERIVED: TTFT minus load duration minus prompt eval duration, minimum 0.",
         "- GPU memory is the whole GPU from `nvidia-smi` (1 s samples). The desktop and the system embedder use VRAM too.",
         f"- Skipped: {skipped} file(s) with label `dry` (dry runs).", "",
         "## Table 1: LLM load (raw mode)", ""]
    o += table1(raw) if raw else ["No raw runs yet."]
    o += ["", "## Table 2: model comparison (agent, one question at a time)", ""]
    o += table2_models(scores) if scores else ["No comparison runs yet."]
    o += ["", "## Table 2b: tool-call correctness under load (agent mode, on this question set)", ""]
    o += table2(agent) if agent else ["No agent runs yet."]
    o += ["", "## Table 3: read-only test", ""] + table3(negative_path)
    o += ["", "## Source files", ""] + [f"- `{r['file']}`" for r in allrows + [x for v in (scores or {}).values() for x in v]]
    return "\n".join(o) + "\n"


def main():
    runs, skipped = load_results(HERE / "results")
    scores = load_scores(HERE / "results")
    text = render(runs, skipped, ROOT / "evidence/negative-test.txt", scores)
    (ROOT / "evidence/results.md").write_text(text)
    print(f"wrote evidence/results.md ({len(runs)} load file(s), {sum(map(len, scores.values()))} score file(s) used; skipped {skipped} dry file(s), label `dry`)")


if __name__ == "__main__":
    main()
