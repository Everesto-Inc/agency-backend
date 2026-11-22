# Evals

| Path | What |
|---|---|
| `datasets/topic_routing.jsonl` | /chat topic router golden set (offline, CI) |
| `datasets/model_tiers.jsonl` | model-tier classifier golden set (offline, CI) |
| `datasets/<topic>.jsonl` | live agent eval cases (real models; LLM-as-judge) |
| `baselines/<topic>.json` | accepted summary the regression gate compares against |
| `reports/` | per-run JSON reports (gitignored) |

Case schema (`services/quality_worker/quality/datasets.py`):

```json
{"id": "ev-001", "topic": "ev_research", "prompt": "...",
 "checks": {"must_contain": [], "must_contain_any": [], "must_not_contain": [], "expect_refusal": false,
            "require_sources": false, "max_latency_s": null, "max_cost_usd": null, "min_judge_score": 3.5},
 "rubric": "what a good answer must do", "reference": "known-good facts (optional)", "tags": ["smoke"]}
```

A case passes when every deterministic check passes **and** the judge's overall score
(groundedness weighted double) is at least `min_judge_score`.

Workflow:
1. `docker compose --profile quality run --rm quality-worker offline --gate` runs every dataset against the running stack (needs provider keys).
2. First good run: `... quality-worker offline --update-baseline` writes `baselines/`; commit it.
3. PRs touching prompts, models or topic code run `quality offline --gate` in CI and fail on regression.
4. User 👍/👎 creates `eval_cases` candidates; an admin approves them
   (`PUT /api/v1/admin/eval-cases/{id}`), then `docker compose --profile quality run --rm quality-worker export-cases` appends them here.
