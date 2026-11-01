"""Built-in tools: Hashline — content-addressed file editing.

hashline_read  — read a file with per-line content hashes as anchors.
hashline_edit — edit by hash reference; rejected if anchors diverge.

Inspired by oh-my-pi's Hashline (https://blog.can.ac/2026/02/12/the-harness-problem/).
"""

import hashlib


def _hash_line(text: str) -> str:
    """Return a 2-char content hash for a single line."""
    return hashlib.md5(text.encode()).hexdigest()[:2]


def _read_with_hashes(path: str, execute) -> tuple[list[str], str | None]:
    """Read file content and return (lines, error)."""
    stdout, stderr, code = execute(
        f"python -c \"print(open('{path}').read())\"")
    if code != 0:
        return [], f"Error reading '{path}': {stderr}"
    return stdout.split("\n"), None


# ── Module-level tools (importable for testing) ────────────

def hashline_read(path, execute=None, permissions=None, audit_log=None,
                    path_map=None):
    """Read a file with per-line content hashes as editing anchors."""
    from .file_io import _check_access
    _check_access(path, permissions, "any", audit_log, path_map=path_map)

    lines, error = _read_with_hashes(path, execute)
    if error:
        return error

    result = []
    for i, line in enumerate(lines):
        if not line:
            continue
        h = _hash_line(line)
        result.append(f"{i+1}:{h}|{line}")

    return "\n".join(result)


def hashline_edit(path, edits, execute=None, permissions=None,
                  audit_log=None, path_map=None):
    """Apply edits to a file using content-hash anchors."""
    from .file_io import _check_access
    _check_access(path, permissions, "write", audit_log, path_map=path_map)

    lines, error = _read_with_hashes(path, execute)
    if error:
        return error

    hash_to_idx: dict[str, int] = {}
    for i, line in enumerate(lines):
        h = _hash_line(line)
        hash_to_idx[h] = i

    # Validate all anchors before applying any edits
    for edit in edits:
        anchor = edit.get("anchor_hash", "")
        if anchor and anchor not in hash_to_idx:
            return f"Edit rejected: anchor '{anchor}' not found."
        end = edit.get("range_end_hash", "")
        if end and end not in hash_to_idx:
            return f"Edit rejected: range end anchor '{end}' not found."

    for edit in reversed(edits):
        op = edit.get("op")
        anchor = edit.get("anchor_hash", "")
        end_anchor = edit.get("range_end_hash", "")
        text = edit.get("text", "")

        if op == "replace":
            if end_anchor:
                start_i = hash_to_idx[anchor]
                end_i = hash_to_idx[end_anchor]
                lines[start_i:end_i + 1] = text.split("\n")
            else:
                lines[hash_to_idx[anchor]] = text
        elif op == "insert_after":
            idx = hash_to_idx[anchor]
            for j, nl in enumerate(text.split("\n")):
                lines.insert(idx + 1 + j, nl)
        elif op == "delete":
            if end_anchor:
                start_i = hash_to_idx[anchor]
                end_i = hash_to_idx[end_anchor]
                del lines[start_i:end_i + 1]
            else:
                del lines[hash_to_idx[anchor]]
        else:
            return f"Unknown edit op: '{op}'"

    import base64
    content = "\n".join(lines)
    encoded = base64.b64encode(content.encode()).decode()
    cmd = (
        f"python -c \""
        f"import base64; "
        f"open('{path}','w').write(base64.b64decode('{encoded}').decode())"
        f"\""
    )
    stdout, stderr, code = execute(cmd)
    if code != 0:
        return f"Error writing '{path}': {stderr}"
    return f"Applied {len(edits)} edit(s) to {path}"


# ── Registry ───────────────────────────────────────────────

def register(registry):
    """Register hashline tools on the given ToolRegistry."""
    registry.register("hashline_read",
        "Read a file with per-line content hashes as editing anchors.",
        {"path": str},
        deps=["execute", "permissions", "audit_log", "path_map"],
    )(hashline_read)

    registry.register("hashline_edit",
        (
            "Apply edits to a file using content-hash anchors from hashline_read. "
            "Each edit operation specifies an op, anchor_hash, and optional text. "
            "Supported ops: replace (replace anchored line), insert_after (add line after anchor), "
            "delete (remove anchored line). Use range_end_hash for delete/insert spanning multiple lines."
        ),
        {"path": str, "edits": list},
        deps=["execute", "permissions", "audit_log", "path_map"],
        param_descriptions={
            "path": "Absolute path of the file to edit, e.g. /workspace/main.py",
            "edits": (
                "List of edit operations to apply in order. Each operation is an object: "
                '{"op": "replace"|"insert_after"|"delete", "anchor_hash": "xx:yy", '
                '"text": "...", "range_end_hash": "xx:yy"?}'
            ),
        },
        examples=[
            {"op": "replace", "anchor_hash": "a1", "text": "new line content"},
            {"op": "insert_after", "anchor_hash": "b2", "text": "inserted line"},
            {"op": "delete", "anchor_hash": "c3"},
        ],
    )(hashline_edit)
