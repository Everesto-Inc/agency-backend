# Using the APIs

Interactive reference for every endpoint: **Backstage → APIs** (each API's *Definition* tab), or Swagger at
`/docs` of each service ([run it locally](getting-started.md)). This page explains the concepts.

| API | Base path | Who calls it |
|---|---|---|
| [Agency API](/catalog/default/api/agency-api/definition) | `/api/v1/…` | frontend, users, admins |
| [EV Helper](/catalog/default/api/agency-ev-research-api/definition) | `/api/v1/ev_research/…` | directly, or via `/chat` |
| [Product Finder](/catalog/default/api/agency-product-finder-api/definition) | `/api/v1/product_finder/…` | directly, or via `/chat` |
| [Finance Analyst](/catalog/default/api/agency-finance-api/definition) | `/api/v1/finance/…` | directly, or via `/chat` |
| [Model Gateway](/catalog/default/api/agency-model-gateway-api/definition) | `/v1/…` (OpenAI-compatible) | **internal only** — the topic services |

## Endpoint groups of the Agency API

| Group | Endpoints | |
|---|---|---|
| Auth | `POST /auth/register`, `/login`, `/refresh`, `/logout` | tokens |
| Account | `GET /auth/me`, `PUT /auth/me/password`, `GET /auth/me/export`, `DELETE /auth/me` | change password (logs out everywhere), GDPR export + erasure |
| Chat | `POST /chat` | any question, auto-routed |
| Topics | `GET /topics` | assistants + whether my plan includes them |
| Usage | `GET /usage/me`, `/usage/me/daily`, `/usage/me/runs` | my tokens + cost |
| Dashboard | `GET /dashboard/months`, `GET /dashboard?month=YYYY-MM` | one call per screen |
| Admin … | usage & billing, models & routing, topics & experiments, quality & evals, accounts & runs, dashboard | `role=admin` only |
| Admin → accounts | `GET /admin/users?plan=none` (waiting for activation), `PUT /admin/users/{id}/plan` (activate), `PUT /admin/users/{id}/password` (reset), `POST /admin/users/{id}/disable` | new accounts get plan `none` until an admin activates them |

## Authentication

```bash
TOKEN=$(curl -s localhost:8000/api/v1/auth/login -H 'content-type: application/json' \
  -d '{"email":"me@example.com","password":"a-long-password"}' | jq -r .access_token)
curl -s localhost:8000/api/v1/auth/me -H "authorization: Bearer $TOKEN"
```

- `access_token` lives 15 minutes → `POST /auth/refresh` with the `refresh_token` returns a new pair.
- Refresh tokens are **single-use**: re-using an old one revokes the whole session (theft detection).

## Asking a question: streaming vs JSON

`POST /api/v1/chat` (or `/api/v1/<topic>/run`) with `"stream": true` returns **Server-Sent Events**:

| Event | Meaning |
|---|---|
| `routed` | (`/chat` only) which topic was chosen and how |
| `status` | progress ("Parsing the question", "Compliance approved") |
| `tool_call` / `tool_result` | what the agent looked up |
| `artifact` | structured data behind the answer (sources, numbers, offers) |
| `token` | the answer (Markdown) |
| `error` | something failed (`data.code`) |
| `usage` | tokens + cost of this run |
| `done` | final status, `request_id`, latency |

With `"stream": false` you get the same information as one JSON document (`RunResponse`).
Keep the same `session_id` for follow-up questions (conversation memory).

Rate an answer: `POST /api/v1/<topic>/runs/{request_id}/feedback` with `{"score": -1|0|1, "comment": "…"}` —
rated answers become evaluation candidates ([LLMOps](llmops.md)).

## Errors

Every error from every service has the same shape — branch on `code`:

```json
{"error": {"code": "QUOTA_EXCEEDED", "message": "Daily usage limit reached …", "request_id": "…", "details": {}}}
```

| HTTP | `code` | What the frontend should do |
|---|---|---|
| 401 | `UNAUTHORIZED` | refresh the token / log in |
| 403 | `SUBSCRIPTION_REQUIRED` | show an upgrade prompt (plan lacks this topic) |
| 403 | `FORBIDDEN` | admin-only endpoint |
| 422 | `VALIDATION_ERROR` | `details.errors` lists the fields |
| 429 | `RATE_LIMITED` / `QUOTA_EXCEEDED` | wait `Retry-After` seconds |
| 502 / 503 | `UPSTREAM_ERROR` / `SERVICE_UNAVAILABLE` | retry later |

## Dashboards (for the frontend)

One call per screen; numbers precomputed; daily series with **no gaps** (one point per day); topics with
display names; month-over-month change in %.

| Screen | Calls |
|---|---|
| My usage | `GET /dashboard/months` (picker) → `GET /dashboard?month=2026-11` |
| Platform (admin) | `GET /admin/dashboard/months` → `GET /admin/dashboard?month=2026-11` |
| One user (admin) | `GET /admin/dashboard/users/{user_id}?month=2026-11` |

Typed client for the frontend: `npx openapi-typescript openapi/edge-gateway.json -o src/api/schema.d.ts`.

