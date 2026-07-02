# LazyServer — User Guide

A practical guide for using LazyServer. Written for everyday users, not
developers. (Developer/design docs live in `spec.md`, `architecture.md`,
and `roadmap.md`.)

For installing LazyServer in the first place, see
[`INSTALL.md`](INSTALL.md).

---

## Table of contents

1. [Running LazyServer](#running-lazyserver)
2. [Settings](#settings)
3. [Browsing and reading](#browsing-and-reading)
4. [Editing a config file](#editing-a-config-file)
5. [Creating a new file in a file set](#creating-a-new-file-in-a-file-set)
6. [Controlling a service](#controlling-a-service)
7. [Backups](#backups)
8. [Backups: keeping them safe off the machine](#backups-keeping-them-safe-off-the-machine)
9. [Restoring a file, an entry, or everything](#restoring-a-file-an-entry-or-everything)
10. [Full recovery on a fresh machine](#full-recovery-on-a-fresh-machine)
11. [Dry-run mode](#dry-run-mode)
12. [Working over SSH](#working-over-ssh)

---

## Running LazyServer

```bash
sudo lazyserver          # or the short alias:
sudo lsrv
```

LazyServer **requires root** and exits immediately if you forget
`sudo`. The reason is that editing `/etc/...` and controlling services
both need it; a half-privileged session that discovers this halfway
through a restore is worse than a clean refusal at startup.

Even though it runs as root, LazyServer resolves a **target user** —
you, the actual person — from `$SUDO_USER`. Your settings, tconf
overrides, and backup store are all owned by that user, not by root.
This matters when you come back tomorrow and want to `git push` your
backups: they're yours, no `sudo` needed.

**Global keys** available on every screen:

| Key       | Action                                            |
|-----------|---------------------------------------------------|
| `q`       | Quit                                              |
| `Esc`     | Back (or quit from the home screen)               |
| `b`       | Open the Backup screen                            |
| `R`       | Open the Recovery screen (whole-system recovery)  |
| `d`       | Toggle dry-run mode on/off                        |

The status line at the bottom shows the current distro, target user,
entry counts, and a `DRY-RUN` marker when dry-run is on.

---

## Settings

Settings live in a TOML file at:

```
~/.config/lazyserver/config.toml       (target user's home)
```

Even when LazyServer runs under `sudo`, this file stays under **your**
home directory and stays owned by you. There is no separate root-owned
config.

A first launch creates the file with defaults on first save. You can
also write it by hand:

```toml
# ~/.config/lazyserver/config.toml
editor = "nvim"
backup_store = "~/lsrvbck"
tconf_paths = ["~/tconf-overrides"]
# target_user = "joan"     # optional; override the $SUDO_USER default
```

### What each setting does

- **`editor`** — the program launched by Enter on a config file.
  If unset, LazyServer picks the first of `$VISUAL`, `$EDITOR`,
  `nano`, `vi` that exists. `nano` comes before `vi` so a beginner
  is not trapped in a modal editor on their first run.

- **`backup_store`** — the folder where backups are written. A leading
  `~` expands to the target user's home, so `~/lsrvbck` is
  `/home/joan/lsrvbck` when you're `joan`. The folder is created as
  the target user; when `git` is installed, it's initialised as a git
  repository and every backup becomes a commit.

- **`tconf_paths`** — a list of your own folders of tconf YAML files.
  LazyServer always loads its **bundled** tconf first (the eight
  services + neovim), then each of these folders in order. Duplicate
  entry ids: **the last folder wins**, so you can override a shipped
  entry by putting a file with the same `id` in one of your folders.

- **`target_user`** — override the `$SUDO_USER` auto-detection. Only
  needed if you launch LazyServer from a root shell (`su -`) instead
  of via `sudo`, because `$SUDO_USER` is not set in that case.

### Notes on `$XDG_CONFIG_HOME`

A custom `$XDG_CONFIG_HOME` set in your shell is **not honoured**
under `sudo`: a root process cannot read your env safely, so
LazyServer always uses `~target/.config/`. This is documented in the
spec (FR-7.2), not a bug.

---

## Browsing and reading

The **Home screen** shows two sections: **Services** on top,
**Apps** below. Arrow keys move; Enter opens.

Each **Entry screen** shows:

- the entry's short description (Ubuntu-help-page style),
- the resolved **package name** and **service unit** for your distro,
- the **managed files** — either fixed paths or file sets (glob
  patterns), each with a one-paragraph description and a short
  example snippet,
- any **alias notes** — e.g. "detected Debian; resolved as ubuntu",
- an **Actions** line for services: `1 start · 2 stop · 3 restart ·
  4 reload · 5 enable · 6 disable · 7 status`.

Reading is the point: the descriptions and per-file examples are
written to teach, not to replace man pages. Open bind9 for a first
walkthrough — it's the most thoroughly documented shipped entry.

---

## Editing a config file

On the Entry screen, arrow-key onto a managed file and press
**Enter**. LazyServer:

1. Captures the file's current owner, group, and mode (a `stat()`).
2. Suspends the TUI and launches your chosen editor on the file.
3. When the editor exits, re-applies the original owner/group/mode.
4. Recomputes the SHA-256 checksum and, if the content changed,
   adds the file to the **pending backup** list.

Step 3 is why LazyServer preserves `bind:bind` ownership on
`/etc/named.conf` even though you're running as root: editors that
save via "write to temp, rename over target" would otherwise silently
strip the original ownership.

The file is added to pending **as soon as it changes**, regardless of
whether you edited it through LazyServer or elsewhere — detection
compares content checksums, not timestamps.

---

## Creating a new file in a file set

Some services expect **user-created** files: BIND zone files under
`/etc/bind/db.*`, nginx `sites-available/*`, drop-ins in `conf.d/`.
The tconf calls these **file sets**.

On the Entry screen, focus a file-set row and press **`n`**. The
create screen:

1. Shows a **permission preview** — the exact `owner:group  mode`
   the new file will get, and *why* (e.g. "copied from a sibling
   file in this set", "the service needs to read this file"). If
   the fall-back is `root:root`, it's shown as an explicit warning:
   the service running as another user won't be able to read the
   file.
2. Asks for a filename. Refuses to overwrite an existing file —
   offers to open it instead.
3. Creates the file with the correct owner/group/mode, pre-filled
   from the set's `example` block (a starter template, not a
   real-config guess).
4. Opens it in your editor.

The permission preview is the whole point of this flow: students
working under `sudo` otherwise create root-owned configs that the
service can't read, then wonder why nothing works.

---

## Controlling a service

On a **service** Entry screen the number keys fire actions with **no
confirmation prompt**:

| Key | Action  |
|-----|---------|
| `1` | start   |
| `2` | stop    |
| `3` | restart |
| `4` | reload  |
| `5` | enable  |
| `6` | disable |
| `7` | status  |

The absence of an "are you sure?" prompt is deliberate (spec §9): in
the classroom-VM deployment, breaking things fast and reverting a
snapshot is the pedagogical loop. If you deploy LazyServer outside
that context (production, remote-only, no console access), you should
reconsider — see spec NFR-2 and §9.

The action runs via a subprocess. Output is captured and displayed in
a modal when there is anything noteworthy (non-zero exit, non-empty
stderr, or the `status` action). Successful `start`/`stop`/`restart`
etc. usually just update the status line.

**`disable` and `stop` are not confirmed.** In a production
deployment that would be alarming; in the classroom-VM deployment it's
the whole point. Snapshot before the lesson if you want a hard undo.

---

## Backups

Backups are how you get undo. LazyServer detects a change to any
managed file by comparing its **content SHA-256** against a stored
baseline — mtime is not consulted, and it doesn't matter whether the
change went through LazyServer or through a raw `vim`.

### The pending list

A changed file joins the **pending** set. Pending accumulates across
sessions: it's only cleared for a file when that file is backed up.

Press `b` from anywhere to open the **Backup** screen. You see one row
per pending file with its entry, its status (`modified`, `missing`,
`new`), and a checkbox.

| Key   | Action                       |
| -------| ------------------------------|
| Space | Toggle the focused file      |
| `a`   | Select all                   |
| `n`   | Clear selection              |
| `b`   | Back up the selected files   |
| `B`   | Back up everything pending   |
| `r`   | Rescan (recompute checksums) |
| Esc   | Back                         |

A backup writes the current content into the backup store and updates
the baseline checksum. Those files leave the pending list.

### From the command line

For scripts and cron jobs:

```bash
sudo lsrv backup --list                        # show pending, write nothing
sudo lsrv backup --all                         # back up everything pending
sudo lsrv backup --entry bind9 nginx           # back up these entries only
sudo lsrv --dry-run backup --all               # preview, don't touch the store
```

Exit codes: `0` ok · `1` hard error · `2` some snapshots failed.

### The backup store

When `git` is installed, the store is a **git repository** and every
backup is a commit. You get history, diffs, and a natural way to push
offsite. When git is absent, the store falls back to **plain
timestamped copies** — everything still works, but you don't get the
history layer.

**The store folder is owned by the target user, not root.** That's
what makes offsite copying frictionless — see the next section.

---

## Backups: keeping them safe off the machine

LazyServer stores your backups in a folder **on this same machine** (the
one you set as `backup_store`). That's deliberate and simple — but it
means if the machine itself is lost, wiped, or reinstalled, the backups
stored on it are lost too.

So for backups that survive the machine, you copy the backup folder
somewhere else. LazyServer makes this easy: **the backup folder belongs
to you** (your normal user, not root), so you can move it with ordinary
tools — no special permissions, no `sudo`.

Here are the common ways, simplest first.

### Copy it to another machine or a USB drive

Your backup folder is just files. Copy it like any other folder:

```bash
# to a USB drive mounted at /mnt/usb
cp -r ~/lsrvbck /mnt/usb/
```

```bash
# Remember, the backup folder is configured by you in the config file at ~/.config/lazyserver/config.toml
# on your HOST, pull the store off the VM before you roll back:
rsync -av user@<vm-ip>:~/lsrvbck ~/lazyserver-backup-safe/

# on your HOST, push the store to the recovering VM:
rsync -av ~/lazyserver-backup-safe/lsrvbck user@<vm-ip>:~/
```

### Push it to GitHub (recommended for recovery)

If LazyServer used **git** for your backups (it does automatically when
`git` is installed), your backup folder is already a git repository. You
can push it to GitHub or any git host:

```bash
cd ~/lsrvbck
git remote add origin git@github.com:youruser/my-server-backups.git
git push -u origin master
```

After that, each time you back up you just `git push` again to send the
latest snapshots offsite. This is the recommended approach, because it's
also how you **recover onto a fresh machine**: install LazyServer,
`git clone` your backups back, and restore.

### Store it on a network drive

If you point `backup_store` at a folder on a mounted network drive (NFS,
Samba, etc.), backups land there directly — your operating system
handles the network, and LazyServer doesn't need to know the difference.

```toml
# in ~/.config/lazyserver/config.toml
backup_store = "/mnt/nas/lazyserver-backups"
```

---

**In short:** LazyServer keeps backups local and simple; *moving them
off the machine is your choice of `cp`, `rsync`, `git push`, or a
network drive.* The folder is yours to move however suits you.

---

## Restoring a file, an entry, or everything

Restore is undo. Every restore is **itself reversible**: LazyServer
takes an automatic **pre-restore snapshot** of the current on-disk
state before it overwrites anything (spec FR-3.2). That snapshot is
your one-line escape hatch if you restored the wrong version.

### From the TUI

- From the **Home screen** or **Entry screen**, press `r` with an
  entry highlighted to open the restore-snapshots picker for that
  entry.
- The picker lists every snapshot in the store for that entry,
  newest first, with the timestamp and the changed files. Enter
  opens it.
- From the snapshot view: **space** to toggle files, **a** select
  all, **n** clear, **r** restore selected, **R** restore all in
  this snapshot. Esc backs out.

### From the command line

```bash
# One file, latest snapshot for its owning entry.
sudo lsrv restore --file /etc/bind/named.conf.options

# Everything for one entry, at the latest snapshot.
sudo lsrv restore --entry bind9

# Every entry, latest snapshot each. Whole-system restore.
sudo lsrv restore --all

# Pin a specific snapshot (not valid with --all).
sudo lsrv restore --entry bind9 --snapshot 2026-06-30-142530

# Preview only.
sudo lsrv --dry-run restore --entry bind9
```

Exit codes match backup: `0` / `1` / `2`.

**Undoing a restore.** Before overwriting anything, LazyServer prints
the pre-restore timestamp and the exact command to reverse the
operation, e.g.:

```
Pre-restore snapshot: 2026-07-02-091522-pre-restore
Undo with: sudo lsrv restore --entry bind9 --snapshot 2026-07-02-091522-pre-restore
```

### File sets and the no-delete rule

For file sets (BIND zones, nginx sites), restore writes back every
file the backup holds, **including files no longer on disk**. It
**never deletes** a live file that is absent from the backup — it
reports such "extras" and leaves them alone. Mirror-with-delete is
not offered in v1.0.

---

## Full recovery on a fresh machine

The disaster-recovery flow: fresh OS, no configs, LazyServer just
installed. You want the machine to come back.

### Prerequisites

1. LazyServer installed (see `INSTALL.md`).
2. Your **backup store** cloned onto the new machine, and
   `backup_store` in settings pointing at it.
3. Your **tconf overrides** (if any) cloned onto the new machine,
   and `tconf_paths` pointing at them.

Both are typically the same GitHub repos you pushed from your
previous machine.

### The command

```bash
sudo lsrv recover --all
```

For each entry, in a stable order, LazyServer will:

1. **Install** the entry's package (via `apt`, `pacman`, etc.,
   derived from the entry's distro block).
2. **Restore** its latest deliberate backup (skipping any
   pre-restore snapshots).
3. **Enable and start** the service (for services).

You see the log stream live to your terminal. When it finishes, two
artifacts are written under `<store>/recovery/`:

- `recovery-YYYYMMDD-HHMMSS.log` — human-readable
- `recovery-YYYYMMDD-HHMMSS.json` — machine-readable, per-entry
  status (`ok` / `partial` / `failed` / `skipped`)

Exit codes: `0` every entry ok · `2` some entries partial or failed
(script on this) · `1` hard error before the run even started.

Entries with **zero backups** are skipped — LazyServer does not
install a service just to leave it with an empty config file. This
matches how you actually built the box: services you never touched
during normal use aren't part of your recovery.

### From the TUI

Press `R` (capital) on any screen to open the Recover screen. It
gives you the same plan step-by-step, with the option to recover a
single entry (`r`) or all of them (`R`). The artifacts are written
either way.

---

## Dry-run mode

Every write operation supports dry-run. From the CLI:

```bash
sudo lsrv --dry-run backup --all
sudo lsrv --dry-run restore --entry bind9
sudo lsrv --dry-run recover --all
```

From the TUI, press **`d`** to toggle. The status line grows a
`DRY-RUN` marker; actions still run through the plan but write
nothing (no snapshots, no restored files, no `systemctl` invocations
— the runner prints what it *would* have done).

Dry-run is the safest way to try recovery on a machine you don't
want to touch yet. It also produces the recovery artifacts, marked
`"dry_run": true` in the JSON, so you can pre-review the plan.

---

## Working over SSH

LazyServer is designed to run on the server, reached over SSH:

```bash
ssh joan@server
sudo lsrv
```

Some notes:

- **Set `TERM` sanely.** Modern terminals (kitty, WezTerm, Alacritty)
  advertise `TERM` values a minimal server won't have installed and
  the TUI will refuse to start. See the terminfo entry in
  `INSTALL.md` §7 for the fix (`TERM=xterm-256color sudo -E lsrv`).
- **`-E` on sudo** preserves `TERM` through the privilege change.
  Without it, you get root's `TERM`, which may not match.
- **Console access is the ultimate escape.** If you stop `sshd` from
  inside LazyServer, you're locked out of SSH — that's the point of
  the classroom-VM deployment context. Have a snapshot, or don't do
  that on a remote-only production box.
