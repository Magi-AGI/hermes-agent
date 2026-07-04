"""Regression tests for always-bound optional MCP SDK type exports (R6).

``tools.mcp_tool`` imports several optional ``mcp.types`` symbols (sampling,
elicitation, notification types) that were added across different MCP SDK
releases. Downstream code and tests import a subset of them unconditionally,
e.g. ``from tools.mcp_tool import CreateMessageResultWithTools``. On SDK
versions that predate a symbol the name was never bound at module scope, so the
unconditional import raised ``ImportError: cannot import name
'CreateMessageResultWithTools' from 'tools.mcp_tool'`` and took down the whole
MCP test suite at collection time. The module now binds a placeholder for any
optional type the SDK does not provide; these tests prove that safety net.
"""

import importlib
import sys

import pytest

import tools.mcp_tool as mcp_tool

# The full set of optional MCP SDK type names the module guarantees to export.
_OPTIONAL_TYPE_NAMES = (
    "CreateMessageResult",
    "CreateMessageResultWithTools",
    "ErrorData",
    "SamplingCapability",
    "SamplingToolsCapability",
    "TextContent",
    "ToolUseContent",
    "ElicitRequestParams",
    "ElicitResult",
    "ServerNotification",
    "ToolListChangedNotification",
    "PromptListChangedNotification",
    "ResourceListChangedNotification",
)


@pytest.mark.parametrize("name", _OPTIONAL_TYPE_NAMES)
def test_optional_mcp_type_is_always_exported_as_a_type(name):
    """Every optional type name resolves to a class, regardless of SDK version."""
    value = getattr(mcp_tool, name)
    assert isinstance(value, type), f"{name} should be a type, got {value!r}"


def test_create_message_result_with_tools_is_importable():
    """The exact import that previously failed at collection must succeed."""
    from tools.mcp_tool import CreateMessageResultWithTools

    assert isinstance(CreateMessageResultWithTools, type)


def test_bind_optional_mcp_type_binds_placeholder_for_missing_name():
    """A name absent from the module namespace gets a placeholder subclass."""
    sentinel = "CreateMessageResultWithTools_test_probe"
    mcp_tool.__dict__.pop(sentinel, None)
    try:
        mcp_tool._bind_optional_mcp_type(sentinel)
        bound = mcp_tool.__dict__[sentinel]
        assert isinstance(bound, type)
        assert issubclass(bound, mcp_tool._UnavailableMcpType)
        # The placeholder is inert: isinstance stays total and never matches a
        # real value, and Hermes never instantiates it.
        assert not isinstance(object(), bound)
    finally:
        mcp_tool.__dict__.pop(sentinel, None)


def test_bind_optional_mcp_type_preserves_existing_real_binding():
    """When the SDK supplied a real type, the fallback must not clobber it."""
    sentinel = "CreateMessageResultWithTools_test_real"

    class _RealType:
        pass

    mcp_tool.__dict__[sentinel] = _RealType
    try:
        mcp_tool._bind_optional_mcp_type(sentinel)
        assert mcp_tool.__dict__[sentinel] is _RealType
    finally:
        mcp_tool.__dict__.pop(sentinel, None)


def test_module_imports_when_sdk_lacks_symbol(monkeypatch):
    """Simulate an SDK missing the symbol and prove a fresh import still works.

    Deletes ``CreateMessageResultWithTools`` from ``mcp.types`` (which makes the
    module's combined sampling-types import fail exactly as it does on an older
    SDK), then reimports ``tools.mcp_tool`` from scratch and asserts the name is
    still bound — to the inert placeholder rather than the real type.
    """
    mcp_types = pytest.importorskip("mcp.types")
    monkeypatch.delattr(mcp_types, "CreateMessageResultWithTools", raising=False)

    original = sys.modules.pop("tools.mcp_tool", None)
    try:
        reloaded = importlib.import_module("tools.mcp_tool")
        assert isinstance(reloaded.CreateMessageResultWithTools, type)
        assert issubclass(
            reloaded.CreateMessageResultWithTools, reloaded._UnavailableMcpType
        )
    finally:
        # Restore the originally-imported module so other tests keep the same
        # object identity (this module holds process-wide MCP registries).
        if original is not None:
            sys.modules["tools.mcp_tool"] = original
