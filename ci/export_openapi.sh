#!/usr/bin/env bash
# Writes the OpenAPI spec of every service to openapi/<service>.json (shown in Backstage → APIs).
#   bash ci/export_openapi.sh           # regenerate (commit the result)
#   bash ci/export_openapi.sh --check   # CI: fail if a committed spec is out of date
# Specs come from the code (FastAPI), so docs can't drift: change an endpoint → re-export → commit.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
OUT="$ROOT/openapi"
CHECK=${1:-}
[ "$CHECK" = "--check" ] && OUT=$(mktemp -d)
mkdir -p "$OUT"

# service package | import path of the FastAPI app | spec file
SPECS=(
  "services/edge_gateway|edge_gateway.main:app|edge-gateway"
  "services/topic_ev_research|ev_research.main:app|ev-research"
  "services/topic_product_finder|product_finder.main:app|product-finder"
  "services/topic_finance|finance.main:app|finance"
  "services/model_gateway|model_gateway.main:app|model-gateway"
)
ENV_ROOT="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/agency-venvs"

for spec in "${SPECS[@]}"; do
  IFS='|' read -r pkg target name <<<"$spec"
  (
    cd "$pkg"
    export UV_PROJECT_ENVIRONMENT="${ENV_ROOT}/$(basename "$pkg")"
    # import-time settings only (nothing connects anywhere): config + dummy dev secrets
    CONFIG_DIR="$ROOT/config" PROMPTS_DIR="$ROOT/prompts" ENVIRONMENT=dev JWT_SECRET_KEY=export-only-not-a-secret-000000 \
    MODEL_GATEWAY_CLIENTS="edge=$(printf 'x%.0s' {1..40})" \
      uv run --frozen --quiet python -c "
import importlib, json, sys
mod, attr = '$target'.split(':')
app = getattr(importlib.import_module(mod), attr)
json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
print()" > "$OUT/$name.json"
  )
  echo "openapi: $name.json"
done

if [ "$CHECK" = "--check" ]; then
  if ! diff -rq "$OUT" "$ROOT/openapi" >/dev/null; then
    diff -ru "$ROOT/openapi" "$OUT" | head -40
    echo "::error::openapi/ is out of date — run: bash ci/export_openapi.sh  (and commit openapi/)"
    exit 1
  fi
  echo "openapi/ is up to date"
fi
