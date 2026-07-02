# LazyServer — Installation

A practical install guide, ordered so you hit the requirements before the
install steps and the fixes before the frustration.

> LazyServer is a terminal application meant to run **on the server** you
> want to configure (typically a VM), reached over SSH. It runs as
> **root** and stores backups under a normal user's home. See
> `docs/spec.md` §9 for the deployment context.

---

## 1. Before you install

Check every item below before running the install line. Skipping this
section is the single biggest source of "why doesn't it work" — the
tools LazyServer is built on have specific versions and paths that
are not the default on every distribution.

### Supported systems

| Distro           | Package manager | Init      | Status              |
|------------------|-----------------|-----------|---------------------|
| Ubuntu Server    | apt             | systemd   | supported           |
| Arch Linux       | pacman          | systemd   | supported           |
| Debian           | apt             | systemd   | inferred (via ID_LIKE) — usually works, flagged in the footer |
| Manjaro          | pacman          | systemd   | inferred (via ID_LIKE) |
| Fedora, openSUSE | dnf, zypper     | systemd   | not yet — data-only work to add |

Linux only; there is no macOS or Windows build.

### Python 3.11 or newer — **check this first**

LazyServer needs Python **3.11+**. The most common install failure is
running it against Python 3.10.

```bash
python3 --version
```

- **Ubuntu 22.04 LTS** ships **Python 3.10** by default. LazyServer will
  fail to install or start. Install a newer Python:

  ```bash
  sudo add-apt-repository ppa:deadsnakes/ppa
  sudo apt update
  sudo apt install python3.12 python3.12-venv
  ```

  Then use `python3.12` in the install commands below instead of `python3`.

- **Ubuntu 24.04** ships Python 3.12: no extra step.
- **Arch / Manjaro**: rolling release, ships current Python: no extra step.
- **Debian 12 (bookworm)**: ships Python 3.11: no extra step.

### `git` — recommended, not required

If `git` is installed, LazyServer automatically uses it for the backup
store and you get commit history, diffs, and one-command offsite push
(see the User Guide). Without git, backups fall back to plain timestamped
copies — everything still works, you just lose the history layer.

```bash
sudo apt install git      # Debian/Ubuntu
sudo pacman -S git        # Arch
```

### Root access

LazyServer edits `/etc/...` config and controls services, so it
**requires root at startup** (spec NFR-3). You need `sudo` on the
target machine. Running without root exits immediately with:

```
lazyserver: lazyserver must run as root. Re-run with `sudo lazyserver` (or `sudo lsrv`).
```

This is intentional — a half-privileged session that discovers it can't
`chown` a file halfway through a restore is worse than a clean refusal
at startup.

---

## 2. Recommended install: `pipx`

**Use pipx.** It installs LazyServer into an isolated virtual
environment, keeps it off the system Python, and gives you one-command
upgrade and uninstall. It also side-steps the Debian/Ubuntu pygments
conflict described in §3.

### Install pipx

```bash
# Debian / Ubuntu
sudo apt install pipx
pipx ensurepath

# Arch / Manjaro
sudo pacman -S python-pipx
pipx ensurepath
```

`pipx ensurepath` adds `~/.local/bin` to your `PATH`. **Open a new
shell** (or `source ~/.bashrc`) so the change takes effect before the
next step.

### Install LazyServer

```bash
pipx install git+https://github.com/Joan-mh/Lazyserver.git
```

If your system Python is older than 3.11, tell pipx which Python to
use for the venv:

```bash
pipx install --python python3.12 git+https://github.com/Joan-mh/Lazyserver.git
```

### Verify

```bash
lazyserver --version
lsrv --version          # short alias, same binary
```

Both should print the same version.

---

## 3. Alternative: `pip` (when pipx is not an option)

Use this only if pipx is unavailable — for example, on a minimal server
where you can't add packages.

### The pygments/Debian conflict

Plain `pip install` on recent Debian and Ubuntu fails on transitive
dependencies. The current wording (pip 24.x on Debian 12 / Ubuntu
24.04) is:

```
ERROR: Cannot uninstall Pygments 2.17.2, RECORD file not found.
Hint: The package was installed by debian.
```

Older pip versions phrased the same conflict as
`error: uninstall-distutils-installed-package  Cannot uninstall
'Pygments'. It is a distutils installed project`. Either way, the
cause is the same: `Pygments` is installed by the OS package manager
as a system Python package; pip refuses to touch it.

Two workarounds:

```bash
# Preferred: tell pip to ignore the system record and lay down its own copy.
sudo pip install --ignore-installed git+https://github.com/Joan-mh/Lazyserver.git

# Or, install into your user site (does not need --ignore-installed on
# every distro; still needs the Python 3.11+ pin).
pip install --user git+https://github.com/Joan-mh/Lazyserver.git
```

`--user` puts the entry point at `~/.local/bin/lazyserver`. Make sure
`~/.local/bin` is on your `PATH`. Note: if you later switch to pipx,
this user-site install can shadow the pipx one — see the
[stale-binary entry](#stale-lazyserver-binary-shadowing-pipx) in §7.

**Downsides vs pipx:** no isolated venv, less clean to uninstall, more
likely to collide with a future system-package update. Move to pipx
when you can.

---

## 4. First run

```bash
sudo lazyserver
```

What happens:

1. The root check passes.
2. The **target user** is resolved from `$SUDO_USER` (that's you, not
   root). All config and backups end up owned by this user.
3. Distro is detected from `/etc/os-release`. The footer shows
   `distro: ubuntu` (or `arch`, etc.). If your distro was inferred via
   `ID_LIKE` (Debian → ubuntu, Manjaro → arch), the footer says
   `⚠ inferred: ...`. Package names and paths are usually right but
   not guaranteed — worth reading before you `1` a service.
4. Settings are loaded from
   `~/.config/lazyserver/config.toml` under your (target-user) home.
   Missing file → sensible defaults (nano/vi editor, no backup store yet).
5. The Home screen appears with **Services** on top and **Apps**
   below. Arrow keys to move, `Enter` to open.

Now read `docs/USER-GUIDE.md` for what to do next — the first thing you
probably want is to set `backup_store` in the settings file.

---

## 5. Upgrading

```bash
# pipx
pipx upgrade lazyserver

# pip
sudo pip install --ignore-installed --upgrade git+https://github.com/Joan-mh/Lazyserver.git
```

Neither touches your `~/.config/lazyserver/config.toml`, your local
tconf overrides, or your backup store — those are yours.

---

## 6. Uninstalling

```bash
# pipx
pipx uninstall lazyserver

# pip
sudo pip uninstall lazyserver
```

To fully remove state as well:

```bash
rm -rf ~/.config/lazyserver
# and, if you no longer want the backup history:
rm -rf ~/lsrvbck        # or wherever you pointed backup_store
```

---

## 7. Troubleshooting

Each entry: the error you see, then the fix.

### `lazyserver: must run as root`

You forgot `sudo`. Run:

```bash
sudo lazyserver
```

### `Error opening terminal: xterm-kitty` (or `xterm-ghostty`, etc.)

LazyServer (via Textual, via ncurses) needs a terminfo entry for the
terminal you're using. Modern terminals (kitty, ghostty, WezTerm,
Alacritty) advertise a `TERM` value your server may not have installed.

Two fixes, cheapest first:

```bash
# Override TERM just for this run — -E preserves it through sudo.
TERM=xterm-256color sudo -E lazyserver
```

Or install the missing terminfo database on the server:

```bash
sudo apt install ncurses-term          # Debian/Ubuntu
sudo pacman -S ncurses                 # Arch (usually already present)
```

Or SSH from a standard terminal (GNOME Terminal, Konsole, xterm).
`TERM=xterm-256color` is a good default that works everywhere.

### `command not found: lazyserver` after installing

`~/.local/bin` isn't on your `PATH`. Re-run `pipx ensurepath` and open
a new shell:

```bash
pipx ensurepath
exec $SHELL -l
```

If you installed with `pip --user`, add `~/.local/bin` to your
`PATH` in `~/.bashrc` or `~/.zshrc`.

### `python3.11: command not found` on Ubuntu 22.04

Ubuntu 22.04 ships Python 3.10. See §1 — install `python3.12` from the
deadsnakes PPA and pass `--python python3.12` to pipx.

### `Cannot uninstall Pygments … RECORD file not found` (or `distutils installed project`)

The Debian/Ubuntu pip conflict — same cause under two different pip
wordings. Use pipx (§2), or add `--ignore-installed` (§3).

### Stale `lazyserver` binary shadowing pipx

If you previously did `sudo pip install lazyserver` (or
`pip install --user …`, see §3) and then switched to pipx, the old
entry point may still be at `/usr/local/bin/lazyserver` or
`~/.local/bin/lazyserver` and shadow the pipx one. Check with:

```bash
which -a lazyserver
```

Remove the stale one:

```bash
# sudo pip install (system-wide):
sudo pip uninstall lazyserver
# or, worst case:
sudo rm /usr/local/bin/lazyserver /usr/local/bin/lsrv

# pip install --user (in your home):
pip uninstall lazyserver
# or:
rm ~/.local/bin/lazyserver ~/.local/bin/lsrv
```

### `sudo lazyserver` starts as root but writes files as root

It shouldn't — LazyServer resolves the target user from `$SUDO_USER`
and chowns config, settings, and backup store to that user. If you
launched as **real** root (`su -`, not `sudo`), `$SUDO_USER` is unset
and LazyServer falls back to `$USER` (still root). Two fixes:

```bash
# Preferred: use sudo instead of su.
sudo lazyserver

# Or, if you must launch from a root shell, set the target user in settings:
# ~/.config/lazyserver/config.toml
target_user = "yourname"
```

### TUI does not receive `Ctrl+C` / weird key remapping

Textual reads keys through the terminal. Some multiplexers (screen,
older tmux) or non-standard terminals eat keys. Try a plain SSH
session in GNOME Terminal / Konsole with `TERM=xterm-256color`.
