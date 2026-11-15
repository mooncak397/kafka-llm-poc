#!/usr/bin/env bash
# S07 proof: kafka-consumer-groups --describe as poc-admin in a short-lived pod. Password: Secret -> env, never printed.
C="kubectl --context kind-kafka-poc -n kafka"
$C delete pod kafka-lag-test --ignore-not-found >/dev/null
$C run kafka-lag-test --restart=Never --image=quay.io/strimzi/kafka:1.3.1-kafka-4.3.2 --overrides='{
 "spec":{"containers":[{"name":"kafka-lag-test","image":"quay.io/strimzi/kafka:1.3.1-kafka-4.3.2",
  "command":["bash","-c","printf \"security.protocol=SASL_PLAINTEXT\\nsasl.mechanism=SCRAM-SHA-512\\nsasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username=\\\"poc-admin\\\" password=\\\"%s\\\";\\n\" \"$PW\" > /tmp/a.p; for g in orders-fast orders-slow; do bin/kafka-consumer-groups.sh --bootstrap-server poc-kafka-bootstrap:9092 --command-config /tmp/a.p --describe --group $g 2>&1 | grep -v WARN; done"],
  "env":[{"name":"PW","valueFrom":{"secretKeyRef":{"name":"poc-admin","key":"password"}}}],
  "resources":{"limits":{"memory":"768Mi"}}}]}}' >/dev/null
$C wait pod/kafka-lag-test --for=jsonpath='{.status.phase}'=Succeeded --timeout=120s >/dev/null 2>&1
$C logs kafka-lag-test; $C delete pod kafka-lag-test >/dev/null
