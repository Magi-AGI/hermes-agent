"""G1 (bucket C): a compute-host child's shutdown must not end the parent's live durable row.

Under upstream's ownership model the child runs an isolated turn with an INERT borrowed token
(``_install_borrowed_lease``): the parent holds the real lease and owns the session's lifecycle.
The child's shutdown flush (``flush_all_sessions`` -> ``_finalize_session(end_reason=
"compute_host_<reason>")``) used to call ``db.end_session`` and leave ``agent.close()`` free to end
the row a second time, so a host restart or orphan exit ended a conversation the parent (and the
user) still had open. A session holding a borrowed token is owned by another runtime.
"""

from __future__ import annotations

import io

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tests.tui_gateway.test_gateway_owned_session_reap import _make_real_session, _read_real_row


def _borrow(session, session_id):
    server._install_borrowed_lease(
        "sid-1", session, {"active_session_lease": {"session_id": session_id, "lease_id": "parent-lease"},
                           "source": "desktop"})
    assert str(session["active_session_lease"].lease_id).startswith("borrowed:")


@pytest.mark.parametrize("reason", ["shutdown", "orphan", "sigterm", "stdin_closed"])
def test_child_shutdown_flush_leaves_the_parents_row_open(tmp_path, monkeypatch, reason):
    session_id = f"desktop-borrowed-{reason}"
    db_path, session, agent = _make_real_session(tmp_path, monkeypatch, source="desktop", session_id=session_id)
    _borrow(session, session_id)
    monkeypatch.setattr(server, "_sessions", {"sid-1": session})

    ComputeHost(stdout=io.StringIO(), heartbeat_secs=0).flush_all_sessions(reason=reason)
    # Process exit: atexit teardown closes the agent too; its own row-finalize must stay spared.
    agent.close()

    row = _read_real_row(db_path, session_id)
    assert (row["ended_at"], row["end_reason"]) == (None, None)
    assert agent._end_session_on_close is False


def test_a_child_that_owns_its_session_still_ends_it(tmp_path, monkeypatch):
    """Control: without a borrowed token (legacy self-claim), the child owns the lifecycle and
    its shutdown ends the row as before."""
    session_id = "desktop-self-owned"
    db_path, session, _agent = _make_real_session(tmp_path, monkeypatch, source="desktop", session_id=session_id)
    monkeypatch.setattr(server, "_sessions", {"sid-1": session})

    ComputeHost(stdout=io.StringIO(), heartbeat_secs=0).flush_all_sessions(reason="shutdown")

    assert _read_real_row(db_path, session_id)["end_reason"] == "compute_host_shutdown"
