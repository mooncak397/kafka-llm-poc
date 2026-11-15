#!/usr/bin/env bash
# Usage: check-ollama.sh base-url [context] model. Asks the model for a tool call from a short-lived pod in kind (same path later pods use).
# Prints one word. Needs the curl image pulled by the node (about 25 MB).
set -u
URL="${1:?base url}"; CTX="${2:-kind-kafka-poc}"; MODEL="${3:?model tag}"  # pinned model comes from AGENT_MODEL in the Makefile
BODY='{"model":"'"$MODEL"'","stream":false,"think":false,"options":{"num_ctx":2048},"messages":[{"role":"user","content":"What is the weather in Berlin? Use the tool."}],"tools":[{"type":"function","function":{"name":"get_weather","description":"Get weather for a city","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]}'
out=$(kubectl --context "$CTX" run ollama-check --rm -i --restart=Never --quiet --image=docker.io/curlimages/curl:8.11.1 --command -- \
  curl -s -m 120 "$URL/api/chat" -d "$BODY" 2>/dev/null)
if echo "$out" | grep -q '"tool_calls"'; then echo "[green] ollama (poc) ready"; if [ -n "${SHOW:-}" ]; then echo "$out"; fi; else echo "[RED] ollama (poc) ready"; exit 1; fi
