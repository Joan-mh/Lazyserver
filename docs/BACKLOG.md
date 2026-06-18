# LazyServer — Backlog

Parked ideas: things we have **decided we may want** but are **not building now**.
This is distinct from `roadmap.md`, which is the committed, phased plan being
executed. An item here is a candidate, not a commitment.

For each entry: what it is, why it was deferred, and where it might land.

> Convention: when an idea moves from "parked" to "committed", move it out of
> this file and into `roadmap.md` (or a `spec.md` requirement), so the two files
> never disagree about what is actually being built.

---

## Detect already-installed apps and services

**Status:** deferred to **v2.0**
**Raised:** during Phase 1 review.
**Relates to:** spec §1 (configuration access), schema §9 (AI prompt template).

LazyServer is currently **declaration-driven**: it knows about a service or app
only because a tconf file exists for it. It never inspects the machine to ask
"what is actually installed here?" This idea would let the tool reflect the real
state of the server, not just the tconf catalogue.

The idea splits into three genuinely different features, in increasing order of
effort and decreasing order of reliability:

1. **Installed-status on known entries.** For entries that already have a tconf
   file, show whether each is actually installed and running on this machine
   (e.g. a badge: *not installed* / *installed · stopped* / *installed ·
   running*, optionally *enabled · disabled* at boot). Cheap: the resolver
   already knows the per-distro package name and service unit, so this is a
   couple of extra checks through `runner.py`. No schema change. Strong didactic
   value — a learner sees at a glance which services are live and the difference
   between "installed but stopped" and "running". This is the most attractive
   piece and the natural first step.

2. **Detect installed-but-unknown services.** Scan the machine for packages or
   running units that have **no** tconf entry, and offer: "these exist but
   LazyServer doesn't know them yet — create a tconf file?" Ties naturally into
   the AI prompt template (schema §9): detection finds the name, LazyServer
   offers to scaffold an entry. More work, and detection alone only yields a
   *name* — it does not know where the config lives or what it means without a
   tconf file.

3. **Auto-generate a tconf entry from a running system.** Inspect the installed
   package, locate its config files, and build the entry automatically. Hard and
   unreliable — there is no universal way to know which files are "the config" —
   so this is the least certain piece. Likely v2.0+ at the earliest, if ever.

**Why deferred:** the core of LazyServer (browse, learn, edit, control, back up,
restore, full recovery) should ship first. Detection is an enhancement that sits
on top of a working core, not a foundation.

**If revisited:** feature 1 is small enough that it could be pulled forward into
v1.0 if there is appetite; it would live in the TUI display layer (where entries
are shown). Features 2 and 3 are v2.0+.

---

## (template for the next parked idea)

**Status:** deferred to [version / phase]
**Raised:** [when]
**Relates to:** [spec / schema / arch section]

[What it is, why it was deferred, where it might land.]
