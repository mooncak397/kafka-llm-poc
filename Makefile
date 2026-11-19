.PHONY: mcp-deploy mcp-test ollama-start ollama-stop up stop reset status check leakscan hooks data-start data-stop pf-prometheus pf-grafana grafana proxy-deploy proxy-test agent-deploy agent-test anthropic-key unsafe-demo down-unsafe ask score-test load-test load-dryrun results leakscan-test langchain-test
# kind runs on rootless podman. Context name kind-kafka-poc holds no OS user name.
export KIND_EXPERIMENTAL_PROVIDER = podman
CLUSTER = kafka-poc
NODE    = $(CLUSTER)-control-plane
CTX     = kind-$(CLUSTER)
STRIMZI_VERSION = 1.3.1
K       = kubectl --context $(CTX)
# Our own Ollama (user unit). The kind gateway IP exists only inside the rootless podman netns, so the unit runs there.
# Later steps (proxy S13, agent) read the URL from here. The system Ollama on 127.0.0.1:11434 is not ours: never touch it.
OLLAMA_URL = http://10.89.1.1:11435
OLLAMA_UNIT = ollama-poc.service
PROXY_IMAGE = localhost/llm-proxy:0.2.0  # keep in sync with k8s/10-llm-proxy.yaml
AGENT_IMAGE = localhost/kafka-agent:0.1.5  # keep in sync with k8s/12-agent.yaml
MCP_IMAGE = localhost/mcp-confluent:1.6.1  # keep in sync with k8s/11-mcp-confluent.yaml
# The local model: ONE place. check-ollama.sh and the raw load mode use it (S20 recommendation).
OLLAMA_MODEL = qwen3:4b-instruct-2507-q4_K_M
# The agent backend and model, filled into k8s/12-agent.yaml by sed at deploy. Default = local Ollama.
# Hosted Claude (needs `make anthropic-key` once): make agent-deploy AGENT_BACKEND=anthropic AGENT_MODEL=claude-sonnet-5-5
# (or claude-haiku-5-5). A redeploy without these variables (agent-deploy, up, unsafe-demo, down-unsafe) keeps the deployed
# backend and model; no deployment = Ollama. Back to local: make agent-deploy AGENT_BACKEND=ollama
# Reads only the LLM_BACKEND and MODEL env values of deploy/agent (never the key). Recursive `=`, so kubectl runs only when used.
DEPLOYED_ENV = $(K) -n llm get deploy agent -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="$(1)")].value}' 2>/dev/null
ifeq ($(origin AGENT_BACKEND),command line)
AGENT_MODEL = $(OLLAMA_MODEL)
else
AGENT_BACKEND = $(or $(shell $(call DEPLOYED_ENV,LLM_BACKEND)),ollama)
AGENT_MODEL = $(or $(shell $(call DEPLOYED_ENV,MODEL)),$(OLLAMA_MODEL))
endif
AGENT_EFFORT = medium
AGENT_DAILY_MAX_USD = 5
# Profile knobs. Defaults = SAFE profile (layer 1 on). Only `make unsafe-demo` overrides them, for the negative test (S21).
MCP_EXTRA =
MCP_READ_ONLY = true
AGENT_EXTRA_TOOLS =
MCP_SED = sed -e 's|__MCP_EXTRA__|$(MCP_EXTRA)|' -e 's|__MCP_READ_ONLY__|$(MCP_READ_ONLY)|'
AGENT_SED = sed -e 's|__AGENT_MODEL__|$(AGENT_MODEL)|' -e 's|__AGENT_EXTRA_TOOLS__|$(AGENT_EXTRA_TOOLS)|' -e 's|__AGENT_BACKEND__|$(AGENT_BACKEND)|' \
	-e 's|__AGENT_EFFORT__|$(AGENT_EFFORT)|' -e 's|__AGENT_DAILY_MAX_USD__|$(AGENT_DAILY_MAX_USD)|'

up:       ## create the cluster if missing, start the node if stopped, wait Ready, then check
	@if ! kind get clusters 2>/dev/null | grep -qx $(CLUSTER); then kind create cluster --config k8s/00-kind.yaml --wait 180s; \
	else podman start $(NODE) >/dev/null; kind export kubeconfig --name $(CLUSTER); fi
	@$(K) wait --for=condition=Ready node --all --timeout=180s
	@$(K) apply -f k8s/01-namespaces.yaml
	@helm --kube-context $(CTX) upgrade --install strimzi oci://quay.io/strimzi-helm/strimzi-kafka-operator \
		--version $(STRIMZI_VERSION) -n strimzi -f k8s/02-strimzi-values.yaml --wait --timeout 300s
	@$(K) apply -f k8s/03-kafka-metrics.yaml -f k8s/04-kafka.yaml -f k8s/05-kafka-users.yaml
	@$(K) -n kafka wait kafka/poc --for=condition=Ready --timeout=300s
	@$(MAKE) --no-print-directory data-start
	@$(K) apply -f k8s/08-prometheus.yaml
	@$(K) -n monitoring rollout status deploy/prometheus --timeout=180s
	@$(MAKE) --no-print-directory grafana
	@$(MAKE) --no-print-directory ollama-start
	@$(MAKE) --no-print-directory mcp-deploy
	@$(MAKE) --no-print-directory agent-deploy
	@$(MAKE) --no-print-directory check
data-start: ## topics, users, then producer and both consumer groups (apply sets replicas back to 1)
	@$(K) apply -f k8s/06-fake-data-topics-users.yaml
	@$(K) -n kafka wait kafkauser/fake-producer kafkauser/fake-consumer --for=condition=Ready --timeout=120s
	@$(K) -n kafka wait kafkatopic --all --for=condition=Ready --timeout=120s
	@$(K) apply -f k8s/07-fake-data-workloads.yaml
pf-prometheus: ## Prometheus UI on http://127.0.0.1:9090 (localhost only, Ctrl-C stops)
	@$(K) -n monitoring port-forward --address 127.0.0.1 svc/prometheus 9090:9090
pf-grafana: ## Grafana UI on http://127.0.0.1:3000 (localhost only, anonymous Viewer, Ctrl-C stops)
	@$(K) -n monitoring port-forward --address 127.0.0.1 svc/grafana 3000:3000
grafana:  ## admin Secret (random, only if missing, never printed), dashboards ConfigMap from grafana/*.json, then Grafana
	@$(K) -n monitoring get secret grafana-admin >/dev/null 2>&1 || openssl rand -hex 16 | tr -d '\n' | $(K) -n monitoring create secret generic grafana-admin --from-file=admin-password=/dev/stdin >/dev/null
	@$(K) -n monitoring create configmap grafana-dashboards --from-file=grafana --dry-run=client -o yaml | $(K) apply --server-side --force-conflicts -f - >/dev/null
	@$(K) apply -f k8s/09-grafana.yaml >/dev/null
	@$(K) -n monitoring rollout status deploy/grafana --timeout=240s
proxy-test: ## unit tests of the proxy
	@uv run --project proxy pytest proxy -q
proxy-deploy: ## build the proxy image (podman), load it into kind (no registry), apply, wait
	@cd proxy && uv export --no-dev --no-emit-project -q -o requirements.txt && podman build -q -t $(PROXY_IMAGE) . >/dev/null 2>&1
	@podman save -o proxy/image.tar $(PROXY_IMAGE) 2>/dev/null && kind load image-archive proxy/image.tar --name $(CLUSTER) && rm -f proxy/image.tar
	@sed 's|__OLLAMA_URL__|$(OLLAMA_URL)|' k8s/10-llm-proxy.yaml | $(K) apply -f - >/dev/null
	@$(K) apply -f k8s/08-prometheus.yaml >/dev/null
	@$(K) -n llm rollout status deploy/llm-proxy --timeout=120s
mcp-deploy: ## build the mcp-confluent image (podman, skipped if present), load it into kind (no registry), apply, wait
	@podman image exists $(MCP_IMAGE) || podman build -q -t $(MCP_IMAGE) mcp >/dev/null
	@podman save -o mcp/image.tar $(MCP_IMAGE) 2>/dev/null && kind load image-archive mcp/image.tar --name $(CLUSTER) && rm -f mcp/image.tar
	@$(K) -n kafka get secret mcp-api-key >/dev/null 2>&1 || openssl rand -hex 24 | tr -d '\n' | $(K) -n kafka create secret generic mcp-api-key --from-file=key=/dev/stdin >/dev/null
	@$(MCP_SED) k8s/11-mcp-confluent.yaml | $(K) apply -f - >/dev/null
	@$(K) -n kafka rollout status deploy/mcp-confluent --timeout=120s
agent-test: ## unit tests of the agent (fake Ollama, fake MCP)
	@uv run --project agent pytest agent -q
agent-deploy: ## build the agent image (podman), load it into kind (no registry), copy the MCP key Secret into ns llm by pipe, apply, wait
	@cd agent && uv export --no-dev --no-emit-project -q -o requirements.txt && podman build -q -t $(AGENT_IMAGE) . >/dev/null 2>&1
	@podman save -o agent/image.tar $(AGENT_IMAGE) 2>/dev/null && kind load image-archive agent/image.tar --name $(CLUSTER) && rm -f agent/image.tar
	@$(K) apply -f k8s/01-namespaces.yaml >/dev/null
	@$(K) -n kafka get secret mcp-api-key -o jsonpath='{.data.key}' | base64 -d | $(K) -n llm create secret generic mcp-api-key --from-file=key=/dev/stdin --dry-run=client -o yaml | $(K) apply -f - >/dev/null
	@[ "$(AGENT_BACKEND)" != anthropic ] || $(K) -n llm get secret anthropic-api-key >/dev/null 2>&1 || { echo "no Secret anthropic-api-key in ns llm: run make anthropic-key first"; exit 1; }
	@$(AGENT_SED) k8s/12-agent.yaml | $(K) apply -f - >/dev/null
	@$(K) apply -f k8s/08-prometheus.yaml >/dev/null
	@$(K) -n llm rollout status deploy/agent --timeout=120s
anthropic-key: ## store the Anthropic API key as Secret anthropic-api-key in ns llm (hidden prompt, pipe; never printed, never a file)
	@$(K) apply -f k8s/01-namespaces.yaml >/dev/null
	@printf 'Anthropic API key (input hidden): '; stty -echo; read -r k; stty echo; echo; [ -n "$$k" ] || { echo "empty, nothing stored"; exit 1; }; \
	printf '%s' "$$k" | $(K) -n llm create secret generic anthropic-api-key --from-file=key=/dev/stdin --dry-run=client -o yaml | $(K) apply -f - >/dev/null && echo "Secret anthropic-api-key stored"
score-test: ## unit tests of the S20 scorer (no cluster needed)
	@cd loadtest && python3 -m unittest -q test_score
LOAD = uv run --project loadtest python loadtest/load.py
load-test: ## unit tests of the load stats code and the results generator (no cluster needed)
	@uv run --project loadtest pytest loadtest -q
load-dryrun: ## 1 user, 2 questions, both modes, label dry (ignored by make results). Needs the cluster (make up)
	@$(LOAD) --model $(OLLAMA_MODEL) --mode raw --users 1 --duration 60 --max-requests 2 --run dry
	@$(LOAD) --model $(AGENT_MODEL) --mode agent --users 1 --duration 120 --max-requests 2 --run dry
langchain-test: ## unit tests of the LangChain demo (own venv, no cluster, no API)
	@uv run --project demos/langchain pytest demos/langchain -q
results:  ## evidence/results.md from loadtest/results/load-*.json (skips label dry)
	@uv run --project loadtest python loadtest/make_results.py
ask: ## make ask Q="Which topics exist?" [RUN=u1] : asks the agent pod (kubectl exec, no port-forward)
	@./scripts/ask.sh $(CTX) "$(Q)" "$(RUN)"
mcp-test: ## MCP tools/list + list-topics + get-consumer-group-lag through a port-forward
	@./scripts/mcp-tools-list.sh $(CTX)
data-stop:  ## stop producer and consumers (topics and data stay)
	@$(K) -n kafka scale deploy fake-producer consumer-orders-fast consumer-orders-slow --replicas=0
ollama-start: ## install and start OUR Ollama user unit (node must run first; idempotent)
	@mkdir -p ~/.config/systemd/user
	@cmp -s scripts/ollama-poc.service ~/.config/systemd/user/$(OLLAMA_UNIT) || { cp scripts/ollama-poc.service ~/.config/systemd/user/$(OLLAMA_UNIT); systemctl --user daemon-reload; systemctl --user restart $(OLLAMA_UNIT); }
	@systemctl --user start $(OLLAMA_UNIT)
	@for i in $$(seq 30); do podman exec $(NODE) curl -sf -m2 $(OLLAMA_URL)/api/version >/dev/null && exit 0; sleep 1; done; echo "ollama (poc) did not answer"; exit 1
ollama-stop: ## stop only OUR Ollama (never the system one)
	@systemctl --user stop $(OLLAMA_UNIT) 2>/dev/null || true
stop:     ## stop OUR Ollama and the node container; data stays
	@$(MAKE) --no-print-directory ollama-stop
	@podman stop $(NODE) 2>/dev/null || true
	@echo "stopped"
reset:    ## delete the cluster and rebuild from k8s/ (fake data only)
	@read -p "Delete cluster $(CLUSTER) and rebuild? [y/N] " a; [ "$$a" = y ] || { echo aborted; exit 1; }
	@kind delete cluster --name $(CLUSTER)
	@$(MAKE) --no-print-directory up
unsafe-demo: ## NEGATIVE TEST ONLY: layer 1 off (delete-topics allowed, read_only false). Layers 2 and 3 stay. Undo: make down-unsafe
	@echo "!!! UNSAFE PROFILE ON: the model can see delete-topics. Same mcp-reader user (no write ACL). Run make down-unsafe after the test."
	@$(MAKE) --no-print-directory mcp-deploy agent-deploy MCP_EXTRA=,delete-topics MCP_READ_ONLY=false AGENT_EXTRA_TOOLS=delete-topics
down-unsafe: ## back to the safe profile (five read tools, read_only true, no extra env); safe to repeat
	@$(MAKE) --no-print-directory mcp-deploy agent-deploy
status:
	@echo "node container: $$(podman inspect -f '{{.State.Status}}' $(NODE) 2>/dev/null || echo none)"
	@$(K) get nodes --no-headers 2>/dev/null || echo "kubectl: no answer"
	@{ $(K) -n kafka get deploy mcp-confluent -o jsonpath='{.spec.template.spec.containers[0].args}'; $(K) -n llm get deploy agent -o jsonpath='{.spec.template.spec.containers[0].env}'; } 2>/dev/null | grep -q delete-topics && echo "!!! UNSAFE PROFILE ON (delete-topics allowed). Run: make down-unsafe" || true
check:    ## each step adds its own line
	@$(K) get nodes --no-headers 2>/dev/null | grep -qw Ready && echo "[green] kind node Ready" || { echo "[RED] kind node Ready"; exit 1; }
	@$(K) -n strimzi get deploy strimzi-cluster-operator -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null | grep -qx True && echo "[green] strimzi operator ready" || { echo "[RED] strimzi operator ready"; exit 1; }
	@$(K) -n kafka get kafka poc -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null | grep -qx True && echo "[green] kafka ready" || { echo "[RED] kafka ready"; exit 1; }
	@t=$$($(K) -n kafka get kafkauser poc-admin mcp-reader -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{" "}{end}' 2>/dev/null); n=$$($(K) -n kafka get secret poc-admin mcp-reader -o name 2>/dev/null | wc -l); [ "$$t" = "True True " ] && [ "$$n" = 2 ] && echo "[green] kafka users ready (poc-admin, mcp-reader, secrets exist)" || { echo "[RED] kafka users ready"; exit 1; }
	@./scripts/check-fake-data.sh $(CTX)
	@./scripts/check-prometheus.sh $(CTX)
	@./scripts/check-grafana.sh $(CTX)
	@./scripts/check-ollama.sh $(OLLAMA_URL) $(CTX) $(OLLAMA_MODEL)
	@./scripts/check-proxy.sh $(CTX)
	@./scripts/check-mcp.sh $(CTX)
	@./scripts/check-agent.sh $(CTX)
	@./scripts/check-profile.sh $(CTX)
leakscan:
	@./scripts/leakscan.sh
leakscan-test: ## rule 5 skips only the accepted merge hash; a non-noreply commit in a throwaway repo still fails
	@./scripts/test-commit-emails.sh
hooks:    ## install the tracked pre-commit hook (runs leakscan) into .git/hooks
	@install -m 755 scripts/pre-commit-hook.sh .git/hooks/pre-commit && echo "pre-commit hook installed"
