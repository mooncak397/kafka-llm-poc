#!/usr/bin/env bash
# `make check` line for S17: mcp-confluent pod ready (readiness = HTTP port open). Full proof: `make mcp-test`.
C="kubectl --context ${1:-kind-kafka-poc}"
ready=$($C -n kafka get deploy mcp-confluent -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
if [ "${ready:-0}" = 1 ]; then echo "[green] mcp server ready (mcp-confluent 1.6.1, http :8080)"
else echo "[RED] mcp server ready (ready=${ready:-0})"; exit 1; fi
