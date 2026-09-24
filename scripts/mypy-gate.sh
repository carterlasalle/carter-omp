#!/bin/sh
# trace:exempt reason=deploy-packaging-no-runtime-behavior
# CI gate: mypy error count must not exceed the committed baseline.
# The baseline (19 errors / 6 files as of 2026-09-24) is documented in
# pyproject.toml [tool.mypy] and docs/adrs/analyzer-gates-and-baselines.md.
# Fix incrementally; update BASELINE when errors are fixed, never to hide new ones.
BASELINE=19
COUNT=$(uv run mypy src/carter_omp 2>&1 | grep -c "error:" || true)
echo "mypy errors: $COUNT (baseline: $BASELINE)"
if [ "$COUNT" -gt "$BASELINE" ]; then
  echo "FAIL: mypy error count grew ($COUNT > $BASELINE). Fix new errors, do not raise the baseline to hide them."
  exit 1
fi
