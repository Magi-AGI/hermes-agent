"""Client-facing projection helpers for model-only compaction carriers."""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.context_compressor import ContextCompressor, is_compaction_summary_message


_COMPACTION_INTERNAL_FIELDS = (
    "tool_calls",
    "finish_reason",
    "reasoning",
    # Provider replay/metadata fields that ride the wire on every request but are invisible to
    # ``msg["content"]``/``msg["tool_calls"]`` accounting. Codex Responses sessions in particular carry
    # ``codex_reasoning_items`` blobs of ``encrypted_content`` that can dominate the serialized session (a
    # measured 214-turn session held ~115K tokens / 27% of its payload there — #55572).
    # ``reasoning_details`` is handled separately (see ``_reasoning_details_text_chars``): its signed/base64
    # envelope is excluded from the budget, mirroring the preflight estimator's exclusion in
    # ``model_metadata._estimate_message_tokens_without_images`` (#73298).
    # An assistant turn may carry only reasoning/thinking content with no visible text (extended-thinking
    # turns, thinking-only recovery responses). Such a turn is persisted with its reasoning fields and is
    # recallable from the transcript, but dropping it here as "empty" makes it vanish from the
    # resumed/reloaded session view while the desktop's reasoning disclosure has nothing to render. Keep it
    # when it carries reasoning so the "Thinking…" block still shows. (#44022)
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
)


RESTATED_REQUEST_DISPLAY_KEY = "compaction_restated"


def mark_restated_request(message: Dict[str, Any]) -> Dict[str, Any]:
    """Flag a compaction in-flight replay (#100818) so it never reads as newly typed input.

    Compaction mid-turn restates the unfinished request after the handoff, headed by
    ``_INFLIGHT_TASK_REPLAY_HEADER`` (merged onto the carrier, or as its own row). It stays a user
    turn for the model and for rewind ordinals (``user_originated_turn_view``), so the row keeps its
    role; the display copy drops the scaffold header and carries ``display_metadata.compaction_restated``
    so clients can say it was restated, not sent again. Returns a new dict only when it changes.
    """
    from agent.context_compressor import _INFLIGHT_TASK_REPLAY_HEADER
    content = message.get("content")
    if message.get("role") != "user":
        return message
    if isinstance(content, str):
        stripped = content.lstrip()
        if not stripped.startswith(_INFLIGHT_TASK_REPLAY_HEADER):
            return message
        new_content: Any = stripped[len(_INFLIGHT_TASK_REPLAY_HEADER):].strip()
    elif isinstance(content, list):
        # Multimodal replay: the header leads the first text block; media blocks pass through.
        idx = next((i for i, b in enumerate(content)
                    if isinstance(b, dict) and b.get("type") == "text"), None)
        text = content[idx].get("text") if idx is not None else None
        if not isinstance(text, str) or not text.lstrip().startswith(_INFLIGHT_TASK_REPLAY_HEADER):
            return message
        rest = text.lstrip()[len(_INFLIGHT_TASK_REPLAY_HEADER):].strip()
        new_content = list(content)
        if rest:
            new_content[idx] = {**content[idx], "text": rest}
        else:
            del new_content[idx]
    else:
        return message
    marked = message.copy()
    marked["content"] = new_content
    metadata = message.get("display_metadata")
    marked["display_metadata"] = {**(metadata if isinstance(metadata, dict) else {}), RESTATED_REQUEST_DISPLAY_KEY: True}
    return marked


def project_compaction_message_for_display(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return authentic transcript content, or ``None`` for a pure handoff.

    Model-facing recovery history retains the complete carrier. Display
    projections instead remove the handoff, inherited tool state, and internal
    reasoning while preserving any real prior-tail content or live user ask
    embedded in the carrier; an in-flight replay is flagged as restated.
    """
    if not isinstance(message, dict):
        return None
    from agent.system_turn_display import tag_system_injected_user_turn

    if not is_compaction_summary_message(message):
        projected = message.copy()
        # Rows written before write-time tagging: type them here so display and rewind counting agree.
        tag_system_injected_user_turn(projected)
        return mark_restated_request(projected)

    projected = ContextCompressor._strip_context_summary_handoff_message(message)
    if projected is None:
        return None

    projected = projected.copy()
    for key in _COMPACTION_INTERNAL_FIELDS:
        projected.pop(key, None)
    projected.pop("display_kind", None)
    # A system notice preserved inside the carrier is still a notice once unwrapped.
    tag_system_injected_user_turn(projected)
    return mark_restated_request(projected)
