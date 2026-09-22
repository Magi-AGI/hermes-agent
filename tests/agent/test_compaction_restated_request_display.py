"""A compaction in-flight replay (#100818) must not read as the user re-sending an old prompt.

Measured 2026-09-22 (claudetriad state.db, one long autonomous session): every mid-turn compaction left a
role='user' carrier with _compressed_summary=1 and display_kind=NULL, because the restated request after the
end marker is a live view. Both display paths served that restatement as a plain user bubble, so the user saw
an earlier prompt ("Continue with C2-2.") apparently injected without their input.
"""

import agent.context_compressor as cc
import tui_gateway.server as server  # session_history bodies are rebound onto server globals
from agent.compaction_display import RESTATED_REQUEST_DISPLAY_KEY
from hermes_cli.web_routers.sessions import _project_for_display

TASK = "Continue with C2-2."
REPLAY = cc._INFLIGHT_TASK_REPLAY_HEADER + "\n\n" + TASK
CARRIER = (cc.ContextCompressor._with_summary_prefix("## Goal\nship it\n\n> continue")
           + "\n\n" + cc._SUMMARY_END_MARKER + "\n\n" + REPLAY)


def test_rest_projection_flags_the_restated_request():
    carrier, standalone = _project_for_display([
        {"role": "user", "content": CARRIER, "display_kind": None, "_compressed_summary": True},
        {"role": "user", "content": REPLAY, "display_kind": None},
    ])
    for projected in (carrier, standalone):
        assert projected["display_content"] == TASK
        assert projected["display_metadata"][RESTATED_REQUEST_DISPLAY_KEY] is True
        assert projected["role"] == "user"  # still a user turn: rewind ordinals must not shift


def test_tui_history_flags_the_restated_request_and_keeps_real_turns_plain():
    out = server._history_to_messages([
        {"role": "user", "content": TASK},
        {"role": "assistant", "content": "working"},
        {"role": "user", "content": CARRIER, "_compressed_summary": True},
    ])
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert "display_metadata" not in out[0]
    assert out[2]["text"] == TASK
    assert out[2]["display_metadata"][RESTATED_REQUEST_DISPLAY_KEY] is True


def test_multimodal_restated_request_is_flagged_and_keeps_media():
    """Codex review round 3 #4: replay messages can carry content blocks (text + image)."""
    from agent.compaction_display import RESTATED_REQUEST_DISPLAY_KEY, mark_restated_request
    from agent.context_compressor import _INFLIGHT_TASK_REPLAY_HEADER

    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    msg = {"role": "user", "content": [{"type": "text", "text": _INFLIGHT_TASK_REPLAY_HEADER + "\n\nWhat's in this?"}, image]}
    out = mark_restated_request(msg)
    assert out["content"] == [{"type": "text", "text": "What's in this?"}, image]
    assert out["display_metadata"][RESTATED_REQUEST_DISPLAY_KEY] is True
    assert msg["content"][0]["text"].startswith(_INFLIGHT_TASK_REPLAY_HEADER), "model-facing row must be unchanged"

    header_only = {"role": "user", "content": [{"type": "text", "text": _INFLIGHT_TASK_REPLAY_HEADER}, image]}
    assert mark_restated_request(header_only)["content"] == [image]
