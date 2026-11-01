---
name: impl-prompt-crafter
description: Transform environment setup, SDK APIs, and user requirements into a high-precision implementation prompt ready for coding agents. Use when the user needs to write a code-generation prompt, implementation plan prompt, SDK-based development directive, or translate product requirements into executable AI coding instructions.
---

# Implementation Prompt Crafter

## Quick Start

Given `[environment]` + `[SDK APIs]` + `[user requirements]`, produce a single prompt that a coding agent can execute without clarification.

**Assembly order matters**: Environment → API Mapping → Requirements → UI Spec → Negative Constraints → State Machine → Config → Deliverables.

## 8-Phase Workflow

### Phase 1 — Environment Lock
- [ ] Target OS + minimum version
- [ ] Language + version
- [ ] Build system & targets (build/run/install/clean)
- [ ] Runtime mode (LSUIElement / CLI / Agent)
- [ ] Output format (signed .app / binary / package)

### Phase 2 — SDK API Mapping
- [ ] List required frameworks
- [ ] Map each feature to **exact preferred API**
- [ ] Note **avoided/deprecated APIs**
- [ ] Flag entitlements or permissions needed

### Phase 3 — Requirement Structuring
- [ ] Order by user flow (trigger → process → feedback → end)
- [ ] One atomic item per numbered line
- [ ] Each item: behavior + mechanism + edge case

### Phase 4 — Quantified Non-Functional Spec
- [ ] Replace all subjective adjectives with numbers
- [ ] Sizes in px/pt, durations in seconds, curves named
- [ ] Materials: exact enum values (e.g. `.hudWindow`)

### Phase 5 — Boundary & Negative Constraints
- [ ] Input method / global shortcut conflicts
- [ ] Clipboard / privacy data cleanup rules
- [ ] LLM/AI conservative policy: what CANNOT be changed
- [ ] Explicit "DO NOT use fake/hardcoded X"

### Phase 6 — State Machine
- [ ] Define all states (idle, active, processing, error)
- [ ] State transition triggers
- [ ] UI feedback per state
- [ ] Error / timeout / cancel branches

### Phase 7 — Configuration & Persistence
- [ ] All tunables with type, default, storage mechanism
- [ ] Sensitive fields: clearable, no hardcode, no log leak

### Phase 8 — Prompt Assembly & Validation
Assemble in order above, then run the validation checklist.

## Validation Checklist

| # | Gate | Pass Criteria |
|---|------|---------------|
| 1 | Testable | Every requirement has a verifiable assertion or manual step |
| 2 | No fuzz | Zero subjective adjectives remain |
| 3 | API explicit | Agent never needs to guess class/method |
| 4 | Guardrails | At least one "DO NOT" or "NEVER" constraint present |
| 5 | Boundaries | IME, clipboard, permissions, error branches covered |
| 6 | State complete | No "then..." gaps in lifecycle |
| 7 | Deliverable clear | File structure, build artifact, signing defined |

## Advanced

See [REFERENCE.md](REFERENCE.md) for detailed phase explanations, a fill-in-the-blank template, and a worked example.