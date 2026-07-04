"""R7: hermes_cli.main must strip the Desktop ownership marker before argparse.

Desktop spawns its backend with ``--hermes-desktop-owner=<nonce>`` so the OS
command line carries a non-secret ownership signal for the Electron reaper. The
CLI must tolerate it: stripping it in-process keeps argparse happy while the
OS-level command line (fixed at process creation) still shows the marker.
"""

import sys

import hermes_cli.main as main_mod


def _run_strip(argv):
    original = sys.argv
    sys.argv = list(argv)
    try:
        main_mod._strip_desktop_owner_marker()
        return list(sys.argv)
    finally:
        sys.argv = original


def test_strips_equals_form_and_preserves_other_args():
    result = _run_strip(
        ["hermes", "--profile", "claudetriad", "--hermes-desktop-owner=nonce-1", "dashboard", "--port", "0"]
    )
    assert result == ["hermes", "--profile", "claudetriad", "dashboard", "--port", "0"]


def test_strips_separate_value_form():
    result = _run_strip(
        ["hermes", "--hermes-desktop-owner", "nonce-2", "dashboard", "--host", "127.0.0.1"]
    )
    assert result == ["hermes", "dashboard", "--host", "127.0.0.1"]


def test_no_marker_is_a_no_op():
    argv = ["hermes", "--profile", "claudetriad", "dashboard", "--port", "0"]
    assert _run_strip(argv) == argv


def test_bare_marker_without_value_is_removed():
    # A trailing marker with no value token must still be dropped cleanly.
    result = _run_strip(["hermes", "dashboard", "--hermes-desktop-owner"])
    assert result == ["hermes", "dashboard"]


def test_marker_uses_the_same_flag_electron_emits():
    # Contract check: the Node backendOwnerArg() builds `--hermes-desktop-owner=`,
    # which is exactly the flag this stripper recognizes.
    assert main_mod._DESKTOP_OWNER_MARKER == "--hermes-desktop-owner"
