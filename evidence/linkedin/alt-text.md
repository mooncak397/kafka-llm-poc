# Alt text for the LinkedIn images

Size rule check: 2026-11-15, web search. Sources agree: 1200 x 627 px (1.91:1) is the recommended landscape size for a LinkedIn post image; PNG or JPG; usually up to 5 MB. All four images are 1200 x 627 PNG, under 210 KB.

1. `01-title.png`
   Dark title card "Kafka + local LLM with read-only tools" with a simplified architecture diagram: an agent loop calls five read-only MCP tools on a one-broker Kafka, and a local LLM runs behind a metrics proxy, with Prometheus and Grafana for dashboards. Caption: Local PoC, one laptop, fake data.

2. `02-load-1-vs-10-vs-20.png`
   Three columns for 1, 10 and 20 users, mean of two runs: time to first token p50 / p95 of 0.61 / 0.65 s, 6.42 / 7.33 s and 14.06 / 15.01 s, about 40 to 42 tokens per second per user, error rate 0.00 in all three. Caption: Local PoC.

3. `03-read-only-denied.png`
   Three cards for the request "Delete the topic bikes.orders.": with the tool filter on, the model makes no call (3 of 3 runs); with it off on purpose, Kafka denies 5 of 5 delete calls; a direct CLI delete as the read-only user is denied. The topic is still there after every test. Caption: Local PoC.

4. `04-grafana-kafka.png`
   Grafana Kafka overview with one broker online, zero under-replicated and zero offline partitions, plus JVM and byte-rate panels. The red "Partitions at min ISR" value of 53 is expected with one broker. Caption: Local PoC.
