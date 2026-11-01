#!/usr/bin/env python3
"""Read-only repo hygiene and agent-trace audit for OH_workspace.

Usage: python3 repo_audit.py [--json]
Exit code 0 = no hard failure; 1 = at least one FAIL (secrets).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
).stdout.strip()

RESULTS = []


def add(level, check, message, hint=""):
    RESULTS.append({"level": level, "check": check, "message": message, "hint": hint})


def run(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT)


def tracked():
    out = run("ls-files", "-z").stdout
    return [p for p in out.split("\0") if p]


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8", errors="ignore") as fh:
        return fh.read()


def frontmatter(path):
    text = read(path)
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    data = {}
    for line in text[3:end].splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def check_files(files):
    required = [
        "LICENSE",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODEOWNERS",
        "CHANGELOG.md",
        "AGENTS.md",
    ]
    for name in required:
        if name not in files:
            add("WARN", "governance-file", name + " is missing or untracked",
                "create it from .dsh/skills/governance-files/templates/")


def check_ignore():
    expected = ["sessions/", ".scratch/", ".soft-serve/"]
    for path in expected:
        if run("check-ignore", "-q", path).returncode != 0:
            add("WARN", "ignore", path + " is not ignored", "add it to .gitignore")
    if run("check-ignore", "-q", "AGENTS.md").returncode == 0:
        add("WARN", "ignore", "AGENTS.md is ignored", "agent instructions must be tracked")


def check_large(files):
    limit = 2 * 1024 * 1024
    for path in files:
        full = os.path.join(ROOT, path)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if size > limit:
            add("WARN", "large-file", path + " is " + str(size // 1024) + " KiB",
                "move it to an external store or document the exception")


SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN (RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY-----")),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("google-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
]
SECRET_NAMES = re.compile(r"(^|/)(\.env|session_key|id_rsa|id_ed25519)(\.|$)|\.(pem|key|p12)$")


def check_secrets(files):
    for path in files:
        if SECRET_NAMES.search(path):
            add("FAIL", "secret-file", path + " looks like a credential file",
                "rotate, remove from git, add to .gitignore")
            continue
        full = os.path.join(ROOT, path)
        try:
            if os.path.getsize(full) > 512 * 1024:
                continue
            text = open(full, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for name, pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                add("FAIL", "secret-pattern", path + " matches " + name,
                    "rotate first; never paste the value; see secret-audit")


ISSUE_STATUS = {"open", "in-review", "closed", "wontfix"}
MR_STATUS = {"open", "approved", "changes-requested", "rejected", "merged"}


def check_tracker(files):
    for path in files:
        match = re.match(r"^(issues|merge-requests)/I-(\d+)[^/]*\.md$", path)
        if not match:
            continue
        if path.endswith("TEMPLATE.md"):
            continue
        data = frontmatter(path)
        if data is None:
            add("WARN", "tracker", path + " has no frontmatter", "copy the template header")
            continue
        if data.get("id") != "I-" + match.group(2):
            add("WARN", "tracker", path + " id does not match file name",
                "set id: I-" + match.group(2))
        if match.group(1) == "issues":
            for key in ("id", "title", "labels", "author", "created", "status"):
                if key not in data:
                    add("WARN", "tracker-issue", path + " missing " + key)
            status = data.get("status", "")
            if status and status not in ISSUE_STATUS:
                add("WARN", "tracker-issue", path + " status=" + status,
                    "allowed: " + ", ".join(sorted(ISSUE_STATUS)))
        else:
            for key in ("id", "issue", "branch", "base", "author", "opened", "status"):
                if key not in data:
                    add("WARN", "tracker-mr", path + " missing " + key)
            status = data.get("status", "")
            if status and status not in MR_STATUS:
                add("WARN", "tracker-mr", path + " status=" + status,
                    "allowed: " + ", ".join(sorted(MR_STATUS)))
            branch = data.get("branch", "")
            if branch and not branch.startswith("mr/I-" + match.group(2)):
                add("WARN", "tracker-mr", path + " branch=" + branch,
                    "expected mr/I-" + match.group(2) + "-slug")
            if status == "merged" and not data.get("merged_as"):
                add("WARN", "tracker-mr", path + " is merged but has no merged_as")


def check_docs():
    tracker = "docs/agents/issue-tracker.md"
    if os.path.exists(os.path.join(ROOT, tracker)):
        text = read(tracker)
        if ".scratch/<feature>" in text or ".scratch/" in text:
            add("WARN", "docs-drift", tracker + " still points issues at .scratch/",
                "the real tracker is issues/ + merge-requests/")
    domain = "docs/agents/domain.md"
    if os.path.exists(os.path.join(ROOT, domain)):
        text = read(domain)
        match = re.search(r"已创建\s*(\d+)\s*个\s*ADR", text)
        if match:
            actual = len([p for p in tracked() if p.startswith("docs/adr/")
                          and p.endswith(".md") and not p.endswith("README.md")])
            if int(match.group(1)) != actual:
                add("WARN", "docs-drift", domain + " claims " + match.group(1)
                    + " ADRs, found " + str(actual), "update the count")


def check_branches():
    merged = run("branch", "--merged", "main", "--format=%(refname:short)").stdout.split()
    stale = [b for b in merged if b not in ("main", "master")]
    if stale:
        add("INFO", "branches", str(len(stale)) + " local branch(es) merged into main",
            "report only; deleting branches needs explicit opt-in")
    worktrees = run("worktree", "list", "--porcelain").stdout.count("worktree ")
    if worktrees > 1:
        add("INFO", "worktrees", str(worktrees) + " worktrees present",
            "prune only after confirming branches are on a remote")


def trace_stats():
    sessions = os.path.join(ROOT, "sessions")
    if os.path.isdir(sessions):
        count = 0
        size = 0
        for dirpath, _dirs, names in os.walk(sessions):
            count += len([n for n in names if n == "events.jsonl"])
            for name in names:
                try:
                    size += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        add("INFO", "traces", str(count) + " session event logs, "
            + str(size // (1024 * 1024)) + " MiB under sessions/",
            "runtime data (harness SessionStore); keep for debug/metrics, rotate, never commit")
    audit = os.path.join(ROOT, ".soft-serve/data/log/push-audit.log")
    if os.path.exists(audit):
        lines = sum(1 for _ in open(audit, encoding="utf-8", errors="ignore"))
        add("INFO", "traces", "push-audit.log has " + str(lines) + " lines",
            "local only; export a monthly summary when needed")


def main():
    files = tracked()
    check_files(files)
    check_ignore()
    check_large(files)
    check_secrets(files)
    check_tracker(files)
    check_docs()
    check_branches()
    trace_stats()

    if "--json" in sys.argv:
        print(json.dumps({"root": ROOT, "results": RESULTS}, ensure_ascii=False, indent=1))
    else:
        icons = {"FAIL": "[FAIL]", "WARN": "[WARN]", "INFO": "[INFO]"}
        for item in RESULTS:
            print(icons.get(item["level"], "[----]"), item["check"], "-", item["message"])
            if item["hint"]:
                print("        hint:", item["hint"])
        counts = {}
        for item in RESULTS:
            counts[item["level"]] = counts.get(item["level"], 0) + 1
        print("summary:", ", ".join(k + "=" + str(v) for k, v in sorted(counts.items())))
    return 1 if any(item["level"] == "FAIL" for item in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
