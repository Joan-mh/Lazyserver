# LazyServer — Architecture & Plan

**Status:** Draft v0.1
**Document type:** Plan (the *how*)

This document records the technical decisions and the module layout. It is
downstream of `spec.md`: if a decision here conflicts with the spec, the spec
wins and this document is corrected.

---

## 1. Technology choices

| Concern | Choice | Rationale |
|---------|--------|-----------|
| Language | **Python 3.11+** | User preference; rich TUI ecosystem; usually present on Linux servers. |
| TUI framework | **Textual** (with Rich) | Modern, panel/keyboard friendly, MC-style layouts, actively maintained. |
| Data format | **YAML** via `ruamel.yaml` or `PyYAML` | Human- and AI-friendly; comments + multiline. `ruamel` preserves comments if we ever round-trip. |
| Backup store | **git when present, plain timestamped copies otherwise**; one interface, two implementations | Free history/diff with git; still works on a minimal OS without git (FR-2.5). Detected at startup. |
| Checksums | `hashlib` SHA-256 (stdlib) | FR-2.1 reliable change detection. |
| Service control | Commands from tconf (systemd defaults derived in a data layer) | NFR-1: no init-system assumptions in code. |
| Packaging | `pipx`-installable; single entry point `lazyserver` (alias `lsrv`) | Simple SSH install. |
| Config persistence | **TOML** at `$XDG_CONFIG_HOME/lazyserver/config.toml` of the **target user**, owned by the target user. Read with stdlib `tomllib`; write with a tiny hand-rolled emitter (or `tomli-w` if it gets fiddly). | FR-7.2. |

> If Textual proves too heavy for low-resource servers, the fallback TUI is
> `urwid`. This is a plan-level note, not a change to the spec.

## 2. Process & privilege model

- LazyServer runs as a local process on the server, normally under `sudo`/root
  (NFR-3). It shells out to the system package manager and service manager using
  the commands defined per distro in tconf.
- Before any privileged action, it checks it has the rights and **fails loudly**
  if not, rather than partially applying a change.

## 3. Distro detection

- Detect the current distro from `/etc/os-release` (`ID`, `ID_LIKE`).
- Map to a tconf distro id (`ubuntu`, `arch`, `fedora`, `opensuse`).
- Exact `ID` match (e.g. `ID=ubuntu`) is a supported distro. Resolution via
  `ID_LIKE` or an aliased `ID` (e.g. Manjaro→arch, Debian→ubuntu) is handled
  the same way but **flagged to the user as inferred** — package names and
  paths are usually but not always identical. Fits the didactic goal: tell
  the student what we inferred.
- A **default installer command** per distro id (apt/pacman/dnf/zypper) lets
  tconf files omit `install` and just give `package`. (Data-layer convenience,
  overridable — see schema §4/§5.)

## 4. Module breakdown (suggested)

```
lazyserver/
├── app.py                 # Textual app entry; screen routing; key bindings
├── cli.py                 # argparse: `lazyserver`, `recover --all`, etc.
├── config.py              # user settings (editor, backup store, tconf paths)
├── platform/
│   ├── distro.py          # os-release detection → distro id
│   ├── user.py            # resolve target user: $SUDO_USER → $USER → setting (FR-1.10)
│   └── runner.py          # safe subprocess wrapper (capture, errors, dry-run)
├── tconf/
│   ├── model.py           # dataclasses: Entry, ManagedFile, DistroProfile
│   ├── loader.py          # read + validate YAML (schema §8 rules)
│   └── resolve.py         # resolve effective path/unit/actions for THIS distro
├── services/
│   ├── control.py         # start/stop/reload/... from resolved actions
│   └── defaults.py        # default action templates as a DATA TABLE keyed by
│                          # init system (e.g. {"systemd": {"start": ["systemctl","start","{unit}"], ...}}).
│                          # NOT a code switch — adding an init system means adding a row, per NFR-1.
├── backup/
│   ├── checksums.py       # SHA-256, baseline store
│   ├── pending.py         # accumulate-across-sessions pending set (FR-2.2)
│   ├── store.py           # BackupStore interface (snapshot/restore/list)
│   ├── store_git.py       # GitBackupStore: shell out to `git` (FR-2.5)
│   ├── store_plain.py     # PlainBackupStore: timestamped copies (FR-2.5)
│   ├── create.py          # create file in set: ownership + preview (FR-1.7/1.8/1.9)
│   └── restore.py         # restore scopes + pre-restore snapshot (FR-3.2)
├── recovery/
│   ├── plan.py            # ordered install→restore→enable plan (FR-5.3.1)
│   ├── run.py             # execute; interactive or non-interactive
│   └── report.py          # human log + JSON summary (FR-5.3.2/.3)
└── ui/
    ├── services_screen.py # services section
    ├── apps_screen.py     # apps section
    ├── files_screen.py    # managed files of an entry; Enter→editor
    ├── backup_screen.py   # pending list + backup scopes
    ├── restore_screen.py  # restore scopes
    └── settings_screen.py # editor / paths (FR-7)
```

> Services and Applications are **separate screens/sections** (FR per spec §3)
> but share `tconf/`, `backup/`, and `recovery/` machinery.

## 5. Key data flows

**Editing a file (FR-1.3):**
`files_screen` → resolve effective path for this distro → suspend TUI →
launch `$editor path` → on return, recompute checksum → if changed, add to
pending set.

**Backup (FR-2):**
selection (pending / files / entries) → `store.snapshot()` writes content into
git-backed store + commits → update baseline checksums → clear those from
pending.

**Restore (FR-3):**
selection (file / entry / system) → `restore.pre_snapshot()` of current live
files → copy chosen version back with correct owner/mode → report.

**Recovery (FR-5.3):**
`plan.build()` orders entries; for each: install → restore config → enable/start;
`report` writes human log + JSON summary; interactive (TUI step-through) or
`lazyserver recover --all` (non-interactive).

## 6. Error handling & logging

- A single subprocess runner captures stdout/stderr/exit code; every external
  command is logged.
- Recovery emits two artifacts (FR-5.3.2/.3): `recovery-YYYYMMDD-HHMMSS.log`
  (human) and `recovery-YYYYMMDD-HHMMSS.json` (machine: per-entry status).

## 7. Testing strategy

- Pure logic (loader/validation, checksum/pending, distro resolution, recovery
  planning) is unit-tested with no root and no real services.
- The subprocess runner has a **dry-run** mode so service/install/recovery flows
  can be tested without touching the system.
- **Vagrant VMs** (Ubuntu + Arch) are used in v2.0 work for end-to-end testing;
  not required for the MVP unit tests.

## 8. Decisions still open

- Textual vs urwid final call (lean: Textual; revisit if footprint matters).

Resolved:
- Backup store is git-when-present / plain-otherwise, detected at startup,
  behind one `BackupStore` interface (FR-2.5); when git is used, LazyServer
  shells out to the `git` CLI (simplest, no extra dependency).
- User-config is TOML at the target user's `$XDG_CONFIG_HOME/lazyserver/`
  (spec FR-7.2); read via stdlib `tomllib`.
- `actions` value shape: `default` (string) or argv list, no shell
  (tconf-schema §5).
- `files`/`file_sets` share one flat id namespace per entry; cross-folder
  entry-id duplicates resolve last-wins (tconf-schema §8, spec FR-7.4).
