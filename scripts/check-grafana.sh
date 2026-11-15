#!/usr/bin/env bash
# `make check` line for S10: Grafana health API answers and four dashboards exist (search API, anonymous Viewer).
# Uses the API server proxy (no port-forward, no password).
C="kubectl --context ${1:-kind-kafka-poc}"
P="/api/v1/namespaces/monitoring/services/grafana:3000/proxy"
h=$($C get --raw "$P/api/health" 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)["database"])' 2>/dev/null || echo none)
t=$($C get --raw "$P/api/search?type=dash-db" 2>/dev/null | python3 -c 'import sys,json; print(" ".join(sorted(d["title"] for d in json.load(sys.stdin))))' 2>/dev/null || echo none)
if [ "$h" = ok ] && [ "$t" = "LLM load comparison LLM overview Strimzi Kafka Strimzi Kafka Exporter" ]; then echo "[green] grafana ready (health ok, dashboards: LLM load comparison, LLM overview, Strimzi Kafka, Strimzi Kafka Exporter)"
else echo "[RED] grafana ready (health: $h, dashboards: $t)"; exit 1; fi
