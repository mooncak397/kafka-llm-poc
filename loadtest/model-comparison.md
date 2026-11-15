# Model comparison for the agent (S20)

Date: 2026-11-15. Ollama 0.40.16 (our instance, `OLLAMA_NUM_PARALLEL=1`, `num_ctx` 4096). GPU: GTX 1060 6 GB.
Question set: `loadtest/questions-tools.jsonl`, **N = 12 questions per run** (3 simple, 4 single tool with argument, 3 two-step, 2 unanswerable).
One laptop, one broker, fake data. One user, one question at a time. Every number below is "on this question set"; with n = 12 a difference of 1 or 2 questions is inside the run-to-run noise (temperature 0.6, runs of the same model differ by up to 2).

Same agent image, same system prompt, same options, same 5 read-only tools for every model (the prompt is the one tuned for `qwen3:4b` in S19; no per-model change). `think` is never sent.

## Results

Per-run values are shown as `run1 / run2 / run3` (first run for models with one run). Tool, args, answer: questions correct out of 12.

| Model (tag) | Disk size | Runs | Tool correct | Args correct | Answer correct | Empty answers | Median s/answer | Max s | VRAM MiB (nvidia-smi, loaded model minus idle) |
|---|---|---|---|---|---|---|---|---|---|
| `qwen3.5:4b` | 3.3 GB | 3 | 11 / 11 / 11 | 11 / 11 / 11 | 12 / 11 / 11 | 0 / 0 / 0 | 16.3 / 16.6 / 16.4 | 51 / 69 / 77 | 3386 (24% CPU / 76% GPU: does not fit) |
| `qwen3:4b-instruct-2507-q4_K_M` | 2.5 GB | 3 | 9 / 11 / 10 | 9 / 10 / 10 | 9 / 10 / 10 | 1 / 0 / 0 | 2.1 / 2.0 / 2.0 | 8.9 / 10.6 / 8.8 | 3050 |
| `granite4:3b` | 2.1 GB | 3 | 10 / 10 / 10 | 10 / 10 / 10 | 10 / 10 / 9 | 0 / 0 / 0 | 2.9 / 2.6 / 2.4 | 7.4 / 7.2 / 7.0 | 2402 |
| `qwen2.5:3b` | 1.9 GB | 2 | 11 / 10 | 11 / 10 | 9 / 8 | 0 / 0 | 2.5 / 2.7 | 7.5 / 7.1 | 2072 |
| `qwen3:4b` (baseline, thinking-only) | 2.5 GB | 1 | 10 | 10 | 8 | 3 | 17.0 | 32.9 | 3054 |
| `qwen3:1.7b` | 1.4 GB | 1 | 10 | 10 | 9 | 3 | 7.1 | 16.0 | 1637 |
| `llama3.2:3b` | 2.0 GB | 1 | 9 | 7 | 5 | 0 | 3.4 | 7.3 | 2455 |
| `granite3.3:2b` | 1.5 GB | 1 | 4 | 3 | 2 | 1 | 0.8 | 5.1 | 1872 |
| `phi4-mini` (latest) | 2.5 GB | 1 | 2 | 2 | 1 | 0 | 1.4 | 6.9 | 2975 |

Empty answers: the agent hit the 600-token cap while the model was still thinking (qwen3 family). Errors (timeouts, HTTP) were 0 in every run.
VRAM: `nvidia-smi` memory used after the run (model still loaded) minus before the run (model unloaded, system embedder and desktop only). It includes the 4096-token KV cache. `ollama ps` showed 100% GPU for all models except `qwen3.5:4b` (3.7 GB, 24% CPU / 76% GPU). Host RAM available fell to 3.2 GB with `qwen3.5:4b` (above the 1.5 GB stop line).

Gate (plan A11/S20, 60 percent tool correctness): **passed.** The best model made the right tool call on 11 of 12 questions in all 3 runs. The baseline `qwen3:4b` also passed on tool correctness (10 of 12), but gave 3 empty answers and took 17 s median.

## What the failures look like (from the run files)

- `phi4-mini` and `granite3.3:2b`: they write the tool call as plain text in the answer instead of a structured `tool_calls` field. The agent sees no tool call. With the same prompt and request they do not work at all (no per-model tuning was done).
- `llama3.2:3b`: calls tools, but with wrong arguments (for example group `slow`, or `topics` set to a group name), and answers as JSON text on some questions.
- Unanswerable questions (q11 who can read, q12 retention): the models that call a tool anyway invent an answer. Examples: `granite4:3b` and `qwen2.5:3b` gave a made-up retention number (the offset), and `qwen3.5:4b` said consumer groups "can read" the topic in 2 of 3 runs. `qwen3:4b-instruct-2507` refused correctly in all 3 runs.
- Two-step questions q09 and q10: `qwen3:4b-instruct-2507` fails them in some runs because it calls `list-consumer-groups` with `matchStates: ["Stable"]` and then concludes that `orders-slow` does not exist (the slow consumer group may not be "Stable" between its short consumers; not checked). It is a model habit with this tool, not a scorer problem.

## Scorer change during the bake-off (disclosure)

The first round of 9 runs found two scorer bugs: a correct answer written with a narrow no-break space ("18 762") or a non-breaking hyphen ("orders‑slow") was marked wrong, and the honest refusal "tools don't provide access control information" was not recognised. I fixed both (unit tests added) and re-ran every model. The first round is in `loadtest/results/_archive/round1-before-scorer-fix/`. The numbers above are from the fixed scorer only.

## Recommendation

**Use `qwen3:4b-instruct-2507-q4_K_M` for the load tests S23 to S25.**

1. Quality first: on this question set it is in the top group. Tool correct 9, 11, 10 of 12 against 11, 11, 11 for `qwen3.5:4b`, 10, 10, 10 for `granite4:3b`. The gaps are 0 to 2 questions of 12, which n = 12 cannot separate. It had 0 empty answers in the last 2 runs and it is the only model that refused both unanswerable questions in all 3 runs.
2. Speed: median 2.0 s per answer (max 10.6 s) against 16 s median (max 77 s) for `qwen3.5:4b`. With `NUM_PARALLEL=1`, 10 and 20 users queue. At 16 s per answer the queue wait of the 20th user would pass the 180 s request timeout, so the load test would measure timeouts, not the system.
3. Fit: 3050 MiB, 100% on the GPU. `qwen3.5:4b` needs 3.7 GB, runs 24% on the CPU and leaves 3.2 GB host RAM. CPU offload also adds noise to every timing.

If the aim is the best answers at one user (not load), `qwen3.5:4b` leads on this set (tool correct 11 of 12 in 3 of 3 runs; answer correct 12, 11, 11) at 8 times the time per answer.

Known weakness to state in the README: `qwen3:4b-instruct-2507` loses 1 to 3 of 12 on the two-step questions (q09, q10) and the list-groups habit above. The claim "correct under load" must carry the single-user score from this table as its base.
