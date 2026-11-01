# Third-Party Skill Attribution

Installed workspace skills under .dsh/skills/ are adapted from MIT-licensed
projects. Each skill body carries an Attribution section. Upstream sources:

| Skill | Source | License |
|---|---|---|
| agent-trace-governance | kinoward/dayu-harness-skill | MIT, © kinoward |
| git-commit-workflow, mr-review-workflow | laurigates/claude-plugins (git-plugin) | MIT, © Lauri Gates |
| repo-hygiene-audit | rjwalters/repo; Joopinhontas/claude-repo-audit | MIT, © rjwalters; MIT |
| secret-audit | Skillkeel/skillkeel-starter; rjwalters/repo | MIT |
| dependency-audit | Skillkeel/skillkeel-starter | MIT |
| governance-files (templates) | github/awesome-copilot repo-standardizer; cuesoftinc/oss-engineering-standards | MIT, © GitHub, Inc.; MIT |

Changes: rewritten and trimmed for this repository's soft-serve workflow
(no GitHub CLI/API/Actions dependencies), with local schema checks added.
No upstream text is copied verbatim beyond short quotes and parameter names.

MIT license text: https://opensource.org/license/mit
