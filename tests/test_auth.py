"""Tests for dashboard/auth.py: password verification (bcrypt) and the
login-attempt lockout used by /admin and /ask. No NiceGUI, no network —
LoginLockout takes an injectable `now` so the lockout window can be
fast-forwarded deterministically (same pattern as ask.agent's RateLimiter)."""
import bcrypt
import pytest
from auth import LoginLockout, LoginLockoutConfig, check_password

# Hashed once at import time — bcrypt is deliberately slow, so every test
# below reuses this instead of hashing per-test.
_PASSWORD = "correct-horse-battery-staple"
_HASH = bcrypt.hashpw(_PASSWORD.encode(), bcrypt.gensalt()).decode()


# ---------------------------------------------------------------- check_password

def test_check_password_accepts_the_right_password():
    assert check_password(_PASSWORD, _HASH) is True


def test_check_password_rejects_the_wrong_password():
    assert check_password("wrong-password", _HASH) is False


def test_check_password_fails_closed_on_empty_hash():
    assert check_password(_PASSWORD, "") is False


def test_check_password_fails_closed_on_malformed_hash():
    # e.g. an operator pasted the plaintext password instead of a bcrypt
    # hash into DASH_PASSWORD_HASH — must not raise, must not match.
    assert check_password(_PASSWORD, "not-a-bcrypt-hash") is False


# ---------------------------------------------------------------- LoginLockout

def _lockout(clock, *, max_attempts=100, site_max_attempts=100, window_seconds=900) -> LoginLockout:
    config = LoginLockoutConfig(
        max_attempts=max_attempts, site_max_attempts=site_max_attempts, window_seconds=window_seconds
    )
    return LoginLockout(config, now=lambda: clock["t"])


def test_login_lockout_allows_then_blocks_per_identity():
    clock = {"t": 0.0}
    lock = _lockout(clock, max_attempts=2)
    assert lock.check("u1") is None
    lock.record_failure("u1")
    assert lock.check("u1") is None
    lock.record_failure("u1")
    reason = lock.check("u1")
    assert reason is not None and "分鐘" in reason


def test_login_lockout_window_expires():
    clock = {"t": 0.0}
    lock = _lockout(clock, max_attempts=1)
    lock.record_failure("u1")
    assert lock.check("u1") is not None
    clock["t"] += 901  # past the window
    assert lock.check("u1") is None


def test_login_lockout_per_identity_independent():
    clock = {"t": 0.0}
    lock = _lockout(clock, max_attempts=1)
    lock.record_failure("u1")
    assert lock.check("u1") is not None
    assert lock.check("u2") is None  # different identity, untouched


def test_login_lockout_site_wide_cap_blocks_everyone():
    clock = {"t": 0.0}
    lock = _lockout(clock, site_max_attempts=1)
    lock.record_failure("u1")
    reason = lock.check("u2")
    assert reason is not None and "異常" in reason


def test_login_lockout_success_clears_only_that_identity():
    clock = {"t": 0.0}
    lock = _lockout(clock, max_attempts=1)
    lock.record_failure("u1")
    assert lock.check("u1") is not None
    lock.record_success("u1")
    assert lock.check("u1") is None


def test_login_lockout_describe_reflects_the_live_config():
    clock = {"t": 0.0}
    lock = _lockout(clock, max_attempts=7, window_seconds=600)
    hint = lock.describe()
    assert "7" in hint and "10" in hint  # 600s = 10 min


@pytest.mark.parametrize(
    "env_key, attr",
    [
        ("LOGIN_MAX_ATTEMPTS", "max_attempts"),
        ("LOGIN_SITE_MAX_ATTEMPTS", "site_max_attempts"),
        ("LOGIN_LOCKOUT_SECONDS", "window_seconds"),
    ],
)
def test_login_lockout_config_reads_env_vars(monkeypatch, env_key, attr):
    monkeypatch.setenv(env_key, "42")
    assert getattr(LoginLockoutConfig(), attr) == 42


def test_login_lockout_logs_every_failed_attempt(capsys):
    clock = {"t": 0.0}
    lock = _lockout(clock)
    lock.record_failure("u1")
    out = capsys.readouterr().out
    assert "[auth]" in out and "login failed" in out and "u1" in out


def test_login_lockout_logs_blocked_attempts(capsys):
    clock = {"t": 0.0}
    lock = _lockout(clock, max_attempts=1)
    lock.record_failure("u1")
    capsys.readouterr()  # discard the "login failed" line above
    lock.check("u1")
    out = capsys.readouterr().out
    assert "[auth]" in out and "login blocked" in out and "u1" in out
