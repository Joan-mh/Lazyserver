# LazyServer — Adding your own service or app

LazyServer knows a service or an app because a **tconf YAML file**
tells it to. Nine files ship out of the box (bind9, nginx, Apache,
Postfix, Dovecot, Squid, vsftpd, isc-dhcp-server, and neovim). To
manage anything else — a service on your syllabus that isn't shipped,
or a private tool your class uses — you write a tconf file.

This guide walks through **when** to add an entry, **what to gather
first**, and **how to use the AI prompt template** in
[`tconf-schema.md`](tconf-schema.md#9-ai-prompt-template-for-generating-a-new-entry)
to produce a valid file quickly. For the full schema and every
validation rule, that document is the source of truth; this one is
the human on-ramp.

---

## When to write a new tconf entry

Write one when:

- The service is not one of the eight shipped, and you want to
  browse / edit / back up its config through LazyServer.
- You want to override a shipped entry — e.g. add an extra managed
  file, or change a path to match a non-standard install. (You can
  shadow a shipped entry: put a file with the same `id` in one of
  your `tconf_paths` folders and it wins over the bundled one.)
- You built or packaged your own app and want its config included
  in your backup + recovery flow.

You don't need to write one just to *use* a service; LazyServer isn't
required for anything to work. tconf entries exist so the service
becomes browsable, backup-able, and part of `recover --all`.

---

## Where the file lives

Anywhere in a folder listed under `tconf_paths` in your settings:

```toml
# ~/.config/lazyserver/config.toml
tconf_paths = ["~/tconf-overrides"]
```

Convention:

```
~/tconf-overrides/
├── services/
│   └── <id>.yaml       # for daemons — bind9, nginx, ...
└── apps/
    └── <id>.yaml       # for programs with configs but no daemon — neovim, ...
```

The two subfolders are convention, not enforcement — LazyServer sorts
entries by `kind:` inside the YAML. But keeping them separate makes
`git log` in your overrides repo readable.

---

## Before you ask the AI: gather these facts

The AI template needs specifics. If you feed it guesses, you get
plausible-but-wrong YAML that fails validation or, worse, points at
paths that don't exist on your distro. Ten minutes of research first
saves an hour of debugging later.

For **the entry itself:**

- **Name** and a **kind** (`service` or `app`).
- **Category** (short label, e.g. `dns`, `proxy`, `mail`,
  `editor`) — used in the browse list.
- The **distros** you need it to work on — `ubuntu`, `arch`, or both.

For **each distro**, look up:

- The **package name** as installed by that distro's package
  manager. `apt-cache search`, `pacman -Ss`, or the distro's package
  search page.
- The **systemd unit name** (services only). `systemctl list-units
  --type=service | grep <name>`. Note the exact name — Ubuntu often
  uses different unit names than Arch for the same upstream project
  (e.g. `apache2` vs `httpd`).

For **each managed config file:**

- Its **path** on each distro. Same path on both → put it once at
  the top level. Different paths per distro → put it in each
  distro's `file_paths` map.
- A **one-paragraph description** in the style of the Ubuntu help
  pages — what the file is for, not a line-by-line breakdown. Aim
  for what a student needs to know before opening it.
- A **short example** (5–15 lines) that clearly is not the machine's
  real file — an illustration, not a template someone might mistake
  for production config.

If the entry has **user-created config files** (BIND zone files
under `/etc/bind/db.*`, nginx `sites-available/*`, drop-ins in
`conf.d/`), you want a **file set**:

- The **directory** and the **glob pattern** (e.g. `db.*`,
  `*.conf`).
- One representative **example file** and its description.

Get those facts down first, then move on.

---

## Using the AI prompt template

The template lives at [`tconf-schema.md` §9](tconf-schema.md#9-ai-prompt-template-for-generating-a-new-entry).
It is intentionally the source of truth — this section shows you how
to use it, but does not reproduce it (so it can't drift out of sync).

### The workflow

1. **Open an AI assistant** in a fresh chat.
2. **Copy the whole prompt block** from `tconf-schema.md` §9.
3. **Fill in the `[bracketed]` slots** with the facts you gathered
   above. Every bracket is a decision the AI cannot make for you —
   if you leave one blank, the output will hallucinate.
4. **Paste the schema.** The prompt ends with `[paste
   tconf-schema.md]`. Copy the entire `tconf-schema.md` file
   (sections 1 through 10) into the message. This is the specification
   the AI has to follow.
5. **Send** the message.
6. **Save the output** as `~/tconf-overrides/services/<id>.yaml` (or
   `apps/<id>.yaml`).

### What the brackets mean

The template's `[bracketed]` slots, translated:

- **Service or application name** — the human name (`Squid`,
  `PostgreSQL`). Becomes the `name:` field.
- **kind** — `service` for a daemon (has a systemd unit,
  needs start/stop), `app` for something that has configs but
  isn't a network daemon.
- **category** — short label. Same convention as shipped entries:
  look at their `category:` values for style.
- **Distributions I need** — comma-separated distro ids
  (`ubuntu, arch`).

### What the template's rules mean

The rules inside the prompt (numbered 1–7 in §9) exist because they
match how the loader validates the file. If you're curious:

- Rule 1 (schema fields) — validation §8; missing any of these
  rejects the file.
- Rule 2 (files with description and example) — FR-4.3, the
  didactic requirement.
- Rule 3 (file sets for varying files) — FR-1.6, so
  user-created files are covered automatically.
- Rule 4 (didactic text at top level, distro-specific at
  distro level) — schema §5 organisation rule.
- Rule 5 (single `path` when identical across distros) —
  reduces duplication and error.
- Rule 6 (no invented `actions`) — LazyServer derives systemd
  defaults from `service_unit`, so extra actions overwrite
  correct defaults with plausible-but-wrong ones.
- Rule 7 (verify package names and paths) — the AI can't. You must.

---

## Verify the file before you rely on it

The AI does not have your distro installed in front of it. Its
output may be almost right and subtly wrong. Before you trust the
entry, do this:

### 1. Load it

Drop it in your overrides folder and launch LazyServer:

```bash
sudo lsrv
```

If validation fails, you'll see a clear error on startup — the
loader points at the file, the field, and what's wrong. Fix and
retry. Common errors:

- Missing `schema_version: 1`.
- No `distros:` block for the distro you're on.
- Both `files` and `file_sets` missing (at least one is required).
- Duplicate `id` inside the entry (`files` and `file_sets` share
  one flat id namespace per entry).

### 2. Check the resolved facts on the Entry screen

Open the new entry. LazyServer displays:

```
service_unit: <unit>    package: <package>
```

Look at both. If the unit name is wrong, `systemctl start` will fail
against a nonexistent unit. If the package name is wrong,
`recover --all` will fail at the install step.

### 3. Open each managed file

Enter on each file. Verify the **path** matches reality. If you
resolved to `xterm-256color`… wait, wrong doc — if the path is
missing on your machine, LazyServer says so. Either the path is
wrong (fix the tconf), or the file is legitimately absent because
you haven't installed the service yet (install it first, then check).

### 4. Try a dry-run recovery

```bash
sudo lsrv --dry-run recover --all
```

Look for the new entry in the plan. The install command shown
should be `apt install <package>` or `pacman -S <package>` with the
right package name; the enable/start should reference the right
unit. If the plan reads wrong, fix the tconf; if it reads right,
you're done.

---

## Living with your overrides

Your `~/tconf-overrides` folder is just YAML files — put it in git.
That's how you carry your custom entries across machines, and it
plugs straight into full recovery: on a new machine, clone
`tconf-overrides` and `lsrvbck` next to each other, point settings
at both, run `sudo lsrv recover --all`, done.

If a shipped entry gets updated in a future LazyServer release and
your override still shadows it, LazyServer logs the shadowing so
you can decide whether to keep your override or drop it and pick up
the new bundled version.

---

## When the AI can't help

Some entries the AI genuinely can't get right — anything with
non-standard build layouts, forks, or configs that live in unusual
places. The schema doesn't require you to use the AI; you can write
the file by hand. Look at one of the shipped services under
`lazyserver/data/tconf/services/` as a worked example, copy its
structure, and fill in your specifics. The shipped `bind9.yaml`
covers most patterns (files, file sets, per-distro paths, actions,
file-set ownership) in one file — it's a good reference.
