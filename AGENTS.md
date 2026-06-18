# AGENTS.md — Instructions for the coding agent

This repository is built with **Spec Driven Development**. The documents in
`docs/` are the source of truth. Code is downstream of them.

## Order of authority

1. `docs/spec.md` — what the system must do (the contract).
2. `docs/tconf-schema.md` — the exact data format.
3. `docs/architecture.md` — how it is built (tech + modules).
4. `docs/roadmap.md` — the order to build it.

If you find a conflict or a gap, **stop and ask** (or propose a spec edit). Do
**not** silently invent behaviour. When the spec and the code disagree, fix the
spec first, then the code.

## How to work

- Build **phase by phase** following `roadmap.md`. Finish and verify a phase
  before starting the next.
- Tie each piece of work to a requirement id (e.g. `FR-2.2`). Reference it in
  commit messages.
- Keep distro/init-system knowledge in **data**, never in code (NFR-1). If you
  feel the urge to write `systemctl` in a `.py` file as the only way to do
  something, that is a signal to push it into the tconf/data layer instead.
- Destructive actions are reversible by design (pre-restore snapshot, FR-3.2)
  but **do not** prompt for confirmation — see spec NFR-2 and §9 Deployment
  assumptions. A deployment outside the classroom VM context should revisit
  this.
- Fail loudly on missing privileges; never half-apply a change (NFR-3).
- Prefer the standard library; keep dependencies minimal (NFR-4).

## Tech constraints (from architecture.md)

- Python 3.11+, Textual for the TUI, YAML for tconf, SHA-256 for checksums,
  git-backed backup store, `lazyserver` entry point (alias `lsrv`).
- Provide a **dry-run** mode in the subprocess runner so flows can be tested
  without touching a real system.

## Definition of done for any task

- Meets the referenced requirement(s) in `spec.md`.
- Has a test where logic is testable without root/real services.
- Errors are clear and actionable.
- Does not hard-code distro specifics that belong in tconf.

## Scope guard

- **In scope now:** Phases 0–7 (browse, learn, edit, control, backup, restore,
  recovery, polish).
- **Deferred to v2.0:** Vagrant + Ansible export (roadmap Phase 8). Do not build
  it early unless asked.

## Language

All UI strings and all default tconf files are in **English** (FR-6).
