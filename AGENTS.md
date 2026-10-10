# Safeguards
- Never read `~/.config/fish/private.fish` directly. It contains credentials and is sourced by `config.fish`.
- When creating a pull-request branch, ensure it is not the remote's default branch (`main`, `master`, or equivalent).
- Run `gh` commands outside the sandbox.
- Do not include Claude/Codex in commit authorship, co-authorship, or attribution.

# Implementation
- Read the relevant flow first. Choose the first fit: no change, existing code, standard library, platform feature, installed dependency, then minimal new code.
- For shared bugs, trace relevant callers and fix the shared root cause.
- Prefer deletion, existing patterns, and small diffs. Do not add abstractions, dependencies, boilerplate, or unrelated refactors without a demonstrated need.
- Never trade away trust-boundary validation, data-loss protection, security, accessibility basics, real-world calibration, or explicit requirements.
- Leave one runnable check for non-trivial logic. Mark intentional ceilings with the upgrade path.

# Communication
- Write clear, concise English comments like code.
- For code changes, lead with the result; briefly state what was skipped and when to add it. Explain fully when asked.

## Agent skills

### Issue tracker
Issues and specs live in `.scratch/<feature>/`.
For issue operations, read `docs/agents/issue-tracker.md`.

### Triage labels
Use the five default triage roles.
For triage operations, read `docs/agents/triage-labels.md`.

### Domain docs
Single-context: root `CONTEXT.md` and `docs/adr/`.
Before exploration, read `docs/agents/domain.md`.
