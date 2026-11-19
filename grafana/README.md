# Grafana dashboards

Provisioned from these JSON files (`make grafana` builds the ConfigMap, `make up` calls it).
`strimzi-kafka.json` and `strimzi-kafka-exporter.json` come from the Strimzi examples at tag 1.3.1
(Apache-2.0, see NOTICE). One change: the data source variable `DS_PROMETHEUS` is hidden and fixed to the
provisioned Prometheus. Edit a file, then run `make grafana` (reload within 30 s).

Open Grafana with `make pf-grafana`, then use a kiosk URL. Replace FROM and TO with absolute epoch
milliseconds (example: `date -d '2026-11-15 14:00' +%s%3N`).

```
# Strimzi Kafka
# http://127.0.0.1:3000/d/7ddce8bf9ea7ddb5/strimzi-kafka?orgId=1&kiosk&from=FROM&to=TO&refresh=
# Strimzi Kafka Exporter
# http://127.0.0.1:3000/d/52a9e2042fa225a2/strimzi-kafka-exporter?orgId=1&kiosk&from=FROM&to=TO&refresh=
# LLM overview (all runs; one run: add &var-run=u1)
# http://127.0.0.1:3000/d/llm-overview/llm-overview?orgId=1&kiosk&from=FROM&to=TO&refresh=
```

Empty panels on purpose (no cAdvisor/kubelet scrape, no matching JMX rule): Memory Usage, CPU Usage,
Available Disk Space, Unclean Leader Election Rate, Network Processor Avg Idle, Connection Count per Listener,
Failed Produce/Fetch Request Rate (no failures, so no series). "Partitions at minimum ISR" is red (53) on
purpose: one broker, replication 1.

`llm-overview.json` is our own (uid `llm-overview`), data source uid `prometheus`, metrics from the proxy v2. No GPU panels
(S15 skipped, cut line L2). Rate windows are 2 m (15 s scrape). Queue wait is DERIVED. In-flight, errors and token
rates have no `run` label, so the `run` variable does not filter them. The three bar panels use cumulative histograms per run.
The bottom row (agent LLM cost) reads the agent's `agent_llm_*` metrics: cost per model, mean cost per question and tokens by kind
(uncached input, cache read, cache write, output). The values are cumulative since the agent pod started, so a restart resets them.

## LLM load comparison (S26)

`llm-load-comparison.json` (uid `llm-load-comparison`, time zone UTC, no refresh). The variable `run` is hidden and fixed to
`u1,u10,u20`. One table and six bar panels per `run` label (TTFT p50 and p95, tokens/s median, queue wait p50 and p95 (derived),
error rate, request count) and one time series (in flight and requests per second). No GPU panel (S15 skipped) and no
tool-call panel (S25 skipped, no agent-mode data).

The bar and table panels are instant queries on cumulative histograms. They read the value at the END of the time range, so
the range must end after the last run. Any such range gives the same numbers. The time series needs the range of the runs.
Panel values agree with `evidence/results.md` Table 1 only within the bucket resolution of the proxy histograms (buckets 5 s,
10 s, 20 s, 30 s): u1 within about 5 %, u10 and u20 p50 within about 15 %, p95 up to 30 % too high. `u1` also holds 19 requests from
manual tests before S24 (19 of 293). Table 1 and the JSON files are clean.

Run time ranges, UTC, from `loadtest/results/load-raw-u*.json`. Start = field `date` (before warm-up). End = start + longest
request end (`start + total`) + 15 s, so the end is an upper bound. Date 2026-11-15.

| Run | Start (UTC) | End (UTC) | Start (epoch ms) | End (epoch ms) |
|---|---|---|---|---|
| u1 (1 of 2) | 15:03:11 | 15:07:25 | 1794754991000 | 1794755245329 |
| u1 (2 of 2) | 15:09:16 | 15:13:30 | 1794755356000 | 1794755610645 |
| u10 (1 of 2) | 15:15:18 | 15:19:40 | 1794755718000 | 1794755980616 |
| u10 (2 of 2) | 15:21:27 | 15:25:49 | 1794756087000 | 1794756349208 |
| u20 (1 of 2) | 15:27:37 | 15:32:06 | 1794756457000 | 1794756726658 |
| u20 (2 of 2) | 15:33:54 | 15:38:21 | 1794756834000 | 1794757101948 |

Kiosk URLs (after `make pf-grafana`). The first is the full range (15:02:00 to 15:40:00 UTC, the six runs, all panels).
The other three show one level: the `var-run` value filters the bar panels and the request rate. The in-flight line has no
`run` label, so the narrow time range is what isolates it. For the bar panels use this `to` or any later one.

```
# All six runs (use this for the time series, evidence 06)
# http://127.0.0.1:3000/d/llm-load-comparison/llm-load-comparison?orgId=1&kiosk&from=1794754920000&to=1794757200000&refresh=
# u1 only
# http://127.0.0.1:3000/d/llm-load-comparison/llm-load-comparison?orgId=1&kiosk&from=1794754920000&to=1794755670000&refresh=&var-run=u1
# u10 only
# http://127.0.0.1:3000/d/llm-load-comparison/llm-load-comparison?orgId=1&kiosk&from=1794755670000&to=1794756390000&refresh=&var-run=u10
# u20 only
# http://127.0.0.1:3000/d/llm-load-comparison/llm-load-comparison?orgId=1&kiosk&from=1794756390000&to=1794757200000&refresh=&var-run=u20
```

Prometheus keeps the run data in an `emptyDir`. A Prometheus pod restart deletes the data of the six runs and the panels go empty.
The JSON files in `loadtest/results/` stay.
