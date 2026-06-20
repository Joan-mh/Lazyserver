# LazyServer — Roadmap

**Status:** Draft v0.1
**Document type:** Tasks (break the spec into verifiable phases)

Each phase ends in something you can actually run and check. The agent should
complete and verify a phase before starting the next, and should treat each
checklist item as a unit of work tied back to a requirement in `spec.md`.

---

## Phase 0 — Project skeleton

- [ ] Repo layout per `architecture.md` §4; `pyproject.toml`; `lazyserver`
      entry point (alias `lsrv`).
- [ ] `config.py`: read/write user settings (editor, backup store path, tconf
      paths) — FR-7.
- [ ] `platform/distro.py`: detect distro from `/etc/os-release` — arch §3.
- [ ] `platform/user.py`: resolve target user ($SUDO_USER → $USER → setting) — FR-1.10.
- [ ] `platform/runner.py`: subprocess wrapper with capture + dry-run.
- **Done when:** `lazyserver --version` runs; settings persist; distro detected.

## Phase 1 — tconf data layer

- [ ] `tconf/model.py`, `loader.py`, `resolve.py` — schema §2–§5.
- [ ] Support both fixed `files` and glob-based `file_sets` — schema §3/§3b, FR-1.6.
- [ ] Validation with clear errors — schema §8.
- [ ] Ship the 8 pre-loaded service files + 1 example app (neovim) — FR-4.2.
- **Done when:** all default tconf files load and validate; effective paths/units
      resolve correctly for ubuntu and arch.

## Phase 2 — Read-only TUI

- [ ] Services section and Apps section (separate) — spec §3.
- [ ] Entry view lists managed files with description + example — FR-1.1/1.2/4.3.
- **Done when:** you can browse all entries and read didactic content over SSH.

## Phase 3 — Edit + service control

- [ ] Enter opens file in chosen editor; re-check on return — FR-1.3.
- [ ] Create a new file in a file_set (or a missing fixed file): permission
      preview → name input → pre-fill from `example` → editor — FR-1.7/1.8/1.9.
      Ownership resolution (sibling → directory → YAML override → root+warning)
      is unit-testable without root via the dry-run runner.
- [ ] start/stop/restart/reload/enable/disable/status from resolved actions,
      with confirmation on destructive actions — FR-1.5, NFR-2.
- **Done when:** you can edit a real config and control a real service on a test VM.

## Phase 4 — Backup

- [x] SHA-256 baseline + checksum change detection — FR-2.1.
- [x] Pending set that accumulates across sessions — FR-2.2.
- [x] Backup store behind one interface: build `PlainBackupStore` first (always
      works), then add `GitBackupStore` as the enhancement; detect git at
      startup — FR-2.5.
- [x] Backup by pending / by files / by entries — FR-2.3/2.4.
- [x] Expand file_set globs at backup time so new user-created files are caught — FR-1.6.
- **Done when:** edits show up as pending; backing up clears them; history is in git. ✓ VM-verified.

## Phase 5 — Restore

- [x] Restore by file / by entry / whole system — FR-3.1.
- [x] Restore file_sets including files absent from disk; never delete extras — FR-3.4.
- [x] Automatic pre-restore snapshot — FR-3.2.
- [x] Correct ownership/permissions on restore — FR-3.3.
- **Done when:** a bad edit can be fully undone, and the undo is itself reversible.
      ✓ VM-verified on real configs (CLI + TUI, all three scopes, partial
      single-file restore + undo round-trip).

## Phase 6 — Full recovery

- [ ] Ordered plan install→restore→enable — FR-5.3.1.
- [ ] Interactive (TUI) and non-interactive `recover --all` — FR-5.3.4.
- [ ] Human log + JSON summary of successes/failures — FR-5.3.2/3.
- **Done when:** on a fresh VM, clone tconf+backups → `recover --all` rebuilds
      the box and reports what failed.

## Phase 7 — Polish & docs (→ v1.0)

- [ ] README, install-over-SSH guide, the AI-prompt how-to (schema §9).
- [ ] Unit tests for loader/checksums/pending/resolution/recovery planning.
- [ ] dry-run end-to-end pass.
- **v1.0 ships here.**

## Phase 8 — v2.0: Vagrant + Ansible (deferred)

- [ ] Vagrantfile(s) for Ubuntu + Arch test VMs.
- [ ] Export an Ansible playbook generated from tconf entries — spec §5 / req 6.
- **Done when:** `lazyserver export ansible` yields a playbook that installs the
      registered entries.

---

## Suggested order of value

MVP that already earns its place = **Phases 0–5** (browse, learn, edit, control,
back up, restore). Recovery (6) and the v2.0 export (8) build on that base.
