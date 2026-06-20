import os
import pwd
from pathlib import Path

import pytest

from lazyserver import config
from lazyserver.platform.user import TargetUser


def _self_target_user(tmp_home: Path) -> TargetUser:
    me = pwd.getpwuid(os.getuid())
    return TargetUser(name=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, home=tmp_home)


def test_load_missing_returns_defaults(tmp_path: Path):
    s = config.load(tmp_path / "config.toml")
    assert s == config.Settings()


def test_round_trip(tmp_path: Path):
    user = _self_target_user(tmp_path)
    s = config.Settings(
        editor="nano",
        backup_store="/var/lib/lazyserver/backups",
        tconf_paths=("/etc/lazyserver/tconf", "/home/alice/.config/lazyserver/tconf"),
        target_user="alice",
    )
    path = config.default_path(user)
    config.save(s, path, owner=user)
    assert path.exists()
    assert config.load(path) == s


def test_default_path_is_under_target_user_home(tmp_path: Path):
    user = _self_target_user(tmp_path)
    p = config.default_path(user)
    assert p == tmp_path / ".config" / "lazyserver" / "config.toml"


def test_resolve_editor_picks_settings_first(tmp_path: Path):
    s = config.Settings(editor="/bin/sh")
    assert config.resolve_editor(s, env={"VISUAL": "vim", "EDITOR": "emacs"}) == "/bin/sh"


def test_resolve_editor_falls_through_chain():
    s = config.Settings()
    chosen = config.resolve_editor(s, env={"VISUAL": "definitely-missing-xyz"})
    assert chosen in ("nano", "vi")


def test_resolve_editor_visual_before_editor():
    s = config.Settings()
    chosen = config.resolve_editor(
        s, env={"VISUAL": "/bin/sh", "EDITOR": "/bin/true"}
    )
    assert chosen == "/bin/sh"


def test_writer_escapes_strings(tmp_path: Path):
    user = _self_target_user(tmp_path)
    s = config.Settings(editor='ed"itor', backup_store="C:\\path\\with\\back")
    path = config.default_path(user)
    config.save(s, path, owner=user)
    assert config.load(path) == s


# ---------- malformed TOML (NFR-5: clear errors) ----------


def test_malformed_toml_raises_config_error_naming_the_file(tmp_path: Path):
    """A student-edited config.toml with a syntax typo must not crash
    the app with a raw traceback (NFR-5)."""
    p = tmp_path / "config.toml"
    p.write_text('editor = "unterminated\n', encoding="utf-8")
    with pytest.raises(config.ConfigError) as excinfo:
        config.load(p)
    msg = str(excinfo.value)
    assert str(p) in msg
    # Recovery hint: students can't reach the TUI to fix it from inside,
    # so the error must tell them they can delete the file.
    assert "delete" in msg.lower()


# ---------- ~ expansion in config paths (FR-1.10 consistency) ----------


def test_expand_user_path_expands_tilde_against_target_user_home(tmp_path: Path):
    """`~/foo` in a config setting must expand to the *target* user's home,
    not be taken literally — students writing `backup_store = "~/lsrvbck"`
    should not get a folder named `~`."""
    user = _self_target_user(tmp_path)
    assert config.expand_user_path("~/lsrvbck", user) == tmp_path / "lsrvbck"
    assert config.expand_user_path("~", user) == tmp_path


def test_expand_user_path_passes_absolute_and_relative_through(tmp_path: Path):
    user = _self_target_user(tmp_path)
    assert config.expand_user_path("/var/lib/lsrv", user) == Path("/var/lib/lsrv")
    assert config.expand_user_path("rel/path", user) == Path("rel/path")


def test_resolved_backup_store_expands_tilde(tmp_path: Path):
    user = _self_target_user(tmp_path)
    s = config.Settings(backup_store="~/lsrvbck")
    assert config.resolved_backup_store(s, user) == tmp_path / "lsrvbck"


def test_resolved_backup_store_none_when_unset(tmp_path: Path):
    user = _self_target_user(tmp_path)
    assert config.resolved_backup_store(config.Settings(), user) is None


def test_resolved_tconf_paths_expands_each(tmp_path: Path):
    user = _self_target_user(tmp_path)
    s = config.Settings(tconf_paths=("~/t1", "/abs/t2"))
    assert config.resolved_tconf_paths(s, user) == (
        tmp_path / "t1",
        Path("/abs/t2"),
    )


def test_malformed_toml_error_includes_line_position_when_available(tmp_path: Path):
    """When tomllib reports a position, surface it so the student can
    jump straight to the problem instead of bisecting the file."""
    p = tmp_path / "config.toml"
    p.write_text('editor = "unterminated\n', encoding="utf-8")
    with pytest.raises(config.ConfigError) as excinfo:
        config.load(p)
    assert "line" in str(excinfo.value).lower()
