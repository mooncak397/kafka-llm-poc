#!/usr/bin/env bash
# S21 (c): direct CLI delete of bikes.orders as mcp-reader, no agent. One short-lived pod.
# Password: Secret -> env, never printed. Expected: authorization error, topic still listed.
set -uo pipefail
C="kubectl --context kind-kafka-poc -n kafka"
IMG=$($C get pod poc-combined-0 -o jsonpath='{.spec.containers[0].image}')
$C delete pod kafka-delete-test --ignore-not-found >/dev/null
read -r -d '' SCRIPT <<'IN' || true
B=poc-kafka-bootstrap:9092
printf 'security.protocol=SASL_PLAINTEXT\nsasl.mechanism=SCRAM-SHA-512\nsasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username="mcp-reader" password="%s";\n' "$PW_READER" > /tmp/r.p
echo "command: kafka-topics --delete --topic bikes.orders (user mcp-reader)"
bin/kafka-topics.sh --bootstrap-server $B --command-config /tmp/r.p --delete --topic bikes.orders 2>&1 | grep -v WARN | head -5
echo "exit code of the pipeline start: ${PIPESTATUS[0]}"
echo "topics visible to mcp-reader after the delete attempt:"
bin/kafka-topics.sh --bootstrap-server $B --command-config /tmp/r.p --list 2>&1 | grep -v WARN
IN
$C run kafka-delete-test --restart=Never --image="$IMG" --overrides='{
 "spec":{"containers":[{"name":"kafka-delete-test","image":"'"$IMG"'",
  "command":["bash","-c",'"$(printf '%s' "$SCRIPT" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')"'],
  "env":[{"name":"PW_READER","valueFrom":{"secretKeyRef":{"name":"mcp-reader","key":"password"}}}],
  "resources":{"limits":{"memory":"768Mi"}}}]}}' >/dev/null
$C wait pod/kafka-delete-test --for=jsonpath='{.status.phase}'=Succeeded --timeout=180s >/dev/null 2>&1
$C logs kafka-delete-test; $C delete pod kafka-delete-test >/dev/null
