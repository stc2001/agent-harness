"""Skills system — filesystem-based skill discovery.

Skills live as SKILL.md files in the skills/ directory and are loaded
into sandboxes at spawn time.  AgentRuntime scans the skills/ directory
and appends a metadata pointer to the system prompt so the LLM knows
what skills are available.  Full skill bodies are read on demand by the
agent via read_file/bash tools — no pre-loading.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SkillMeta:
    """Lightweight skill metadata for system prompt inclusion."""
    name: str
    description: str


def list_host_skill_metas(base_dir: str = "skills/") -> list[SkillMeta]:
    """Scan host skills/ directory for SKILL.md files.

    Used by CLI ``harness skills list --host`` for host-side skill discovery.
    For sandbox-side scanning, use SDK ``_SkillsClient.scan()``.
    """
    base = Path(base_dir)
    skills: list[SkillMeta] = []

    if not base.is_dir():
        return skills

    for skill_dir in base.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        try:
            name, desc = _parse_skill_meta(skill_file)
            if name == skill_dir.name:
                skills.append(SkillMeta(name=name, description=desc))
        except Exception:
            continue

    return sorted(skills, key=lambda s: s.name)


def _parse_skill_meta(filepath: Path) -> tuple[str, str]:
    """Extract name and description from SKILL.md frontmatter."""
    import yaml
    raw = filepath.read_text(encoding="utf-8")
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            fm = yaml.safe_load(parts[1]) or {}
            return fm.get("name", ""), fm.get("description", "")


def compute_skill_mounts(
    skill_names: list[str],
    skills_dir: str = "skills",
) -> list[dict[str, str]]:
    """Build shared_folders entries for selective skill bind-mounts.

    Each entry maps ``{skills_dir}/{name}`` on the host to
    ``.skills/{name}`` inside the sandbox.  This is a pure function —
    it does not check filesystem existence (that's the caller's job).

    Args:
        skill_names: Skill names to mount.  Duplicates are silently
                     deduplicated.
        skills_dir: Host directory containing skill subdirectories.

    Returns:
        List of shared_folders entries (ready for SandboxConfig).
    """
    seen: set[str] = set()
    mounts: list[dict[str, str]] = []

    for name in skill_names:
        if name in seen:
            continue
        seen.add(name)
        mounts.append({
            "host_path": os.path.join(skills_dir, name),
            "sandbox_path": f".skills/{name}",
        })

    return mounts
    return "", ""
