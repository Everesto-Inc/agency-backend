# Run it locally

Everything runs in Docker: MySQL (same engine as production HeatWave), Redis, the database migrations,
the model gateway, the edge gateway and the three assistants.

## 1. Prerequisites

- Docker Desktop (or Docker Engine + Compose v2)
- Two API keys: **Gemini** (Google AI Studio, with credit) and **Tavily** (web search)
- Optional, for running tests outside Docker: [uv](https://docs.astral.sh/uv/)

## 2. Secrets file

Compose reads secrets from an env file that is never committed (`.env` by default).

```bash
python3 scripts/gen_secrets.py --env-file .env      # generates the internal secrets (JWT key, service secrets)
# open .env and fill in:
#   GEMINI_API_KEY=...
#   TAVILY_API_KEY=...
python3 scripts/gen_secrets.py --check .env         # shows set / MISSING (values hidden)
```

!!! note "Using another file name"
    With e.g. `.env.dev`, pass it to every command: `docker compose --env-file .env.dev …`

## 3. Start

```bash
docker compose up -d --build        # first build takes a few minutes
docker compose ps                   # all services "healthy"
curl -s localhost:8000/health/ready # {"status":"ok","checks":{"mysql":"ok","redis":"ok"}}
```

| Service | Local URL | API docs (Swagger) |
|---|---|---|
| Public API (edge gateway) | http://localhost:8000 | http://localhost:8000/docs |
| EV Helper | http://localhost:8001 | http://localhost:8001/api/v1/ev_research/docs |
| Product Finder | http://localhost:8002 | http://localhost:8002/api/v1/product_finder/docs |
| Finance Analyst | http://localhost:8003 | http://localhost:8003/api/v1/finance/docs |
| Model gateway (internal) | http://localhost:8010 | http://localhost:8010/docs |
| MySQL | `127.0.0.1:33069` (user `agency` / `localdev`) | e.g. DBeaver |

## 4. First question

In http://localhost:8000/docs:

1. **Auth → POST /api/v1/auth/register** with an email + password (≥ 10 characters). Copy `access_token`.
2. Click **Authorize** (top right), paste the token.
3. **Chat → POST /api/v1/chat** with `{"prompt": "Range and battery of the 2024 Hyundai Ioniq 5 in Germany?", "stream": false}`.
4. **Dashboard → GET /api/v1/dashboard** — the tokens and cost that question used.

New accounts are on the **free** plan (EV Helper only). For all topics, make yourself an admin on the `pro` plan:

```bash
docker compose run --rm -it edge-gateway python -m edge_gateway.cli create-admin you@example.com
```

## 5. Useful commands

```bash
docker compose logs -f model-gateway                         # follow one service
docker compose exec model-gateway python -m model_gateway.models_check   # are the Gemini model ids + key OK?
docker compose --profile quality run --rm eval-user          # once: account used by the quality jobs
docker compose --profile quality run --rm quality-worker smoke   # every topic end to end
docker compose exec mysql mysql -uagency -plocaldev agency  # SQL shell
docker compose down                                          # stop (data kept); add -v to wipe the database
```

## 6. Tests

Each package has its own environment (the agent frameworks have conflicting dependencies):

```bash
bash ci/test.sh                      # lint + every package's tests + OpenAPI spec check (what CI runs)
cd services/edge_gateway && uv run --extra test pytest     # one package
```

Real-MySQL integration test (migrations + every SQL query), against the compose MySQL:

```bash
docker compose up -d --wait mysql
cd libs/agency_platform
AGENCY_TEST_DATABASE_URL=mysql://agency:localdev@127.0.0.1:33069/agency_test uv run --extra test pytest tests/test_mysql_integration.py
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `create .env first` on `docker compose up` | no env file — step 2 |
| Answers fail, gateway logs `402 … prepayment credits are depleted` | the Gemini project has no credit (AI Studio → Billing) |
| `models_check` shows `NotFoundError` | a model id in `config/models.yaml` doesn't exist for your key |
| `403 SUBSCRIPTION_REQUIRED` | your plan doesn't include that topic — use an admin/pro account |
| `429 QUOTA_EXCEEDED` | daily token/cost limit of your plan reached (resets 00:00 UTC) |

---

[← Start here](index.md) · **[Next: Architecture →](architecture.md)**
