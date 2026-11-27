# Architecture

## Services

| Service | Role | Talks to |
|---|---|---|
| **edge gateway** | Public API: register/login (JWT), `/chat` routing, topic catalogue, usage, dashboards, admin | topics, model gateway (topic classification), MySQL, Redis |
| **EV Helper** | LangGraph state machine: parse → crawl + verify facts → calculate → write answer | model gateway, Tavily, web pages, MySQL (fact cache), Redis |
| **Product Finder** | CrewAI research agent with tools: search reviews/guides/shops → read pages (prices verified on the page by code) → search again → answer; every price in the answer checked against what the tools returned | model gateway, Tavily, shops, MySQL (offer history), Redis |
| **Finance Analyst** | AutoGen team: data analyst + strategist + compliance reviewer | model gateway, market data, Redis |
| **model gateway** | The only service with the Gemini key. Picks the model tier, falls back, caches, redacts PII, records every call | Gemini, MySQL (`llm_calls`, `usage_daily`), Redis (live quotas) |
| **quality worker** | Jobs: offline evals + gate, online LLM-judge, drift, data retention, smoke test | the APIs above, MySQL |

**Why one service per topic?** LangGraph, CrewAI and AutoGen pull in conflicting dependencies, and each
topic should be deployable and scalable on its own. Everything they have in common lives in the shared
library `libs/agency_platform`, so a new topic gets auth, quotas, tracking and streaming for free.

## What happens during one question

```mermaid
sequenceDiagram
  actor U as User
  participant E as edge gateway
  participant T as topic service (e.g. EV Helper)
  participant G as model gateway
  participant M as Gemini
  participant DB as MySQL
  participant R as Redis
  rect rgba(59, 130, 246, 0.14)
  U->>E: POST /api/v1/chat (Bearer token)
  E->>R: account + plan (60 s cache)
  E->>E: pick the topic (keywords, cheap LLM call only if unclear)
  E->>T: POST /api/v1/ev_research/run (same token)
  end
  rect rgba(245, 158, 11, 0.14)
  T->>R: preflight: plan includes topic? rate limit, daily token/cost quota
  T->>DB: agent_runs: status running
  end
  rect rgba(34, 197, 94, 0.14)
  loop every LLM call of the agent
    T->>G: OpenAI-style call, model = "task-synthesis" (never a model name)
    G->>G: route to tier (low/mid/high) ≤ plan ceiling, redact PII
    G->>M: call the chosen Gemini model (fallback on error)
    G->>R: live tokens + cost per user/topic
    G->>DB: llm_calls + usage_daily (batched)
  end
  end
  rect rgba(139, 92, 246, 0.14)
  T-->>U: streamed events: status, token (answer), usage, done
  T->>DB: agent_runs: tokens, cost, latency, answer (PII-redacted)
  end
```

<small>🟦 authenticate + route · 🟨 preflight checks · 🟩 every LLM call · 🟪 answer + bookkeeping</small>

## Request authentication

- **Users** get a short-lived **access token** (JWT, 15 min) and a single-use **refresh token** from the
  edge gateway. Every service verifies the access token itself (in production with the edge's public key —
  only the edge can sign).
- **Services → model gateway** use a **call token** minted per run, signed with that service's own secret.
  It carries user, plan and request id, so every LLM call is attributed to the right user and topic.

## Data stores

| Store | Holds | Notes |
|---|---|---|
| MySQL (local 8.4 / OCI HeatWave in prod) | users, runs, LLM call ledger, usage per user × topic × day, eval results, caches of verified facts | source of truth — see [Data](data.md) |
| Redis | live quota counters, rate limits, chat memory, response + LLM caches, short-lived account cache | everything rebuildable or expiring |

## Shared library: `libs/agency_platform`

| Module | What it gives every service |
|---|---|
| `service/` | app factory: logging, request ids, security headers, error envelope, health + metrics, OpenAPI conventions |
| `auth/` | tokens, accounts, refresh-token rotation with reuse detection, `CurrentUser` / `AdminUser` dependencies |
| `agents/` | `TopicAgent` contract, `TopicRunner` (preflight, streaming, timeout, caching, accounting), topic service factory |
| `billing/` | live usage meter, quotas, rate limiter (Redis) |
| `llm/` | model gateway client, call tokens, tiers |
| `repositories/` | **all SQL** (runs, usage, LLM calls, evals, admin metrics, retention) |
| `config/` | hot-reloaded YAML parameters and prompts, validated by schemas |
| `security/` | PII redaction, SSRF-safe fetching, grounding checks, untrusted-content fencing |

