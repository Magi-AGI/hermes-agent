"""Windows sharing violations on credential reads and lock-file opens are retried, bounded.

A reader racing another process's ``os.replace`` (or a lock file being created by a peer) can get
WinError 5/32/33. The strict writer already retries its rename within a fixed budget; readers and
the lock-file open get the same bounded policy. After the budget the last error is raised
unchanged — a real permission problem never turns into a silent success. Deterministic: the
contended error is simulated, no timing.
"""

import threading
from pathlib import Path

import pytest

import hermes_cli.auth as auth
from hermes_cli import auth_credential_io as io


def _contended() -> OSError:
    err = PermissionError(13, "The process cannot access the file")
    err.winerror = 32
    return err


@pytest.fixture
def contention(monkeypatch):
    monkeypatch.setattr(io, "_is_contended", lambda exc: getattr(exc, "winerror", None) in (5, 32, 33))
    monkeypatch.setattr(auth, "is_contended_error", lambda exc: getattr(exc, "winerror", None) in (5, 32, 33),
                        raising=False)
    monkeypatch.setattr(io.time, "sleep", lambda _s: None)
    monkeypatch.setattr(auth.time, "sleep", lambda _s: None)


def _failing(real, fails, error_factory):
    calls = {"n": 0}

    def wrapped(self, *a, **k):
        calls["n"] += 1
        if calls["n"] <= fails:
            raise error_factory()
        return real(self, *a, **k)
    return wrapped, calls


def test_witnessed_read_retries_a_sharing_violation(tmp_path, monkeypatch, contention):
    path = tmp_path / "auth.json"
    path.write_bytes(b'{"version": 1}')
    wrapped, calls = _failing(Path.read_bytes, 2, _contended)
    monkeypatch.setattr(Path, "read_bytes", wrapped)
    data, sha = io.read_witnessed_bytes(path)
    assert data == b'{"version": 1}' and sha and calls["n"] == 3


def test_witnessed_read_raises_after_the_budget_and_never_retries_a_real_denial(tmp_path, monkeypatch,
                                                                                contention):
    path = tmp_path / "auth.json"
    path.write_bytes(b"{}")
    wrapped, calls = _failing(Path.read_bytes, 10 ** 6, _contended)
    monkeypatch.setattr(Path, "read_bytes", wrapped)
    with pytest.raises(PermissionError):
        io.read_witnessed_bytes(path)
    assert calls["n"] == io.REPLACE_ATTEMPTS
    denied, denied_calls = _failing(Path.read_bytes, 10 ** 6, lambda: PermissionError(13, "denied"))
    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(PermissionError):
        io.read_witnessed_bytes(path)
    assert denied_calls["n"] == 1


def test_lock_file_open_retries_a_sharing_violation(tmp_path, monkeypatch, contention):
    (tmp_path / "auth.lock").write_text(" ", encoding="utf-8")  # a peer created it
    wrapped, calls = _failing(Path.open, 2, _contended)
    monkeypatch.setattr(Path, "open", wrapped)
    with auth._file_lock(tmp_path / "auth.lock", threading.local(), 5.0, "timeout"):
        pass
    assert calls["n"] >= 3


def test_lock_file_open_raises_after_its_deadline(tmp_path, monkeypatch, contention):
    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 0.25
        return clock["t"]

    monkeypatch.setattr(auth.time, "monotonic", monotonic)
    (tmp_path / "auth.lock").write_text(" ", encoding="utf-8")
    wrapped, _calls = _failing(Path.open, 10 ** 6, _contended)
    monkeypatch.setattr(Path, "open", wrapped)
    with pytest.raises(PermissionError):
        with auth._file_lock(tmp_path / "auth.lock", threading.local(), 1.0, "timeout"):
            pytest.fail("the lock must not be granted without its file")
