"""Unit tests of the load stats code and the results generator. No cluster needed: `make load-test`."""
import argparse, json, tempfile
from pathlib import Path

import pytest

import load
import make_results as mr


def test_percentiles():
    assert load.pctl([], 50) is None
    assert load.pctl([None, 4.0], 95) == 4.0
    assert load.pctl([1, 2, 3, 4, 5], 50) == 3
    assert load.pctl([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)
    assert load.pctl([5, 1], 50) == 3  # unsorted input, interpolation


def test_queue_wait_clamps_at_zero():
    assert load.queue_wait(10.0, 1e9, 2e9) == pytest.approx(7.0)
    assert load.queue_wait(1.0, 5e9, 2e9) == 0.0  # model load longer than TTFT: never negative
    assert load.queue_wait(2.0, None, None) == 2.0


def test_gpu_stats_and_parse():
    assert load.gpu_stats([4700, None, 5200, 4800]) == (4700, 5200)
    assert load.gpu_stats([]) == (None, None)
    assert load.parse_mib("4739\n") == 4739
    assert load.parse_mib("[N/A]") is None and load.parse_mib("") is None


def test_label_check():
    assert load.check_label("u10") == "u10"
    for bad in ("", "U1", "a_b", "x" * 25, "u1\n"):
        with pytest.raises(argparse.ArgumentTypeError):
            load.check_label(bad)


def chunks(*ds):
    return [json.dumps(d).encode() + b"\n" for d in ds]


def test_stream_parser():
    st = load.Stream()
    fake = chunks({"message": {"thinking": "hmm"}}, {"message": {"content": ""}}, {"message": {"content": "Hi"}},
                  {"message": {"content": "!"}}, {"done": True, "eval_count": 50, "eval_duration": 2_000_000_000,
                                                  "load_duration": 1_000_000_000, "prompt_eval_duration": 500_000_000})
    for t, line in enumerate(fake):
        st.feed(line, float(t))
    assert st.ttft == 2.0  # thinking and empty content do not count
    f = load.raw_fields(st)
    assert f["tps"] == 25 and f["tokens"] == 50
    assert f["qwait"] == pytest.approx(0.5)  # 2.0 - 1.0 - 0.5


def test_stream_tool_calls_and_garbage():
    st = load.Stream()
    st.feed(b"not json", 0.1)
    st.feed(chunks({"message": {"tool_calls": [{"function": {"name": "x"}}]}})[0], 0.7)
    assert st.ttft == 0.7 and st.final is None
    st.feed(chunks({"error": "boom"})[0], 0.8)
    assert st.error == "boom"


def test_error_counting():
    recs = [{"status": s, "total": 1.0, "ttft": 1.0, "tps": 20.0, "qwait": 0.0} for s in ("ok", "ok", "timeout", "503")]
    s = load.summarize("raw", recs)
    assert (s["requests_ok"], s["requests_total"], s["error_rate"]) == (2, 4, 0.5)
    assert load.summarize("raw", [])["error_rate"] is None


def test_agent_summary_counts_timeouts_as_wrong():
    recs = [{"status": "ok", "total": 2.0, "tool_ok": True, "args_ok": True, "answer_ok": True},
            {"status": "timeout", "total": 180.0, "tool_ok": False, "args_ok": False, "answer_ok": False}]
    s = load.summarize("agent", recs)
    assert (s["tool_correct"], s["answer_correct"], s["timeouts"], s["errors"]) == (1, 1, 1, 1)


# ---- results generator on fixture JSON ----
def fixture(mode, label, users, date, **over):
    s = ({"requests_ok": 9, "requests_total": 10, "error_rate": 0.1, "total_p50": 3.0, "total_p95": 5.0, "ttft_p50": 1.0, "ttft_p95": 2.0,
          "tokens_per_s_median": 40.0, "queue_wait_derived_p50": 0.1, "queue_wait_derived_p95": 0.9} if mode == "raw" else
         {"requests_ok": 11, "requests_total": 12, "error_rate": 0.08, "total_p50": 3.0, "total_p95": 7.0, "tool_correct": 10, "args_correct": 9,
          "answer_correct": 8, "timeouts": 1, "errors": 1, "score_errors": 0})
    r = {"date": date, "mode": mode, "run": label, "users": users, "model": "m:1", "ollama_version": "0.0.1", "ollama_num_parallel": 1,
         "num_ctx": 4096, "gpu": {"gpu_peak_mib": 5000}, "summary": s}
    r.update(over)
    return r


def test_results_generator():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        rows = [fixture("raw", "u1", 1, "2026-11-16T10:00:00+00:00"), fixture("raw", "u1", 1, "2026-11-16T10:10:00+00:00"),
                fixture("raw", "u1", 1, "2026-11-16T10:20:00+00:00"),  # third run of u1: only the last 2 are shown
                fixture("raw", "dry", 1, "2026-11-16T09:00:00+00:00"), fixture("agent", "u10", 10, "2026-11-16T11:00:00+00:00")]
        for i, r in enumerate(rows):
            (d / f"load-{r['mode']}-{r['run']}-{i}.json").write_text(json.dumps(r))
        neg = d / "neg.txt"
        neg.write_text("Title\nDate: 2026-11-15 | Model: m\n\nTable 3: read-only test\n| A | B |\n|---|---|\n| x | 1 |\n\nCounts\n| not | table |\n")
        runs, skipped = mr.load_results(d)
        assert skipped == 1 and len(runs) == 4
        text = mr.render(runs, skipped, neg)
    assert "| 1 | 1 of 2 | 9 / 10 | 0.10 | 1.00 / 2.00 | 40.0 | 0.10 / 0.90 | 5000 |" in text
    assert text.count("| 1 | 1 of 2 |") == 1 and text.count("| 1 | 2 of 2 |") == 1
    assert "| 10 | 1 of 1 | 12 | 10 / 12 | 9 / 12 | 8 / 12 | 1 |" in text
    assert "| x | 1 |" in text and "not | table" not in text  # table 3 copied verbatim, stops at the table end
    assert "label `dry`" in text and "dry" not in "".join(l for l in text.splitlines() if l.startswith("- `load-"))
    assert "0.0.1" in text and "OLLAMA_NUM_PARALLEL=1" in text and "GTX 1060 6 GB" in text
