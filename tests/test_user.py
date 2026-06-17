import os

import pytest

from lazyserver.platform import user


def test_override_wins():
    me = os.environ.get("USER") or "root"
    result = user.resolve(override=me, env={"SUDO_USER": "ghost", "USER": "ghost"})
    assert result.name == me


def test_sudo_user_preferred_over_user():
    me = os.environ.get("USER") or "root"
    result = user.resolve(env={"SUDO_USER": me, "USER": "ghost"})
    assert result.name == me


def test_falls_back_to_user_when_no_sudo():
    me = os.environ.get("USER") or "root"
    result = user.resolve(env={"USER": me})
    assert result.name == me


def test_no_candidate_raises():
    with pytest.raises(user.TargetUserError):
        user.resolve(env={})


def test_unknown_user_raises():
    with pytest.raises(user.TargetUserError):
        user.resolve(env={"USER": "definitely-not-a-real-user-xyz-123"})
