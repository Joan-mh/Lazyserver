# tconf — Data Format Specification

**Status:** Draft v0.1
**Applies to:** the YAML files LazyServer reads to know about services and apps.

This document defines the shape of a tconf file, gives a fully worked example,
and provides an **AI prompt template** the user can paste into an assistant to
generate a new, correct entry.

---

## 1. Folder layout

```
tconf/
├── services/          # network daemons
│   ├── bind9.yaml
│   ├── nginx.yaml
│   └── ...
└── apps/              # non-daemon applications
    ├── neovim.yaml
    └── ...
```

Decisions (from spec):

- **One YAML file per entry** (hybrid model). A file is self-contained.
- **Shared didactic content is written once** inside the file; only
  distro-specific values are split into a `distros:` map. This avoids
  duplicating explanations across distributions.
- Format is **YAML** (comments + multiline strings make it pleasant for humans
  and for AI to author).

## 2. Top-level fields of an entry

| Field | Required | Type | Meaning |
|-------|----------|------|---------|
| `schema_version` | yes | int | Format version. Start at `1`. |
| `id` | yes | string | Stable unique id, lowercase, e.g. `bind9`. |
| `name` | yes | string | Human display name, e.g. `BIND 9 (DNS)`. |
| `kind` | yes | enum | `service` or `app`. |
| `category` | no | string | e.g. `dns`, `http`, `mail`, `editor`. For grouping. |
| `description` | yes | string (multiline) | Short, friendly overview. Ubuntu-help style. |
| `docs_url` | no | string | Link to upstream/official docs. |
| `files` | yes* | list | Fixed, known config files (see §3). |
| `file_sets` | yes* | list | User-created files matched by directory + glob (see §3b). |
| `distros` | yes | map | Per-distribution values (see §4). |

\* An entry must declare at least one of `files` or `file_sets` (it may have both).

## 3. The `files` list (fixed files)

Each item describes one **fixed** config file — a single known path that ships
with the package (e.g. `nginx.conf`, `named.conf.options`).

| Field | Required | Type | Meaning |
|-------|----------|------|---------|
| `id` | yes | string | Stable id within the entry, e.g. `named_conf`. |
| `path` | yes/override | string | Absolute path. May be overridden per distro (see §4). |
| `description` | yes | string | What this file is for. One short paragraph. |
| `example` | no | string (multiline) | A representative snippet. **Illustrative, not the live file.** |
| `optional` | no | bool | If the file may legitimately be absent. Default `false`. |

> If a file lives at a different path on different distros, omit `path` here and
> set it inside each distro block's `file_paths` map (see §4). A `path` given
> here is the default used when a distro provides no override.

## 3b. The `file_sets` list (user-created files)

Some entries have config files the **user creates**, whose names and number are
not known in advance: BIND zone files (`/etc/bind/db.*`), nginx virtual hosts
(`sites-available/*`), Squid/systemd drop-ins (`conf.d/*.conf`). These cannot be
listed as fixed `files`. A `file_set` describes them by **directory + glob
pattern**; LazyServer expands the pattern at backup/restore time, so files
created *after* the tconf file was written are still covered automatically.

| Field | Required | Type | Meaning |
|-------|----------|------|---------|
| `id` | yes | string | Stable id within the entry, e.g. `zone_files`. |
| `directory` | yes/override | string | Directory to scan. May be overridden per distro. |
| `pattern` | yes | string | Glob applied within `directory`, e.g. `db.*`. Non-recursive unless it contains `**`. |
| `description` | yes | string | What these files are for. One short paragraph. |
| `example` | no | string (multiline) | A representative single file. **Illustrative**; also used as the starter template when creating a new file (FR-1.7). |
| `optional` | no | bool | If the set may legitimately be empty. Default `true`. |
| `owner` | no | string | Force owner for newly created files. **Normally unnecessary** — LazyServer copies from an existing sibling file. |
| `group` | no | string | Force group for newly created files. Normally unnecessary. |
| `mode` | no | string | Force mode (octal, e.g. `"0640"`) for newly created files. Normally unnecessary. |

How LazyServer uses a file_set:

- **Modification detection / backup (FR-2):** the glob is expanded at backup
  time; every matched file is checksummed and backed up individually. New files
  appear in the pending list automatically as soon as they exist.
- **Restore (FR-3):** restoring the entry restores every file the backup store
  holds for the set — **including files no longer present on disk** (the
  disaster-recovery case). By default restore **never deletes** a live file that
  is absent from the backup; it reports such "extra" files instead of removing
  them (NFR-2, safe by default). Mirror/delete may be added later as an explicit,
  confirmed option.
- **Create (FR-1.7/1.8/1.9):** the user can create a new file in the set from
  inside LazyServer. It is pre-filled from `example`, given correct ownership
  (sibling → directory → optional `owner`/`group`/`mode` override → `root:root`
  with warning), and the resolved owner/mode is previewed above the name input
  before the user types the name.

> Per-distro directory differences go in the distro block's `file_set_dirs` map
> `{file_set_id: directory}` (see §4), mirroring how `file_paths` overrides fixed
> files.

## 4. The `distros` map

Keys are distro ids: `ubuntu`, `arch`, `fedora`, `opensuse`. Only ship the ones
you support; others can be added later as data.

Each distro block:

| Field | Required | Type | Meaning |
|-------|----------|------|---------|
| `package` | yes | string | Package name to install. |
| `install` | no | string/list | Command(s) to install. If omitted, derived from the distro's default installer + `package`. |
| `service_unit` | for services | string | The unit/daemon name used by control commands (e.g. `named`, `bind9`). |
| `actions` | no | map | Overrides for control commands (see §5). |
| `file_paths` | no | map | `{file_id: absolute_path}` overrides for files whose path differs here. |
| `file_set_dirs` | no | map | `{file_set_id: directory}` overrides for file_sets whose directory differs here. |

## 5. Service-control actions

Standard action ids: `start`, `stop`, `restart`, `reload`, `enable`, `disable`,
`status`.

LazyServer provides **sensible systemd defaults** computed from `service_unit`
(e.g. `start` → `systemctl start <unit>`). An entry only specifies `actions`
when it needs to **override** a default (non-systemd init, extra flags, a
pre-check, etc.). This keeps files short while still allowing full control —
satisfying NFR-1 (no code change to express an unusual command).

> Defaults are a convenience of the data layer, not hard-coded service
> knowledge: they are generated from the unit name and can always be overridden.

## 6. Worked example — `tconf/services/bind9.yaml`

```yaml
schema_version: 1
id: bind9
name: BIND 9 (DNS)
kind: service
category: dns
docs_url: https://bind9.readthedocs.io/

description: |
  BIND 9 is the most widely used DNS server on the internet. It answers
  name-resolution queries (turning names like example.com into IP addresses)
  and can act as an authoritative server for your own zones or as a caching
  resolver for a network. Configuration is split between a main options file
  and one or more zone files.

files:
  - id: named_conf_options
    description: |
      Global server options: listening addresses, recursion settings,
      forwarders, and access control. This is where you decide whether the
      server is a resolver for your LAN, an authoritative server, or both.
    example: |
      options {
          directory "/var/cache/bind";
          recursion yes;
          allow-query { localhost; 192.168.0.0/24; };
          forwarders { 1.1.1.1; 8.8.8.8; };
      };

  - id: named_conf_local
    description: |
      Where you declare your own zones. Each zone points to a zone file that
      holds the actual DNS records for a domain.
    example: |
      zone "example.lan" {
          type master;
          file "/etc/bind/db.example.lan";
      };

file_sets:
  - id: zone_files
    directory: /etc/bind            # may be overridden per distro
    pattern: "db.*"
    description: |
      Zone files hold the actual DNS records (A, MX, CNAME, ...) for each domain
      you host. You create one per zone, so their names depend on your domains;
      LazyServer tracks every file matching the pattern rather than fixed names,
      and backs up new zones automatically as you add them.
    example: |
      $TTL 86400
      @   IN  SOA ns1.example.lan. admin.example.lan. ( 1 3600 900 604800 86400 )
      @   IN  NS  ns1.example.lan.
      ns1 IN  A   192.168.0.10
      www IN  A   192.168.0.20

distros:
  ubuntu:
    package: bind9
    service_unit: named            # Ubuntu ships the unit as 'named' (bind9 alias exists)
    file_paths:
      named_conf_options: /etc/bind/named.conf.options
      named_conf_local: /etc/bind/named.conf.local

  arch:
    package: bind
    service_unit: named
    file_paths:
      named_conf_options: /etc/named.conf      # Arch keeps options in named.conf
      named_conf_local: /etc/named.conf
    file_set_dirs:
      zone_files: /var/named                   # Arch stores zone files under /var/named
```

Notes the example demonstrates:

- Didactic text (entry `description`, per-file `description`) appears **once**.
- Only paths/package/unit differ per distro, inside `distros`.
- No `actions` block → LazyServer uses systemd defaults from `service_unit`.
- `example` blocks are illustrative snippets, clearly not the machine's real file.

## 7. Worked example — `tconf/apps/neovim.yaml` (application)

```yaml
schema_version: 1
id: neovim
name: Neovim
kind: app
category: editor
docs_url: https://neovim.io/doc/

description: |
  Neovim is a modern, extensible Vim-based text editor. Its configuration lives
  in the user's config directory and controls plugins, key mappings, and
  appearance.

files:
  - id: init_lua
    path: ~/.config/nvim/init.lua     # same path on all distros → set here
    description: |
      The main Neovim configuration, written in Lua. Loaded at startup; it sets
      options, key mappings, and bootstraps plugins.
    example: |
      vim.opt.number = true
      vim.opt.expandtab = true
      vim.opt.shiftwidth = 2

distros:
  ubuntu:
    package: neovim
  arch:
    package: neovim
```

This shows: an **app** entry, a per-user config path (`~` expands for the target
user), no service unit and no actions (apps are not daemons).

## 8. Validation rules (for the implementation)

The loader must reject a file and report a clear error when:

- `schema_version` is missing or unsupported.
- `id`, `name`, `kind`, `description`, or `distros` is missing.
- the entry declares neither `files` nor `file_sets` (at least one required).
- `kind` is not `service`/`app`.
- a `service` entry has a distro block lacking `service_unit`.
- a fixed file has neither a top-level `path` nor a `file_paths` entry in the
  distros that are present.
- a `file_set` is missing `directory`/`pattern` (after applying any
  `file_set_dirs` override) or `description`.
- duplicate `id`s across entries, or duplicate file / file_set `id`s within an
  entry.

## 9. AI prompt template (for generating a new entry)

> Paste the block below into an AI assistant, fill the **[brackets]**, and attach
> or paste this schema document. Review the result before using it — the
> assistant may not know your distro's exact paths.

```
You are generating a single LazyServer "tconf" YAML file. Follow the tconf
schema I provide exactly. Output ONLY valid YAML — no prose, no backticks.

Entry to create:
- Service or application name: [e.g. Squid]
- kind: [service | app]
- category: [e.g. proxy]
- Distributions I need: [e.g. ubuntu, arch]

Requirements:
1. Include schema_version: 1, id, name, kind, category, and a short friendly
   `description` in the style of the Ubuntu help pages (a paragraph, no
   line-by-line detail).
2. List the real, important configuration files a learner edits for this entry.
   For EACH file: an id, a one-paragraph `description` of what the file is for,
   and a short illustrative `example` snippet (clearly an example, not a full
   real file).
3. If the entry has user-created files whose names or number vary (DNS zone
   files, nginx virtual hosts, drop-in `conf.d/*.conf` files), express them as
   `file_sets` (directory + glob `pattern`), NOT as fixed `files`. Give each set
   a `description` and one representative `example` file.
4. Put shared/didactic text ONCE at the top level. Put ONLY distro-specific
   values (package, service_unit for services, differing fixed paths via
   file_paths, and differing set directories via file_set_dirs) inside the
   `distros` map.
5. If a file path is identical on all listed distros, set `path` on the file
   instead of repeating it per distro.
6. Do not invent `actions` unless the service does NOT use systemd or needs a
   non-standard command; LazyServer derives systemd defaults from service_unit.
7. Verify package names and config paths are correct for each listed
   distribution.

Here is the schema you must follow:
[paste tconf-schema.md]
```

## 10. Resolved decisions

- **`~` expansion for app files:** expands to the **target user's** home, resolved
  at startup from `$SUDO_USER` (fallback `$USER`, settings override) — spec
  FR-1.10. App files are owned by the target user; service files use system
  ownership — FR-1.8.
- **`example` as starter template:** yes — when creating a new file in a set (or a
  missing fixed file), LazyServer pre-fills it from `example` — FR-1.7.

No open questions remain blocking implementation.
