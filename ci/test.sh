#!/usr/bin/env bash
# Tests + lint for every package, run by Loop CI (test-command) and runnable locally:
#   bash ci/test.sh
# Each package has its own uv lockfile (the agent frameworks conflict), so each gets its own
# environment -- created OUTSIDE the repo so SonarQube/Semgrep never scan installed libraries.
set -euo pipefail
cd "$(dirname "$0")/.."

PACKAGES=(libs/agency_platform services/model_gateway services/edge_gateway services/topic_ev_research
          services/topic_product_finder services/topic_finance services/quality_worker)
PYTHON_VERSION=3.13
ENV_ROOT="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/agency-venvs"

if ! command -v uv >/dev/null 2>&1; then
  # Ubuntu 24.04 runners forbid pip into the system Python (PEP 668): use uv's pinned installer.
  curl -LsSf https://astral.sh/uv/0.13.7/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "::group::lint (ruff)"
uv tool run ruff check libs services scripts
echo "::endgroup::"

for pkg in "${PACKAGES[@]}"; do
  echo "::group::tests ${pkg}"
  module=$(find "$pkg" -mindepth 1 -maxdepth 1 -type d ! -name tests ! -name '.*' ! -name '*.egg-info' | head -1)
  (
    cd "$pkg"
    export UV_PROJECT_ENVIRONMENT="${ENV_ROOT}/$(basename "$pkg")"
    uv sync --frozen --extra test --python "$PYTHON_VERSION" --quiet
    uv run --frozen --with pytest-cov pytest -q -p no:warnings -p no:cacheprovider \
      --cov="$(basename "$module")" --cov-report=xml:coverage.xml
  )
  echo "::endgroup::"
done

echo "::group::OpenAPI specs (openapi/ = what Backstage shows)"
bash ci/export_openapi.sh --check
echo "::endgroup::"
