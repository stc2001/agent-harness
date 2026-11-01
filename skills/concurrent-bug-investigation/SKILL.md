---
name: concurrent-bug-investigation
description: Disciplined investigation loop for concurrency bugs and flakes — double-execution, races, deadlocks, resource leaks under multi-thread/multi-process load. Deterministic repro → variable isolation → mechanism verification → closed-loop fix. Use when a bug only appears under concurrency, a test is flaky (passes alone, fails in full suite), something runs twice / spawns twice, or resources leak after parallel work. Triggers: "并发", "竞态", "race", "flake", "偶发", "双执行", "spawn 两次", "并发下", "resource leak", "parallel".
---

# Concurrent Bug Investigation

A discipline for bugs that only manifest under concurrency. The failure is almost never what it looks like — "TOCTOU window too short" was a red herring that cost a full day. **Verify the mechanism before touching a fix.**

When exploring the codebase, use the project's domain glossary (CONTEXT.md) and check ADRs in the affected area.

## Workflow Overview

```
Layer 1  Deterministic repro   (real timing unreliable → stub-controlled trigger)
Layer 2  Variable isolation    (one variable at a time; 2×2 quadrant for interactions)
Layer 3  Mechanism verification (process tree / signals / kernel behavior — not symptoms)
Layer 4  Closed-loop fix       (TDD + side-effect audit: correctness + leaks + contracts)
```

Each step is recorded as a triple: **(event → goal → object)**.

---

## Layer 1 — Deterministic repro

**Goal**: turn "occasional flake" into "guaranteed failure" with a known trigger.

1. Identify the external uncertainty the code depends on (real processes, timing, randomness, network).
2. Replace it with a stub/mock that injects a **controllable uncertainty window**.
3. Amplify with concurrency (threading, barrier, high iteration count).

**Pass criteria**: the same test fails *every time*; stub parameters map cleanly to the failure.

```python
class ControllableStub:
    def __init__(self, fail_window: int = 20):
        self.calls, self.fail_window = 0, fail_window
    def is_alive(self, *a):
        self.calls += 1
        return self.calls > self.fail_window  # "not ready" for first N calls

def test_concurrent_x():
    stub = ControllableStub(fail_window=15)
    # ... concurrent calls to code under test ...
    assert not stub.double_executed
```

**If you cannot reproduce deterministically, stop and fix the repro first.** A bug you can't reproduce deterministically can't be bisected, verified, or regression-tested. "It's a real flake" is the last resort, not the first.

---

## Layer 2 — Variable isolation

**Goal**: find the *unique* trigger; rule out innocent variables.

1. List every suspect (flags, params, timing, concurrency level).
2. Change **exactly one variable** per run; fix everything else; record the result.
3. Tabulate variable × result.
4. For suspected interactions, use a **2×2 quadrant**:

| Var A | Var B | Result |
|:---:|:---:|:---:|
| ✓ | ✓ | 1/10 |
| ✓ | ✗ | 3/10 |
| ✗ | ✓ | **10/10** ← decisive |
| ✗ | ✗ | 10/10 |

**Read the table**: when flipping one variable flips the result to 10/10, that variable is the culprit. (Here: anchor's own `--die-with-parent`, not the probe's.)

**Pass criteria**: exactly one variable flips the result; all others are inert.

---

## Layer 3 — Mechanism verification

**Goal**: explain *why* the trigger causes the bug — mechanism, not symptom.

Techniques, in order of availability:
1. **Process/resource observation**: `/proc` tree, `wchan`, pgid/sid, `ps --forest`.
2. **Signal tracing**: SIGCHLD handler to capture child exit signals and status.
3. **Syscall/audit level**: trace kill/killpg/signal sources (strace, auditctl, eBPF if available).
4. **Documentation**: man pages for the exact semantics (e.g. bwrap `--die-with-parent` cascade).
5. **Confirmation flip**: temporarily remove/add the suspect flag and confirm the behavior flips.

**Pass criteria**: the mechanism fully explains the symptom *and* predicts extra behavior (e.g. "removing DWP will leak" was confirmed by experiment).

---

## Layer 4 — Closed-loop fix

**Goal**: fix the root cause without introducing side effects.

1. **TDD**: write the deterministic regression test (Layer 1 stub scenario) → confirm RED.
2. **Minimal change**: touch only the root cause; no speculative abstraction.
3. **Side-effect audit** — always check all three:
   - **Correctness**: original bug gone (run flaky test several times).
   - **Resource cleanup**: no leaks (see Leak Check below).
   - **Interface contract**: protocols/signatures not broken by new dependencies.
4. **Full regression** + multiple runs of the original flaky test.

---

## Leak Check (mandatory after concurrency fixes)

Concurrency bugs love to hide leaks. Always verify after fixing:

```bash
# baseline
ps -eo pid,cmd | grep -c "[<pattern>]"     # [x] trick avoids self-match
# run N operations (spawn/kill loop)
# compare delta — 0 passes, >0 leaks
pgrep -x sleep | wc -l
```

**Anti-pattern**: `grep "pattern"` matches itself. Use `grep "[p]attern"` or `pgrep -x`.

---

## Common traps

| Trap | Symptom | Counter |
|------|---------|---------|
| Fixing the symptom | adding waits/timeouts "relieves" the flake | mechanism first |
| Changing multiple variables | can't tell which one worked | one at a time |
| Ignoring leaks | main bug fixed, resources now leak | mandatory leak check |
| Dirty environment | leftover processes skew results | clean before each experiment round |
| Tool self-match | grep/ps matches itself | `[x]` trick or `pgrep -x` |

---

## Outputs

Finish with:
1. **Technical report** `docs/technical-report-<issue>.md` — success AND failed paths, triple-step records.
2. **ADR** if the fix changes architecture semantics.
3. **Deterministic regression test** in the suite.
4. **SOP/skill** if the method generalizes.

## Reference

- `docs/technical-report-parallel-toctou.md` — worked example (PDEATHSIG mis-fire, full trajectory).
- `docs/sop/concurrent-bug-investigation-sop.md` — this skill's source document.
