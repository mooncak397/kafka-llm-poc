#!/usr/bin/env bash
# `make check` line for S07: slow group lag above 0, fast group lag small. Reads the kafka-exporter metrics
# through the API server proxy: no password, no extra pod. Skips (exit 0) when the producer is scaled to 0.
C="kubectl --context ${1:-kind-kafka-poc}"
[ "$($C -n kafka get deploy fake-producer -o jsonpath='{.spec.replicas}' 2>/dev/null)" = 0 ] && { echo "[skip] fake data stopped (make data-start)"; exit 0; }
ex=$($C -n kafka get pod -l strimzi.io/name=poc-kafka-exporter -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
m=$($C get --raw "/api/v1/namespaces/kafka/pods/$ex:9404/proxy/metrics" 2>/dev/null)
lag() { echo "$m" | awk -v g="$1" '$0 ~ "^kafka_consumergroup_lag\\{consumergroup=\""g"\"" {s+=$NF; f=1} END{print f?s:"none"}'; }
fast=$(lag orders-fast); slow=$(lag orders-slow)
# fast: auto-commit every 5 s at about 3 msg/s => a few messages of lag is normal. Limit 30.
if [ "$slow" != none ] && [ "$fast" != none ] && [ "$slow" -gt 0 ] && [ "$fast" -le 30 ]; then
  echo "[green] fake data flowing (lag fast=$fast, slow=$slow)"
else
  echo "[RED] fake data flowing (lag fast=$fast, slow=$slow)"; exit 1
fi
