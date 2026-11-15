`request-template.json`: the chat request body for the pinned model `qwen3:4b-instruct-2507-q4_K_M` (S17b, model tag updated in S23). Do NOT send `think` (the plain thinking-only `qwen3:4b` build ignores `think:false`; see the model note in the main README). Fill `QUESTION` and `tools`. S19 and S23 copy these fields.

S20: `questions-tools.jsonl` (12 questions, expected tools and arguments from the fake data), `score.py` (scorer, `--model` labels only), `test_score.py` (`make score-test`), `bakeoff.sh` (one model per call), `results/` (one small JSON per run), `model-comparison.md` (table and recommendation).

S23: `load.py` (load script, `--mode raw|agent`), `make_results.py` (writes `evidence/results.md`), `questions-raw.txt` (raw mode questions), `test_load.py` (`make load-test`), `pyproject.toml` + `uv.lock` (own uv project). Results: `results/load-<mode>-<label>-<UTC>.json`. Label `dry` = dry runs, ignored by `make results`.
