"""Every command a Codex restore / registry refusal tells the user to run exists in THIS release.

A restore refusal told the user to run ``hermes auth migrate --rebuild``, a stage-2 flag the stage-1
CLI does not have. The commands named in these messages must parse with the real ``auth`` argparse
tree. Fake paths only; nothing is executed.
"""

import argparse
import re

import pytest


def _auth_cli() -> argparse.ArgumentParser:
    from hermes_cli.subcommands.auth import build_auth_parser
    parser = argparse.ArgumentParser(prog="hermes")
    build_auth_parser(parser.add_subparsers(dest="command"), cmd_auth=lambda args: None)
    return parser


def _messages():
    from hermes_cli.auth_codex_registry import CodexRegistryUnreadable
    from hermes_cli.auth_codex_restore import SIGN_IN_AGAIN, _refuse_incomplete
    return {
        "restore-inventory-incomplete": str(_refuse_incomplete("the credential registry /x", "damaged")),
        "restore-sign-in-again": SIGN_IN_AGAIN,
        "registry-unreadable": str(CodexRegistryUnreadable()),
    }


@pytest.mark.parametrize("which", sorted(_messages()))
def test_every_command_a_refusal_names_parses(which):
    message = _messages()[which]
    commands = re.findall(r"`hermes ([^`]+)`", message)
    assert commands, f"{which}: names no command to run: {message}"
    parser = _auth_cli()
    for command in commands:
        try:
            parser.parse_args(command.split())
        except SystemExit:
            pytest.fail(f"{which}: `hermes {command}` does not exist: {message}")
