# LazyServer — Functional Specification

**Status:** Draft v0.1 (specification phase)
**Document type:** Specify (the *what* and *why*, not the *how*)

---

## 1. Summary

LazyServer is a terminal (TUI) application for Linux servers that helps a user
**configure, back up, restore, and learn** about network services and
applications. It is operated through a normal SSH session: LazyServer runs *on*
the server, and the user reaches it like any other terminal program. There is no
GUI and no separate client/server protocol.

LazyServer has two purposes that are equally important:

1. **Utility** — a fast, keyboard-driven way to reach the config files of common
   network services, control those services (start/stop/reload/enable), and back
   up / restore configuration files so a mistaken edit can be undone.
2. **Didactic** — a guide for *learning* how network services are configured.
   Each service ships with a short, friendly explanation (in the style of the
   Ubuntu help pages) of what it does and what each of its config files is for.

All user-facing text and all default data files are in **English**.

## 2. Target platforms

LazyServer must run on:

- **Ubuntu Server** (apt, systemd)
- **Arch Linux** (pacman, systemd)

and must be designed so that support for **Fedora** (dnf) and **openSUSE**
(zypper) can be added later **without changing application code** — only by
adding data. The application is Linux-only.

Cross-distro differences (package names, config paths, service unit names,
install commands) live in **data**, never hard-coded in the program. See the
`distros` mechanism in `tconf-schema.md`.

## 3. Core concepts and vocabulary

| Term | Meaning |
|------|---------|
| **LazyServer** | The application. The command/binary is `lazyserver` (alias `lsrv`). |
| **tconf** | The data format and the folder of YAML files LazyServer reads. "A tconf file" = one service or application definition. |
| **Entry** | A single service or application defined by one tconf YAML file. |
| **Service** | A network daemon (e.g. bind9, nginx). Lives under `tconf/services/`. |
| **Application** | A program with config files but not a network daemon (e.g. neovim). Lives under `tconf/apps/`. |
| **Managed file** | A config file that an entry declares LazyServer can edit/back up. |
| **Backup store** | A user-configured folder where backups are kept (git-backed). |
| **Distro profile** | The per-distribution block inside an entry (package, paths, unit name, commands). |

**Services** and **Applications** are presented in **separate sections** of the
interface, but they share the same file format and the same backup/restore
machinery.

## 4. Functional requirements

### FR-1 Service & application configuration access

- **FR-1.1** LazyServer shows a list of entries (services in one section,
  applications in another).
- **FR-1.2** Selecting an entry shows its managed config files, each with its
  short description and example.
- **FR-1.3** Pressing **Enter** on a config file opens it in the user's chosen
  editor (see FR-7 settings). On return, LazyServer re-checks the file (FR-2.1).
- **FR-1.4** All per-entry data (how to install, the managed files with example
  and description, and the control commands) comes from that entry's tconf file.
- **FR-1.5** Each entry exposes service-control actions where applicable:
  **start, stop, restart, reload, enable, (disable), status**. These map to
  commands defined per distro in the tconf file — LazyServer does not assume
  systemd command syntax in code.
- **FR-1.6 Managed file sets.** Besides fixed config files, an entry may declare
  **file sets**: user-created config files identified by a directory + glob
  pattern (e.g. BIND zone files `/etc/bind/db.*`, nginx `sites-available/*`).
  LazyServer expands the pattern when it scans, backs up, or restores, so files
  the user creates *after* the entry was defined are covered automatically. File
  sets participate in modification detection, backup, and restore exactly like
  fixed files.
- **FR-1.7 Create a file in a set.** From a file set (or a declared-but-missing
  fixed file), the user can create a new file from inside LazyServer: LazyServer
  shows a permission preview (FR-1.9), prompts for the file name, creates the
  file in the set's resolved directory **pre-filled with the set's `example`**
  (used as a starter template), and opens it in the editor. LazyServer refuses
  to overwrite an existing file of the same name (it offers to open it instead).
- **FR-1.8 Correct ownership on create.** A file created via FR-1.7 is given the
  correct owner, group, and mode. Resolution depends on the entry `kind`:
  - **`kind: app`** — config lives under the **target user's** home (see FR-1.10);
    the file is created owned by the target user and group, and `~` expands to
    that user's home.
  - **`kind: service`** — config lives in system paths; ownership is resolved in
    this order:
    1. explicit `owner`/`group`/`mode` on the file set, if present;
    2. otherwise, copied from an existing sibling file in the set;
    3. otherwise, the set directory's owner/group with a default mode (`0640`);
    4. last resort, `root:root` **with a warning** that the service may be unable
       to read the file.
  This exists to teach correct practice: students working in `sudo` otherwise
  create root-owned configs that the service (running as e.g. `bind`) cannot
  read, or app configs owned by `root` instead of themselves.
- **FR-1.9 Permission preview.** Before the user types the file name, LazyServer
  resolves the owner/group/mode that *will* be applied (per FR-1.8) and displays
  it directly above the name input, phrased to explain *why* (the service or the
  user needs to read/own the file). When a service falls back to `root:root`,
  the preview is shown as an explicit warning rather than reassurance.
- **FR-1.10 Target user.** At startup LazyServer resolves a single **target
  user** — the real human using it, not `root`. It reads `$SUDO_USER`; if unset
  (LazyServer was started as real root, not via `sudo`), it falls back to
  `$USER`, and a setting may override it. The target user's name and home
  directory are stored for the session and used for: expanding `~` in app file
  paths, and owning app files created or restored (FR-1.8). Service files are
  unaffected — they use system ownership.

### FR-2 Backup

- **FR-2.1 Modification detection.** LazyServer detects that a managed file
  changed by comparing a **content checksum** (e.g. SHA-256) against the checksum
  stored at the last backup. Detection does **not** rely on mtime and is **not**
  limited to files edited through LazyServer — any change to a managed file is
  detected.
- **FR-2.2 Pending list.** Files whose current checksum differs from the
  last-backed-up checksum are "pending backup." The pending set **accumulates
  across sessions** and is only cleared for a file when that file is backed up.
- **FR-2.3 Backup scopes.** The user can back up:
  - all pending files at once,
  - one or more individually selected files,
  - all files of one or more selected entries.
- **FR-2.4** A backup writes the current content of the selected files into the
  backup store and records their checksums as the new baseline, removing them
  from the pending list.
- **FR-2.5 git-optional backup store.** Backup and restore must work whether or
  not `git` is installed. LazyServer detects `git` at startup: when present, the
  backup store is a git repository and each backup is a commit, giving version
  history and diffs; when absent, the store falls back to plain timestamped
  copies. The core promise (backup and restore) holds either way — git is an
  enhancement, not a requirement. (This matters for disaster recovery on a
  freshly installed, minimal OS where git may not yet be present.)

### FR-3 Restore

- **FR-3.1 Restore scopes.** The user can restore:
  - a single file,
  - all files of an entry,
  - the whole system (every managed file that has a backup).
- **FR-3.2 Safety backup.** Before overwriting any live file, LazyServer first
  backs up the current on-disk version (a "pre-restore" snapshot), so a restore
  is itself reversible.
- **FR-3.3** Restore copies the chosen version from the backup store back to the
  file's real location with correct ownership/permissions.
- **FR-3.4 No-delete default.** For file sets, restore writes back every file the
  backup holds (including files no longer on disk), but by default **never
  deletes** a live file that is absent from the backup. It reports such "extra"
  files instead. Mirror/delete may be offered later as an explicit, confirmed
  option.

### FR-4 Didactic content

- **FR-4.1** LazyServer is oriented to learning network services: DHCP, DNS,
  FTP, proxy, mail, HTTP server, etc.
- **FR-4.2 Pre-loaded entries.** The following ship as tconf files out of the box:
  `isc-dhcp-server`, `bind9`, `vsftpd`, `Squid`, `nginx`, `Apache (httpd)`,
  `Postfix`, `Dovecot`.
- **FR-4.3** Each entry carries: a short entry-level description of the service,
  and for **each managed file** a brief explanation of what the file is for plus
  a representative example. Depth target: a short paragraph per file, Ubuntu
  help-page style. **No line-by-line commentary.**
- **FR-4.4** If a service/app is not present, the user creates its tconf file by
  hand. The schema and an **AI prompt template** to generate a correct file are
  provided in `tconf-schema.md`.

### FR-5 Applications

- **FR-5.1** Applications (e.g. neovim) are entries too, with install commands
  and managed config files, and participate in backup/restore.
- **FR-5.2** The user can record, in a tconf file: how to install the entry, its
  managed config files, and thus make it eligible for backup.
- **FR-5.3 Full recovery.** On a freshly reinstalled OS, after installing
  LazyServer and providing the tconf folder + backup store, the user can run a
  **full recovery**: install everything and restore its configuration.
  - **FR-5.3.1** Recovery order per entry: **install package → restore config →
    enable/start** (where applicable).
  - **FR-5.3.2** Recovery produces a **human-readable log** of everything done,
    including errors.
  - **FR-5.3.3** Recovery produces a **machine-readable summary** (JSON) listing
    which entries succeeded and which failed.
  - **FR-5.3.4** Recovery can run **interactively** (entry by entry inside the
    TUI) or **non-interactively** (a `lazyserver recover --all` command suitable
    for scripting).
- **FR-5.4** tconf files and the backup store may live in a Git repository
  (e.g. on GitHub), so a user can clone them onto the new machine.

### FR-6 Language

- **FR-6.1** All UI text and all default tconf files are in English.

### FR-7 Settings

- **FR-7.1** The user configures: the **editor** to use, the **backup store**
  folder location, and the **tconf folder** location(s).
- **FR-7.2** Settings persist between runs in a user config file.

## 5. Out of scope (this version)

- **Vagrant + Ansible export** (generate an Ansible playbook from tconf data,
  Vagrant for testing). Deferred to **v2.0** — see `roadmap.md`.
- Windows / non-Linux support.
- A graphical interface or web interface.
- Remote-agent architecture (LazyServer is local to the server it manages).

## 6. Non-functional requirements

- **NFR-1 No code changes for new distros or services.** Adding a distro or an
  entry is a data-only operation.
- **NFR-2 Safe by default.** Destructive actions (restore, recovery,
  service control) confirm before acting; restore always takes a pre-restore
  snapshot (FR-3.2).
- **NFR-3 Privilege.** Editing system config and controlling services needs
  root. LazyServer is expected to run via `sudo`/as root; it must fail with a
  clear message if it lacks the privileges an action needs, rather than
  half-completing it.
- **NFR-4 Minimal dependencies.** Prefer the Python standard library and a
  single TUI toolkit; keep install over SSH simple.
- **NFR-5 Clear errors.** Every failed action explains what failed and why.

## 7. Primary user journeys

1. **Fix a config and undo a mistake.** Open nginx → edit `nginx.conf` →
   reload → site breaks → restore previous version (pre-restore snapshot is
   taken automatically) → reload → fixed.
2. **Learn a service.** Open bind9 → read the entry description → open each
   managed file to read what it's for and see an example.
3. **Routine backup.** Over several sessions, edit several files → open the
   pending-backup list → back up all pending → list clears.
4. **Disaster recovery.** New OS → install LazyServer → clone tconf + backups
   → `lazyserver recover --all` → read the log and the failed-items summary →
   manually handle the few failures.

## 8. Resolved decisions

- **Data-folder name:** kept as `tconf`.
- **Binary:** `lazyserver`, with short alias `lsrv` (since `ls` is taken).
- **`disable` action:** included alongside `enable` (FR-1.5).
- **Target user:** resolved from `$SUDO_USER` (fallback `$USER`, settings
  override) — FR-1.10.
- **App vs service ownership:** app files belong to the target user; service
  files use system-ownership resolution — FR-1.8.
- **Backup store:** git when available, plain timestamped copies otherwise —
  FR-2.5.

No open questions remain blocking implementation.
