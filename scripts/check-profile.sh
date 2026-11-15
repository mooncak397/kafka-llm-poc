#!/usr/bin/env bash
# `make check` line (S21a): the MCP allow-list is the safe one (exactly the five read tools) and the agent has no extra tools.
# Reuses scripts/mcp-tools-list.sh. RED while `make unsafe-demo` is on, so nobody forgets `make down-unsafe`.
CTX="${1:-kind-kafka-poc}"
SAFE="describe-consumer-group get-consumer-group-lag get-partition-offsets list-consumer-groups list-topics"
got=$("$(dirname "$0")/mcp-tools-list.sh" "$CTX" 2>/dev/null | sed -n '/^tools\/list/,/^$/p' | grep '^  ' | tr -d ' ' | sort | tr '\n' ' ' | sed 's/ $//')
extra=$(kubectl --context "$CTX" -n llm get deploy agent -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="AGENT_EXTRA_TOOLS")].value}' 2>/dev/null)
if [ "$got" = "$SAFE" ] && [ -z "$extra" ]; then echo "[green] safe profile (tools/list = the five read tools, agent has no extra tools)"
else echo "[RED] UNSAFE PROFILE: tools/list = [$got], agent extra tools = [$extra]. Run: make down-unsafe"; exit 1; fi
