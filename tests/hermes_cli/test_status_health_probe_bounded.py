"""/api/status's gateway health probe releases the route at its budget (futures audit, G10).

The probe used to run inside ``with ThreadPoolExecutor()``, whose exit joined the still-running
probe, so a gateway that accepted the connection and never answered held the route anyway.
"""

import threading
import time


def test_health_probe_returns_at_budget_while_probe_still_runs(monkeypatch):
    from hermes_cli.web_routers import status

    release = threading.Event()
    monkeypatch.setattr(status, "_probe_gateway_health", lambda: release.wait(4.0) and (True, {}))
    monkeypatch.setattr(status, "_GATEWAY_HEALTH_ROUTE_TIMEOUT", 0.2)
    start = time.monotonic()
    try:
        assert status._bounded_health_probe() == (False, None)
    finally:
        release.set()
    assert time.monotonic() - start < 2.0
