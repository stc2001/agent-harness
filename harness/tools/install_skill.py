"""Built-in tool: install_skill — validate a skill installed in the sandbox.

Agent calls this tool after placing SKILL.md into the sandbox's
.skills/ directory.  The tool reads the SKILL.md frontmatter and
validates it has required fields (name, description).
"""


def register(registry):
    @registry.register(
        "install_skill",
        "Validate a skill installed in the sandbox. Returns DONE if the "
        "skill's SKILL.md has valid frontmatter, or an error if not found "
        "or malformed.",
        {"name": str},
        deps=["execute"],
    )
    def install_skill(name, execute=None):
        if execute is None:
            return "Error: sandbox execute not available"

        cmd = f"head -30 .skills/{name}/SKILL.md 2>/dev/null"
        stdout, stderr, code = execute(cmd)
        if code != 0 or not stdout.strip():
            return f"Error: skill '{name}' not found in .skills/"

        return _validate_frontmatter(stdout, name)


def _validate_frontmatter(raw, name):
    """Parse YAML frontmatter and validate required fields."""
    import yaml
    if not raw.startswith("---"):
        return f"Error: SKILL.md for '{name}' has no YAML frontmatter"

    parts = raw.split("---", 2)
    if len(parts) < 3:
        return f"Error: SKILL.md for '{name}' has malformed frontmatter"

    try:
        fm = yaml.safe_load(parts[1]) or {}
    except Exception:
        return f"Error: SKILL.md for '{name}' has invalid YAML frontmatter"

    skill_name = fm.get("name", "")
    if skill_name != name:
        return (f"Error: SKILL.md name '{skill_name}' does not match "
                f"directory name '{name}'")

    if not fm.get("description"):
        return f"Error: SKILL.md for '{name}' is missing description"

    return f"DONE: skill '{name}' validated in .skills/"
