"""R10 property tests: every profile's MCP state is a PURE FUNCTION of
(the connection's last published raw list, that profile's current policy).

For each profile P using connection K: P's live registry slot for toolset ``mcp-{name}`` and
P's schema-cache entry must equal ``f(list, policy)``, computed here INDEPENDENTLY of the code
under test, after every kind of input change: publish (new list / capabilities), teardown and
revival, policy change (filter, resources, prompts), a profile gaining or losing the connection,
and the interleavings a publish racing a removal and a reconnect racing a policy change.
"""

from __future__ import annotations

import random
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mcp.types as types
import pytest

from hermes_constants import hermes_home_key, reset_hermes_home_override, set_hermes_home_override
from tools import mcp_schema_cache as cache
from tools import mcp_tool as core
from tools import mcp_tool_registration as registration
from tools.mcp_tool import MCPServerTask
from tools.registry import ToolRegistry

NAME = "r10srv"
URL = "https://mcp.example.invalid/mcp"
TOOLSET = f"mcp-{NAME}"
UTILS = {"resources": ("list_resources", "read_resource"), "prompts": ("list_prompts", "get_prompt")}


@contextmanager
def profile(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    token = set_hermes_home_override(path)
    try:
        yield hermes_home_key()
    finally:
        reset_hermes_home_override(token)


def tool(name):
    return types.Tool.model_validate({"name": name, "description": f"{name} tool", "inputSchema": {"type": "object"}})


def policy(include=None, exclude=None, resources=True, prompts=True):
    tools = {"resources": resources, "prompts": prompts}
    if include is not None:
        tools["include"] = list(include)
    if exclude is not None:
        tools["exclude"] = list(exclude)
    return {"url": URL, "lazy": True, "tools": tools}


def expected(tool_names, caps, config) -> set:
    """f(list, policy), written independently of the implementation."""
    if config is None or tool_names is None:
        return set()
    tools_cfg = config.get("tools") or {}
    include, exclude = tools_cfg.get("include"), set(tools_cfg.get("exclude") or ())
    kept = [t for t in tool_names if (t in include if include is not None else t not in exclude)]
    out = {f"mcp__{NAME}__{t}" for t in kept}
    for family, keys in UTILS.items():
        if tools_cfg.get(family, True) and caps.get(family):
            out |= {f"mcp__{NAME}__{k}" for k in keys}
    return out


class World:
    """Owner + two adopters sharing one live connection, with a real registry and real caches."""

    def __init__(self, tmp_path: Path, registry: ToolRegistry):
        self.registry = registry
        self.homes = {p: tmp_path / p for p in ("owner", "a1", "a2")}
        self.scopes = {}
        for p, home in self.homes.items():
            with profile(home) as scope:
                self.scopes[p] = scope
        self.key = (self.scopes["owner"], NAME)
        self.server = MCPServerTask(NAME)
        self.server._config = policy()
        self.tool_names = ["alpha", "beta"]
        self.caps = {"resources": True, "prompts": False}
        self._apply_list()
        core._servers[self.key] = self.server
        core._server_scope_keys[self.key] = self.scopes["owner"]
        self.policies = {"owner": policy(), "a1": None, "a2": None}

    def _apply_list(self):
        self.server._tools = [tool(t) for t in self.tool_names]
        self.server.initialize_result = SimpleNamespace(capabilities=SimpleNamespace(
            tools=object(), resources=object() if self.caps["resources"] else None,
            prompts=object() if self.caps["prompts"] else None))
        self.server.session = SimpleNamespace()
        self.server._list_cache_meta = {}

    # --- input changes -----------------------------------------------------------------------
    def publish(self, tool_names=None, caps=None):
        if tool_names is not None:
            self.tool_names = list(tool_names)
        if caps is not None:
            self.caps = dict(caps)
        self._apply_list()
        self.server._list_published = True
        with profile(self.homes["owner"]):
            registration._set_profile_policy(self.key, self.scopes["owner"], self.policies["owner"],
                                             server=self.server, reconcile=False)
            registration._reconcile_connection(self.key, self.server)

    def teardown(self):
        self.server._list_published = False
        with profile(self.homes["owner"]):
            registration._reconcile_connection(self.key, self.server)

    def set_policy(self, who, config):
        self.policies[who] = config
        with profile(self.homes[who]):
            registration._set_profile_policy(self.key, self.scopes[who], config)

    # --- the invariant ----------------------------------------------------------------------
    def live(self, who) -> set:
        return set(self.registry.get_tool_names_for_toolset_in_scope(TOOLSET, self.scopes[who]))

    def assert_invariant(self, context=""):
        published = self.server._list_published
        for who in self.homes:
            want = expected(self.tool_names if published else None, self.caps, self.policies[who])
            assert self.live(who) == want, (context, who, sorted(self.live(who)), sorted(want))
            membership = self.scopes[who] in core._server_tool_scopes.get(self.key, set())
            assert membership == (self.policies[who] is not None), (context, who, "membership")
            if published and self.policies[who] is not None:
                with profile(self.homes[who]):
                    entry = cache.get_cached_entry(NAME, cache.config_fingerprint(self.policies[who]))
                assert entry is not None, (context, who, "cache missing")
                cached = {f"mcp__{NAME}__{row['name']}" for row in entry["tools"]}
                cached |= {f"mcp__{NAME}__{row['handler_key']}" for row in entry["utility_tools"]}
                assert cached == want, (context, who, "cache", sorted(cached), sorted(want))


@pytest.fixture()
def world(tmp_path):
    with patch("agent.secret_scope.serves_routed_profile", return_value=True), \
            patch("tools.mcp_tool._servers", {}), patch("tools.mcp_tool._server_tool_scopes", {}), \
            patch("tools.mcp_tool._server_scope_keys", {}), patch("tools.mcp_tool._server_scope_configs", {}), \
            patch("tools.mcp_tool._server_scope_epochs", {}), patch("tools.mcp_tool._server_trust_levels", {}), \
            patch("tools.registry.registry", ToolRegistry()) as reg:
        w = World(tmp_path, reg)
        w.publish()
        yield w


def test_each_input_change_recomputes(world):
    w = world
    w.assert_invariant("initial publish")
    w.set_policy("a1", policy(resources=False))
    w.assert_invariant("a1 gains")
    w.set_policy("a2", policy(include=["beta"], prompts=True))
    w.assert_invariant("a2 gains with include")
    w.publish(["alpha", "gamma"], {"resources": True, "prompts": True})
    w.assert_invariant("publish: new list and capabilities")
    w.set_policy("a1", policy(resources=True, exclude=["gamma"]))
    w.assert_invariant("a1 policy change")
    w.set_policy("owner", policy(resources=False))
    w.assert_invariant("owner policy change")
    w.teardown()
    w.assert_invariant("teardown")
    w.set_policy("a2", policy())
    w.assert_invariant("policy change while torn down")
    w.publish()
    w.assert_invariant("revival")
    w.set_policy("a1", None)
    w.assert_invariant("a1 loses")
    w.publish([], {"resources": False, "prompts": False})
    w.assert_invariant("empty publish")


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_random_input_sequences_keep_state_a_pure_function(world, seed):
    rng = random.Random(seed)
    w = world
    pool = ["alpha", "beta", "gamma", "delta"]

    def random_policy():
        if rng.random() < 0.25:
            return None
        include = rng.sample(pool, rng.randint(0, 3)) if rng.random() < 0.3 else None
        exclude = rng.sample(pool, rng.randint(0, 2)) if include is None and rng.random() < 0.3 else None
        return policy(include=include, exclude=exclude, resources=rng.random() < 0.5, prompts=rng.random() < 0.5)

    for step in range(40):
        op = rng.choice(["publish", "policy", "policy", "teardown"])
        if op == "publish":
            w.publish(rng.sample(pool, rng.randint(0, 4)),
                      {"resources": rng.random() < 0.6, "prompts": rng.random() < 0.6})
        elif op == "teardown":
            w.teardown()
        else:
            who = rng.choice(["a1", "a2", "owner"])
            config = random_policy() if who != "owner" else (random_policy() or policy())
            w.set_policy(who, config)
        w.assert_invariant(f"seed {seed} step {step} {op}")


def test_publish_racing_removal_converges(world):
    """A profile loses the connection in the middle of a publication (re-entrantly, between the
    publisher's policy read and its registry write): no tool, no membership, no policy remains."""
    w = world
    w.set_policy("a1", policy())
    w.assert_invariant("a1 adopted")
    original = registration._make_tool_filter
    fired = []

    def filter_with_removal(server_name, config):
        if config == w.policies["a1"] and not fired:
            fired.append(True)
            w.set_policy("a1", None)
        return original(server_name, config)

    with patch.object(registration, "_make_tool_filter", side_effect=filter_with_removal):
        w.publish(["alpha", "beta", "gamma"])
    assert fired
    w.assert_invariant("after racing removal")
    assert (w.key, w.scopes["a1"]) not in core._server_scope_configs


def test_reconnect_racing_policy_change_converges(world):
    """A reconnect republishes while the adopter's policy changes under it: the result is
    f(new list, NEW policy), not the policy the publisher first read."""
    w = world
    w.set_policy("a1", policy(resources=True))
    original = registration._make_tool_filter
    fired = []

    def filter_with_policy_change(server_name, config):
        if config == w.policies["a1"] and not fired:
            fired.append(True)
            w.set_policy("a1", policy(resources=False, include=["gamma"]))
        return original(server_name, config)

    with patch.object(registration, "_make_tool_filter", side_effect=filter_with_policy_change):
        w.teardown()
        w.publish(["alpha", "gamma"])
    assert fired
    w.assert_invariant("after reconnect racing policy change")


def test_concurrent_publishers_and_policy_writers_converge(world):
    """Threads publishing and changing policy concurrently: after they stop, one more input of
    each kind leaves every profile equal to f(final list, final policy)."""
    w = world
    stop = threading.Event()
    errors = []

    def publisher():
        rng = random.Random(11)
        while not stop.is_set():
            try:
                w.server._tools = [tool(t) for t in rng.sample(["alpha", "beta", "gamma"], rng.randint(0, 3))]
                w.server._list_published = True
                registration._reconcile_connection(w.key, w.server)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

    def policy_writer():
        rng = random.Random(12)
        while not stop.is_set():
            try:
                cfg = policy(resources=rng.random() < 0.5) if rng.random() < 0.7 else None
                registration._set_profile_policy(w.key, w.scopes["a1"], cfg)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

    threads = [threading.Thread(target=publisher), threading.Thread(target=policy_writer)]
    for t in threads:
        t.start()
    threading.Event().wait(0.5)
    stop.set()
    for t in threads:
        t.join(5)
    assert not errors, errors
    w.set_policy("a1", policy(prompts=False))
    w.publish(["beta"])
    w.assert_invariant("after concurrent writers")


def test_adopter_only_name_does_not_survive_a_no_capability_reconnect(world):
    """Codex r9 item 4: the owner's filter excludes ``beta``, an adopter allows it. After a
    successor without the tools capability, the adopter's ``beta`` must be gone although the
    owner never had any name (the old code keyed the republish on the owner's names)."""
    import asyncio

    w = world
    w.set_policy("owner", policy(include=[], resources=False, prompts=False))
    w.set_policy("a1", policy(include=["beta"], resources=False, prompts=False))
    w.publish(["beta"], {"resources": False, "prompts": False})
    w.assert_invariant("owner empty, adopter has beta")
    assert w.server._registered_tool_names == [], "precondition: the owner itself has no names"
    assert w.live("a1") == {f"mcp__{NAME}__beta"}

    async def forbidden_list(**_kwargs):
        raise AssertionError("no tools capability: tools/list must not be called")

    w.server.session = SimpleNamespace(list_tools=forbidden_list)
    w.server.initialize_result = SimpleNamespace(capabilities=SimpleNamespace(tools=None, resources=None, prompts=None))
    with profile(w.homes["owner"]):
        asyncio.run(w.server._discover_tools())
    w.tool_names = []
    w.caps = {"resources": False, "prompts": False}
    w.assert_invariant("after no-capability successor")
    assert w.live("a1") == set()
