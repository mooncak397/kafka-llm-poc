#!/usr/bin/env bash
# `make check` line for S09: Prometheus answers for broker JMX and Kafka Exporter metrics.
# Uses the API server proxy (no port-forward, no password).
C="kubectl --context ${1:-kind-kafka-poc}"
q() { $C get --raw "/api/v1/namespaces/monitoring/services/prometheus:9090/proxy/api/v1/query?query=count($1)" 2>/dev/null \
      | python3 -c 'import sys,json; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else 0)' 2>/dev/null || echo 0; }
a=$(q kafka_server_brokertopicmetrics_messagesin_total); b=$(q kafka_consumergroup_lag)
if [ "$a" -gt 0 ] && [ "$b" -gt 0 ]; then echo "[green] prometheus has messagesin_total ($a series) and consumergroup_lag ($b series)"
else echo "[RED] prometheus has messagesin_total ($a series) and consumergroup_lag ($b series)"; exit 1; fi
