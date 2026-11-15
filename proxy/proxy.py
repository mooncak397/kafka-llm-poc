"""Thin pass-through proxy for Ollama with Prometheus metrics (S13 v1, S14 v2: latency metrics).

TTFT = seconds from request arrival at the proxy to the first streamed chunk that holds content,
tool_calls (/api/chat) or response (/api/generate). Chunks with only thinking or empty text do not count.
Non-stream requests (body "stream": false): TTFT is NOT measurable, so it is NOT observed. The derived queue
wait needs TTFT, so it is not observed either. Tokens/s and load_duration still come from the final JSON.
"""
import asyncio
import json
import os
import re
import time

import aiohttp
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

UPSTREAM = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
RUN_RE = re.compile(r"[a-z0-9-]{1,24}")  # fullmatch: a trailing newline must not pass
# Seconds: GTX 1060 + 4B model, warm answer in under 1 s, queued 20 users wait minutes.
SECONDS_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 45, 60, 90, 120, 180)
TPS_BUCKETS = (1, 2, 5, 10, 15, 20, 25, 30, 40, 50, 60)


def run_label(header) -> str:
    """Header X-Run -> label value. Only a few safe values, so a client cannot blow up the series count."""
    if header is None:
        return "none"
    return header if RUN_RE.fullmatch(header) else "invalid"


def make_metrics():
    # own registry per app: tests can create many apps without duplicate-name errors
    r = CollectorRegistry()
    return r, {
        "requests": Counter("llm_requests_total", "Requests by final status and run", ["status", "run"], registry=r),
        "errors": Counter("llm_errors_total", "Failed requests (upstream error, no answer, bad status)", registry=r),
        "in_flight": Gauge("llm_in_flight", "Requests being served now", registry=r),
        "prompt": Counter("llm_prompt_tokens_total", "prompt_eval_count from Ollama", registry=r),
        "generated": Counter("llm_generated_tokens_total", "eval_count from Ollama", registry=r),
        "ttft": Histogram("llm_ttft_seconds", "Arrival to first content chunk (stream requests only)", ["run"],
                          buckets=SECONDS_BUCKETS, registry=r),
        "tps": Histogram("llm_tokens_per_second", "eval_count / eval_duration", ["run"], buckets=TPS_BUCKETS, registry=r),
        "queue": Histogram("llm_queue_wait_derived_seconds",
                           "DERIVED: TTFT - load_duration - prompt_eval_duration, min 0 (Ollama reports no queue time)",
                           ["run"], buckets=SECONDS_BUCKETS, registry=r),
        "load": Histogram("llm_load_duration_seconds", "load_duration from Ollama (model load)", ["run"],
                          buckets=SECONDS_BUCKETS, registry=r),
    }


def has_content(d) -> bool:
    msg = d.get("message") or {}
    return bool(msg.get("content") or msg.get("tool_calls") or d.get("response"))


def observe_final(m, run: str, last_line: bytes, ttft):
    """Final chunk (stream) or whole body (non-stream) carries the counts and the durations (nanoseconds)."""
    try:
        d = json.loads(last_line)
    except ValueError:
        return
    if not (isinstance(d, dict) and d.get("done")):
        return
    m["prompt"].inc(d.get("prompt_eval_count") or 0)
    m["generated"].inc(d.get("eval_count") or 0)
    count, ev = d.get("eval_count") or 0, d.get("eval_duration") or 0
    if count > 0 and ev > 0:
        m["tps"].labels(run).observe(count / (ev / 1e9))
    load = d.get("load_duration")
    if load is not None:
        m["load"].labels(run).observe(load / 1e9)
    if ttft is not None:
        busy = ((load or 0) + (d.get("prompt_eval_duration") or 0)) / 1e9
        m["queue"].labels(run).observe(max(0.0, ttft - busy))


async def forward(request: web.Request) -> web.StreamResponse:
    t0 = time.monotonic()  # request arrival
    m = request.app["m"]
    run = run_label(request.headers.get("X-Run"))
    m["in_flight"].inc()
    status, out, last, buf, ttft = "502", None, b"", b"", None
    try:
        body = await request.read()
        stream = False  # no body (GET /api/tags) or "stream": false -> TTFT not measurable
        if body:
            try:
                j = json.loads(body)
                stream = j.get("stream", True) is not False if isinstance(j, dict) else True
            except ValueError:
                stream = True
        async with request.app["session"].request(
            request.method, UPSTREAM + request.path, data=body or None,
            headers={"Content-Type": request.content_type},
        ) as up:
            status = str(up.status)
            out = web.StreamResponse(status=up.status, headers={"Content-Type": up.content_type})
            await out.prepare(request)
            async for chunk in up.content.iter_any():
                now = time.monotonic()  # before the write: a slow client must not add to TTFT
                await out.write(chunk)  # body passes through unchanged
                *lines, buf = (buf + chunk).split(b"\n")
                last = lines[-1] if lines else last
                for ln in lines if (stream and ttft is None) else ():
                    try:
                        d = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and has_content(d):
                        ttft = now - t0
                        m["ttft"].labels(run).observe(ttft)
                        break
            if buf.strip():
                last = buf
            observe_final(m, run, last, ttft)
            await out.write_eof()
            return out
    except (aiohttp.ClientError, asyncio.TimeoutError):
        status = "502"  # counted as error in finally
        if out is None:  # nothing sent yet: we can still answer with a clean 502
            return web.json_response({"error": "upstream unreachable"}, status=502)
        return out  # upstream broke mid-stream
    except (ConnectionResetError, asyncio.CancelledError):
        status = "client_closed"  # client left; in_flight must still go down (finally)
        raise
    finally:
        if status.isdigit() and int(status) >= 400:
            m["errors"].inc()
        m["requests"].labels(status, run).inc()
        m["in_flight"].dec()


async def metrics(request: web.Request) -> web.Response:
    return web.Response(body=generate_latest(request.app["registry"]), headers={"Content-Type": CONTENT_TYPE_LATEST})


async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def make_app() -> web.Application:
    app = web.Application()
    app["registry"], app["m"] = make_metrics()

    async def session_ctx(app):
        # no total timeout: a long generation may take minutes
        app["session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=300))
        yield
        await app["session"].close()

    app.cleanup_ctx.append(session_ctx)
    app.add_routes([
        web.post("/api/chat", forward), web.post("/api/generate", forward), web.get("/api/tags", forward),
        web.get("/metrics", metrics), web.get("/healthz", healthz),
    ])
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=int(os.environ.get("PORT", "8080")), print=None)
