# Standalone carter-omp with explicit-trigger authorization

<!-- trace:v1 id=SPEC-CO-DYKKXZEZ type=spec work=WORK-CO-Q8Z1HJJJ -->

## Problem

robomp in the oh-my-pi monorepo starts OMP runs on any GitHub activity; Carter needs a standalone repo where only his explicit label or mention authorizes work

## Goals

- standalone build with uv+Yarn

- ID-based authorization

- strict trigger routing

- capability enforcement

- operator docs

## Non-goals

- GitHub App token minting automation

- per-repo network sandbox enforcement

- production deployment

## Test strategy

uv run pytest (incl. authorization matrix + adversarial), yarn lint/typecheck/test/build, trace verify --changed

## Requirements

### Standalone extraction with renames

<!-- trace:v1 id=REQ-CO-9W82BZDD type=requirement work=WORK-CO-Q8Z1HJJJ derived_from=SPEC-CO-DYKKXZEZ -->

Repository builds standalone via uv and Yarn with carter_omp naming, pinned OMP runtime, and no monorepo mount

Acceptance:

- uv run pytest green

- yarn lint/typecheck/test/build green

### Immutable identity and capability model

<!-- trace:v1 id=REQ-CO-T692W95P type=requirement work=WORK-CO-Q8Z1HJJJ derived_from=SPEC-CO-DYKKXZEZ -->

Authorization uses immutable GitHub user and repo IDs with host-assigned Capability sets

Acceptance:

- authorization matrix tests green

### Authorization-first webhook routing

<!-- trace:v1 id=REQ-CO-BKNZHMZ0 type=requirement work=WORK-CO-Q8Z1HJJJ derived_from=SPEC-CO-DYKKXZEZ -->

Only exact authorized label or mention triggers queue OMP work; all other events skip without model invocation

Acceptance:

- strict router tests green

- no-model guarantee holds

### Capability enforcement at tools and replay

<!-- trace:v1 id=REQ-CO-9N23MPRP type=requirement work=WORK-CO-Q8Z1HJJJ derived_from=SPEC-CO-DYKKXZEZ -->

Host tools require capabilities, expose only authorized tools, persist TriggerContext, and replays never re-authorize skipped events

Acceptance:

- adversarial tests green

### Operator docs and provenance

<!-- trace:v1 id=REQ-CO-XM327PK3 type=requirement work=WORK-CO-Q8Z1HJJJ derived_from=SPEC-CO-DYKKXZEZ -->

README and docs describe setup, triggers, security model, GitHub App, architecture, upstream provenance, and parity

Acceptance:

- docs present and accurate
