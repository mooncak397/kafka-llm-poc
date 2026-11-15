#!/usr/bin/env bash
# Short-lived pod: logs in as poc-admin over SCRAM and lists topics. Password goes Secret -> env, never printed.
set -euo pipefail
C="kubectl --context kind-kafka-poc -n kafka"
IMG=$($C get pod poc-combined-0 -o jsonpath='{.spec.containers[0].image}')
$C delete pod kafka-cli-test --ignore-not-found >/dev/null
$C run kafka-cli-test --rm -i --restart=Never --image="$IMG" --overrides='{
 "spec":{"containers":[{"name":"kafka-cli-test","image":"'"$IMG"'","stdin":true,
  "command":["bash","-c","printf \"security.protocol=SASL_PLAINTEXT\\nsasl.mechanism=SCRAM-SHA-512\\nsasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username=\\\"poc-admin\\\" password=\\\"$PW\\\";\\n\" > /tmp/c.properties; bin/kafka-topics.sh --bootstrap-server poc-kafka-bootstrap:9092 --command-config /tmp/c.properties --list && echo LIST_OK"],
  "env":[{"name":"PW","valueFrom":{"secretKeyRef":{"name":"poc-admin","key":"password"}}}],
  "resources":{"limits":{"memory":"512Mi"}}}]}}' 2>&1 | grep -v '^pod ".*" deleted' 
