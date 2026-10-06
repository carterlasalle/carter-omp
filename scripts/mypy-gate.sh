#!/bin/sh
# trace:exempt reason=deploy-packaging-no-runtime-behavior
# CI gate: mypy error count must not exceed the committed baseline.
# The baseline is 0 errors as of 2026-10-06 (the 19-error entry of 2026-09-24
# was cleared by real fixes, not suppressions); documented in pyproject.toml
# [tool.mypy] and docs/adrs/analyzer-gates-and-baselines.md.
# Lower it when errors are fixed; never raise it to hide new ones.
BASELINE=0
COUNT=$(uv run mypy src/carter_omp 2>&1 | grep -c "error:" || true)
echo "mypy errors: $COUNT (baseline: $BASELINE)"
if [ "$COUNT" -gt "$BASELINE" ]; then
  echo "FAIL: mypy error count grew ($COUNT > $BASELINE). Fix new errors, do not raise the baseline to hide them."
  exit 1
fi
