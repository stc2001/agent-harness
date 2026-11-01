---
name: governance-files
description: Create or repair required governance files (LICENSE, CONTRIBUTING, SECURITY, CODEOWNERS, CHANGELOG, VISION) using soft-serve-aware templates. Use when the repo is missing governance docs, before open-sourcing, or after an audit flags a gap.
whenToUse: A hygiene audit reports missing governance files, or the user asks to prepare the repo for contributors or public release.
---

# Governance Files

Required documents for a project where agents and humans both contribute.
Templates live in templates/ next to this file; copy, then fill the placeholders.

## Inventory

| File | Purpose | Required |
|---|---|---|
| LICENSE | legal terms; decided once by the owner, recorded in an ADR | yes |
| CONTRIBUTING.md | branch, commit, issue/MR, test, review rules | yes |
| SECURITY.md | private vulnerability reporting, supported scope | yes |
| CODEOWNERS | review ownership as a tracked convention (soft-serve does not parse it) | yes |
| CHANGELOG.md | Keep a Changelog entries citing issue/MR ids | yes |
| VISION.md | what the project is and is not | optional |

## Steps

1. Decide LICENSE (MIT or Apache-2.0) with the owner; write an ADR that records
   the choice; set project.license in pyproject.toml.
2. Copy the matching template from templates/ to the repo root.
3. Fill every <PLACEHOLDER>; delete sections that do not apply.
4. Keep all paths repo-root based. Do not create a .github/ directory here:
   issues and MRs are files under issues/ and merge-requests/.
5. Link the files from README (Contributing, Security, Changelog).
6. Add them to the same MR that adds the governance policy; do not direct-push
   docs to main (pre-receive rejects it).

## Rules

- CONTRIBUTING must describe this repo's real flow: issue file, mr/<id>-<slug>
  branch, conventional commit with the issue id, pre-receive policy, tests.
- SECURITY must give a private channel and warn against pasting secrets.
- CODEOWNERS is a convention document; put reviewers in CONTRIBUTING too.
- CHANGELOG is release-only: unreleased work stays in issues/MRs.
- Never invent governance the project does not follow; if the rule is not
  enforced somewhere, say so.

Attribution: structure adapted from github/awesome-copilot repo-standardizer
(MIT, © GitHub, Inc.) and cuesoftinc/oss-engineering-standards (MIT). See
.dsh/skills/_attribution/NOTICE.md.
