"""``hermes trace`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_trace_parser(subparsers, *, cmd_trace: Callable) -> None:
    """Attach the ``trace`` subcommand to ``subparsers``."""
    trace_parser = subparsers.add_parser(
        "trace",
        help="Upload session transcripts to Hugging Face Agent Trace Viewer",
        description=(
            "Export a Hermes session transcript as Claude Code JSONL and upload "
            "it to a private Hugging Face dataset for the Agent Trace Viewer."
        ),
    )
    trace_sub = trace_parser.add_subparsers(dest="trace_command")

    upload = trace_sub.add_parser(
        "upload",
        aliases=["up"],
        help="Upload a session transcript",
    )
    upload.add_argument(
        "session_id",
        nargs="?",
        help="Session id to upload (default: most recent session)",
    )
    upload.add_argument(
        "--public",
        action="store_true",
        help="Create/update a public trace dataset instead of private",
    )
    upload.add_argument(
        "--no-redact",
        action="store_true",
        help="Upload without secret redaction; only use after manual review",
    )
    trace_parser.set_defaults(func=cmd_trace)
