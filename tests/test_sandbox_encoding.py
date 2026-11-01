"""Sandbox command output must survive non-UTF-8 bytes.

MEASURED failure on a real run (local MiniCPM5, seeded history):
    tool_result: Error: 'utf-8' codec can't decode byte 0xe3 in position 61:
                 invalid continuation byte
    -> agent_failed -> the failure was then assembled into a `user` message,
       which broke the tool_call pairing and produced a second, confusing error
       from the API: "Messages with role 'tool' must be a response to a
       preceding message with 'tool_calls'".

Root cause: `subprocess.run(..., text=True)` decodes with the strict default, so
ANY command emitting non-UTF-8 bytes (a latin-1 log, a binary file, a truncated
multi-byte sequence) makes the whole tool call raise instead of returning text.
A shell tool must be byte-tolerant: the model needs to SEE the output, with
undecodable bytes replaced — not have the call fail.
"""
from __future__ import annotations

import subprocess

import pytest

from harness.sandbox.impl import BubblewrapSandbox, SandboxConfig


def _sandbox():
    return BubblewrapSandbox()


def _cfg():
    return SandboxConfig(backend="bubblewrap",
                         firewall={"network": "host"},
                         shared_folders=[])


def test_non_utf8_stdout_does_not_raise(tmp_path):
    """A command printing invalid UTF-8 must return text, not raise."""
    sb = _sandbox()
    cid = sb.spawn(_cfg())
    try:
        # \xe3 alone is an invalid sequence (a truncated 3-byte char)
        out, err, code = sb.execute(
            cid, "python3 -c \"import sys; sys.stdout.buffer.write(b'a'*60+b'\\xe3')\"")
        assert code == 0
        assert isinstance(out, str), "stdout must be text"
        assert len(out) >= 60, "the decodable prefix must survive"
    finally:
        sb.kill(cid)


def test_non_utf8_stderr_does_not_raise():
    """The same for stderr — error paths matter even more."""
    sb = _sandbox()
    cid = sb.spawn(_cfg())
    try:
        out, err, code = sb.execute(
            cid, "python3 -c \"import sys; sys.stderr.buffer.write(b'bad:\\xe3')\"")
        assert code == 0
        assert isinstance(err, str)
    finally:
        sb.kill(cid)
