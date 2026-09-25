"""A model-options payload build reads config a bounded number of times, whatever the model count.

Regression for the 2026-09-13 desktop freeze: capability resolution re-loaded config up to three
times PER MODEL (``agent.models_dev._override_for``), every session window builds a payload over
thousands of models, and all 8 RPC workers convoyed in ``load_config_readonly``. Real config and
real ``models_dev`` against a temp HERMES_HOME; only the provider listing is stubbed, to control
the model count.
"""

from __future__ import annotations

import pytest


def _rows(model_count: int) -> list[dict]:
    return [{
        "slug": "openrouter", "name": "OpenRouter", "is_current": True,
        "models": [f"lab{i % 3}/model-{i}" for i in range(model_count)],
        "total_models": model_count,
    }]


@pytest.fixture()
def payload_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: lab1/model-1\n"
        "model_overrides:\n  _default:\n    context_window: 123456\n"
        "  openrouter:\n    lab1/model-1:\n      supports_reasoning: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import config as cfgmod

    cfgmod._LOAD_CONFIG_CACHE.clear()
    yield home
    cfgmod._LOAD_CONFIG_CACHE.clear()


def _caps(payload: dict) -> dict:
    return next(r for r in payload["providers"] if r.get("slug") == "openrouter")["capabilities"]


def _build(monkeypatch, model_count: int) -> tuple[dict, int]:
    from hermes_cli import config as cfgmod
    from hermes_cli import inventory, model_switch

    monkeypatch.setattr(model_switch, "list_authenticated_providers", lambda **_kw: _rows(model_count))
    real_read = cfgmod.load_config_readonly
    reads = {"n": 0}

    def counting_read(*args, **kwargs):
        reads["n"] += 1
        return real_read(*args, **kwargs)

    monkeypatch.setattr(cfgmod, "load_config_readonly", counting_read)
    ctx = inventory.load_picker_context()
    payload = inventory.build_models_payload(ctx, capabilities=True)
    monkeypatch.setattr(cfgmod, "load_config_readonly", real_read)
    return payload, reads["n"]


def test_config_reads_per_payload_build_do_not_scale_with_model_count(payload_home, monkeypatch):
    few_payload, few_reads = _build(monkeypatch, 5)
    many_payload, many_reads = _build(monkeypatch, 300)

    assert len(_caps(many_payload)) == 300
    assert many_reads == few_reads, (
        f"config was read {few_reads}x for 5 models but {many_reads}x for 300: capability "
        "resolution re-reads config per model, which convoys the RPC pool under many windows"
    )


def test_each_build_sees_the_current_model_overrides(payload_home, monkeypatch):
    first, _ = _build(monkeypatch, 5)
    assert _caps(first)["lab1/model-1"]["reasoning"] is False

    path = payload_home / "config.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("supports_reasoning: false", "supports_reasoning: true"),
                    encoding="utf-8")
    second, _ = _build(monkeypatch, 5)
    assert _caps(second)["lab1/model-1"]["reasoning"] is True
