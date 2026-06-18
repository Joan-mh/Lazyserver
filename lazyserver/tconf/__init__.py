"""tconf — data layer for LazyServer entries (schema spec).

Public surface for downstream phases:
- model.Entry, ManagedFile, FileSet, DistroProfile
- loader.load_file(path), load_folder(path), load_folders(paths)
- resolve.resolve(entry, distro_id, target_user=None) → ResolvedEntry
- defaults.* — INIT_SYSTEM_BY_DISTRO, ACTION_TEMPLATES, INSTALL_TEMPLATES.
- bundled_tconf_path() — folder shipped with the package (FR-4.2).
"""

from __future__ import annotations

from pathlib import Path


def bundled_tconf_path() -> Path:
    """Return the folder of tconf entries shipped with LazyServer (FR-4.2).

    The 8 pre-loaded services and the neovim app live under here. The
    settings loader (FR-7.4) prepends this to `tconf_paths` so a user
    always starts with the shipped entries, and can shadow any of them
    by listing a local folder later in the list.
    """
    return Path(__file__).resolve().parent.parent / "data" / "tconf"
