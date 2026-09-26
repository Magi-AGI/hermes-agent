"""Deterministic cross-thread cancellation tests for compression aux transports."""

from __future__ import annotations

import contextvars
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from agent import auxiliary_client as aux

# Hang guard for waits that end on an event (1 s ones missed thread start-up on a loaded runner).
_HANG_GUARD = 30.0
# A cancelled silent request must wake well before its 30 s request timeout; the old 0.75 s
# bound measured scheduling on a loaded runner rather than that. The fake transports never
# return on their own, so only cancellation can wake the request inside this bound.
_PROMPT_CANCEL_S = 10.0

# Every fake transport stays silent until closed (by the code under test) or released at teardown:
# one that returned on its own after a few seconds let a request that ignored cancellation, or a
# stream no watchdog closed, pass the wider bounds.
_SILENT_FAKES: list[Any] = []


@pytest.fixture(autouse=True)
def _settle_threads_started_by_the_test():
    """On every exit path: release each fake and test gate, cancel watchdog timers that have not
    fired, then join every thread the test started (owners, provider workers, timers) within the
    hang guard and fail on any survivor. A failed assertion mid-test otherwise left owners and a
    watchdog blocked on unreleased gates after teardown."""
    before = set(threading.enumerate())
    yield
    while _SILENT_FAKES:
        _SILENT_FAKES.pop().release()
    started = [t for t in threading.enumerate() if t not in before]
    for thread in started:
        if isinstance(thread, threading.Timer):
            thread.cancel()  # a pending one never runs; a running one finishes once released
    for thread in started:
        thread.join(timeout=_HANG_GUARD)
    survivors = [t.name for t in started if t.is_alive()]
    assert not survivors, f"threads outlived teardown: {survivors}"


class _Silent:
    """Blocks until ``close()`` (recording which thread closed it) or teardown ``release()``."""

    def __init__(self) -> None:
        self.closed = threading.Event()
        self.closed_by: str | None = None
        _SILENT_FAKES.append(self)

    def block(self) -> None:
        self.closed.wait()

    def close(self) -> None:
        if not self.closed.is_set():
            self.closed_by = threading.current_thread().name
        self.closed.set()

    def release(self) -> None:
        self.closed.set()


class _CodexWatchdog:
    """Captures Codex stream guards, records the threads their watchdog fires on, and expires a
    guard on the test's cue. The requests carry a long timeout so nothing expires by itself (an
    80 ms one could pass before stream entry, on the owner's own deadline check as well as on the
    timer); ``expire()`` moves the deadline to now and re-arms the guard's real timer at zero
    delay, so the real watchdog callback runs on its real timer thread."""

    def __init__(self, monkeypatch) -> None:
        self.guards: list[Any] = []
        self.fired: list[str] = []
        real_start, real_fire = aux._CodexStreamGuard.start, aux._CodexStreamGuard._watchdog_fire

        def start(guard) -> None:
            self.guards.append(guard)
            real_start(guard)

        def fire(guard) -> None:
            self.fired.append(threading.current_thread().name)
            real_fire(guard)

        monkeypatch.setattr(aux._CodexStreamGuard, "start", start)
        monkeypatch.setattr(aux._CodexStreamGuard, "_watchdog_fire", fire)

    def expire(self, index: int = 0) -> None:
        """Only brings forward a watchdog production armed and has not fired: it never supplies a
        missing one (Codex r2: expiry that armed its own timer passed with arming removed)."""
        guard = self.guards[index]
        timer = guard._timer
        assert timer is not None and timer.is_alive() and not timer.finished.is_set(), (
            "production armed no pending watchdog for this stream")
        with guard._deadline_lock:
            guard._progress_deadline = guard.hard_deadline = time.monotonic()
        timer.cancel()
        guard._arm_timer(0.0)


class _BlockingStream(_Silent):
    def __init__(self, started: threading.Event) -> None:
        super().__init__()
        self.started = started

    def __iter__(self):
        self.started.set()
        self.block()
        raise RuntimeError("transport closed")

    def get_final_message(self) -> Any:
        self.started.set()
        self.block()
        raise RuntimeError("transport closed")


class _GenericCompletions:
    def __init__(self, stream: _BlockingStream) -> None:
        self.stream = stream

    def create(self, **_kwargs: Any) -> _BlockingStream:
        return self.stream


class _GenericClient:
    def __init__(self, stream: _BlockingStream) -> None:
        self.chat = SimpleNamespace(completions=_GenericCompletions(stream))
        self.stream = stream
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()
        self.stream.close()


class _CodexResponses:
    def __init__(self, stream: _BlockingStream) -> None:
        self.stream = stream

    def create(self, **_kwargs: Any) -> _BlockingStream:
        return self.stream


class _CodexRealClient:
    def __init__(self, stream: _BlockingStream) -> None:
        self.responses = _CodexResponses(stream)
        self.api_key = "test"
        self.base_url = "https://example.test/codex"
        self.stream = stream
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()
        self.stream.close()


class _AnthropicStreamContext:
    def __init__(self, stream: _BlockingStream) -> None:
        self.stream = stream

    def __enter__(self) -> _BlockingStream:
        return self.stream

    def __exit__(self, *_args: Any) -> None:
        self.stream.close()


class _AnthropicMessages:
    def __init__(self, stream: _BlockingStream) -> None:
        self.stream_obj = stream

    def stream(self, **_kwargs: Any) -> _AnthropicStreamContext:
        return _AnthropicStreamContext(self.stream_obj)


class _AnthropicRealClient:
    def __init__(self, stream: _BlockingStream) -> None:
        self.messages = _AnthropicMessages(stream)
        self.stream = stream
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()
        self.stream.close()


class _BedrockRuntimeClient:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release
        self.closed = threading.Event()
        _SILENT_FAKES.append(SimpleNamespace(release=release.set))

    def converse(self, **_kwargs: Any) -> dict[str, Any]:
        self.started.set()
        self.release.wait()
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": "cancelled response"}],
                }
            },
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            "stopReason": "end_turn",
        }

    def close(self) -> None:
        self.closed.set()


def _cancel_silent_request(
    client: Any,
    started: threading.Event,
    invoke: Callable[[Any], Any],
) -> tuple[BaseException, float]:
    cancel_event = threading.Event()
    result: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            with aux.aux_interrupt_protection(cancel_event=cancel_event):
                invoke(client)
        except BaseException as exc:
            result["exc"] = exc

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    # Thread start-up on a loaded CI runner can exceed 1 s; the bound is only "eventually entered the transport".
    assert started.wait(timeout=_HANG_GUARD), "request never entered its silent transport"
    cancelled_at = time.monotonic()
    cancel_event.set()
    worker.join(timeout=_PROMPT_CANCEL_S)
    elapsed = time.monotonic() - cancelled_at
    assert not worker.is_alive(), "explicit cancellation did not wake the silent request"
    return result["exc"], elapsed


def _invoke_generic(client: Any) -> Any:
    return aux._relay_sync_completion(
        client,
        {"model": "test", "messages": [], "timeout": 30},
        create=lambda request: aux._create_with_progress(
            client, request, "compression", force_stream=True
        ),
    )


def test_protected_silent_provider_is_isolated_and_raises_frozen_explicit_cancel() -> None:
    started = threading.Event()
    stream = _BlockingStream(started)
    client = _GenericClient(stream)

    exc, elapsed = _cancel_silent_request(client, started, _invoke_generic)

    assert isinstance(exc, aux.AuxiliaryExplicitCancellation)
    assert exc.cause == "explicit_host_cancel"
    assert not client.closed.is_set()
    assert elapsed < _PROMPT_CANCEL_S
    stream.close()  # release the bounded daemon provider worker


def test_codex_silent_stream_is_isolated_without_closing_shared_client() -> None:
    started = threading.Event()
    stream = _BlockingStream(started)
    real_client = _CodexRealClient(stream)
    client = aux.CodexAuxiliaryClient(real_client, "gpt-test")

    exc, elapsed = _cancel_silent_request(client, started, _invoke_generic)

    assert isinstance(exc, aux.AuxiliaryExplicitCancellation)
    assert not real_client.closed.is_set()
    assert elapsed < _PROMPT_CANCEL_S
    stream.close()


def test_cancelled_codex_orphan_timeout_preserves_cached_shared_client(monkeypatch) -> None:
    """A cancelled Codex worker's delayed timer owns only its event stream."""
    owner_started = threading.Event()
    # The orphan's watchdog expires only after the concurrent call below: "still armed" and
    # "then it fires and closes only its stream" are ordered by the test, not by a timer.
    watchdog = _CodexWatchdog(monkeypatch)

    class _SilentOwnerStream(_Silent):
        def __iter__(self):
            owner_started.set()
            self.block()
            raise RuntimeError("owner stream closed")

    class _SuccessStream:
        def __iter__(self):
            message = SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="ok")],
            )
            return iter(
                [
                    SimpleNamespace(type="response.output_item.done", item=message),
                    SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            status="completed", id="success", usage=None
                        ),
                    ),
                ]
            )

        def close(self) -> None:
            pass

    owner_stream = _SilentOwnerStream()

    class _SharedResponses:
        def __init__(self, real_client: Any) -> None:
            self.real_client = real_client

        def create(self, **kwargs: Any) -> Any:
            if self.real_client.closed.is_set():
                raise RuntimeError("shared client was closed")
            if kwargs["model"] == "owner":
                return owner_stream
            return _SuccessStream()

    class _SharedRealClient:
        def __init__(self) -> None:
            self.closed = threading.Event()
            self.api_key = "test"
            self.base_url = "https://example.test/codex"
            self.responses = _SharedResponses(self)

        def close(self) -> None:
            self.closed.set()
            owner_stream.close()

    real_client = _SharedRealClient()
    wrapper = aux.CodexAuxiliaryClient(real_client, "gpt-test")
    cache_key = ("openai-codex", False, None, None, None)
    cancel_event = threading.Event()
    owner_outcome: dict[str, BaseException] = {}

    def _run_owner() -> None:
        try:
            with aux.aux_interrupt_protection(cancel_event=cancel_event):
                aux._relay_sync_completion(
                    wrapper,
                    {"model": "owner", "messages": [], "timeout": _HANG_GUARD},
                )
        except BaseException as exc:
            owner_outcome["exc"] = exc

    with aux._client_cache_lock:
        aux._client_cache.clear()
        aux._client_cache[cache_key] = (wrapper, "gpt-test", None)
    owner = threading.Thread(target=_run_owner, daemon=True)
    try:
        owner.start()
        assert owner_started.wait(timeout=_HANG_GUARD)
        cancel_event.set()
        owner.join(timeout=_HANG_GUARD)
        assert not owner.is_alive()
        assert isinstance(owner_outcome["exc"], aux.AuxiliaryExplicitCancellation)
        # A real frontend clears the reusable host Event when the next turn
        # starts. The orphan must retain a frozen per-attempt cancellation cause.
        cancel_event.clear()

        # A second user can use the shared client while the cancelled provider
        # worker is still orphaned and its total-timeout timer is still armed.
        assert not owner_stream.closed.is_set()
        concurrent = aux._relay_sync_completion(
            wrapper,
            {"model": "concurrent", "messages": [], "timeout": _HANG_GUARD},
        )
        assert concurrent.choices[0].message.content == "ok"

        # Let the orphan's real adapter timer fire. It may close the attempt's
        # event stream to wake that worker, but never the process-shared client.
        watchdog.expire()
        assert owner_stream.closed.wait(timeout=_HANG_GUARD)
        assert watchdog.fired and owner_stream.closed_by in watchdog.fired, (watchdog.fired, owner_stream.closed_by)
        time.sleep(0.03)
        assert not real_client.closed.is_set()
        with aux._client_cache_lock:
            assert aux._client_cache[cache_key][0] is wrapper

        successive = aux._relay_sync_completion(
            wrapper,
            {"model": "successive", "messages": [], "timeout": _HANG_GUARD},
        )
        assert successive.choices[0].message.content == "ok"
    finally:
        owner_stream.close()
        with aux._client_cache_lock:
            aux._client_cache.clear()


@pytest.mark.parametrize("winner", ["timeout", "cancel"])
def test_codex_timeout_and_explicit_cancel_have_one_linearized_outcome(
    winner: str, monkeypatch,
) -> None:
    """Timeout and explicit cancel can never produce a mixed owner/cleanup result."""
    timer_read_started = threading.Event()
    allow_timer_read_return = threading.Event()
    _SILENT_FAKES.append(SimpleNamespace(release=allow_timer_read_return.set))  # teardown releases the gate
    request_cancelled = threading.Event()
    stream_started = threading.Event()
    # The watchdog expires only once the provider is inside the stream (timeout case: the seam
    # under test; an earlier expiry held the decision lock the provider needs to get there) or
    # once the cancel is in (cancel case: the timer then finds the cancel and closes only the
    # attempt stream).
    watchdog = _CodexWatchdog(monkeypatch)

    class _RacingCancelSource:
        def is_set(self) -> bool:
            if winner == "timeout" and threading.current_thread().name.startswith(
                "Thread-"
            ):
                # Take the timer's false snapshot, then hold it at the exact seam
                # where the historical implementation could race owner polling.
                was_set = request_cancelled.is_set()
                timer_read_started.set()
                assert allow_timer_read_return.wait(timeout=_HANG_GUARD)
                return was_set
            return request_cancelled.is_set()

    class _SilentStream(_Silent):
        def __iter__(self):
            stream_started.set()
            self.block()
            raise RuntimeError("stream closed")

    stream = _SilentStream()

    class _RealClient:
        def __init__(self) -> None:
            self.api_key = "test"
            self.base_url = "https://example.test/codex"
            self.responses = SimpleNamespace(create=lambda **_kwargs: stream)
            self.closed = threading.Event()

        def close(self) -> None:
            self.closed.set()
            stream.close()

    real_client: Any = _RealClient()
    wrapper = aux.CodexAuxiliaryClient(real_client, "gpt-test")
    owner_outcome: dict[str, BaseException] = {}

    def _run_owner() -> None:
        try:
            with aux.aux_interrupt_protection(cancel_event=_RacingCancelSource()):
                aux._relay_sync_completion(
                    wrapper,
                    {"model": "owner", "messages": [], "timeout": _HANG_GUARD},
                )
        except BaseException as exc:
            owner_outcome["exc"] = exc

    owner = threading.Thread(target=_run_owner, name="race-owner", daemon=True)
    owner.start()
    assert stream_started.wait(timeout=_HANG_GUARD)
    if winner == "timeout":
        watchdog.expire()
        assert timer_read_started.wait(timeout=_HANG_GUARD)
        request_cancelled.set()
        allow_timer_read_return.set()
    else:
        request_cancelled.set()
        watchdog.expire()
    owner.join(timeout=_HANG_GUARD)

    assert not owner.is_alive()
    if winner == "timeout":
        assert real_client.closed.is_set()
        assert watchdog.fired, "the watchdog never fired"
        assert isinstance(owner_outcome["exc"], TimeoutError)
        assert not isinstance(owner_outcome["exc"], aux.AuxiliaryExplicitCancellation)
    else:
        assert isinstance(owner_outcome["exc"], aux.AuxiliaryExplicitCancellation)
        assert stream.closed.wait(timeout=_HANG_GUARD), "cancelled timer did not wake its stream"
        assert watchdog.fired and stream.closed_by in watchdog.fired, (watchdog.fired, stream.closed_by)
        assert not real_client.closed.is_set()


def test_anthropic_silent_stream_is_isolated_without_closing_shared_client() -> None:
    started = threading.Event()
    stream = _BlockingStream(started)
    real_client = _AnthropicRealClient(stream)
    client = aux.AnthropicAuxiliaryClient(
        real_client,
        "claude-test",
        "test-key",
        "https://api.anthropic.test",
    )

    exc, elapsed = _cancel_silent_request(client, started, _invoke_generic)

    assert isinstance(exc, aux.AuxiliaryExplicitCancellation)
    assert not real_client.closed.is_set()
    assert elapsed < _PROMPT_CANCEL_S
    stream.close()


def test_cancelled_attempt_does_not_close_or_fail_concurrent_shared_client_call(
    monkeypatch,
) -> None:
    a_started = threading.Event()
    a_release = threading.Event()
    b_started = threading.Event()
    b_release = threading.Event()
    closed = threading.Event()

    class _SharedCompletions:
        def create(self, **kwargs: Any) -> Any:
            if kwargs["model"] == "session-a":
                a_started.set()
                a_release.wait()
            else:
                b_started.set()
                b_release.wait()
            if closed.is_set():
                raise RuntimeError("shared client was closed")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_SharedCompletions()),
        close=lambda: closed.set(),
    )
    cancel_event = threading.Event()
    outcomes: dict[str, Any] = {}
    evictions: list[Any] = []
    monkeypatch.setattr(
        aux, "_evict_cached_client_instance", lambda value: evictions.append(value)
    )

    def _session_a() -> None:
        try:
            with aux.aux_interrupt_protection(cancel_event=cancel_event):
                aux._relay_sync_completion(
                    client, {"model": "session-a", "messages": [], "timeout": 30}
                )
        except BaseException as exc:
            outcomes["a"] = exc

    def _session_b() -> None:
        try:
            outcomes["b"] = aux._relay_sync_completion(
                client, {"model": "session-b", "messages": [], "timeout": 30}
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            outcomes["b"] = exc

    _SILENT_FAKES.extend(SimpleNamespace(release=event.set) for event in (a_release, b_release))
    a_thread = threading.Thread(target=_session_a, daemon=True)
    b_thread = threading.Thread(target=_session_b, daemon=True)
    try:
        a_thread.start()
        b_thread.start()
        assert a_started.wait(timeout=_HANG_GUARD)
        assert b_started.wait(timeout=_HANG_GUARD)
        cancel_event.set()
        a_thread.join(timeout=_HANG_GUARD)
        assert not a_thread.is_alive()
        assert isinstance(outcomes["a"], aux.AuxiliaryExplicitCancellation)
        assert not closed.is_set()
        assert evictions == []
        b_release.set()
        b_thread.join(timeout=_HANG_GUARD)
        assert not b_thread.is_alive()
        assert not isinstance(outcomes["b"], BaseException)
        assert outcomes["b"].choices[0].message.content == "ok"
    finally:
        a_release.set()
        b_release.set()
        for session in (a_thread, b_thread):
            if session.ident is not None:
                session.join(timeout=_HANG_GUARD)


def test_bedrock_silent_nonstream_request_is_isolated_without_close_wakeup() -> None:
    from agent.bedrock_adapter import _bedrock_runtime_client_cache, reset_client_cache

    started = threading.Event()
    release = threading.Event()
    runtime_client = _BedrockRuntimeClient(started, release)
    reset_client_cache()
    _bedrock_runtime_client_cache["us-test-1"] = runtime_client
    client = aux.BedrockAuxiliaryClient("us-test-1", "bedrock-test")
    try:
        exc, elapsed = _cancel_silent_request(client, started, _invoke_generic)
    finally:
        release.set()
        reset_client_cache()

    assert isinstance(exc, aux.AuxiliaryExplicitCancellation)
    assert not runtime_client.closed.is_set()
    assert elapsed < _PROMPT_CANCEL_S


def test_unprotected_sync_completion_stays_on_calling_thread() -> None:
    caller = threading.get_ident()
    observed: list[int] = []
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: (
                    observed.append(threading.get_ident()),
                    SimpleNamespace(choices=[]),
                )[1]
            )
        )
    )

    aux._relay_sync_completion(client, {"model": "test", "messages": []})

    assert observed == [caller]


def test_isolated_provider_worker_inherits_protection_and_progress_hook() -> None:
    caller = threading.get_ident()
    cancel_event = threading.Event()
    progress: list[str] = []
    observed: dict[str, Any] = {}

    def _create(**_kwargs: Any) -> Any:
        observed["thread"] = threading.get_ident()
        observed["protected"] = aux._aux_interrupt_protected()
        aux._notify_aux_progress()
        return SimpleNamespace(choices=[])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )
    with aux.aux_progress_hook(lambda: progress.append("tick")), aux.aux_interrupt_protection(
        cancel_event=cancel_event
    ):
        aux._relay_sync_completion(client, {"model": "test", "messages": []})

    assert observed["protected"] is True
    assert observed["thread"] != caller
    # The hook must reach the isolated worker thread; the seam wrapper adds its own dispatch ticks.
    assert "tick" in progress


def test_isolated_provider_worker_inherits_caller_contextvars() -> None:
    from tools.approval import get_current_session_key
    from tools.approval_context import reset_current_session_key, set_current_session_key

    arbitrary = contextvars.ContextVar("isolated-provider-test", default="missing")
    arbitrary_token = arbitrary.set("caller-value")
    session_token = set_current_session_key("session-from-caller")
    observed: dict[str, str] = {}
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: (
                    observed.update(
                        arbitrary=arbitrary.get(),
                        session_key=get_current_session_key(),
                    ),
                    SimpleNamespace(choices=[]),
                )[1]
            )
        )
    )
    try:
        with aux.aux_interrupt_protection(cancel_event=threading.Event()):
            aux._relay_sync_completion(client, {"model": "test", "messages": []})
    finally:
        reset_current_session_key(session_token)
        arbitrary.reset(arbitrary_token)

    assert observed == {
        "arbitrary": "caller-value",
        "session_key": "session-from-caller",
    }


def test_hard_cancel_wins_when_provider_result_is_published_in_same_race() -> None:
    cancel_event = threading.Event()

    def _create(**_kwargs: Any) -> Any:
        cancel_event.set()
        return SimpleNamespace(choices=[])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )
    with aux.aux_interrupt_protection(cancel_event=cancel_event):
        with pytest.raises(aux.AuxiliaryExplicitCancellation):
            aux._relay_sync_completion(client, {"model": "test", "messages": []})


def test_unrelated_interrupted_error_is_not_reclassified_as_explicit_cancel() -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: (_ for _ in ()).throw(
                    InterruptedError("provider syscall interrupted")
                )
            )
        ),
        close=lambda: None,
    )

    with aux.aux_interrupt_protection(cancel_event=threading.Event()):
        with pytest.raises(InterruptedError, match="provider syscall interrupted") as caught:
            aux._relay_sync_completion(client, {"model": "test", "messages": []})

    assert not isinstance(caught.value, aux.AuxiliaryExplicitCancellation)
