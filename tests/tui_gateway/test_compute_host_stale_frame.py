"""A relayed (delayed) turn.end must not revert a compression that the parent already adopted.

RelayBuffer moves turn.end / turn.error off the stdout reader while control acks (compression A->B)
apply immediately, so an OLDER completion for session A can be adopted AFTER the parent moved to B.
Compression bumps history_version; a frame below the parent's version is stale (Codex review of sync
bucket A1)."""

from tui_gateway import server


def _patch_transfer(monkeypatch):
    calls = []

    def _fake_transfer(sid, session, *, new_session_id):
        calls.append(new_session_id)
        return True

    monkeypatch.setattr(server, "_transfer_active_session_slot", _fake_transfer)
    return calls


def test_stale_turn_end_does_not_revert_compressed_session_key(monkeypatch):
    calls = _patch_transfer(monkeypatch)
    session = {"session_key": "B", "history_version": 11}  # compression ack already applied
    server._compute_host_adopt_frame_meta(session, {"sid": "s", "session_key": "A", "history_version": 10})
    assert session["session_key"] == "B"
    assert session["history_version"] == 11
    assert calls == [], "a stale frame must not re-anchor the lease"


def test_newer_frame_still_adopts_a_rotated_key(monkeypatch):
    calls = _patch_transfer(monkeypatch)
    session = {"session_key": "B", "history_version": 11}
    server._compute_host_adopt_frame_meta(session, {"sid": "s", "session_key": "C", "history_version": 12})
    assert session["session_key"] == "C"
    assert session["history_version"] == 12
    assert calls == ["C"]


def test_same_version_frame_for_current_key_is_a_no_op(monkeypatch):
    calls = _patch_transfer(monkeypatch)
    session = {"session_key": "B", "history_version": 11}
    server._compute_host_adopt_frame_meta(session, {"sid": "s", "session_key": "B", "history_version": 11})
    assert session == {"session_key": "B", "history_version": 11} and calls == []
