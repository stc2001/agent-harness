"""Tests for SDK _SkillsClient — scan() from sandbox filesystem."""

from __future__ import annotations


class FakeSandbox:
    def __init__(self, global_dirs=None, agent_dirs=None, files=None):
        self._global = global_dirs or []
        self._agents = agent_dirs or []
        self._files = files or {}

    def execute(self, cid, cmd):
        if "head" in cmd:
            for name, content in self._files.items():
                if f"/{name}/SKILL.md" in cmd:
                    return (content, "", 0)
            return ("", "", 0)
        if ".skills/" in cmd:
            # Flat scan: return all dirs
            return ("\n".join(self._global + self._agents), "", 0)
        return ("", "", 0)


class TestSkillsScan:
    def test_discovers_all(self):
        from harness.sdk.client import _SkillsClient
        sbx = FakeSandbox(
            global_dirs=["bash-expert", "z-shell"],
            agent_dirs=["test-skill"],
            files={
                "bash-expert": "---\nname: bash-expert\ndescription: Bash\n---\n# B",
                "z-shell": "---\nname: z-shell\ndescription: Zsh\n---\n# Z",
                "test-skill": "---\nname: test-skill\ndescription: Test\n---\n# T",
            })
        sc = _SkillsClient(type("F",(),{"_sandbox_mgr":None})())
        metas = sc.scan(sbx, "mock")
        assert {m.name for m in metas} == {"bash-expert", "z-shell", "test-skill"}

    def test_sorted_alpha(self):
        from harness.sdk.client import _SkillsClient
        sbx = FakeSandbox(
            global_dirs=["z-shell", "bash-expert"],
            files={
                "bash-expert": "---\nname: bash-expert\ndescription: B\n---\n#",
                "z-shell": "---\nname: z-shell\ndescription: Z\n---\n#",
            })
        sc = _SkillsClient(type("F",(),{"_sandbox_mgr":None})())
        metas = sc.scan(sbx, "mock")
        assert [m.name for m in metas] == ["bash-expert", "z-shell"]

    def test_empty_dir(self):
        from harness.sdk.client import _SkillsClient
        sbx = FakeSandbox()
        sc = _SkillsClient(type("F",(),{"_sandbox_mgr":None})())
        assert sc.scan(sbx, "mock") == []

    def test_skips_bad_frontmatter(self):
        from harness.sdk.client import _SkillsClient
        sbx = FakeSandbox(
            global_dirs=["good", "bad"],
            files={
                "good": "---\nname: good\ndescription: OK\n---\n#",
                "bad": "# No frontmatter",
            })
        sc = _SkillsClient(type("F",(),{"_sandbox_mgr":None})())
        metas = sc.scan(sbx, "mock")
        names = {m.name for m in metas}
        assert "good" in names
        assert "bad" not in names

    def test_all_skills_found_flat(self):
        """All skills in .skills/ are found regardless of origin."""
        from harness.sdk.client import _SkillsClient
        sbx = FakeSandbox(
            global_dirs=["bash-expert"],
            agent_dirs=["test-skill"],
            files={
                "bash-expert": "---\nname: bash-expert\ndescription: B\n---\n#",
                "test-skill": "---\nname: test-skill\ndescription: T\n---\n#",
            })
        sc = _SkillsClient(type("F",(),{"_sandbox_mgr":None})())
        metas = sc.scan(sbx, "mock")
        names = {m.name for m in metas}
        assert names == {"bash-expert", "test-skill"}
