---
name: dependency-audit
description: Audit Python dependencies for known vulnerabilities, outdated pins and lockfile drift; propose minimal safe upgrades. Use before a release, after adding or changing dependencies, or when asked about supply-chain risk.
whenToUse: A release is prepared, dependencies changed, or the user asks about outdated or vulnerable packages.
---

# Dependency Audit

Run the ecosystem's real tool, separate fixable from unfixable, and upgrade in
small reviewed steps.

## Run

    .venv/bin/python -m pip list --outdated
    .venv/bin/python -m pip_audit            # if pip-audit is installed
    .venv/bin/python -m pip check
    git diff pyproject.toml uv.lock

If uv is the installer:

    uv pip list --outdated
    uv lock --check

## Tiers

| Tier | Change | Action |
|---|---|---|
| 1 | patch/minor security fix, tests green | apply, review lockfile diff, run tests |
| 2 | major version or behavior change | propose, wait for user decision |
| 3 | no fix available | record in the issue/RM, do not silently pin |

## Steps

1. Read pyproject.toml and the lockfile; identify direct dependencies.
2. Run pip-audit (or pip list --outdated) and capture output.
3. For each finding: classify tier, check the changelog for breaking changes,
   and check the license still fits the project.
4. Apply tier 1 upgrades one group at a time; run make test after each.
5. Record tier 2/3 decisions in the issue or ADR; do not bulk-upgrade blindly.

## Never

- Never run a blind bulk upgrade in one commit.
- Never remove a lockfile or re-resolve without reviewing the diff.
- Never ignore a known vulnerability without recording why.

Attribution: adapted from Skillkeel/skillkeel-starter dependency-audit (MIT).
See .dsh/skills/_attribution/NOTICE.md.
