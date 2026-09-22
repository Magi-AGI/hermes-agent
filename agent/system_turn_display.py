"""Type system-injected user rows at write time so UIs never paint them as the user's own words.

Hermes rides several kinds of scaffolding on ``role='user'`` (the provider contract has nowhere else
to put them): the max-iterations wrap-up request, background-process completion notices, the todo
snapshot re-injected after compaction, the no-human-turn compaction continuation marker, and the
recovery nudges. They were persisted with ``display_kind=NULL``, so the desktop rendered them as
user bubbles — indistinguishable from typed input in the stored row.

Tagging is display-only: role and content are untouched and ``display_kind``/``display_metadata``
are popped from every outgoing API copy (``turn_context._build_api_messages``), so the model's
transcript and the prompt cache are unchanged. The in-memory message is tagged as well as the row,
because ``tui_gateway.methods_prompt._mem_db_pair_agrees`` requires memory and durable rows to agree
on display-marker status. Compaction handoffs are not handled here: standalone ones are already
persisted ``hidden`` and merged carriers hold real user text (see ``compaction_display``).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

SYSTEM_NOTICE_DISPLAY_KIND = "system_notice"


def _notice_table() -> "tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]":
    """``(notice, exact_texts, prefixes)`` rows; built lazily to avoid an import cycle with the loop."""
    from agent.context_compressor import (
        _BACKGROUND_PROCESS_NOTIFICATION_PREFIX, _LEGACY_COMPRESSION_CONTINUATION_USER_CONTENT,
        COMPRESSION_CONTINUATION_USER_CONTENT, MAX_ITERATIONS_SUMMARY_REQUEST,
    )
    from tools.todo_tool import TODO_INJECTION_HEADER
    return (
        ("max_iterations", (MAX_ITERATIONS_SUMMARY_REQUEST,), ()),
        ("background_process", (), (_BACKGROUND_PROCESS_NOTIFICATION_PREFIX,)),
        ("todo_snapshot", (), (TODO_INJECTION_HEADER + "\n", TODO_INJECTION_HEADER)),
        ("compaction_continuation",
         (COMPRESSION_CONTINUATION_USER_CONTENT, _LEGACY_COMPRESSION_CONTINUATION_USER_CONTENT), ()),
    )


def classify_system_injected_user_turn(message: Any) -> Optional[Dict[str, Any]]:
    """``{"display_kind": ..., "display_metadata": {"notice": ...}}`` for a purely synthetic user row, else None.

    Delegates the synthetic/real decision to ``ContextCompressor._is_synthetic_compression_user_turn`` (the
    authoritative content-marker test) so display typing can never disagree with compaction's view of
    which rows are human input. Rows that already carry a ``display_kind`` and compaction handoffs are
    left alone.
    """
    if not isinstance(message, dict) or message.get("role") != "user" or message.get("display_kind"):
        return None
    from agent.context_compressor import (
        ContextCompressor, _content_text_for_contains, is_compaction_summary_message,
    )
    if is_compaction_summary_message(message) or not ContextCompressor._is_synthetic_compression_user_turn(message):
        return None
    text = _content_text_for_contains(message.get("content")).strip()
    notice = "recovery_nudge"
    for name, exact, prefixes in _notice_table():
        if text in exact or (prefixes and text.startswith(prefixes)):
            notice = name
            break
    return {"display_kind": SYSTEM_NOTICE_DISPLAY_KIND, "display_metadata": {"notice": notice}}


def tag_system_injected_user_turn(message: Any) -> bool:
    """Stamp the display type onto *message* in place; True when a tag was applied."""
    tag = classify_system_injected_user_turn(message)
    if tag is None:
        return False
    message["display_kind"] = tag["display_kind"]
    if not message.get("display_metadata"):
        message["display_metadata"] = tag["display_metadata"]
    return True
