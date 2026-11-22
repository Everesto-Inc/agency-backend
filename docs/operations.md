# CI/CD & operations

## CI (Loop, managed by Backstage)

`.github/workflows/ci.yml` runs the platform pipeline (Everesto-Inc/loop) on every PR and on `main`:

1. Gitleaks (secrets) + Trivy (dependencies)
2. `ci/test.sh`: lint, every package's tests, OpenAPI specs up to date
3. Semgrep (code security), SonarQube quality gate
4. Images (linux/arm64) → `ghcr.io/everesto-inc/agency-<service>:sha-<commit>` — pushed from `main` only:
   `agency-edge-gateway`, `agency-model-gateway`, `agency-topic-ev-research`, `agency-topic-product-finder`,
   `agency-topic-finance`, `agency-quality-worker`

`main` is protected: changes go through a pull request with one approval (CODEOWNERS for CI/catalog files).
Docs-only changes (`docs/`, `mkdocs.yml`, `*.md`, `catalog-info.yaml`) don't rebuild images.

## CD (GitOps)

Deployment is declared in **agency-backend-gitops** and synced by Argo CD (manual sync). Images are
promoted by changing their tag there — CI never deploys. *(The manifests still live in `deploy/` here
until they move to the gitops repo.)*

## Secrets in Kubernetes

Infisical (project `orchid-agency-backend-gitops`, per environment) → External Secrets Operator →
Kubernetes Secret → environment of the pods. Nothing secret is ever in git.

## Day-to-day

| Task | How |
|---|---|
| Is everything healthy? | `GET /health/ready` of each service · Grafana "Agency LLMOps" |
| Who spent what? | `GET /api/v1/admin/dashboard?month=YYYY-MM` |
| Why was a run slow / expensive? | `GET /api/v1/admin/runs/{request_id}/calls` — every LLM call with model, tier, reason, cost |
| Model provider problems | `GET /api/v1/admin/metrics/models` (error rate, fallbacks, p95) |
| Quality dropping? | `GET /api/v1/admin/quality` (offline evals, online judge, drift) |
| Look at Redis | [redis.everesto.live](https://redis.everesto.live) |
| Disable a user | `POST /api/v1/admin/users/{id}/disable` (sessions revoked) |

## Alerts

Prometheus rules (`deploy/monitoring/prometheus/alerts.yml`): cost spikes, model error rate and latency,
failing runs, judge score drop, drift flags, eval pass rate.

---

[← Development guide](development.md) · **[Next: Glossary →](glossary.md)**
