#!/usr/bin/env bash
# S17 proof: MCP initialize + tools/list (names only), then list-topics and get-consumer-group-lag.
# Talks streamable HTTP to svc/mcp-confluent through a temporary port-forward (127.0.0.1 only).
# The API key is read from the Secret and sent as a header. It is never printed.
set -euo pipefail
CTX="${1:-kind-kafka-poc}"; C="kubectl --context $CTX -n kafka"
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
$C port-forward --address 127.0.0.1 svc/mcp-confluent "$PORT":8080 >/dev/null 2>&1 & PF=$!
trap 'kill $PF 2>/dev/null' EXIT
MCP_KEY=$($C get secret mcp-api-key -o jsonpath='{.data.key}' | base64 -d) PORT=$PORT python3 -I - <<'PY'
import json, os, sys, time, urllib.request
url = f"http://127.0.0.1:{os.environ['PORT']}/mcp"
hdr = {"content-type": "application/json", "accept": "application/json, text/event-stream",
       "cflt-mcp-api-key": os.environ["MCP_KEY"]}
sid = None
def rpc(method, params=None, id=None):
    global sid
    body = {"jsonrpc": "2.0", "method": method}
    if params is not None: body["params"] = params
    if id is not None: body["id"] = id
    h = dict(hdr); 
    if sid: h["mcp-session-id"] = sid
    r = urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), h), timeout=60)
    sid = r.headers.get("mcp-session-id") or sid
    txt = r.read().decode()
    if id is None: return None
    for line in txt.splitlines():            # reply is plain JSON or one SSE "data:" line
        line = line[5:].strip() if line.startswith("data:") else line.strip()
        if line.startswith("{"): return json.loads(line)
for i in range(30):                          # port-forward needs a moment
    try:
        init = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "poc-check", "version": "0"}}, 1); break
    except Exception: time.sleep(1)
else: sys.exit("mcp server did not answer")
rpc("notifications/initialized")
print("server:", init["result"]["serverInfo"]["name"], init["result"]["serverInfo"]["version"])
tools = rpc("tools/list", {}, 2)["result"]["tools"]
print("tools/list (%d):" % len(tools)); [print(" ", t["name"]) for t in sorted(tools, key=lambda t: t["name"])]
def call(name, args):
    r = rpc("tools/call", {"name": name, "arguments": args}, 3)
    if "error" in r: return "ERROR " + json.dumps(r["error"])[:300]
    return ("ERROR " if r["result"].get("isError") else "") + "".join(c.get("text", "") for c in r["result"]["content"])
print("\nlist-topics:\n", call("list-topics", {})[:600])
sch = next(t for t in tools if t["name"] == "get-consumer-group-lag")["inputSchema"].get("properties", {})
print("\nget-consumer-group-lag (args: %s):" % ",".join(sch))
for g in ("orders-fast", "orders-slow"):
    print(" ", g, "->", call("get-consumer-group-lag", {"groupId": g})[:700])
PY
