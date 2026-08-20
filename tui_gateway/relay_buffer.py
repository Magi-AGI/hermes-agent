"""Off-drain-thread delivery of compute-host WS-bound frames.

The supervisor's stdout drain thread must NEVER do WS-bound work: a WS write
that blocks or throws would wedge or kill the drain thread, the child's stdout
pipe would fill, the child's ``emit()`` would hit ``OSError EINVAL``, and the
child would crash. The drain thread only parses a frame and calls
:meth:`RelayBuffer.enqueue`; this module's dedicated relay thread owns all
WS-bound delivery, coalescing, byte accounting, and overflow policy.

LOCK INVARIANT (do not break): a delivery method (``RelayDelivery.rpc_sink`` /
``.complete_turn`` / ``.fail_session``) is NEVER invoked while holding
``self._cv``. Every delivery happens outside the condition lock (see
``_loop`` / ``_flush_pending`` / ``_safe_deliver``). Overflow / global-cap
handling under the lock only *stages* a ``relay_overflow`` marker into the
buffer and mutates byte/queue bookkeeping; the actual overflow delivery
(error event + ``fail_session``) runs later in ``_deliver``, outside the lock.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol

_Thread = threading.Thread

# Relay buffer sizing (per session). Soft cap triggers coalescing of allowlisted
# streaming deltas; hard cap fails the session rather than growing unbounded or
# wedging the drain thread.
_RELAY_SOFT_CAP_DEFAULT = 1000
_RELAY_HARD_CAP_DEFAULT = 10000
# Coalescible-under-pressure set (Phase C1). VERIFIED safe: the renderer replaces
# accumulated deltas with message.complete's full payload.text
# (completeAssistantMessage -> mergeFinalAssistantText). thinking.delta is ignored
# by the renderer. reasoning.delta is EXCLUDED (message.complete does not reconcile
# reasoning; only reasoning.available does, which is not guaranteed). message.interim
# is a separately-sealed bubble and stays never-drop. Override via
# HERMES_HOST_RELAY_COALESCIBLE (comma-separated event types).
_RELAY_COALESCIBLE_DEFAULT = "thinking.delta,message.delta"
# Phase C2: byte-aware global relay cap. 20k small deltas and 20k large session.info
# frames are very different memory risks, so bound aggregate QUEUED BYTES across all
# sessions, not just per-session frame counts. Override via HERMES_HOST_RELAY_GLOBAL_BYTES.
_RELAY_GLOBAL_BYTES_DEFAULT = 128 * 1024 * 1024  # 128 MiB
# Phase C3: full-state snapshot event types where only the LATEST queued frame per
# session matters — older un-delivered ones are superseded. session.info only in v1.
# Override via HERMES_HOST_RELAY_SUPERSEDE.
_RELAY_SUPERSEDE_DEFAULT = "session.info"
# Phase C4: rpc event types whose consecutive frames may be MERGED (payload.text
# concatenated) to cut WS fan-out — content-preserving.
_RELAY_MERGE_TYPES_DEFAULT = frozenset({"message.delta"})
# Phase C4 microbatch throttle (ms): per-session, message.delta frames are held for
# this window and merged before delivery, cutting gateway loop fan-out BEFORE a
# backlog forms. Flushed immediately before any non-delta/terminal frame for the
# session. 0 disables the throttle. Override via HERMES_HOST_RELAY_DELTA_WINDOW_MS.
_RELAY_DELTA_WINDOW_MS_DEFAULT = 50


class RelayDelivery(Protocol):
    """The WS-bound sink the relay thread delivers through. Implementations may
    block or throw; the relay isolates the drain thread from both."""

    def rpc_sink(self, message: dict) -> None: ...
    def complete_turn(self, frame: dict) -> None: ...
    def fail_session(self, sid: str, reason: str, message: str) -> None: ...


def _env_int(env: dict[str, str] | Any, key: str, default: int) -> int:
    try:
        return int(env.get(key) or default)
    except (TypeError, ValueError):
        return default


def _env_set(env: dict[str, str] | Any, key: str, default_csv: str) -> frozenset[str]:
    raw = env.get(key)
    src = default_csv if raw is None else raw
    return frozenset(t.strip() for t in src.split(",") if t.strip())


@dataclass(frozen=True)
class RelayConfig:
    """Relay sizing/policy. Construction defaults come from :meth:`from_env`; tests
    build a literal. The clamps (soft>=1, hard>soft, global>=1 MiB, window>=0) are
    applied here so ``RelayBuffer`` can trust the values."""

    soft_cap: int = _RELAY_SOFT_CAP_DEFAULT
    hard_cap: int = _RELAY_HARD_CAP_DEFAULT
    global_bytes_cap: int = _RELAY_GLOBAL_BYTES_DEFAULT
    delta_window_ms: int = _RELAY_DELTA_WINDOW_MS_DEFAULT
    coalescible: frozenset[str] = frozenset({"thinking.delta", "message.delta"})
    supersede: frozenset[str] = frozenset({"session.info"})
    merge_types: frozenset[str] = _RELAY_MERGE_TYPES_DEFAULT

    def __post_init__(self) -> None:
        # frozen dataclass: use object.__setattr__ to normalize/clamp.
        object.__setattr__(self, "soft_cap", max(1, int(self.soft_cap)))
        object.__setattr__(self, "hard_cap", max(self.soft_cap + 1, int(self.hard_cap)))
        object.__setattr__(self, "global_bytes_cap", max(1024 * 1024, int(self.global_bytes_cap)))
        object.__setattr__(self, "delta_window_ms", max(0, int(self.delta_window_ms)))

    @classmethod
    def from_env(cls, env: dict[str, str] | Any = os.environ) -> "RelayConfig":
        return cls(
            soft_cap=_env_int(env, "HERMES_HOST_RELAY_SOFT_CAP", _RELAY_SOFT_CAP_DEFAULT),
            hard_cap=_env_int(env, "HERMES_HOST_RELAY_HARD_CAP", _RELAY_HARD_CAP_DEFAULT),
            global_bytes_cap=_env_int(env, "HERMES_HOST_RELAY_GLOBAL_BYTES", _RELAY_GLOBAL_BYTES_DEFAULT),
            delta_window_ms=_env_int(env, "HERMES_HOST_RELAY_DELTA_WINDOW_MS", _RELAY_DELTA_WINDOW_MS_DEFAULT),
            coalescible=_env_set(env, "HERMES_HOST_RELAY_COALESCIBLE", _RELAY_COALESCIBLE_DEFAULT),
            supersede=_env_set(env, "HERMES_HOST_RELAY_SUPERSEDE", _RELAY_SUPERSEDE_DEFAULT),
            merge_types=_RELAY_MERGE_TYPES_DEFAULT,
        )


class RelayBuffer:
    """Per-session bounded buffers + one relay thread that delivers WS-bound frames
    off the drain thread. See the module docstring for the lock invariant.

    Config values are unpacked into MUTABLE instance attributes (``_soft_cap`` etc.)
    so callers/tests can tune a live instance without rebuilding the frozen config.
    """

    def __init__(
        self,
        delivery: RelayDelivery,
        *,
        config: RelayConfig | None = None,
        logger: logging.Logger | None = None,
        start_thread: bool = True,
    ) -> None:
        self._delivery = delivery
        self._logger = logger or logging.getLogger("tui_gateway.relay_buffer")
        cfg = config or RelayConfig.from_env()

        # Mutable, per-instance policy knobs (unpacked from the frozen config).
        self._soft_cap = cfg.soft_cap
        self._hard_cap = cfg.hard_cap
        self._global_bytes_cap = cfg.global_bytes_cap
        self._throttle_ns = cfg.delta_window_ms * 1_000_000
        self._coalescible = cfg.coalescible
        self._supersede = cfg.supersede
        self._merge_types = cfg.merge_types

        self._cv = threading.Condition(threading.Lock())
        self._buffers: dict[str, deque] = {}
        self._ready: deque = deque()
        self._ready_set: set[str] = set()
        self._stopping = False
        self._failed_sids: set[str] = set()
        self._overflow_counts: dict[str, int] = {}
        self._dropped = 0
        self._high_water = 0
        # C2 byte accounting + instrumentation.
        self._total_bytes = 0
        self._session_bytes: dict[str, int] = {}
        self._bytes_high_water = 0
        self._coalesced_by_type: dict[str, int] = {}
        self._inflight_bytes = 0
        # C4 microbatch throttle state (per session).
        self._pending_frames: dict[str, list] = {}
        self._pending_bytes: dict[str, int] = {}
        self._pending_flush_at: dict[str, int] = {}
        self._pending_total = 0
        self._batches = 0

        self._thread = _Thread(target=self._loop, name="compute-host-relay", daemon=True)
        if start_thread:
            self._thread.start()

    # ── frame introspection ────────────────────────────────────────────────

    def _frame_event_type(self, frame: dict[str, Any]) -> str:
        message = frame.get("message")
        if isinstance(message, dict):
            params = message.get("params")
            if isinstance(params, dict):
                return str(params.get("type") or "")
        return ""

    def _sid(self, frame: dict[str, Any]) -> str:
        sid = str(frame.get("sid") or "")
        if sid:
            return sid
        message = frame.get("message")
        if isinstance(message, dict):
            params = message.get("params")
            if isinstance(params, dict):
                return str(params.get("session_id") or "")
        return ""

    def _is_droppable(self, item) -> bool:
        # item = (kind, frame, nbytes)
        if item[0] != "rpc":
            return False
        return self._frame_event_type(item[1]) in self._coalescible

    @staticmethod
    def _approx_frame_bytes(frame: dict) -> int:
        # Bounded recursive string-byte estimate over the WHOLE frame: this must
        # cover non-rpc frames too — turn.end embeds a full session_info, and
        # session.info payloads are deeply nested. Iterative with a node budget so
        # the drain thread stays fast even on a pathologically large frame.
        acc = 64
        budget = 6000
        stack: list = [frame]
        while stack and budget > 0:
            v = stack.pop()
            budget -= 1
            if isinstance(v, str):
                acc += len(v)
            elif isinstance(v, dict):
                for k, vv in v.items():
                    if isinstance(k, str):
                        acc += len(k)
                    stack.append(vv)
            elif isinstance(v, (list, tuple)):
                stack.extend(v)
            else:
                acc += 16
        return acc

    # ── byte accounting (caller holds _cv) ─────────────────────────────────

    def _account_add_locked(self, sid: str, nbytes: int) -> None:
        self._total_bytes += nbytes
        self._session_bytes[sid] = self._session_bytes.get(sid, 0) + nbytes
        if self._total_bytes > self._bytes_high_water:
            self._bytes_high_water = self._total_bytes

    def _account_sub_locked(self, sid: str, nbytes: int) -> None:
        self._total_bytes = max(0, self._total_bytes - nbytes)
        remaining = self._session_bytes.get(sid, 0) - nbytes
        if remaining > 0:
            self._session_bytes[sid] = remaining
        else:
            self._session_bytes.pop(sid, None)

    def _bump_coalesced(self, etype: str, n: int = 1) -> None:
        if etype:
            self._coalesced_by_type[etype] = self._coalesced_by_type.get(etype, 0) + n

    # ── enqueue (DRAIN-THREAD entry; never blocks) ─────────────────────────

    def enqueue(self, kind: str, frame: dict[str, Any]) -> None:
        """Enqueue a WS-bound frame from the drain thread. Never blocks; applies the
        pressure policy so the drain thread can always keep reading stdout.

        ``kind`` is one of ``"rpc"`` / ``"turn_complete"`` / ``"relay_overflow"``.
        """
        sid = self._sid(frame)
        etype = self._frame_event_type(frame) if kind == "rpc" else ""
        # Estimate bytes for ALL kinds — turn.end/turn.error can embed a full
        # session_info, which must count toward the byte cap.
        nbytes = self._approx_frame_bytes(frame)
        item = (kind, frame, nbytes)
        droppable = kind == "rpc" and etype in self._coalescible
        with self._cv:
            if self._stopping:
                return
            if sid in self._failed_sids:
                # Session already failed on overflow (WS unrecoverable); drop frames
                # until the relay delivers the fail marker and clears the flag. This
                # keeps memory bounded for a permanently-stalled session.
                self._dropped += 1
                return
            buf = self._buffers.get(sid)
            if buf is None:
                buf = deque()
                self._buffers[sid] = buf
            # C3: supersede — drop older un-delivered snapshot frames of the same type.
            if etype and etype in self._supersede:
                self._supersede_locked(sid, buf, etype)
            if droppable and len(buf) >= self._soft_cap:
                # Coalesce: shed this ephemeral streaming delta under pressure.
                self._dropped += 1
                self._bump_coalesced(etype)
                return
            if not droppable and len(buf) >= self._soft_cap:
                # Make room by shedding older droppable deltas; NEVER drop terminals.
                self._evict_droppables_locked(sid, buf)
            if not droppable and len(buf) >= self._hard_cap:
                # No droppables left and at the per-session hard cap: WS unrecoverable.
                # Fail explicitly with a bounded footprint.
                self._fail_session_overflow_locked(sid, buf)
            else:
                buf.append(item)
                self._account_add_locked(sid, nbytes)
            depth = len(buf)
            if depth > self._high_water:
                self._high_water = depth
            # C2: byte-aware GLOBAL cap across all sessions. Resident memory =
            # queued + inflight (a popped batch resident while delivery blocks) +
            # pending (deferred microbatch deltas) — all three count so the cap can
            # never read healthy while memory is held elsewhere.
            if (
                self._total_bytes + self._inflight_bytes + self._pending_total
                > self._global_bytes_cap
            ):
                self._enforce_global_cap_locked()
            if sid in self._buffers and sid not in self._ready_set:
                self._ready.append(sid)
                self._ready_set.add(sid)
            self._cv.notify()

    def _supersede_locked(self, sid: str, buf: deque, etype: str) -> None:
        kept: deque = deque()
        removed = 0
        freed = 0
        for it in buf:
            if it[0] == "rpc" and self._frame_event_type(it[1]) == etype:
                removed += 1
                freed += it[2]
                continue
            kept.append(it)
        if removed:
            buf.clear()
            buf.extend(kept)
            self._account_sub_locked(sid, freed)
            self._bump_coalesced(etype, removed)

    def _evict_droppables_locked(self, sid: str, buf: deque) -> None:
        target = len(buf) - self._soft_cap + 1
        if target <= 0:
            return
        kept: deque = deque()
        removed = 0
        freed = 0
        for it in buf:
            if removed < target and self._is_droppable(it):
                removed += 1
                freed += it[2]
                self._bump_coalesced(self._frame_event_type(it[1]))
                continue
            kept.append(it)
        buf.clear()
        buf.extend(kept)
        self._dropped += removed
        self._account_sub_locked(sid, freed)

    def _fail_session_overflow_locked(self, sid: str, buf: deque) -> None:
        dropped = len(buf)
        freed = self._session_bytes.get(sid, 0)
        self._dropped += dropped
        # Drop any deferred microbatch deltas for this failing session too — else
        # stale deltas could flush AFTER relay_overflow and pending memory would
        # survive the session failure.
        if sid in self._pending_bytes or sid in self._pending_frames:
            self._shed_pending_locked(sid)
        self._failed_sids.add(sid)
        count = self._overflow_counts.get(sid, 0) + 1
        self._overflow_counts[sid] = count
        new_buf = deque()
        new_buf.append(("relay_overflow", {"sid": sid}, 0))
        self._buffers[sid] = new_buf
        self._account_sub_locked(sid, freed)
        self._logger.error(
            "compute-host relay hard cap sid=%s (%s frames dropped, overflow #%s); failing session%s",
            sid,
            dropped,
            count,
            " — repeated overflow, WS likely dead" if count > 1 else "",
        )

    def _resident_bytes_locked(self) -> int:
        return self._total_bytes + self._inflight_bytes + self._pending_total

    def _enforce_global_cap_locked(self) -> None:
        # Target RESIDENT = queued + inflight + pending <= cap. Inflight (a batch
        # resident during a blocking delivery) is not reclaimable here, so the other
        # two stages must shrink to make room.
        cap = self._global_bytes_cap
        # Tier 1: shed droppable frames across ALL queued sessions.
        for s, b in list(self._buffers.items()):
            if self._resident_bytes_locked() <= cap:
                return
            self._evict_all_droppables_locked(s, b)
        # Tier 2: fail the largest-by-bytes QUEUED session(s) explicitly.
        guard = 0
        while self._resident_bytes_locked() > cap and self._session_bytes and guard < 256:
            guard += 1
            largest = max(self._session_bytes, key=lambda k: self._session_bytes[k])
            b = self._buffers.get(largest)
            if b is None or (len(b) == 1 and b[0][0] == "relay_overflow"):
                self._session_bytes.pop(largest, None)
                continue
            self._logger.error(
                "compute-host relay GLOBAL cap: resident=%s (queued=%s inflight=%s pending=%s) > cap=%s; failing largest queued sid=%s",
                self._resident_bytes_locked(),
                self._total_bytes,
                self._inflight_bytes,
                self._pending_total,
                cap,
                largest,
            )
            self._fail_session_overflow_locked(largest, b)
            if largest not in self._ready_set:
                self._ready.append(largest)
                self._ready_set.add(largest)
        # Tier 3: pending-only pressure — shed the largest pending microbatch. Safe:
        # message.delta is coalescible and message.complete repairs the final text.
        guard = 0
        while self._resident_bytes_locked() > cap and self._pending_bytes and guard < 256:
            guard += 1
            largest = max(self._pending_bytes, key=lambda k: self._pending_bytes[k])
            self._shed_pending_locked(largest)

    def _shed_pending_locked(self, sid: str) -> None:
        nb = self._pending_bytes.pop(sid, 0)
        frames = self._pending_frames.pop(sid, None)
        self._pending_flush_at.pop(sid, None)
        self._pending_total = max(0, self._pending_total - nb)
        if frames:
            self._dropped += len(frames)
            self._bump_coalesced("message.delta", len(frames))
        self._logger.warning(
            "compute-host relay shed pending microbatch sid=%s (%s bytes) under global pressure", sid, nb
        )

    def _evict_all_droppables_locked(self, sid: str, buf: deque) -> None:
        kept: deque = deque()
        removed = 0
        freed = 0
        for it in buf:
            if self._is_droppable(it):
                removed += 1
                freed += it[2]
                self._bump_coalesced(self._frame_event_type(it[1]))
                continue
            kept.append(it)
        if removed:
            buf.clear()
            buf.extend(kept)
            self._dropped += removed
            self._account_sub_locked(sid, freed)

    # ── relay thread ───────────────────────────────────────────────────────

    def _loop(self) -> None:
        while True:
            with self._cv:
                # Wait for a ready session OR until the next pending microbatch is due.
                while not self._stopping and not self._ready:
                    timeout = self._next_flush_wait_locked()
                    if timeout is None:
                        self._cv.wait()
                    elif timeout <= 0:
                        break  # a pending microbatch is due to flush now
                    else:
                        self._cv.wait(timeout)
                if self._stopping and not self._ready:
                    return
                sid = None
                items: list = []
                freed = 0
                if self._ready:
                    sid = self._ready.popleft()
                    self._ready_set.discard(sid)
                    buf = self._buffers.pop(sid, None)
                    items = list(buf) if buf is not None else []
                    # Whole buffer leaves the queue; its bytes stay resident as INFLIGHT
                    # during the (possibly blocking) delivery below.
                    freed = self._session_bytes.pop(sid, 0)
                    self._total_bytes = max(0, self._total_bytes - freed)
                    self._inflight_bytes += freed
                now = time.monotonic_ns()
                due_sids = [s for s, t in self._pending_flush_at.items() if t <= now]
            # Flush per-session microbatches whose window elapsed (outside the lock).
            for s in due_sids:
                self._flush_pending(s)
            # Process the popped batch.
            if sid is not None:
                if self._throttle_ns > 0:
                    self._process_batch_throttled(sid, items)
                else:
                    # Throttle disabled: merge consecutive deltas on delivery (C4).
                    for kind, frame in self._merge_delivery_items(items):
                        self._safe_deliver(kind, frame)
                with self._cv:
                    self._inflight_bytes = max(0, self._inflight_bytes - freed)
            # Rate-limited instrumentation so the stall/pressure picture is observable.
            self._batches += 1
            if self._batches % 4096 == 0:
                self._logger.info(
                    "relay stats: resident_bytes=%s (queued=%s inflight=%s pending=%s) high_water=%s dropped=%s coalesced=%s overflow_sids=%s",
                    self._total_bytes + self._inflight_bytes + self._pending_total,
                    self._total_bytes,
                    self._inflight_bytes,
                    self._pending_total,
                    self._bytes_high_water,
                    self._dropped,
                    sum(self._coalesced_by_type.values()),
                    len(self._overflow_counts),
                )

    def _next_flush_wait_locked(self):
        # Seconds until the earliest pending microbatch flush, or None if none pending.
        if not self._pending_flush_at:
            return None
        earliest = min(self._pending_flush_at.values())
        return max(0.0, (earliest - time.monotonic_ns()) / 1_000_000_000)

    def _process_batch_throttled(self, sid: str, items: list) -> None:
        for it in items:
            kind, frame = it[0], it[1]
            etype = self._frame_event_type(frame) if kind == "rpc" else ""
            if kind == "rpc" and etype in self._merge_types:
                self._pending_add(sid, frame)  # defer into the per-session microbatch
            else:
                # Ordering: flush this session's deferred deltas BEFORE any non-delta /
                # terminal / ordering-sensitive frame (message.complete, message.interim,
                # tool events, errors, turn_complete, relay_overflow).
                self._flush_pending(sid)
                self._safe_deliver(kind, frame)

    def _pending_add(self, sid: str, frame: dict[str, Any]) -> None:
        nb = self._approx_frame_bytes(frame)
        with self._cv:
            self._pending_frames.setdefault(sid, []).append(frame)
            self._pending_bytes[sid] = self._pending_bytes.get(sid, 0) + nb
            self._pending_total += nb
            self._pending_flush_at.setdefault(sid, time.monotonic_ns() + self._throttle_ns)
            # Pending growth counts toward the cap; enforce so pending-only pressure
            # cannot grow unbounded.
            if self._resident_bytes_locked() > self._global_bytes_cap:
                self._enforce_global_cap_locked()
            self._cv.notify()

    def _flush_pending(self, sid: str) -> None:
        with self._cv:
            frames = self._pending_frames.pop(sid, None)
            nb = self._pending_bytes.pop(sid, 0)
            self._pending_flush_at.pop(sid, None)
            self._pending_total = max(0, self._pending_total - nb)
            # Held resident during the (possibly blocking) delivery below — keep it
            # counted as inflight so the cap can't read healthy meanwhile.
            self._inflight_bytes += nb
        try:
            if not frames:
                return
            if len(frames) == 1:
                self._safe_deliver("rpc", frames[0])
                return
            texts = []
            for fr in frames:
                payload = ((fr.get("message") or {}).get("params") or {}).get("payload")
                if isinstance(payload, dict):
                    texts.append(str(payload.get("text") or ""))
            self._bump_coalesced("message.delta", len(frames) - 1)
            self._safe_deliver("rpc", self._clone_delta_with_text(frames[0], "".join(texts)))
        finally:
            with self._cv:
                self._inflight_bytes = max(0, self._inflight_bytes - nb)

    def _safe_deliver(self, kind: str, frame: dict[str, Any]) -> None:
        try:
            self._deliver(kind, frame)
        except Exception:
            # INVARIANT: a delivery failure fails the AFFECTED session's path (never
            # silently drop a terminal); the relay thread must survive.
            fsid = self._sid(frame)
            self._logger.exception(
                "compute-host relay delivery failed kind=%s sid=%s; failing session", kind, fsid
            )
            try:
                self._delivery.fail_session(
                    fsid, "relay_delivery_failed", "compute-host WS delivery failed"
                )
            except Exception:
                self._logger.exception("compute-host relay session-fail failed sid=%s", fsid)

    def _merge_delivery_items(self, items: list) -> list:
        """Merge consecutive runs of a mergeable rpc type (message.delta) into one
        frame with concatenated payload.text (content-preserving) to cut WS fan-out.
        items are (kind, frame, nbytes); returns (kind, frame) pairs for delivery."""
        out: list = []
        run: list = []
        run_type = ""

        def flush() -> None:
            nonlocal run, run_type
            if not run:
                return
            if len(run) == 1:
                out.append(("rpc", run[0]))
            else:
                texts = []
                for fr in run:
                    payload = ((fr.get("message") or {}).get("params") or {}).get("payload")
                    if isinstance(payload, dict):
                        texts.append(str(payload.get("text") or ""))
                out.append(("rpc", self._clone_delta_with_text(run[0], "".join(texts))))
                self._bump_coalesced(run_type, len(run) - 1)
            run = []
            run_type = ""

        for it in items:
            kind, frame = it[0], it[1]
            etype = self._frame_event_type(frame) if kind == "rpc" else ""
            if kind == "rpc" and etype in self._merge_types:
                if run and etype == run_type:
                    run.append(frame)
                else:
                    flush()
                    run = [frame]
                    run_type = etype
            else:
                flush()
                out.append((kind, frame))
        flush()
        return out

    @staticmethod
    def _clone_delta_with_text(base: dict, text: str) -> dict:
        msg = dict(base.get("message") or {})
        params = dict(msg.get("params") or {})
        payload = dict(params.get("payload") or {})
        payload["text"] = text
        params["payload"] = payload
        msg["params"] = params
        return {**base, "message": msg}

    def _deliver(self, kind: str, frame: dict[str, Any]) -> None:
        if kind == "rpc":
            message = frame.get("message")
            if isinstance(message, dict):
                self._delivery.rpc_sink(message)
            return
        if kind == "turn_complete":
            self._delivery.complete_turn(frame)
            return
        if kind == "relay_overflow":
            sid = str(frame.get("sid") or "")
            try:
                self._delivery.rpc_sink(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "error",
                            "session_id": sid,
                            "payload": {
                                "message": "compute-host output backlog exceeded capacity; turn reset",
                                "reason": "relay_overflow",
                            },
                        },
                    }
                )
            except Exception:
                self._logger.exception("compute-host relay overflow emit failed sid=%s", sid)
            self._delivery.fail_session(
                sid, "relay_overflow", "compute-host output backlog exceeded capacity"
            )
            # Allow a future turn for this session to use the buffer again (it will
            # re-fail cleanly, still bounded, if the WS is still dead).
            with self._cv:
                self._failed_sids.discard(sid)
            return

    # ── observability + teardown ───────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        with self._cv:
            return {
                "queued_bytes": self._total_bytes,
                "inflight_bytes": self._inflight_bytes,
                "pending_bytes": self._pending_total,
                "resident_bytes": self._total_bytes + self._inflight_bytes + self._pending_total,
                "queued_bytes_high_water": self._bytes_high_water,
                "throttle_ms": self._throttle_ns // 1_000_000,
                "queued_frames_high_water": self._high_water,
                "dropped": self._dropped,
                "coalesced_by_type": dict(self._coalesced_by_type),
                "overflow_counts": dict(self._overflow_counts),
                "active_sessions": len(self._buffers),
                "global_bytes_cap": self._global_bytes_cap,
            }

    def stop(self, timeout: float = 2.0) -> None:
        """Idempotent teardown; safe even if construction partially failed."""
        cv = getattr(self, "_cv", None)
        if cv is None:
            return
        with cv:
            self._stopping = True
            cv.notify_all()
        t = getattr(self, "_thread", None)
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)
