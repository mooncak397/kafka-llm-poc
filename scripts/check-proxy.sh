#!/usr/bin/env bash
# `make check` line for S13: proxy pod ready and Prometheus scrapes it (target up). No model call, so the check is fast.
C="kubectl --context ${1:-kind-kafka-poc}"
ready=$($C -n llm get deploy llm-proxy -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
up=$($C get --raw "/api/v1/namespaces/monitoring/services/prometheus:9090/proxy/api/v1/query?query=up%7Bjob%3D%22llm-proxy%22%7D" 2>/dev/null \
  | python3 -c 'import sys,json; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else 0)' 2>/dev/null || echo 0)
if [ "${ready:-0}" = 1 ] && [ "$up" = 1 ]; then echo "[green] llm proxy ready (pod ready, scraped by Prometheus)"
else echo "[RED] llm proxy ready (ready=${ready:-0}, prometheus up=$up)"; exit 1; fi
