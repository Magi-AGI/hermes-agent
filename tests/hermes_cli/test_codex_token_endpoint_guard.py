"""The test session can never reach OpenAI's real Codex token host, by any path.

The autouse ``_block_real_codex_token_endpoint`` fixture (tests/conftest.py) used to re-point one
module constant (``auth_codex.CODEX_OAUTH_TOKEN_URL``); every other copy of the URL
(``auth_constants``, ``auth``, ``auth_codex_browser``, ``web_routers.oauth``) and every hand-built
request still went out. The guard now sits at the transport: a request for that host fails before
any connection is opened, and the fixture fails the test loudly even if the caller swallowed the
error. Here the layer BELOW the guard is replaced with a recorder, so nothing can leave the machine
whether or not the guard works.
"""

import httpcore
import httpx
import pytest

from hermes_cli import auth_constants


@pytest.fixture
def below_the_guard(monkeypatch):
    reached = []

    def fake_pool_request(self, request):
        reached.append(request.url.host)
        raise httpcore.ConnectError("recorded instead of connecting")

    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", fake_pool_request)
    return reached


def test_a_request_to_the_real_token_host_never_reaches_the_connection_layer(
        below_the_guard, _block_real_codex_token_endpoint):
    with httpx.Client() as client, pytest.raises(Exception):
        client.post(auth_constants.CODEX_OAUTH_TOKEN_URL, data={"grant_type": "refresh_token"})
    assert below_the_guard == [], "the request reached the connection layer"
    assert _block_real_codex_token_endpoint, "the attempt is recorded so the fixture fails the test"
    _block_real_codex_token_endpoint.clear()  # this test made the attempt on purpose
