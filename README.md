# agency-backend — Autonomous Agency LLMOps platform

Part of `oracle/apps/llmops/autonomous-agency/`, one git repo each:

| Repo | What |
|---|---|
| **agency-backend** (this) | APIs, agents, model gateway, quality jobs, migrations |
| agency-frontend | Web app (sign-in, chat, usage dashboards) |
| agency-backend-gitops | Kubernetes desired state for this repo, synced by Argo CD |
| agency-frontend-gitops | Kubernetes desired state for the frontend |

(`deploy/` here still holds the manifests until they move to agency-backend-gitops.)

A multi-agent platform where every topic agent is its own service, built on
whatever framework suits it, and every one gets the same LLMOps guarantees from
a shared platform:

| | Topic 1 · EV helper | Topic 2 · Cheapest product finder | Topic 3 · Finance analyst |
|---|---|---|---|
| Framework | LangGraph | CrewAI | AutoGen (agentchat) |
| Service / API | `topic-ev-research` · `/api/v1/ev_research` | `topic-product-finder` · `/api/v1/product_finder` | `topic-finance` · `/api/v1/finance` |
| Grounding | crawled specs, numbers verified on page, Decimal TCO math | JSON-LD offers or LLM extraction verified against `currency+amount` on page, ECB FX, deterministic ranking | every figure from deterministic market-data tools, compliance agent gate |
| Model tiers used | `intent_parsing`→low, `extraction`→low, `synthesis`→mid | `query_planning`→low, `extraction`→low, `product_matching`→mid | `data_analysis`→mid, `investment_analysis`→high, `compliance_review`→low |

## Architecture

```
                         ┌─────────────── Ingress (TLS, SSE-friendly) ───────────────┐
 client ── JWT ─────────►│ /api/v1/auth|chat|usage|admin   /api/v1/<topic>/*          │
                         └──────┬────────────────────────────────┬────────────────────┘
                                ▼                                ▼
                     ┌────────────────────┐   proxy   ┌───────────────────────────────┐
                     │   edge-gateway     │──/chat───►│ topic services (1 per topic)   │
                     │ identity, plans,   │           │  TopicRunner envelope:         │
                     │ topic auto-routing,│           │  entitlement · rate limit ·    │
                     │ usage, admin stats │           │  quotas · timeout · run record │
                     └─────────┬──────────┘           │  · usage · session memory      │
                               │ call token           └──────────────┬────────────────┘
                               ▼                                     │ per-run call token
                     ┌───────────────────────────────────────────────▼──────────────┐
                     │  model-gateway  (OpenAI-compatible, cluster-internal only)    │
                     │  alias → tier (task map | classifier+LLM judge) → plan clamp  │
                     │  → model candidates → fallback → escalation                   │
                     │  records every attempt: tokens, $ cost, latency, errors       │
                     │  → Redis (live quotas) · MySQL llm_calls · Prometheus ·       │
                     │    LangSmith                                                  │
                     └───────────────┬───────────────────────────────────────────────┘
                                     ▼
                       Gemini (low/mid/high) · in-tier fallbacks · ...
```

```
libs/agency_platform/       shared LLMOps library: settings, params, prompts, auth,
                            plans/quotas, model-gateway client, runner, security, observability
services/model_gateway/     the one LLM router every framework talks to
services/edge_gateway/      register/login/refresh, /chat auto-router, usage, admin analytics
services/topic_*/           one deployable per topic (own deps, own image, own scaling)
services/quality_worker/    evals + LLM judge, regression gate, online judging, drift, retention, smoke (CronJobs/CI)
config/                     PARAMETERS (hot-reloaded): models.yaml, plans.yaml, topics.yaml, privacy.yaml, evals.yaml, agents/*.yaml
prompts/                    versioned prompts (hot-reloaded); version recorded on every LLM call
migrations/                 forward-only SQL, run once per deploy
deploy/                     Kustomize base + dev/prod overlays + Argo CD apps
evals/                      golden sets (routing, tiering, per-topic agent cases), baselines, reports
```

Each package has its own `pyproject.toml` + `uv.lock`: LangGraph, CrewAI and AutoGen
pin conflicting dependencies, so one shared environment would be fragile.

## Model routing (one router for all agents)

Agents never name a model. They state what *kind of work* a call is:

```python
await ctx.llm.structured(msgs, Intent, task="intent_parsing")   # cheap tier
await ctx.llm.chat(msgs, task="synthesis")                      # mid tier
cfg = ctx.llm.openai_compat(task="investment_analysis")         # for CrewAI/AutoGen/crawl4ai
```

The model gateway resolves the alias:

1. `task-<name>` → `config/topics.yaml` per-topic override → `config/models.yaml` `routing.tasks`
2. `tier-<low|mid|high>` → that tier
3. `auto` → heuristic complexity score (free); the ambiguous middle band → low-tier LLM judge (cached)
4. raise to `X-Agency-Min-Tier` (structured output that failed validation escalates one tier)
5. **clamp to the user's plan ceiling** (`plans.yaml` `max_tier`), so a free user is never routed to the expensive tier
6. try the tier's candidate models in order (fallback across providers), then escalate a tier on total failure

Framework-native clients (CrewAI, AutoGen, crawl4ai) are pointed at the gateway with
`base_url` + a **per-run signed call token as the API key**, so their calls are tiered,
capped, attributed and tracked exactly like the platform's own calls. Provider API keys
exist only in the model gateway.

## What every topic gets for free

* **Subscription gating**: login required; per-plan topic entitlements (`SUBSCRIPTION_REQUIRED`
  → upsell), plan resolved fresh each request (upgrades and expiries apply immediately).
* **Quotas**: per-user requests/min, daily token **and** $-cost budget (hard stop mid-run in
  the gateway), per-topic daily run caps, all from `plans.yaml`.
* **Tracking**: every LLM attempt in `llm_calls` (task, prompt version, tier, model, tokens,
  cost, latency, fallback), every run in `agent_runs` (status, latency, cost, user feedback),
  Prometheus metrics, LangSmith traces.
* **Runtime**: SSE streaming with heartbeats, WebSocket (first-message auth), hard timeouts,
  accounting preserved on client disconnect, Redis session memory scoped per user.
* **Caching**: identical first-turn questions answered from cache (TTL per topic; key includes the
  prompt version and A/B arm and the plan's tier ceiling); deterministic LLM calls (temperature 0)
  cached in the gateway.

## Usage & cost per user, per topic (for sign-in / billing)

Every LLM call lands in `usage_daily` (user × topic × day: tokens, $ cost, calls, runs) and in live
Redis counters. Non-agent spend is attributed to the pseudo-topics `routing` (the /chat classifier) and
`evals` (quality jobs), so per-topic numbers always add up to the user's total.

| Endpoint | Who | What |
|---|---|---|
| `GET /api/v1/usage/me` | user | today (live), month to date, last 30 days — tokens + $ per topic, limits |
| `GET /api/v1/usage/me/daily?days=30` | user | daily series per topic (charts) |
| `GET /api/v1/usage/me/runs` | user | recent runs with tokens + $ each |
| `GET /api/v1/admin/usage/topics` | admin | totals per topic, all users |
| `GET /api/v1/admin/usage/users` | admin | top users by cost with per-topic breakdown |
| `GET /api/v1/admin/usage/users/{id}` | admin | one user, per topic + daily |

Plans can cap each topic separately (`topic_daily_requests`, `topic_daily_tokens` in `plans.yaml`).

## Dashboards (for the frontend)

One call per screen; numbers precomputed, daily series gap-free (one point per day, zeros
included), topics with display names, month-over-month change in %. Months are UTC `YYYY-MM`.

| Screen | Endpoints |
|---|---|
| User: my usage | `GET /api/v1/dashboard/months` (month picker) → `GET /api/v1/dashboard?month=2026-11` |
| Admin: platform | `GET /api/v1/admin/dashboard/months` → `GET /api/v1/admin/dashboard?month=2026-11` |
| Admin: one user | `GET /api/v1/admin/dashboard/users/{user_id}?month=2026-11` (exactly what that user sees) |

User dashboard: totals, previous month + change, per-topic breakdown (share of cost, cost per run),
daily series, run stats, last 10 runs, and live quota for the current month. Admin dashboard adds
per-model tokens/cost, user counts (active, new, per plan) and top spenders. Users only ever see
their own data; admin endpoints return 403 for non-admins.

Typed client for the frontend, generated from the spec:
`npx openapi-typescript http://localhost:8000/openapi.json -o src/api/schema.d.ts`



| Capability | How |
|---|---|
| Offline evals with real models | `evals/datasets/<topic>.jsonl` → `quality offline`: deterministic checks + LLM-as-judge (`prompts/quality.yaml`, groundedness weighted double) → `eval_runs`/`eval_results` + JSON report |
| Regression gate | `quality offline --gate` compares to `evals/baselines/<topic>.json` (pass rate, judge score, $/case, p95; thresholds in `config/evals.yaml`). CI runs it on PRs touching prompts, models or topic code once provider keys are repo secrets |
| Prompt A/B tests | `experiments:` block in `prompts/<topic>.yaml`; sticky per user; results at `GET /admin/experiments` (cost, latency, feedback, judge score per variant) |
| Model canaries | `canaries:` in `config/models.yaml` (sticky % of a tier's runs); compare at `GET /admin/canaries` |
| Online quality | `eval_sampled` runs (`online.sample_rate`) judged hourly (`quality online` CronJob) |
| Drift | hourly `quality drift`: judge score, negative feedback, error rate, cost/run, tier mix (Jensen-Shannon) vs trailing baseline → `drift_reports`, webhook alert, Prometheus gauge |
| Feedback → dataset | 👍/👎 creates an `eval_cases` candidate → admin approves (`PUT /admin/eval-cases/{id}`) → `quality export-cases` appends to the dataset |
| Dashboards & alerts | `docker compose --profile monitoring up -d` (Grafana :3001 "Agency LLMOps", Prometheus :9090). Alert rules live in `deploy/monitoring/prometheus/alerts.yml` (cost spikes, model errors/latency, run failures, judge score, drift, eval pass rate); `scripts/sync_alert_rules.py` renders the k8s `PrometheusRule` |

## Privacy

* PII (email, phone, card, IBAN, SSN — configurable in `config/privacy.yaml`) is redacted in the model
  gateway before any provider call, and in stored prompts and answers.
* Retention (`quality retention` CronJob, daily): prompt/answer text nulled after 30 days, per-call ledger
  after 180 days. `usage_daily` aggregates are kept for billing.
* `GET /api/v1/auth/me/export` exports a user's data; `DELETE /api/v1/auth/me` (with password re-auth)
  erases the account and anonymises the LLM ledger.

## Local development (docker compose only)

Needs Docker and two keys: `GEMINI_API_KEY` and `TAVILY_API_KEY`. No Infisical locally.

```bash
python3 scripts/gen_secrets.py --env-file .env   # once: creates .env (internal secrets + empty key lines)
#   -> open .env and fill GEMINI_API_KEY= and TAVILY_API_KEY=
python3 scripts/gen_secrets.py --check .env      # what's set / missing (values hidden)

docker compose up -d --build                     # mysql, redis, migrate, model-gateway, edge, 3 topics
docker compose run --rm -it edge-gateway python -m edge_gateway.cli create-admin you@example.com
docker compose exec model-gateway python -m model_gateway.models_check   # model ids + key valid?

docker compose ps
docker compose logs -f model-gateway
docker compose down                              # stop (data kept); add -v to wipe the database
docker compose up -d --force-recreate            # after editing .env
```

Quality jobs (profile `quality`; `Q="docker compose --profile quality run --rm"`):

```bash
$Q eval-user                                     # once: service account the jobs log in with
$Q quality-worker smoke                          # every topic end to end + per-topic usage recorded
$Q quality-worker offline --tags smoke --gate    # evals with LLM judge + regression gate
$Q quality-worker offline --update-baseline      # accept results as baseline -> commit evals/baselines/
$Q quality-worker export-cases                   # approved feedback cases -> evals/datasets
$Q quality-worker online | drift | retention     # what the CronJobs run in Kubernetes
docker compose --profile monitoring up -d        # Prometheus :9090, Grafana :3001
docker compose exec mysql mysql -uagency -plocaldev agency   # database shell
```

| API docs (Swagger, dev only) | URL |
|---|---|
| Main API: auth, chat, topics, usage, admin | http://localhost:8000/docs |
| EV research | http://localhost:8001/api/v1/ev_research/docs |
| Product finder | http://localhost:8002/api/v1/product_finder/docs |
| Finance | http://localhost:8003/api/v1/finance/docs |
| Model gateway (internal) | http://localhost:8010/docs |

Log in (`POST /api/v1/auth/login`), click **Authorize**, paste the `access_token`: the token is
remembered across reloads and works on every topic's docs page too. In prod the topic docs live
on the same host under their prefix (`/api/v1/<topic>/docs`), so the ingress routes them.

API conventions (enforced by a contract test in every service, `tests/test_api_contract.py`):
endpoints grouped by tags, every endpoint has a summary and a typed response model, operationIds
are the handler names (clean generated clients), every error uses the same
`{"error": {code, message, request_id, details}}` envelope, bearer auth is a declared security scheme.

```bash
TOKEN=$(curl -s localhost:8000/api/v1/auth/register -H 'content-type: application/json' \
  -d '{"email":"me@example.com","password":"a-long-password"}' | jq -r .access_token)
curl -N localhost:8000/api/v1/chat -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"prompt":"Tesla Model 3 2024 vs diesel in France, 15000 km/year"}'
```

Edit anything in `config/` or `prompts/` while the stack runs: the next request uses it. An
invalid edit is rejected and the last good version keeps serving.

Tests (no network, no DB), per package with [uv](https://docs.astral.sh/uv/):
`cd services/model_gateway && uv run --extra test pytest` (same for every package under `libs/` and
`services/`); lint with `uv tool run ruff check libs services`.

## Database (MySQL locally, OCI HeatWave MySQL in prod)

- Local: `mysql:8.4` in docker-compose, started with HeatWave's defaults (primary key required
  on every table, UTC, utf8mb4), so schema problems show up locally first.
- Prod: HeatWave MySQL on a private subnet. Set `DATABASE_URL` in Infisical `/shared`:
  `mysql://agency:<password>@<db-private-ip>:3306/agency?ssl-mode=REQUIRED`
  (or `ssl-mode=VERIFY_CA&ssl-ca=/path/ca.pem`). Outside dev the services refuse to start
  without TLS to the database. Open the DB's private IP in `deploy/overlays/<env>/egress-data.yaml`.
- Schema: `migrations/NNNN_*.sql`, applied by the migrate job (compose locally, Argo CD
  PreSync in Kubernetes). MySQL DDL isn't transactional, so migrations must be re-runnable;
  never edit an applied migration, add a new one.
- All SQL lives in `libs/agency_platform/agency_platform/repositories/` (plus `auth/accounts.py`
  and each topic's `repository.py`). Routes, runners and jobs don't embed SQL.
- Real-MySQL check of the migrations and every repository query (separate `agency_test` database):
  `docker compose up -d --wait mysql`, then in `libs/agency_platform`:
  `AGENCY_TEST_DATABASE_URL=mysql://agency:localdev@127.0.0.1:33069/agency_test uv run --extra test pytest tests/test_mysql_integration.py`

## Adding a topic

1. `services/topic_<name>/` with a `TopicAgent` subclass (`topic`, `framework`, `run()`), plus
   `main.py`: `app = create_topic_service(MyAgent(), settings())`.
2. `config/topics.yaml` entry (service URL, keywords, timeouts); `config/agents/<name>.yaml` for
   its parameters; `prompts/<name>.yaml`.
3. Entitle it in `config/plans.yaml`; add `<NAME>_GATEWAY_SECRET` to Infisical and to
   `MODEL_GATEWAY_CLIENTS`.
4. Use `ctx.llm` (or `ctx.llm.openai_compat(task=...)` for a framework). Never hold provider keys.
5. Add it to `docker-compose.yaml`, `deploy/base/`, the CI matrix, and the routing golden set.

## Secrets (Infisical)

Least privilege by folder. Each Kubernetes workload syncs only its own folder via the
Infisical Operator (`InfisicalSecret` CRs in `deploy/base`):

| Folder | Keys | Consumer |
|---|---|---|
| `/shared` | `DATABASE_URL`, `REDIS_URL`, `JWT_PUBLIC_KEY`, `LANGSMITH_API_KEY` | all |
| `/edge` | `JWT_PRIVATE_KEY`, `MODEL_GATEWAY_CLIENT_SECRET` | edge-gateway |
| `/model-gateway` | `GEMINI_API_KEY`, `MODEL_GATEWAY_CLIENTS` | model-gateway |
| `/topics/<name>` | `MODEL_GATEWAY_CLIENT_SECRET`, `TAVILY_API_KEY` (where used) | that topic |
| `/quality` | `MODEL_GATEWAY_CLIENT_SECRET`, `EVAL_USER_EMAIL`, `EVAL_USER_PASSWORD`, `ALERT_WEBHOOK_URL` | quality CronJobs |

Generate internal secrets with `python3 scripts/gen_secrets.py --eddsa` (staging/prod use
Ed25519 JWTs; only the edge holds the private key). The cluster needs a one-time bootstrap
secret `infisical-universal-auth` (machine identity) per namespace.

## Deploy (GitOps)

CI comes from the platform (Backstage import → Loop, see `ci/README.md`). The previous pipeline (`ci/legacy-ci-cd.yaml`, inactive) ran: secret scan → lint → tests and evals per package →
dependency audit → manifest validation → build only the changed services → Trivy scan → push.
It then bumps image tags in `deploy/overlays/dev`; Argo CD syncs. Prod is promoted with a manually
approved `workflow_dispatch`. Migrations run as an Argo CD PreSync Job.

Argo CD needs `kustomize.buildOptions: --load-restrictor LoadRestrictionsNone` (argocd-cm) because
`config/` and `prompts/` at the repo root are the single source for the ConfigMaps.
Render locally: `kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/overlays/prod`.

## Security measures

* Asymmetric JWTs (pinned algorithm, iss/aud/exp/jti required); 15-minute access tokens; rotating
  refresh tokens with reuse detection (revokes the whole family); bcrypt off the event loop;
  timing-equalised login; per-IP and per-account brute-force limits.
* Prod boot refuses unsafe configuration (HS256, missing keys, wildcard CORS).
* Per-service gateway credentials; provider keys isolated in one service; model gateway not exposed.
* SSRF guard on every agent fetch (scheme/port/credential checks, private and metadata IPs blocked on
  every redirect hop), plus NetworkPolicy egress blocks as a second layer.
* Prompt-injection hygiene: third-party content fenced as data with unforgeable delimiters; facts
  verified deterministically against source pages; numbers in answers come from code, not models.
* Input limits (body size, prompt length, control-character stripping), uniform error envelopes
  (no stack traces), security headers, log redaction of tokens/keys/DSNs.
* Containers: non-root, read-only root FS, all capabilities dropped, seccomp, no SA token.
