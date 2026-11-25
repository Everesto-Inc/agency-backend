# Data (MySQL & Redis)

## MySQL

Local: MySQL 8.4 in compose (started with HeatWave's defaults: primary key required, UTC, utf8mb4).
Production: OCI HeatWave MySQL. Schema = `migrations/NNNN_*.sql`, applied by the `migrate` job.

| Table | One row per | Written by |
|---|---|---|
| `users` | account (email, role, plan, plan expiry) | edge |
| `refresh_tokens` | issued refresh token (hashed), with its session family | edge |
| `agent_runs` | question answered by a topic (status, tokens, cost, latency, PII-redacted prompt/answer, feedback) | topic services |
| `conversations` | chat history: one row per conversation (id = chat `session_id`, title, pinned, turns) — its messages are that session's `agent_runs` | edge (`/chat`) |
| `llm_calls` | LLM attempt (model, tier, why, tokens, cost, latency, cache/canary) | model gateway |
| `usage_daily` | user × topic × day (runs, calls, tokens, cost) — billing / dashboards | gateway + topics |
| `country_level_info`, `ev_model_specs`, `ev_research_runs` | verified EV facts (cached) and the EV audit log | EV Helper |
| `product_offers` | verified offer seen (price history) | Product Finder |
| `eval_cases`, `eval_runs`, `eval_results`, `drift_reports` | evaluation data | quality worker, feedback |

Rules:

- **All SQL lives in repositories** (`libs/agency_platform/agency_platform/repositories/`, `auth/accounts.py`,
  and each topic's `repository.py`). Routes and agents call repositories — never SQL.
- Migrations are **forward-only and re-runnable** (MySQL DDL isn't transactional). Never edit an applied
  migration; add `NNNN_next.sql`.
- Timestamps are UTC (`DATETIME(6)`), money is `DECIMAL`, ids are UUID strings.

## Chat memory

What an assistant remembers of the current chat: the last messages, word for word (EV / Finance / Product Finder
read the last 4). **One memory per chat**, shared by every assistant — switching assistant keeps the context.

| Copy | Where | Kept |
|---|---|---|
| fast | Redis `sess:<user>:<chat>` (last 20 messages) | 24 h after the last message |
| durable | MySQL `agent_runs` of that chat (PII-redacted) | text 30 days (`config/privacy.yaml`) |

When Redis has nothing (expired, restarted), the runner reloads the chat's last answered turns from MySQL and puts
them back into Redis — reopening an old chat continues with context. Deleting a chat erases both.

**Long chats** — the older part of a chat (beyond the last 4 messages) is kept as a short **running summary**
(`conversations.summary`, ≤ 8 bullets): after an answer, in the background, the cheap model (task `summarization`,
low tier) folds the older messages into it once 6 of them are not summarized yet (≈ every 3 questions). Every
assistant gets "summary + last 4 messages + question". The prompt is `prompts/memory.yaml` (editable, versioned).

## Redis

Everything in Redis expires or can be rebuilt — losing it loses no permanent data.

| Key pattern | What |
|---|---|
| `usage:tok:<user>:<day>`, `usage:cost:…`, `usage:topic:…`, `usage:req:<topic>:…` | live daily usage + quota counters |
| `usage:run:<request_id>` | tokens/cost of one run (read when the run finishes) |
| `rl:<key>:<window>` | rate limits (per user, login, register) |
| `acct:<user>` | 60 s account/plan cache |
| `sess:<user>:<chat>` | chat memory: the last 20 messages, **one per chat** (shared by all assistants), 24 h — reloaded from MySQL `agent_runs` when gone |
| `rcache:<topic>:…` | response cache (identical first-turn questions) |
| `llmcache:…`, `route:judge:…` | LLM call cache, routing-judge verdicts |
| `fx:…`, `mkt:…` | exchange rates, market data |

Browse it: [redis.everesto.live](https://redis.everesto.live) (GitHub login, platform-team).

!!! note "Shared Redis"
    In the platform Redis each app gets its own user, limited to keys starting with its `REDIS_KEY_PREFIX`
    (Backstage → Create deployment → Redis). Every key is built with `rkey()` (`agency_platform/cache.py`),
    which puts `REDIS_KEY_PREFIX` in front (empty locally) — e.g. `orchid-agency-backend:usage:tok:…`.
    New Redis code must build its keys with `rkey()` too, or the shared Redis answers `NOPERM`.

