# Reference: Implementation Prompt Crafter

## Phase Details

### Phase 1 — Environment Lock
Eliminate all platform/toolchain ambiguity before the agent writes a single line of code.

**Template:**
```
Implement a [App Type] for [Platform] [Version]+ using [Language] [Version]+.
Build system: [SPM / Xcode / Makefile targets: build, run, install, clean].
Runtime mode: [LSUIElement / regular app / CLI].
Output: [signed .app / unsigned binary / ...].
```

### Phase 2 — SDK API Mapping
Explicitly bind each feature to a concrete API. This prevents the agent from choosing a convenient but wrong API.

**Template:**
```
Use the following SDK APIs (do not substitute without explicit reason):
- Feature A: ExactClass.exactMethod()
- Feature B: Framework.preferredAPI
- Avoid: DeprecatedClass.legacyMethod — use ModernClass.modernMethod instead
```

### Phase 3 — Requirement Structuring
Order by user flow. Each item must be atomic and contain behavior + mechanism + edge case.

**Template:**
```
Requirements:
1. [Trigger]: [User action] → [System behavior]
   - Mechanism: [Specific API / algorithm]
   - Visual feedback: [UI component, size, animation duration]
   - Edge case: [what if X happens]
```

### Phase 4 — Quantified Non-Functional Spec
Every subjective feeling must be converted to objective numbers.

**Checklist:**
- Sizes: px / pt
- Animations: duration (s), easing curve (spring / easeInOut), physics params
- Visual materials: exact enum values (`.hudWindow`, `.vibrantDark`)
- Colors: system semantic color or hex
- Window behavior: level (`.floating`), activation policy, movability

### Phase 5 — Boundary & Negative Constraints
Explicit "DO NOT" rules prevent the agent from over-optimizing or making unsafe assumptions.

**Categories to cover:**
- Input method conflicts (CJK switching, third-party IME)
- Global shortcut vs system shortcut conflicts
- Clipboard / privacy data lifecycle (backup → use → restore)
- LLM/AI conservative correction policy (what is allowed to change)
- Fake implementations (stub UI, hardcoded animation, mock data)

### Phase 6 — State Machine
Define component lifecycle explicitly. No implied "then..." transitions.

**Template:**
```
State Flow:
Idle → (Hold Fn) → Recording → (Release Fn) → [If LLM enabled] Refining → Injecting → Idle
- Recording: show floating window, animate waveform from live audio
- Refining: floating window shows "Refining...", await LLM response
- Error/Timeout: [define fallback behavior]
```

### Phase 7 — Configuration & Persistence

**Checklist:**
- All tunables with data type, default value, storage mechanism
- Sensitive info: must be clearable, never hardcoded, never logged
- UserDefaults vs Keychain vs plist decision rationale

### Phase 8 — Prompt Assembly & Validation

**Assembly order (fixed):**
1. Environment Lock
2. SDK API Mapping
3. Requirement Structuring
4. Quantified UI Spec
5. Boundary & Negative Constraints
6. State Machine
7. Configuration
8. Deliverables

---

## Fill-in-the-Blank Template

Copy and fill this to generate the final prompt:

```
Implement a [PLATFORM] app ([LANGUAGE], [VERSION]+) with the following requirements:

ENVIRONMENT:
- Build: [BUILD_SYSTEM], targets: [TARGETS]
- Runtime: [MODE], output: [OUTPUT_FORMAT]
- Permissions/Entitlements: [LIST]

SDK APIs (do not substitute):
- [Feature A]: [Exact API]
- [Feature B]: [Exact API]
- [Avoid]: [Deprecated/Wrong API]

REQUIREMENTS:
1. [Trigger]: [Action] → [Behavior]
   - Mechanism: [API + approach]
   - Visual: [Size, color, animation_duration, material]
   - Edge case: [Handling]

2. ...

NEGATIVE CONSTRAINTS:
- DO NOT [unwanted behavior 1]
- NEVER [unwanted behavior 2]

STATE FLOW:
[Idle] → [Event] → [State A] → [Event] → [State B]

CONFIGURATION:
- [Key]: stored in [UserDefaults/Keychain], default [value], type [type]

DELIVERABLES:
- [ ] Makefile with [targets]
- [ ] Signed [output format]
```

---

## Worked Example Summary

The macOS voice-input prompt referenced in the skill introduction follows this structure exactly:

| Phase | Example Content |
|-------|-----------------|
| Environment | macOS 14+, Swift, SPM, LSUIElement, signed .app |
| API Mapping | CGEventTap, SFSpeechRecognizer, NSPanel+NSVisualEffectView, TISInputSource |
| Requirements | Fn hold/release flow, language switching, LLM refinement |
| Quantified Spec | 56px/28px, 0.35s/0.25s/0.22s, weights [0.5, 0.8, 1.0, 0.75, 0.55] |
| Negative Constraints | "Never rewrite correct text", "no hardcoded fake animations" |
| State Machine | Idle → Recording → Refining → Injecting → Idle |
| Config | API Base/Key/Model in UserDefaults, Key field clearable |
| Deliverables | Makefile (build/run/install/clean), signed .app bundle |

---

## Implicit Assumption Warning

This workflow assumes the coding agent **has the target SDK in its training context**.

If the SDK is:
- Private / undocumented
- Extremely new (released after knowledge cutoff)
- Custom / in-house

**Inject SDK reference material** into the prompt (header files, doc excerpts, or `@` file references) before Phase 2.
