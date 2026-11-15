#!/usr/bin/env bash
# S20 model bake-off, one model per call: bakeoff.sh <tag> [label-suffix]
# Sets MODEL on the agent, waits for the rollout, unloads our Ollama models, runs score.py, records VRAM (nvidia-smi delta + `ollama ps`), unloads.
# Uses OUR Ollama only (inside the rootless netns). Output: loadtest/results/bakeoff-<tag>.log
set -uo pipefail
cd "$(dirname "$0")/.."
TAG="$1"; SFX="${2:-}"
O() { podman unshare --rootless-netns env OLLAMA_HOST=10.89.1.1:11435 ollama "$@"; }
vram() { nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits; }
unload() { O ps | awk 'NR>1{print $1}' | while read -r m; do O stop "$m"; done; sleep 3; }
kubectl --context kind-kafka-poc -n llm set env deploy/agent MODEL="$TAG" >/dev/null
kubectl --context kind-kafka-poc -n llm rollout status deploy/agent --timeout=120s >/dev/null
unload
echo "vram_before_MiB $(vram) avail_MB $(free -m | awk '/Mem:/{print $7}')"
python3 loadtest/score.py --model "$TAG$SFX"
echo "vram_after_MiB $(vram) avail_MB $(free -m | awk '/Mem:/{print $7}')"
O ps
unload
