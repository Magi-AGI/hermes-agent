"""Direct unit tests for tui_gateway.relay_buffer.RelayBuffer.

Ported from the pre-extraction reference suite
(test-evidence/relay-overlay-reference/test_host_relay_hardening.py). The 21 relay
mechanics/pressure/coalescing tests now target RelayBuffer directly via a fake
RelayDelivery, so they run without a subprocess or supervisor. The two
supervisor-integration tests (#6 recovery, #24 shutdown, #25 wait_for_exit) live in
test_compute_host_phase1.py; the child emit-failfast test (#3) is deferred with the
compute_host emit overlay chunk.

Relay mechanics tests use `tool.result` (non-droppable, non-merged) to exercise pure
relay behavior. Coalescing/merge/supersede tests use real event types
(thinking.delta, message.delta, session.info).
"""
from __future__ import annotations

import threading
import time
from collections import deque

from tui_gateway.relay_buffer import RelayBuffer, RelayConfig


class _FakeDelivery:
    """Records every RelayDelivery call; optionally forwards rpc_sink to `sink`
    (so a test can make the WS block or throw)."""

    def __init__(self, sink=None):
        self._sink = sink
        self.rpc_calls: list = []
        self.complete_calls: list = []
        self.fail_calls: list = []  # (sid, reason, message)
        self._lock = threading.Lock()

    def rpc_sink(self, message):
        with self._lock:
            self.rpc_calls.append(message)
        if self._sink is not None:
            self._sink(message)

    def complete_turn(self, frame):
        with self._lock:
            self.complete_calls.append(frame)

    def fail_session(self, sid, reason, message):
        with self._lock:
            self.fail_calls.append((sid, reason, message))


def _mk_rb(sink=None, **cfg) -> RelayBuffer:
    config = RelayConfig(**cfg) if cfg else None
    return RelayBuffer(_FakeDelivery(sink), config=config)


def _rpc(sid: str, etype: str, seq: int | None = None, text: str | None = None) -> dict:
    payload: dict = {}
    if seq is not None:
        payload["seq"] = seq
    if text is not None:
        payload["text"] = text
    return {
        "type": "rpc",
        "sid": sid,
        "message": {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {"type": etype, "session_id": sid, "payload": payload},
        },
    }


def _etype(msg: dict) -> str:
    return str(((msg.get("params") or {}).get("type")) or "")


def _wait(cond, timeout=5.0, interval=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(interval)
    return cond()


# 1. Slow/blocking rpc_sink must not block the enqueue path.
def test_blocking_sink_does_not_block_drain():
    gate = threading.Event()
    delivered = []
    lock = threading.Lock()

    def sink(msg):
        gate.wait(10.0)
        with lock:
            delivered.append(msg)

    rb = _mk_rb(sink)
    try:
        t0 = time.monotonic()
        for i in range(200):
            rb.enqueue("rpc", _rpc("s1", "tool.result", i))  # non-droppable, 1:1
        enqueue_secs = time.monotonic() - t0
        assert enqueue_secs < 2.0, f"enqueue blocked ({enqueue_secs:.2f}s) while sink stalled"
        gate.set()
        assert _wait(lambda: len(delivered) == 200), f"only {len(delivered)}/200 delivered"
    finally:
        gate.set()
        rb.stop()


# 2. A throwing rpc_sink must not kill the relay thread.
def test_throwing_sink_survives():
    calls = []
    lock = threading.Lock()

    def sink(msg):
        with lock:
            calls.append(msg)
        if len(calls) <= 3:
            raise RuntimeError("boom (simulated WS failure)")

    rb = _mk_rb(sink)
    try:
        for i in range(10):
            rb.enqueue("rpc", _rpc("s1", "tool.result", i))
        assert _wait(lambda: len(calls) == 10), f"relay stopped after throw: {len(calls)}/10"
        assert rb._thread.is_alive(), "relay thread died on a throwing sink"
    finally:
        rb.stop()


# 4. Never-drop terminals survive pressure; only allowlisted deltas coalesce.
def test_never_drop_survives_pressure():
    gate = threading.Event()
    delivered = []
    lock = threading.Lock()

    def sink(msg):
        gate.wait(10.0)
        with lock:
            delivered.append(msg)

    rb = _mk_rb(sink)
    rb._soft_cap = 5
    rb._hard_cap = 500
    try:
        rb.enqueue("rpc", _rpc("s1", "thinking.delta", -1))
        time.sleep(0.05)
        terminals = 0
        for i in range(400):
            if i % 20 == 0:
                rb.enqueue("rpc", _rpc("s1", "message.complete", i, text="done"))
                terminals += 1
            else:
                rb.enqueue("rpc", _rpc("s1", "thinking.delta", i))
        assert rb._dropped > 0, "expected thinking.delta to be coalesced under pressure"
        gate.set()

        def _terminals():
            with lock:
                return sum(1 for m in delivered if _etype(m) == "message.complete")

        assert _wait(lambda: _terminals() == terminals), f"terminals lost: {_terminals()}/{terminals}"
    finally:
        gate.set()
        rb.stop()


# 5. Per-session ordering preserved through the relay.
def test_per_session_ordering():
    delivered = []
    lock = threading.Lock()

    def sink(msg):
        with lock:
            delivered.append(msg)

    rb = _mk_rb(sink)
    try:
        for i in range(50):
            rb.enqueue("rpc", _rpc("A", "tool.result", i))
            rb.enqueue("rpc", _rpc("B", "tool.result", i))
        assert _wait(lambda: len(delivered) == 100), f"only {len(delivered)}/100 delivered"
        for sid in ("A", "B"):
            seqs = [m["params"]["payload"]["seq"] for m in delivered if m["params"]["session_id"] == sid]
            assert seqs == list(range(50)), f"session {sid} out of order/missing: {seqs[:8]}..."
    finally:
        rb.stop()


# 7. A relay delivery exception fails the affected session (via fail_session).
def test_delivery_failure_fails_session():
    def sink(_msg):
        raise RuntimeError("WS delivery boom")

    rb = _mk_rb(sink)
    try:
        rb.enqueue("rpc", _rpc("sZ", "tool.result", 0))
        assert _wait(lambda: len(rb._delivery.fail_calls) == 1), f"session not failed: {rb._delivery.fail_calls}"
        assert rb._delivery.fail_calls[0][0] == "sZ"
        assert rb._delivery.fail_calls[0][1] == "relay_delivery_failed", rb._delivery.fail_calls[0]
        assert rb._thread.is_alive(), "relay thread died on delivery exception"
    finally:
        rb.stop()


# 8. Per-session hard cap under a blocked sink: bounded memory + explicit fail.
def test_hard_cap_bounds_and_fails():
    gate = threading.Event()

    def sink(_msg):
        gate.wait(10.0)

    rb = _mk_rb(sink)
    rb._soft_cap = 5
    rb._hard_cap = 20
    buf_len = -1
    try:
        rb.enqueue("rpc", _rpc("sH", "tool.result", 0))  # non-droppable
        time.sleep(0.05)
        for i in range(1, 500):
            rb.enqueue("rpc", _rpc("sH", "tool.result", i))
        buf = rb._buffers.get("sH")
        buf_len = len(buf) if buf is not None else 0
        assert buf_len <= rb._hard_cap + 1, f"memory not bounded: {buf_len} frames"
        assert "sH" in rb._failed_sids, "session not marked failed at hard cap"
        gate.set()
        assert _wait(lambda: len(rb._delivery.fail_calls) == 1), f"session not failed explicitly: {rb._delivery.fail_calls}"
        assert rb._delivery.fail_calls[0][1] == "relay_overflow", rb._delivery.fail_calls[0]
    finally:
        gate.set()
        rb.stop()


# 9. C4: _merge_delivery_items merges consecutive message.delta (concatenated text),
#    passes other types through, preserves order.
def test_c4_merge_consecutive_deltas():
    rb = _mk_rb()
    try:
        items = [
            ("rpc", _rpc("s", "message.delta", text="A"), 10),
            ("rpc", _rpc("s", "message.delta", text="B"), 10),
            ("rpc", _rpc("s", "tool.start"), 10),
            ("rpc", _rpc("s", "message.delta", text="C"), 10),
            ("rpc", _rpc("s", "message.delta", text="D"), 10),
            ("rpc", _rpc("s", "message.complete", text="final"), 10),
        ]
        out = rb._merge_delivery_items(items)
        # A+B -> 1, tool.start, C+D -> 1, message.complete = 4 delivered frames
        assert len(out) == 4, f"expected 4 merged frames, got {len(out)}"
        assert _etype(out[0][1]["message"]) == "message.delta"
        assert out[0][1]["message"]["params"]["payload"]["text"] == "AB", out[0]
        assert _etype(out[1][1]["message"]) == "tool.start"
        assert out[2][1]["message"]["params"]["payload"]["text"] == "CD", out[2]
        assert _etype(out[3][1]["message"]) == "message.complete"
    finally:
        rb.stop()


# 10. C1: message.delta is coalescible under pressure; message.complete survives and
#     carries the canonical final text (renderer reconciles).
def test_c1_message_delta_coalesced_complete_intact():
    gate = threading.Event()
    delivered = []
    lock = threading.Lock()

    def sink(msg):
        gate.wait(10.0)
        with lock:
            delivered.append(msg)

    rb = _mk_rb(sink)
    rb._soft_cap = 5
    rb._hard_cap = 5000
    try:
        rb.enqueue("rpc", _rpc("s1", "message.delta", text="x"))  # relay pops+blocks
        time.sleep(0.05)
        for i in range(300):
            rb.enqueue("rpc", _rpc("s1", "message.delta", i, text="x"))
        rb.enqueue("rpc", _rpc("s1", "message.complete", text="CANONICAL"))
        assert rb._dropped > 0, "expected message.delta coalescing under pressure"
        gate.set()

        def _complete_text():
            with lock:
                for m in delivered:
                    if _etype(m) == "message.complete":
                        return m["params"]["payload"].get("text")
            return None

        assert _wait(lambda: _complete_text() == "CANONICAL"), "message.complete final text not delivered intact"
    finally:
        gate.set()
        rb.stop()


# 11. C2: byte-aware GLOBAL cap bounds aggregate memory and fails largest session.
def test_c2_global_byte_cap():
    gate = threading.Event()

    def sink(_msg):
        gate.wait(10.0)

    rb = _mk_rb(sink)
    rb._soft_cap = 100000
    rb._hard_cap = 100000
    rb._global_bytes_cap = 20000  # tiny global cap for the test
    big = "z" * 2000  # ~2KB non-droppable frames
    try:
        rb.enqueue("rpc", _rpc("A", "tool.result", 0, text=big))
        time.sleep(0.05)
        for i in range(1, 60):
            rb.enqueue("rpc", _rpc("A", "tool.result", i, text=big))
            rb.enqueue("rpc", _rpc("B", "tool.result", i, text=big))
        assert rb._total_bytes <= rb._global_bytes_cap, (
            f"global bytes not bounded: {rb._total_bytes} > {rb._global_bytes_cap}"
        )
        assert len(rb._overflow_counts) >= 1, "no session failed under global cap pressure"
    finally:
        gate.set()
        rb.stop()


# 12. C3: session.info supersede — only the latest queued snapshot per session survives.
def test_c3_session_info_supersede():
    gate = threading.Event()
    delivered = []
    lock = threading.Lock()

    def sink(msg):
        gate.wait(10.0)
        with lock:
            delivered.append(msg)

    rb = _mk_rb(sink)
    try:
        rb.enqueue("rpc", _rpc("s1", "tool.result", 0))  # relay pops+blocks
        time.sleep(0.05)
        for i in range(10):
            rb.enqueue("rpc", _rpc("s1", "session.info", seq=i, text=f"snap{i}"))
            rb.enqueue("rpc", _rpc("s1", "message.complete", i, text="t"))
        buf = rb._buffers.get("s1")
        infos_buffered = sum(1 for it in (buf or []) if _etype(it[1]["message"]) == "session.info")
        assert infos_buffered == 1, f"expected 1 superseded session.info buffered, got {infos_buffered}"
        gate.set()

        def _infos_delivered():
            with lock:
                return [m for m in delivered if _etype(m) == "session.info"]

        assert _wait(lambda: len(_infos_delivered()) == 1), f"expected 1 session.info delivered, got {len(_infos_delivered())}"
        assert _infos_delivered()[0]["params"]["payload"]["seq"] == 9, "not the latest session.info"
    finally:
        gate.set()
        rb.stop()


# 13. reasoning.delta is NOT coalesced in v1 (excluded pending renderer verification).
def test_reasoning_delta_preserved():
    gate = threading.Event()
    delivered = []
    lock = threading.Lock()

    def sink(msg):
        gate.wait(10.0)
        with lock:
            delivered.append(msg)

    rb = _mk_rb(sink)
    rb._soft_cap = 5
    rb._hard_cap = 5000
    assert "reasoning.delta" not in rb._coalescible, "reasoning.delta must be excluded in v1"
    try:
        rb.enqueue("rpc", _rpc("s1", "reasoning.delta", text="r"))  # relay pops+blocks
        time.sleep(0.05)
        for i in range(50):
            rb.enqueue("rpc", _rpc("s1", "reasoning.delta", i, text="r"))
        # reasoning.delta is non-droppable + non-merged -> all buffered, none coalesced.
        buf = rb._buffers.get("s1")
        buffered = len(buf) if buf is not None else 0
        assert buffered >= 50, f"reasoning.delta was coalesced/lost: only {buffered} buffered"
    finally:
        gate.set()
        rb.stop()


# 14. Byte estimate covers nested payloads AND non-rpc frames (turn.end embeds a
#     full session_info). Deltas stay cheap.
def test_byte_estimate_covers_nested_and_turn_end():
    rb = _mk_rb()
    try:
        big_info = {
            "tools": ["t" * 500 for _ in range(20)],
            "history": {"messages": ["m" * 300 for _ in range(30)]},
        }
        turn_end = {"type": "turn.end", "sid": "s1", "request_id": "r", "session_info": big_info}
        nb = rb._approx_frame_bytes(turn_end)  # non-rpc frame with deep nesting
        assert nb > 10000, f"nested/turn.end payload undercounted: {nb} bytes"
        small = rb._approx_frame_bytes(_rpc("s1", "message.delta", text="hi"))
        assert small < 500, f"small delta overcounted: {small}"
    finally:
        rb.stop()


# 15. Inflight bytes: a popped batch held during a blocking delivery stays counted,
#     so the global cap cannot read healthy while memory is resident.
def test_inflight_bytes_counted_during_blocked_delivery():
    gate = threading.Event()
    entered = threading.Event()

    def sink(_m):
        entered.set()
        gate.wait(10.0)

    rb = _mk_rb(sink)
    big = "z" * 3000
    inflight = total = 0
    try:
        for i in range(5):
            rb.enqueue("rpc", _rpc("s1", "tool.result", i, text=big))
        assert entered.wait(3.0), "relay never entered delivery"
        with rb._cv:
            inflight = rb._inflight_bytes
            total = rb._total_bytes
        assert inflight > 0, f"inflight not counted during blocked delivery: {inflight}"
        assert total + inflight >= 3000, f"resident bytes undercounted: total={total} inflight={inflight}"
    finally:
        gate.set()
        rb.stop()


# 16. Global cap accounts INFLIGHT under pressure.
def test_global_cap_accounts_inflight_pressure():
    gate = threading.Event()
    entered = threading.Event()

    def sink(_m):
        entered.set()
        gate.wait(10.0)

    rb = _mk_rb(sink)
    rb._soft_cap = 100000
    rb._hard_cap = 100000
    rb._global_bytes_cap = 30000
    big = "z" * 3000
    queued = inflight = 0
    try:
        for i in range(8):
            rb.enqueue("rpc", _rpc("A", "tool.result", i, text=big))
        assert entered.wait(3.0), "relay never entered delivery"
        for i in range(20):
            rb.enqueue("rpc", _rpc("B", "tool.result", i, text=big))
        with rb._cv:
            queued = rb._total_bytes
            inflight = rb._inflight_bytes
        assert queued + inflight <= rb._global_bytes_cap + 4000, (
            f"resident not bounded vs inflight: queued={queued} inflight={inflight} cap={rb._global_bytes_cap}"
        )
        assert len(rb._overflow_counts) >= 1, "no session failed under inflight+queued pressure"
    finally:
        gate.set()
        rb.stop()


# 17. C4 throttle: deltas held for the window then delivered as one merged frame.
def test_c4_throttle_timer_flush():
    delivered = []
    lock = threading.Lock()

    def sink(msg):
        with lock:
            delivered.append(msg)

    rb = _mk_rb(sink)
    rb._throttle_ns = 40 * 1_000_000  # 40ms window
    deltas = []
    try:
        for i, t in enumerate(["A", "B", "C"]):
            rb.enqueue("rpc", _rpc("s1", "message.delta", i, text=t))
        assert _wait(lambda: any(_etype(m) == "message.delta" for m in delivered), 2.0), "timer never flushed"
        with lock:
            deltas = [m for m in delivered if _etype(m) == "message.delta"]
        combined = "".join(d["params"]["payload"].get("text", "") for d in deltas)
        assert combined == "ABC", f"merged text wrong: {combined!r}"
        assert len(deltas) <= 2, f"expected time-merged delivery, got {len(deltas)} frames"
    finally:
        rb.stop()


# 18. C4 ordering: a terminal frame flushes deferred deltas BEFORE itself.
def test_c4_terminal_flushes_pending_first():
    delivered = []
    lock = threading.Lock()

    def sink(msg):
        with lock:
            delivered.append((_etype(msg), (msg.get("params") or {}).get("payload", {}).get("text", "")))

    rb = _mk_rb(sink)
    rb._throttle_ns = 5_000 * 1_000_000  # huge window so ONLY a terminal flushes
    try:
        rb.enqueue("rpc", _rpc("s1", "message.delta", 0, text="hello "))
        rb.enqueue("rpc", _rpc("s1", "message.delta", 1, text="world"))
        rb.enqueue("rpc", _rpc("s1", "message.complete", text="hello world"))
        assert _wait(lambda: any(t == "message.complete" for t, _ in delivered), 2.0), "complete not delivered"
        with lock:
            types = [t for t, _ in delivered]
            delta_text = "".join(txt for t, txt in delivered if t == "message.delta")
        if "message.delta" in types:
            assert types.index("message.delta") < types.index("message.complete"), f"delta after complete: {types}"
        assert delta_text == "hello world", f"deferred deltas not flushed correctly: {delta_text!r}"
    finally:
        rb.stop()


# 19. C4 resident accounting includes pending microbatch bytes.
def test_c4_resident_includes_pending():
    rb = _mk_rb()
    rb._throttle_ns = 5_000 * 1_000_000  # huge window; deltas stay pending
    try:
        big = "p" * 2000
        for i in range(5):
            rb.enqueue("rpc", _rpc("s1", "message.delta", i, text=big))
        assert _wait(lambda: rb._pending_total > 0, 2.0), "pending bytes not tracked"
        stats = rb.stats()
        assert stats["pending_bytes"] > 0, "pending_bytes missing"
        assert stats["resident_bytes"] == stats["queued_bytes"] + stats["inflight_bytes"] + stats["pending_bytes"], stats
    finally:
        rb.stop()


# 20. C4 disabled path: window=0 -> no pending state, merge-on-delivery only.
def test_c4_throttle_disabled():
    delivered = []
    lock = threading.Lock()

    def sink(msg):
        with lock:
            delivered.append(msg)

    rb = _mk_rb(sink)
    rb._throttle_ns = 0  # disabled
    try:
        for i in range(20):
            rb.enqueue("rpc", _rpc("s1", "message.delta", i, text="x"))
        assert _wait(lambda: len(delivered) >= 1, 2.0), "nothing delivered with throttle disabled"
        time.sleep(0.1)
        assert rb._pending_total == 0, "pending state used despite disabled throttle"
        with lock:
            combined = "".join(m["params"]["payload"].get("text", "") for m in delivered if _etype(m) == "message.delta")
        assert combined == "x" * 20, f"content lost with throttle disabled: {len(combined)}/20"
    finally:
        rb.stop()


# 21. A blocked pending-flush keeps its bytes counted as inflight/resident.
def test_pending_flush_blocked_counts_as_inflight():
    gate = threading.Event()
    entered = threading.Event()

    def sink(_m):
        entered.set()
        gate.wait(10.0)

    rb = _mk_rb(sink)
    rb._throttle_ns = 5_000 * 1_000_000  # huge window; only a terminal flushes
    inflight = 0
    try:
        big = "q" * 2000
        rb.enqueue("rpc", _rpc("s1", "message.delta", 0, text=big))
        rb.enqueue("rpc", _rpc("s1", "message.delta", 1, text=big))
        rb.enqueue("rpc", _rpc("s1", "message.complete", text="done"))
        assert entered.wait(3.0), "flush delivery never entered the sink"
        with rb._cv:
            inflight = rb._inflight_bytes
        assert inflight >= 4000, f"flushed pending bytes not held as inflight during block: {inflight}"
    finally:
        gate.set()
        rb.stop()


# 22. Pending-only over-cap sheds/drains the largest pending sid.
def test_pending_only_overcap_shed():
    rb = _mk_rb()
    rb._throttle_ns = 5_000 * 1_000_000  # huge window; deltas accumulate in pending
    rb._global_bytes_cap = 20000
    resident = 0
    try:
        big = "z" * 2000
        for i in range(40):
            rb.enqueue("rpc", _rpc("A", "message.delta", i, text=big))
            rb.enqueue("rpc", _rpc("B", "message.delta", i, text=big))
        with rb._cv:
            resident = rb._total_bytes + rb._inflight_bytes + rb._pending_total
        assert resident <= rb._global_bytes_cap + 4000, (
            f"pending-only resident not bounded: {resident} > cap {rb._global_bytes_cap}"
        )
        assert rb._dropped > 0, "no pending microbatch was shed under cap pressure"
    finally:
        rb.stop()


# 23. Session overflow clears that sid's pending microbatch.
def test_overflow_clears_pending():
    rb = _mk_rb()
    try:
        with rb._cv:
            rb._pending_frames["sO"] = [_rpc("sO", "message.delta", text="p")]
            rb._pending_bytes["sO"] = 500
            rb._pending_total += 500
            rb._pending_flush_at["sO"] = time.monotonic_ns() + 10**12
            buf: deque = deque()
            for i in range(rb._hard_cap + 2):
                buf.append(("rpc", _rpc("sO", "tool.result", i), 100))
            rb._buffers["sO"] = buf
            nb = 100 * len(buf)
            rb._session_bytes["sO"] = nb
            rb._total_bytes += nb
            rb._fail_session_overflow_locked("sO", buf)  # drive the overflow directly
            pend_frames = rb._pending_frames.get("sO")
            pend_bytes = rb._pending_bytes.get("sO", 0)
            pend_total = rb._pending_total
            failed = "sO" in rb._failed_sids
        assert failed, "sO not marked failed on overflow"
        assert not pend_frames, f"pending frames not cleared on overflow: {pend_frames}"
        assert pend_bytes == 0, f"pending bytes not cleared on overflow: {pend_bytes}"
        assert pend_total == 0, f"pending total not cleared on overflow: {pend_total}"
    finally:
        rb.stop()
