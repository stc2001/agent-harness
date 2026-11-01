---
name: secret-audit
description: Scan the working tree and git history for secrets and credentials, report findings without exfiltration, and give a remediation plan (rotate first, purge history only deliberately). Use before pushing, on a suspected leak, or during a hygiene audit.
whenToUse: Before a commit or push that touches config, on a leaked-key suspicion, or when the user asks to scan for secrets.
---

# Secret Audit

A leaked credential is compromised the moment it is committed. Rotate first,
then clean.

## Run

    bash .dsh/skills/secret-audit/scripts/secret_scan.sh
    bash .dsh/skills/secret-audit/scripts/secret_scan.sh --history

External tools are optional: if gitleaks or trufflehog is on PATH, run them too;
the script itself has no dependency beyond git and grep.

## Workflow

1. Scan the working tree (default) and, when history is in scope, the full
   history (--history may be slow; run it deliberately).
2. Discriminate: example/test fixtures, placeholders (xxx, changeme), public
   docs and already-rotated sample keys are not incidents. Docs that quote a
   pattern are not leaks.
3. Report each hit as path:line, plus a truncated fingerprint (first/last 4
   characters). Never paste a full secret into chat, issues or commits.
4. Remediation:
   - rotate/revoke the credential with the provider first;
   - remove the value from the working tree and add an ignore rule;
   - only then plan history removal (git filter-repo / BFG) with the user;
     it rewrites every SHA and requires a coordinated force-push.
5. Add a regression guard: pre-commit hook or make target running this script.

## Never

- Never print or transmit the full secret.
- Never rewrite history or force-push as a first response.
- Never commit a .env or key file "temporarily".

## Known local exceptions

The repo may contain historic test keys under docs/ or local gitignored paths.
Treat them as findings to rotate, not as noise to ignore; keep the ignore rules
for exact paths only.

Attribution: adapted from Skillkeel/skillkeel-starter secret-audit (MIT) and
rjwalters/repo scrub (MIT). See .dsh/skills/_attribution/NOTICE.md.
