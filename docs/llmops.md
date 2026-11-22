# LLMOps: models, usage, quality

## One router for all models

Agents never name a model. They ask the model gateway for a **task** (`task-synthesis`), a **tier**
(`tier-high`) or `auto`; `config/models.yaml` decides the model:

| Tier | Model | Price (per 1M tokens in / out) | Typical tasks |
|---|---|---|---|
| low | `gemini-3.1-flash-lite` | $0.25 / $1.50 | intent parsing, extraction, classification |
| mid | `gemini-3.8-flash` | $0.75 / $3.75 | synthesis, tool use, product matching |
| high | `gemini-3.1-pro-preview` | $2.00 / $12.00 | reasoning, investment analysis, eval judge |

- **Plan ceiling**: the free plan never reaches `high` (`max_tier` in `config/plans.yaml`).
- **Fallback**: if a model fails, the next one of the tier is tried; then one tier up (never above the ceiling).
- **`auto`**: a free heuristic classifier scores the request; only unclear cases get a cheap LLM verdict (cached).
- **Canaries**: send a % of a tier to a new model (`canaries:` in `models.yaml`), compare at `GET /admin/canaries`.
- **Caching**: identical deterministic calls (temperature 0, no tools) are served from Redis.

Check that every model id + the key work: `docker compose exec model-gateway python -m model_gateway.models_check`.

## Tokens and cost per user, per topic

Every LLM call is recorded by the gateway — the only place that sees all calls, whatever the framework:

1. **Live** (Redis): today's tokens/cost per user and per topic → quotas are checked before every run.
2. **Ledger** (MySQL `llm_calls`): one row per attempt — model, tier, why it was chosen, tokens, cost, latency.
3. **Rollup** (MySQL `usage_daily`): user × topic × day — what `/usage` and the dashboards read.

Calls not made by a topic are booked to pseudo-topics: `routing` (the `/chat` classifier) and `evals`.

## Plans and limits (`config/plans.yaml`)

| Plan | Topics | Max tier | Tokens / day | $ / day | Requests / min |
|---|---|---|---|---|---|
| none | — | low | 0 | 0 | 0 |
| free (sign-up) | EV Helper | mid | 50,000 | 0.05 | 10 |
| pro | all | high | 2,000,000 | 5.00 | 60 |
| internal (service accounts) | all | high | 20,000,000 | 50.00 | 120 |

Change a user's plan: `PUT /api/v1/admin/users/{id}/plan` (a billing webhook would call this).

## Prompts and A/B tests

Prompts live in `prompts/<topic>.yaml` with a version; `experiments:` splits users between variants
(each user always sees the same one). Every run stores the **prompt fingerprint**, so cost, latency,
feedback and judge scores can be compared per variant at `GET /api/v1/admin/experiments`.

## Quality

| What | How |
|---|---|
| Offline evals | `evals/datasets/<topic>.jsonl` → deterministic checks + **LLM-as-judge** (groundedness counts double) |
| Regression gate | results vs `evals/baselines/<topic>.json` — pass rate, judge score, $/case, p95 latency |
| Online judging | a sample of real runs is judged hourly (`quality online`) |
| Drift | judge score, thumbs-down rate, errors, cost/run and tier mix vs the previous week (`quality drift`) |
| Feedback → dataset | 👍/👎 → eval-case candidate → admin approves → `quality export-cases` appends to the dataset |

```bash
docker compose --profile quality run --rm quality-worker offline --tags smoke --gate
```

## Grounding (why answers can be trusted)

- EV facts come from crawled pages; every number the model extracts must **literally appear on the page**.
- Product prices must appear next to a currency on the shop page; ranking is deterministic.
- Finance figures come from market-data tools only; a compliance agent must approve the answer.
- Web content is treated as untrusted data (fenced), and fetches are SSRF-safe.

## Privacy

Emails, phone/card numbers, IBANs … are redacted **before** any provider call and before storage
(`config/privacy.yaml`). Old prompt/answer text is removed by the daily retention job; users can export
or delete all their data (`/auth/me/export`, `DELETE /auth/me`).

