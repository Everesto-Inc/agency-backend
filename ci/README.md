# CI

CI comes from the platform: Backstage "Import repository" adds `.github/workflows/ci.yml`
(Loop: Gitleaks, Trivy, tests, Semgrep, SonarQube, linux/arm64 image to ghcr.io).

This repo builds one image per service from the same `Dockerfile`
(`--build-arg SERVICE=<package> --build-arg MODULE=<asgi app>`), so `ci.yml` calls Loop once per
service (Loop inputs `image` + `build-args`):

| Image | SERVICE | MODULE |
|---|---|---|
| agency-edge-gateway | edge_gateway | edge_gateway.main:app |
| agency-model-gateway | model_gateway | model_gateway.main:app |
| agency-topic-ev-research | topic_ev_research | ev_research.main:app (+ INSTALL_PLAYWRIGHT=true) |
| agency-topic-product-finder | topic_product_finder | product_finder.main:app |
| agency-topic-finance | topic_finance | finance.main:app |
| agency-quality-worker | quality_worker | quality.__main__:main |

`legacy-ci-cd.yaml` is the pre-platform pipeline (not active: it's outside .github/workflows).
Kept as reference for jobs to add after Loop with `needs: loop`: per-package pytest matrix,
LLM quality gate on PRs, alert-rule sync check.
