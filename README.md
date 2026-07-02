# LazyServer

A terminal (TUI) tool that helps you **configure, back up, restore, and
learn about** Linux network services and applications — reached over
SSH, running on the server itself, no separate client.

Built for classroom Linux VMs (Ubuntu Server + Arch), where the goal is
"try, break, undo, learn" and every mistake is one snapshot away from
recoverable.

## What it does

- **Browse** every managed service (bind9, nginx, Apache, Postfix,
  Dovecot, Squid, vsftpd, isc-dhcp-server, …) and app (neovim). Each
  ships with a short, Ubuntu-help-page-style description and a
  per-file explanation of what each config file is for.
- **Edit** any config file in your editor of choice (`$VISUAL`,
  `$EDITOR`, or nano/vi). Ownership and mode survive the edit — a
  root-run session leaves `/etc/named.conf` owned by `bind:bind`, not
  by root.
- **Control** services: start / stop / restart / reload / enable /
  disable / status, driven by tconf data — no init-system code inside
  LazyServer.
- **Back up** managed files with automatic change detection (SHA-256,
  not mtime). Backups are commits in a git repo you can push offsite.
- **Restore** by file, entry, or whole system — with an automatic
  pre-restore snapshot so a bad restore is itself undoable.
- **Recover** a wiped machine: clone your backups repo, run
  `sudo lsrv recover --all`, and the box rebuilds itself
  (install package → restore config → enable service), producing a
  human log and a JSON summary of what worked and what didn't.

## Install

```bash
# 1. Install pipx if you don't already have it.
sudo apt install pipx && pipx ensurepath        # Debian/Ubuntu
sudo pacman -S python-pipx && pipx ensurepath   # Arch

# 2. Open a new shell so the PATH change takes effect, then:
pipx install git+https://github.com/Joan-mh/Lazyserver.git

# 3. Verify.
lazyserver --version
```

Then:

```bash
sudo lazyserver
```

Full guide, requirements, and troubleshooting — including the
**Python 3.11+** requirement (Ubuntu 22.04 ships 3.10), the pygments
pip conflict, and the `Error opening terminal` fix — are in
[`docs/INSTALL.md`](docs/INSTALL.md).

## Learn the tool

- [`docs/USER-GUIDE.md`](docs/USER-GUIDE.md) — how to use it: settings,
  editing, controlling services, backups, restores, recovery.
- [`docs/ADDING-ENTRIES.md`](docs/ADDING-ENTRIES.md) — write a tconf
  file for a service or app that doesn't ship out of the box (using
  the AI prompt template).

## Design and specification

LazyServer is built spec-first. If you want to understand *why* it
behaves the way it does, read these in order:

1. [`docs/spec.md`](docs/spec.md) — the contract (what it must do).
2. [`docs/tconf-schema.md`](docs/tconf-schema.md) — the YAML data
   format that drives everything.
3. [`docs/architecture.md`](docs/architecture.md) — modules and tech
   choices.
4. [`docs/roadmap.md`](docs/roadmap.md) — the phased build plan.

Contributor guidance for AI coding agents lives in
[`AGENTS.md`](AGENTS.md).

## Status

**v1.0** (Phases 0–7). Ubuntu Server + Arch Linux, systemd. Fedora and
openSUSE need only data files (a distro block per entry), not code
changes — that's the point of the tconf layer.

Vagrant + Ansible export is deferred to v2.0.

## License and disclaimer

LazyServer is released under the [MIT License](LICENSE).

**Use on recoverable machines.** LazyServer edits system config files
and controls services as root. It is designed for classroom Linux VMs
where a broken box is one snapshot away from recoverable — see
[`docs/spec.md`](docs/spec.md) §9 for the assumed deployment context.
Running it on a production server, a bare-metal box without a recovery
plan, or a remote-only machine you cannot console into is at your own
risk.

**No warranty.** The software is provided *as is*, without warranty of
any kind, express or implied. The authors accept no responsibility for
data loss, service outages, misconfigured machines, or any other
consequence of running LazyServer. See the [LICENSE](LICENSE) file for
the full terms.
