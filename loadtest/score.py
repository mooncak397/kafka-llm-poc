#!/usr/bin/env python3
"""S20 scorer: runs loadtest/questions-tools.jsonl against the agent (POST /ask, one question at a time, run label `score`).

Expected values come from the FAKE DATA design (questions-tools.jsonl), never from model output. Numbers that change over
time (lag, offsets) are checked against live values that THIS script reads from Prometheus (Kafka exporter metrics).
Transport: `kubectl exec` into the agent pod (no port-forward). --model only labels the output; the model is set in the agent env.
Stdlib only. Unit tests: test_score.py (`make score-test`).
"""
import argparse, json, re, statistics, subprocess, sys, time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
TOOLS = {"list-topics", "list-consumer-groups", "describe-consumer-group", "get-consumer-group-lag", "get-partition-offsets"}
CTX = "kind-kafka-poc"
TOLERANCE = 0.15   # reported number vs live value
ABS_TOL = 20       # a lag near 0 needs an absolute margin (fast group)
REFUSAL = re.compile(r"\b(cannot|can't|can not|unable|not able|no tool|no way|not possible|isn't possible|not available|not accessible|not exposed|no access|"
                     r"(?:don't|do not|doesn't|does not|didn't|can't|couldn't) (?:\w+ )?(?:provide|have|support|show|expose|include|offer|give|know|retrieve|access))\b", re.I)
RETENTION_CLAIM = re.compile(r"\d+\s*(days?|hours?|minutes?|ms|milliseconds?|seconds?|bytes?|[kmg]i?b)\b", re.I)


def load_questions(path=HERE / "questions-tools.jsonl"):
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def validate_questions(qs):
    """Returns a list of problems (empty = ok): ids unique, all tools known, 12 questions, required fields."""
    bad = []
    ids = [q.get("id") for q in qs]
    if len(set(ids)) != len(ids):
        bad.append("duplicate ids")
    for q in qs:
        for f in ("id", "question", "tools", "args", "facts"):
            if f not in q:
                bad.append(f"{q.get('id')}: missing {f}")
        for t in list(q.get("tools", [])) + list(q.get("extra_tools", [])) + [a["tool"] for a in q.get("args", [])]:
            if t not in TOOLS:
                bad.append(f"{q.get('id')}: unknown tool {t}")
        if q.get("kind") == "unanswerable" and (q.get("tools") or q.get("args")):
            bad.append(f"{q.get('id')}: unanswerable question must expect no tool")
        if not q.get("facts"):
            bad.append(f"{q.get('id')}: no fact check")
    return bad


def norm(text):
    """Unicode hyphens and thousands separators (space, narrow no-break space) as models write them."""
    t = re.sub(r"[\u2010-\u2015\u2212]", "-", text or "")
    return re.sub(r"(?<=\d)[ \u00a0\u202f](?=\d{3}\b)", "", t)


def numbers(text):
    return [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", norm(text)) if n.replace(",", "").replace(".", "").isdigit()]


def number_near(text, live):
    """True if any number in the text is within 15 percent (or ABS_TOL) of the live value."""
    margin = max(abs(live) * TOLERANCE, ABS_TOL)
    return any(abs(n - live) <= margin for n in numbers(text))


def says_yes(text):
    t = (text or "").lower().strip()
    return bool(re.match(r"^\W*(yes|yep|correct|true)\b", t) or (re.search(r"\b(exists?|is there|does exist)\b", t) and not re.search(r"\b(not exist|doesn't exist|no such|no topic|not found)\b|^\W*no\b", t)))


def tool_correct(q, calls):
    called = {c["tool"] for c in calls}
    return set(q["tools"]) <= called and called <= set(q["tools"]) | set(q.get("extra_tools", []))


def args_correct(q, calls):
    """Every expected (tool, args) pair appears in some call. Needs tool_correct. No expected args: same as tool_correct."""
    if not tool_correct(q, calls):
        return False
    def match(exp):
        return any(c["tool"] == exp["tool"] and all(str((c.get("args") or {}).get(k, "")).lower() == str(v).lower() for k, v in exp["args"].items()) for c in calls)
    return all(match(e) for e in q["args"])


def fact_ok(fact, text, live):
    low = norm(text).lower()
    if "contains_all" in fact:
        return all(s.lower() in low for s in fact["contains_all"])
    if "not_contains_any" in fact:
        return not any(s.lower() in low for s in fact["not_contains_any"])
    if "yes" in fact:
        return says_yes(text)
    if "refuses" in fact:
        return bool(REFUSAL.search(text or ""))
    if "no_retention_claim" in fact:
        return not RETENTION_CLAIM.search(text or "")
    if "number_near_lag" in fact:
        return number_near(text, live["lag"][fact["number_near_lag"]])
    if "number_near_offset" in fact:
        return number_near(text, live["offset"][fact["number_near_offset"]])
    raise ValueError(f"unknown fact {fact}")


def answer_correct(q, resp, live):
    text = (resp.get("answer") or "").strip()
    return bool(text) and not resp.get("error") and all(fact_ok(f, text, live) for f in q["facts"])


def score_one(q, resp, live, seconds):
    calls = resp.get("tool_calls") or []
    return {"id": q["id"], "tool_ok": tool_correct(q, calls), "args_ok": args_correct(q, calls), "answer_ok": answer_correct(q, resp, live),
            "empty": not (resp.get("answer") or "").strip(), "error": resp.get("error"), "seconds": round(seconds, 1),
            "tools": [c["tool"] for c in calls], "args": [c.get("args") for c in calls], "answer": (resp.get("answer") or "")[:400]}


def summarize(rows):
    secs = [r["seconds"] for r in rows]
    return {"n": len(rows), "tool_correct": sum(r["tool_ok"] for r in rows), "args_correct": sum(r["args_ok"] for r in rows),
            "answer_correct": sum(r["answer_ok"] for r in rows), "empty": sum(r["empty"] for r in rows),
            "errors": sum(bool(r["error"]) and not r["empty"] for r in rows), "errors_total": sum(bool(r["error"]) for r in rows),
            "median_s": round(statistics.median(secs), 1) if secs else None, "max_s": max(secs) if secs else None}


# ---- live side (needs the cluster) ----
def kubectl(*a, stdin=None, timeout=900):
    return subprocess.run(["kubectl", "--context", CTX, *a], input=stdin, capture_output=True, text=True, timeout=timeout)


def prom(query):
    from urllib.parse import quote
    r = kubectl("get", "--raw", f"/api/v1/namespaces/monitoring/services/prometheus:9090/proxy/api/v1/query?query={quote(query)}", timeout=30)
    return json.loads(r.stdout)["data"]["result"]


def read_live():
    lag = {m["metric"]["consumergroup"]: float(m["value"][1]) for m in prom("sum by (consumergroup) (kafka_consumergroup_lag)")}
    off = {m["metric"]["topic"]: float(m["value"][1]) for m in prom("sum by (topic) (kafka_topic_partition_current_offset)")}
    return {"lag": lag, "offset": off}


ASK = ("import json,sys,urllib.request,urllib.error\n"
       "b=json.dumps({'q':sys.stdin.read(),'run':'score'}).encode()\n"
       "try: r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/ask',b,{'content-type':'application/json'}),timeout=900); j=json.load(r)\n"
       "except urllib.error.HTTPError as e: j=json.load(e)\n"
       "print(json.dumps(j))")


def ask_agent(question):
    r = kubectl("-n", "llm", "exec", "-i", "deploy/agent", "--", "python", "-I", "-c", ASK, stdin=question)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"answer": "", "tool_calls": [], "turns": 0, "error": f"scorer: no JSON from agent ({r.stderr.strip()[:100]})"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="label only; the model is set in the agent env (MODEL)")
    ap.add_argument("--questions", default=str(HERE / "questions-tools.jsonl"))
    ap.add_argument("--out", default=str(HERE / "results"))
    a = ap.parse_args()
    qs = load_questions(a.questions)
    bad = validate_questions(qs)
    if bad:
        sys.exit("bad question file: " + "; ".join(bad))
    rows = []
    for q in qs:
        t0 = time.monotonic()
        resp = ask_agent(q["question"])
        secs = time.monotonic() - t0
        row = score_one(q, resp, read_live(), secs)  # live values read right after the answer
        rows.append(row)
        print(f"{q['id']} tool={'ok' if row['tool_ok'] else 'NO'} args={'ok' if row['args_ok'] else 'NO'} answer={'ok' if row['answer_ok'] else 'NO'} "
              f"{secs:5.1f}s tools={','.join(row['tools']) or '-'} err={row['error'] or '-'}", flush=True)
    s = summarize(rows)
    n = s["n"]
    print(f"\nmodel {a.model} | n = {n}\ntool correct   {s['tool_correct']}/{n}\nargs correct   {s['args_correct']}/{n}\nanswer correct {s['answer_correct']}/{n}\n"
          f"empty answers  {s['empty']}\nerrors         {s['errors']} (+{s['empty']} empty)\nseconds/answer median {s['median_s']}  max {s['max_s']}")
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    f = out / f"score-{re.sub(r'[^a-z0-9]+', '-', a.model.lower()).strip('-')}-{stamp}.json"
    f.write_text(json.dumps({"model": a.model, "date": datetime.now().isoformat(timespec="seconds"), "summary": s, "rows": rows}, indent=1) + "\n")
    print("wrote", f.relative_to(HERE.parent))


if __name__ == "__main__":
    main()
