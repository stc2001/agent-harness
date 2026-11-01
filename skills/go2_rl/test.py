"""Micro-validator for Go2 locomotion policy self-testing.

Usage (in sandbox):
    python /workspace/skills/go2_rl/test.py /workspace/output/policy.py

Checks policy.py for basic compliance before the heavy Genesis validation.
Returns exit code 0 on pass, 1 on failure.
"""
import ast
import importlib.util
import sys
from pathlib import Path

BANNED = {
    "torch", "tensorflow", "tf", "jax", "keras", "transformers",
    "sklearn", "stable_baselines3", "urllib", "requests", "socket", "http",
}
BANNED_CALLS = {"eval", "exec", "compile", "__import__"}
REQUIRED_OBS_KEYS = {"joint_pos", "joint_vel", "imu_ang_vel", "imu_gravity", "ref_cmd", "timestamp"}
REQUIRED_CTRL_MODES = {"position", "velocity", "force", "hybrid"}


def _check_imports(tree: ast.AST) -> list[str]:
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BANNED:
                    violations.append(f"banned import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in BANNED:
                violations.append(f"banned import: {node.module}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in BANNED_CALLS:
                violations.append(f"banned call: {node.func.id}()")
    return violations


def _has_go2policy(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Go2Policy":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "step":
                    return True
    return False


def test_policy(policy_path: str) -> dict:
    """Run all checks. Returns {"ok": bool, "errors": [...]}."""
    errors = []
    path = Path(policy_path)

    if not path.exists():
        return {"ok": False, "errors": [f"file not found: {policy_path}"]}

    # 1. Syntax
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return {"ok": False, "errors": [f"syntax error: {e}"]}

    # 2. Go2Policy class
    if not _has_go2policy(tree):
        return {"ok": False, "errors": ["missing Go2Policy class with step() method"]}

    # 3. Banned imports/calls
    errors.extend(_check_imports(tree))

    # 4. Load module and test
    try:
        spec = importlib.util.spec_from_file_location("policy", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        policy = mod.Go2Policy()
    except Exception as e:
        return {"ok": False, "errors": [f"import failed: {e}"]}

    # 5. step() returns dict with control_mode
    obs = {
        "joint_pos": [0.0] * 12,
        "joint_vel": [0.0] * 12,
        "imu_ang_vel": [0.0] * 3,
        "imu_gravity": [0.0, 0.0, -1.0],
        "lidar_points": None,
        "ref_cmd": {"lin_vel_x": 0.5, "lin_vel_y": 0.0, "ang_vel_yaw": 0.0},
        "timestamp": 0.0,
    }
    try:
        cmd = policy.step(obs)
    except Exception as e:
        return {"ok": False, "errors": [f"step() crashed: {e}"]}

    if not isinstance(cmd, dict):
        return {"ok": False, "errors": ["step() must return a dict"]}
    if "control_mode" not in cmd:
        return {"ok": False, "errors": ["step() return missing 'control_mode'"]}
    if cmd["control_mode"] not in REQUIRED_CTRL_MODES:
        errors.append(f"unknown control_mode: {cmd['control_mode']}")

    return {"ok": len(errors) == 0, "errors": errors}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test.py <policy_path>")
        print('Example: python /workspace/skills/go2_rl/test.py /workspace/output/policy.py')
        sys.exit(1)
    result = test_policy(sys.argv[1])
    if result["ok"]:
        print("PASS: policy.py passed basic checks")
        sys.exit(0)
    else:
        for e in result["errors"]:
            print(f"FAIL: {e}")
        sys.exit(1)
