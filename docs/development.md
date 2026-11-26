# Development guide

## Layout of a service

```
services/topic_ev_research/
  pyproject.toml, uv.lock     own dependencies (each service resolves independently)
  ev_research/
    main.py                   app = create_topic_service(EVResearchAgent(), settings())  (+ extra routers)
    agent.py                  TopicAgent subclass: turns the framework's events into AgentEvents
    graph.py                  the LangGraph state machine (framework-specific logic)
    sources.py, calc.py, …    domain logic
    repository.py             this topic's SQL
    params.py                 typed view of config/agents/ev_research.yaml
  tests/                      unit tests + test_api_contract.py
```

The edge gateway has `routes_*.py` (thin HTTP layer), `schemas.py` (response models), `dashboard.py`
(pure shaping logic), `usage_service.py`.

## Conventions (enforced by tests / CI)

| Rule | Why |
|---|---|
| Routes stay thin; **SQL only in repositories** | one place per table, testable, DB-agnostic routes |
| Agents ask the gateway for a **task or tier**, never a model name | routing, plan ceilings, cost tracking stay central |
| No provider keys outside the model gateway | least privilege |
| Every endpoint: a **tag**, a **summary**, a **typed response model**, errors via `openapi.errors(...)` | clean API docs + generated clients (`test_api_contract.py` fails otherwise) |
| Errors are raised as `AppError` subclasses (`agency_platform.errors`) | uniform `{"error": {code, message, …}}` envelope |
| Tunables in `config/*.yaml`, prompts in `prompts/*.yaml` | changed without a code release |
| After changing an endpoint: `bash ci/export_openapi.sh` and commit `openapi/` | Backstage API pages = code (CI checks) |
| Frameworks must not use the shared Redis for their own state (CrewAI's lock store reads `REDIS_URL`: overridden in `product_finder/crews.py`) | our Redis user is ACL-restricted to its key prefix — admin commands like `CLIENT SETNAME` fail |

## Add an endpoint (example: edge gateway)

1. Response model in `edge_gateway/schemas.py`.
2. Data access: a method on a repository in `libs/agency_platform/.../repositories/` (with a test; SQL that
   touches MySQL also goes into `tests/test_mysql_integration.py`).
3. Route in the right `routes_*.py`, e.g.:

    ```python
    @usage_router.get("/me/top-topics", summary="My most expensive topics this month")
    async def my_top_topics(user: CurrentUser) -> list[TopicBreakdown]:
        ...
    ```

4. `bash ci/test.sh` (runs the API contract test), `bash ci/export_openapi.sh`, commit `openapi/`.

## Add a topic

1. `services/topic_<name>/` with a `TopicAgent` subclass (`topic`, `framework`, `run()`) and
   `main.py`: `app = create_topic_service(MyAgent(), settings())`.
2. `config/topics.yaml` entry (service URL, keywords, timeouts), `config/agents/<name>.yaml`, `prompts/<name>.yaml`.
3. Entitle it in `config/plans.yaml`; add `<NAME>_GATEWAY_SECRET` (secrets) and to `MODEL_GATEWAY_CLIENTS`.
4. Use `ctx.llm` (or `ctx.llm.openai_compat(task=...)` to plug a framework in).
5. Add it to `docker-compose.yaml`, the CI image matrix (`.github/workflows/ci.yml`), `ci/test.sh`,
   `ci/export_openapi.sh`, `catalog-info.yaml` (a new API entity) and the routing golden set.

## Change a model, a limit or a prompt

| Change | Edit | Check |
|---|---|---|
| Model of a tier / new model | `config/models.yaml` | `models_check`; evals vs baseline |
| A task's tier | `routing.tasks` in `config/models.yaml` (or `task_tiers` per topic) | `/admin/metrics/routing` |
| Plan limits | `config/plans.yaml` | `/usage/me` |
| Prompt | new `version` in `prompts/<topic>.yaml` (or an `experiments:` variant) | offline evals; `/admin/experiments` |

## Tests

```bash
bash ci/test.sh                                         # everything CI runs
cd services/model_gateway && uv run --extra test pytest -k routing    # focused
```

Tests never call real models or the internet: `agency_platform.testing` provides `FakeLLM` and
`fake_context`. Real-model checks are the quality worker's job ([LLMOps](llmops.md)).

