#!/usr/bin/env python3
"""S23 load script: N closed-loop virtual users against the proxy (mode raw) or the agent (mode agent).

Runs on the HOST. It starts its own `kubectl port-forward` (127.0.0.1 only, random free port) and stops it at the end.
Closed loop: each user asks, waits for the answer, thinks 1 s, repeats until the duration ends. Requests that
started before the end are awaited (up to 180 s), so a queue at 10 or 20 users is not hidden.
Question k of user u is qs[(u + k) % len(qs)]: fixed order, runs repeat.
Warm-up: one request before the timer, with header `X-Run: warmup` so it stays out of the run's Prometheus series.
Agent mode: refuses to start if the worst-case API cost of the deployed model is above --max-usd (Ollama: $0).
Stdlib plus aiohttp. Unit tests: test_load.py (`make load-test`).
"""
import argparse, asyncio, contextlib, json, math, re, shutil, statistics, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

import score  # reused: question loader, scorer, live fact reader (the live checks stay live)

HERE = Path(__file__).parent
TIMEOUT_S, THINK_S = 180, 1.0
RUN_RE = re.compile(r"[a-z0-9-]{1,24}")  # same rule as proxy and agent; fullmatch so a trailing newline fails
# One stub tool: the model may answer with tool_calls, but nothing runs (raw mode measures the LLM only).
STUB_TOOL = {"type": "function", "function": {"name": "list-topics", "description": "List the Kafka topics.",
                                              "parameters": {"type": "object", "properties": {}, "required": []}}}


# ---- pure helpers (unit tested) ----
def check_label(s):
    if not RUN_RE.fullmatch(s):
        raise argparse.ArgumentTypeError("label must match [a-z0-9-]{1,24}")
    return s


def pctl(values, p):
    """Linear-interpolation percentile (p in 0..100) of the non-None values. None if there are none."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * p / 100
    lo = int(k)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (k - lo)


def queue_wait(ttft, load_ns, prompt_ns):
    """DERIVED queue wait = TTFT - load_duration - prompt_eval_duration, min 0 (Ollama reports no queue time)."""
    return max(0.0, ttft - ((load_ns or 0) + (prompt_ns or 0)) / 1e9)


def gpu_stats(samples):
    s = [x for x in samples if x is not None]
    return (min(s), max(s)) if s else (None, None)


def parse_mib(text):
    """nvidia-smi `--format=csv,noheader,nounits` output -> MiB of the first GPU, or None."""
    try:
        return int(text.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


class Stream:
    """Parses one streamed /api/chat answer. TTFT = first chunk with content or tool_calls (same as the proxy);
    message.thinking is ignored."""
    def __init__(self):
        self.ttft = self.final = self.error = None

    def feed(self, line, elapsed):
        try:
            d = json.loads(line)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        msg = d.get("message") or {}
        if self.ttft is None and (msg.get("content") or msg.get("tool_calls")):
            self.ttft = elapsed
        self.error = d.get("error") or self.error
        if d.get("done"):
            self.final = d


def raw_fields(st):
    """Per-request numbers from a finished Stream. Nanosecond fields from Ollama become seconds."""
    f = st.final or {}
    n, ev = f.get("eval_count") or 0, f.get("eval_duration") or 0
    return {"ttft": st.ttft, "tokens": n or None, "tps": n / (ev / 1e9) if n and ev else None,
            "load_s": (f.get("load_duration") or 0) / 1e9, "prompt_s": (f.get("prompt_eval_duration") or 0) / 1e9,
            "qwait": queue_wait(st.ttft, f.get("load_duration"), f.get("prompt_eval_duration")) if st.ttft is not None else None}


def summarize(mode, recs):
    n = len(recs)
    ok = sum(r["status"] == "ok" for r in recs)
    s = {"requests_ok": ok, "requests_total": n, "error_rate": (n - ok) / n if n else None,
         "total_p50": pctl([r["total"] for r in recs], 50), "total_p95": pctl([r["total"] for r in recs], 95)}
    if mode == "raw":
        s.update(ttft_p50=pctl([r["ttft"] for r in recs], 50), ttft_p95=pctl([r["ttft"] for r in recs], 95),
                 tokens_per_s_median=pctl([r["tps"] for r in recs], 50),
                 queue_wait_derived_p50=pctl([r["qwait"] for r in recs], 50), queue_wait_derived_p95=pctl([r["qwait"] for r in recs], 95))
    else:
        s.update(tool_correct=sum(bool(r.get("tool_ok")) for r in recs), args_correct=sum(bool(r.get("args_ok")) for r in recs),
                 answer_correct=sum(bool(r.get("answer_ok")) for r in recs), timeouts=sum(r["status"] == "timeout" for r in recs),
                 errors=n - ok, score_errors=sum(r["status"] == "score_error" for r in recs))
    return s


# ---- environment ----
def ollama_version():
    # shortcut: asks the host `ollama` binary (our user unit runs the same /usr/bin/ollama); upgrade if the unit gets its own binary
    if not shutil.which("ollama"):
        return "unknown"
    r = subprocess.run(["ollama", "--version"], capture_output=True, text=True, timeout=10)
    m = re.search(r"\d+\.\d+\.\d+\S*", r.stdout + r.stderr)
    return m.group(0) if m else "unknown"


def num_parallel():
    """OLLAMA_NUM_PARALLEL from the unit file we install (scripts/ollama-poc.service)."""
    m = re.search(r"OLLAMA_NUM_PARALLEL=(\d+)", (HERE.parent / "scripts/ollama-poc.service").read_text())
    return int(m.group(1)) if m else None


async def gpu_sample():
    try:
        p = await asyncio.create_subprocess_exec("nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits",
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await p.communicate()
        return parse_mib(out.decode())
    except OSError:
        return None


async def gpu_sampler(samples, stop):
    # NOTE: this is the whole GPU. The desktop and the system embedder use VRAM too, so the peak is not "the model alone".
    while not stop.is_set():
        samples.append(await gpu_sample())
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), 1)


@contextlib.asynccontextmanager
async def port_forward(svc):
    """kubectl port-forward on 127.0.0.1, random free local port. Yields the base URL. Stopped on exit."""
    p = await asyncio.create_subprocess_exec("kubectl", "--context", score.CTX, "-n", "llm", "port-forward", "--address", "127.0.0.1",
                                             f"svc/{svc}", ":8080", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    drain = None
    try:
        async with asyncio.timeout(20):
            while True:
                line = await p.stdout.readline()
                if not line:
                    raise RuntimeError(f"port-forward to {svc} ended")
                if m := re.search(rb"127\.0\.0\.1:(\d+)", line):
                    break
        drain = asyncio.create_task(p.stdout.read())  # keep the pipe empty ("Handling connection" lines)
        yield f"http://127.0.0.1:{m[1].decode()}"
    finally:
        if drain:
            drain.cancel()
        with contextlib.suppress(ProcessLookupError):
            p.terminate()
        await p.wait()


# ---- one request ----
async def raw_call(s, base, tpl, q, label):
    body = {**tpl, "messages": [tpl["messages"][0], {"role": "user", "content": q["question"]}], "tools": [STUB_TOOL]}
    t0, st, status = time.monotonic(), Stream(), "error"
    try:
        async with s.post(base + "/api/chat", json=body, headers={"X-Run": label}) as r:
            if r.status != 200:
                status = str(r.status)
            else:
                async for line in r.content:
                    st.feed(line, time.monotonic() - t0)
                status = "ok" if st.final and not st.error else "error"
    except asyncio.TimeoutError:
        status = "timeout"
    except aiohttp.ClientError:
        status = "error"
    return {"total": time.monotonic() - t0, "status": status, **raw_fields(st)}


async def agent_call(s, base, q, label):
    t0, resp = time.monotonic(), {"answer": "", "tool_calls": []}
    try:
        async with s.post(base + "/ask", json={"q": q["question"], "run": label}) as r:
            resp = await r.json(content_type=None)  # 4xx/5xx bodies are JSON with "error" too
    except asyncio.TimeoutError:
        resp["error"] = "timeout"
    except (aiohttp.ClientError, ValueError):
        resp["error"] = "no JSON from agent"
    secs = time.monotonic() - t0
    status = "timeout" if resp.get("error") == "timeout" else "error" if resp.get("error") else "ok"
    rec = {"total": secs, "status": status, "ttft": None, "tokens": None}
    try:  # live values are read right after the answer, as in score.py
        row = score.score_one(q, resp, await asyncio.to_thread(score.read_live), secs)
        rec.update(tool_ok=row["tool_ok"], args_ok=row["args_ok"], answer_ok=row["answer_ok"])
    except Exception as e:  # Prometheus or kubectl failed: not scored, and counted as its own status
        print(f"score error: {type(e).__name__}", file=sys.stderr)
        rec.update(tool_ok=False, args_ok=False, answer_ok=False, status="score_error")
    return rec


# ---- the run ----
async def run_load(a, base, qs, tpl):
    call = (lambda s, q, lab: raw_call(s, base, tpl, q, lab)) if a.mode == "raw" else (lambda s, q, lab: agent_call(s, base, q, lab))
    recs, started = [], [0]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as s:
        baseline = await gpu_sample()
        w = await call(s, qs[0], "warmup")
        print(f"warm-up: {w['status']} {w['total']:.1f}s (not counted)", flush=True)
        samples, stop = [], asyncio.Event()
        sampler = asyncio.create_task(gpu_sampler(samples, stop))
        t0 = time.monotonic()

        async def user(u):
            k = 0
            while time.monotonic() - t0 < a.duration and not (a.max_requests and started[0] >= a.max_requests):
                i, q, k, started[0] = started[0], qs[(u + k) % len(qs)], k + 1, started[0] + 1
                start = time.monotonic() - t0
                r = await call(s, q, a.run)
                recs.append({"i": i, "user": u, "start": start, **r})
                await asyncio.sleep(THINK_S)

        await asyncio.gather(*(user(u) for u in range(a.users)))
        stop.set()
        await sampler
    gmin, gpeak = gpu_stats(samples)
    return recs, {"gpu_baseline_mib": baseline, "gpu_min_mib": gmin, "gpu_peak_mib": gpeak}


def table_row(res):
    s, g = res["summary"], res["gpu"]
    f = lambda x, nd=2: "-" if x is None else f"{x:.{nd}f}"
    head = f"{res['mode']} | {res['run']} | users {res['users']} | ok {s['requests_ok']}/{s['requests_total']} | err {f(s['error_rate'])}"
    if res["mode"] == "raw":
        return (f"{head} | TTFT p50/p95 {f(s['ttft_p50'])}/{f(s['ttft_p95'])} s | {f(s['tokens_per_s_median'], 1)} tok/s | "
                f"queue wait (derived) p50/p95 {f(s['queue_wait_derived_p50'])}/{f(s['queue_wait_derived_p95'])} s | GPU peak {g['gpu_peak_mib']} MiB")
    return (f"{head} | tool {s['tool_correct']} args {s['args_correct']} answer {s['answer_correct']} of {s['requests_total']} | "
            f"timeouts {s['timeouts']} | s/question p50/p95 {f(s['total_p50'], 1)}/{f(s['total_p95'], 1)} | GPU peak {g['gpu_peak_mib']} MiB")


async def amain(a):
    default_q = "questions-raw.txt" if a.mode == "raw" else "questions-tools.jsonl"
    path = Path(a.questions or HERE / default_q)
    if path.suffix == ".jsonl":
        qs = score.load_questions(path)
        if bad := score.validate_questions(qs):
            sys.exit("bad question file: " + "; ".join(bad))
    else:
        qs = [{"question": l.strip()} for l in path.read_text().splitlines() if l.strip()]
    if not qs:
        sys.exit("no questions")
    tpl = json.loads((HERE / "request-template.json").read_text())
    tpl.pop("tools", None)
    if a.model:
        tpl["model"] = a.model
    if a.mode == "agent":
        await asyncio.to_thread(score.read_live)  # fail fast if Prometheus is not reachable
        # closed loop: at most one question per THINK_S per user, plus the warm-up (a high bound; --max-requests makes it exact)
        n = (a.max_requests or a.users * math.ceil(a.duration / THINK_S)) + 1
        score.budget_gate(n, await asyncio.to_thread(score.deployed_model), a.max_usd)
    date = datetime.now(timezone.utc)
    async with port_forward("llm-proxy" if a.mode == "raw" else "agent") as base:
        recs, gpu = await run_load(a, base, qs, tpl)
    res = {"date": date.isoformat(timespec="seconds"), "mode": a.mode, "run": a.run, "users": a.users, "duration_s": a.duration,
           "max_requests": a.max_requests, "model": tpl["model"], "ollama_version": ollama_version(), "ollama_num_parallel": num_parallel(),
           "num_ctx": tpl["options"]["num_ctx"], "num_predict": tpl["options"]["num_predict"], "gpu": gpu,
           "summary": summarize(a.mode, recs),
           "requests": [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items() if k in ("i", "user", "start", "ttft", "total", "tokens", "status")} for r in recs]}
    out = HERE / "results" / f"load-{a.mode}-{a.run}-{date.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, indent=1) + "\n")
    print(table_row(res))
    print("wrote", out.relative_to(HERE.parent))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["raw", "agent"], required=True)
    ap.add_argument("--users", type=int, required=True)
    ap.add_argument("--duration", type=int, default=240, help="seconds (default 240 = 4 min)")
    ap.add_argument("--run", type=check_label, required=True, help="label for X-Run and the file name, [a-z0-9-]{1,24}")
    ap.add_argument("--questions", help="file: .jsonl (agent format) or one question per line; default depends on the mode")
    ap.add_argument("--max-requests", type=int, default=0, help="cap on counted requests (dry runs); 0 = no cap")
    ap.add_argument("--model", help="model tag; default: the one in request-template.json (make passes the model)")
    ap.add_argument("--max-usd", type=float, default=1.0, help="agent mode: refuse to start above this worst-case API cost (default 1.0)")
    a = ap.parse_args()
    if a.users < 1 or a.duration < 1:
        ap.error("users and duration must be at least 1")
    asyncio.run(amain(a))


if __name__ == "__main__":
    main()
