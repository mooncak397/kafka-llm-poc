#!/usr/bin/env bash
# `make ask Q="..."`: POST /ask inside the agent pod (python is in the image). Prints answer, tool names, error.
set -euo pipefail
C="kubectl --context ${1:-kind-kafka-poc} -n llm"
[ -n "${2:-}" ] || { echo 'usage: make ask Q="question" [RUN=u1]'; exit 2; }
Q="$2" RUN="${3:-none}" ; [ -n "$RUN" ] || RUN=none
printf '%s' "$Q" | $C exec -i deploy/agent -- env RUN="$RUN" python -I -c '
import json, os, sys, urllib.request
body = json.dumps({"q": sys.stdin.read(), "run": os.environ["RUN"]}).encode()
try:
    r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8080/ask", body, {"content-type": "application/json"}), timeout=900)
    j = json.load(r)
except urllib.error.HTTPError as e:
    j = json.load(e)
print("answer:", j.get("answer") or "(none)")
print("tools :", ", ".join(c["tool"] + "(" + json.dumps(c["args"]) + ")=" + c["status"] for c in j.get("tool_calls", [])) or "(none)")
print("turns :", j.get("turns"), "| error:", j.get("error"))
'
