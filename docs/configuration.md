# Configuration & secrets

Three kinds of settings, three homes — never mixed:

| Kind | Where | Changed by | Reload |
|---|---|---|---|
| **Parameters** (tunable behaviour) | `config/*.yaml` | a PR | hot-reloaded: the next request uses it; an invalid edit is rejected and the last good version keeps serving |
| **Prompts** | `prompts/*.yaml` (versioned, A/B) | a PR | hot-reloaded |
| **Secrets / wiring** | environment variables | local: env file · prod: Infisical → External Secrets | restart |

## Parameter files

| File | Controls |
|---|---|
| `config/models.yaml` | models, prices, tiers, task → tier map, classifier, canaries, LLM cache |
| `config/plans.yaml` | plans: topics, model ceiling, daily token/cost limits, rate limit, per-topic limits |
| `config/topics.yaml` | the assistants: display name, framework, keywords for routing, timeouts, response-cache TTL |
| `config/privacy.yaml` | which PII is redacted, retention periods |
| `config/evals.yaml` | eval concurrency, gate thresholds, online sample rate, drift thresholds |
| `config/agents/<topic>.yaml` | per-assistant knobs (crawl limits, staleness of cached facts, assumptions, …) |

## Environment variables (secrets)

| Variable | Used by | Notes |
|---|---|---|
| `DATABASE_URL` | all | `mysql://user:pass@host:3306/agency?ssl-mode=REQUIRED` — TLS is mandatory outside dev |
| `REDIS_URL` | all | `redis://…/0` |
| `JWT_SECRET_KEY` | all | **dev only** (HS256) |
| `JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY` | edge / all | staging + prod: Ed25519, only the edge holds the private key |
| `GEMINI_API_KEY` | model gateway **only** | provider keys never reach the topic services |
| `TAVILY_API_KEY` | EV Helper, Product Finder | web search |
| `<SERVICE>_GATEWAY_SECRET` | each service + model gateway | signs that service's call tokens to the gateway |
| `EVAL_USER_PASSWORD` | quality worker | account the quality jobs log in with |
| `LANGSMITH_API_KEY` | optional | tracing |

Generate the internal ones: `python3 scripts/gen_secrets.py` (add `--eddsa` for the Ed25519 JWT key pair).

!!! warning "Production safety checks"
    Outside `ENVIRONMENT=dev` the services refuse to start with: an HS256 `JWT_SECRET_KEY`, no `JWT_PUBLIC_KEY`,
    `*` in CORS origins, or a `DATABASE_URL` without `ssl-mode=REQUIRED|VERIFY_CA|VERIFY_IDENTITY`.
