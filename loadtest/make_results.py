#!/usr/bin/env python3
"""Writes evidence/results.md from loadtest/results/load-*.json. No number is typed by hand.

Per (mode, label) the LAST 2 runs are shown, one row each (plan A14: report both). Label `dry` is skipped.
Table 3 is copied from evidence/negative-test.txt as it is.
"""
import json, re, sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
RUNS_PER_LEVEL = 2
SKIP_LABELS = {"dry"}
HARDWARE = "GTX 1060 6 GB"  # label only; the GPU memory column comes from nvidia-smi samples


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


def render(runs, skipped, negative_path):
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
    o += ["", "## Table 2: tool-call correctness (agent mode, on this question set)", ""]
    o += table2(agent) if agent else ["No agent runs yet."]
    o += ["", "## Table 3: read-only test", ""] + table3(negative_path)
    o += ["", "## Source files", ""] + [f"- `{r['file']}`" for r in allrows]
    return "\n".join(o) + "\n"


def main():
    runs, skipped = load_results(HERE / "results")
    text = render(runs, skipped, ROOT / "evidence/negative-test.txt")
    (ROOT / "evidence/results.md").write_text(text)
    print(f"wrote evidence/results.md ({len(runs)} run file(s) used; skipped {skipped} dry file(s), label `dry`)")


if __name__ == "__main__":
    main()
