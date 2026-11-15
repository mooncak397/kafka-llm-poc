#!/usr/bin/env bash
# S06 test: what can mcp-reader do? One short-lived pod. Passwords: Secret -> env, never printed.
# Uses a throw-away topic and group made by poc-admin, deleted at the end (also on failure).
set -uo pipefail
C="kubectl --context kind-kafka-poc -n kafka"
IMG=$($C get pod poc-combined-0 -o jsonpath='{.spec.containers[0].image}')
$C delete pod kafka-acl-test --ignore-not-found >/dev/null
read -r -d '' SCRIPT <<'IN' || true
B=poc-kafka-bootstrap:9092
mk(){ printf 'security.protocol=SASL_PLAINTEXT\nsasl.mechanism=SCRAM-SHA-512\nsasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username="%s" password="%s";\n' "$1" "$2" > /tmp/$1.p; }
mk poc-admin "$PW_ADMIN"; mk mcp-reader "$PW_READER"
A="bin/kafka-topics.sh --bootstrap-server $B --command-config /tmp/poc-admin.p"
R="--bootstrap-server $B --command-config /tmp/mcp-reader.p"
t(){ n=$1; shift; if out=$("$@" 2>&1) && ! echo "$out" | grep -q 'AuthorizationException\|AUTHORIZATION_FAILED'; then echo "[ALLOWED] $n"; else echo "[DENIED ] $n: $(echo "$out" | grep -oE '[A-Za-z]+AuthorizationException[^.]*|AUTHORIZATION_FAILED[^.]*' | head -1)"; fi; }
echo "ACLs of mcp-reader:"; bin/kafka-acls.sh --bootstrap-server $B --command-config /tmp/poc-admin.p --list --principal User:mcp-reader | grep -E "ResourcePattern|\(principal" | sed "s/^ *//"
$A --create --topic s06-tmp --partitions 1 2>&1 | grep -v WARN
echo hello | bin/kafka-console-producer.sh --bootstrap-server $B --producer.config /tmp/poc-admin.p --topic s06-tmp >/dev/null 2>&1
bin/kafka-console-consumer.sh --bootstrap-server $B --consumer.config /tmp/poc-admin.p --topic s06-tmp --group s06-grp --from-beginning --max-messages 1 --timeout-ms 15000 >/dev/null 2>&1
t "reader list topics"            bin/kafka-topics.sh $R --list
echo "reader sees: $(bin/kafka-topics.sh $R --list 2>&1 | tr "\n" " ")"; bin/kafka-topics.sh $R --describe --topic s06-tmp 2>&1 | tail -3
t "reader describe topic"         bin/kafka-topics.sh $R --describe --topic s06-tmp
t "reader list groups"            bin/kafka-consumer-groups.sh $R --list
t "reader describe group"         bin/kafka-consumer-groups.sh $R --describe --group s06-grp
t "reader describe cluster"       bin/kafka-metadata-quorum.sh $R describe --status
t "reader DescribeUserScramCredentials (all)" bin/kafka-configs.sh $R --describe --entity-type users
t "reader DescribeUserScramCredentials (one)" bin/kafka-configs.sh $R --describe --entity-type users --entity-name mcp-reader
t "admin  DescribeUserScramCredentials (all)" bin/kafka-configs.sh --bootstrap-server $B --command-config /tmp/poc-admin.p --describe --entity-type users
t "reader describe topic config (DescribeConfigs)" bin/kafka-configs.sh $R --describe --entity-type topics --entity-name s06-tmp
t "reader create topic (must deny)" bin/kafka-topics.sh $R --create --topic s06-evil --partitions 1
t "reader delete topic (must deny)" bin/kafka-topics.sh $R --delete --topic s06-tmp
t "reader write (must deny)"      sh -c "echo x | bin/kafka-console-producer.sh --bootstrap-server $B --producer.config /tmp/mcp-reader.p --topic s06-tmp"
t "reader delete group (must deny)" bin/kafka-consumer-groups.sh $R --delete --group s06-grp
t "reader alter acls (must deny)" bin/kafka-acls.sh $R --add --allow-principal User:x --operation Read --topic s06-tmp
t "reader alter scram (must deny)" bin/kafka-configs.sh $R --alter --entity-type users --entity-name x --add-config 'SCRAM-SHA-512=[password=x]'
bin/kafka-consumer-groups.sh --bootstrap-server $B --command-config /tmp/poc-admin.p --delete --group s06-grp >/dev/null 2>&1
$A --delete --topic s06-tmp >/dev/null 2>&1; $A --delete --topic s06-evil >/dev/null 2>&1
echo "leftover topics: $($A --list | grep -c '^s06-')"
IN
$C run kafka-acl-test --restart=Never --image="$IMG" --overrides='{
 "spec":{"containers":[{"name":"kafka-acl-test","image":"'"$IMG"'",
  "command":["bash","-c",'"$(printf '%s' "$SCRIPT" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')"'],
  "env":[{"name":"PW_ADMIN","valueFrom":{"secretKeyRef":{"name":"poc-admin","key":"password"}}},
         {"name":"PW_READER","valueFrom":{"secretKeyRef":{"name":"mcp-reader","key":"password"}}}],
  "resources":{"limits":{"memory":"768Mi"}}}]}}' >/dev/null
# logs, not attach: attach loses the first lines of a fast pod
$C wait pod/kafka-acl-test --for=jsonpath='{.status.phase}'=Succeeded --timeout=180s >/dev/null 2>&1
$C logs kafka-acl-test; $C delete pod kafka-acl-test >/dev/null
