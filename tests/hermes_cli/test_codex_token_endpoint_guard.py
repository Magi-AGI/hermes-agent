"""The test session can never reach OpenAI's real Codex token host, by any path, at any time.

The guard used to be a function-scoped fixture re-pointing one module constant: other copies of the
URL, collection-time imports, module/session fixtures (they run before function fixtures) and
Python child processes (they do not inherit monkeypatches) all went out. It is now installed when
``tests/conftest.py`` is imported (before collection) and, through ``tests/netguard/sitecustomize.py``
on the children's ``PYTHONPATH``, in every child interpreter. In each test below the layer BELOW
the guard is replaced with a recorder, so nothing can leave the machine whether or not the guard
works — the recorder proves nothing reached it.
"""

import json
import os
import subprocess
import sys

import httpcore
import httpx
import pytest

from hermes_cli import auth_constants


def _attempt_with_recorder():
    """One POST to the real token URL with the connection layer replaced by a recorder."""
    reached = []
    real = httpcore.ConnectionPool.handle_request

    def recorder(self, request):
        reached.append(request.url.host)
        raise httpcore.ConnectError("recorded instead of connecting")

    httpcore.ConnectionPool.handle_request = recorder
    try:
        with httpx.Client() as client:
            client.post(auth_constants.CODEX_OAUTH_TOKEN_URL, data={"grant_type": "refresh_token"})
    except Exception:
        pass
    finally:
        httpcore.ConnectionPool.handle_request = real
    return reached


_AT_COLLECTION = _attempt_with_recorder()  # runs while this module is imported by the collector


@pytest.fixture(scope="module")
def module_scoped_attempt():
    return _attempt_with_recorder()


def test_a_request_to_the_real_token_host_never_reaches_the_connection_layer(
        module_scoped_attempt, _block_real_codex_token_endpoint):
    assert _AT_COLLECTION == [], "a collection-time request reached the connection layer"
    assert module_scoped_attempt == [], "a module-scoped fixture's request reached the connection layer"
    assert _attempt_with_recorder() == [], "a test's request reached the connection layer"
    assert _block_real_codex_token_endpoint, "attempts are recorded so the fixture fails the test"
    _block_real_codex_token_endpoint.clear()  # this test made them on purpose


def test_a_child_python_is_guarded_too():
    child = r'''
import json, httpcore, httpx
reached = []
def recorder(self, request):
    reached.append(request.url.host.decode() if isinstance(request.url.host, bytes) else request.url.host)
    raise httpcore.ConnectError("recorded instead of connecting")
httpcore.ConnectionPool.handle_request = recorder
error = None
try:
    httpx.Client().post("https://auth.openai.com/oauth/token", data={"grant_type": "refresh_token"})
except Exception as exc:
    error = type(exc).__name__
print(json.dumps({"reached": reached, "error": error}))
'''
    out = subprocess.run([sys.executable, "-c", child], env=dict(os.environ), capture_output=True, text=True,
                         timeout=120)
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["reached"] == [], f"the child's request reached the connection layer: {out.stderr[-500:]}"
    assert result["error"] == "ConnectionRefusedError"
