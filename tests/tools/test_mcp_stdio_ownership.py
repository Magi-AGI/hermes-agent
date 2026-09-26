"""A stdio server owns exactly its own process tree (review round 1, Codex P1).

Server roots were a process-wide child-PID delta, and two servers starting together each saw the
other's root in their window: tearing one down then reaped the other's interpreter and helper.
PID + create time proves identity, not ownership; the spawn token does.
"""

import asyncio
import sys

import psutil

from tools.mcp_tool import MCPServerTask

_SPAWN_HELPER = ("import pathlib, subprocess, sys; "
                 "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                 "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); sys.stdin.read()")


def test_concurrently_started_servers_track_and_reap_only_their_own_trees(tmp_path):
    release = {name: asyncio.Event() for name in ("alpha", "beta")}
    ready = {name: asyncio.Event() for name in release}

    class HeldServer(MCPServerTask):
        async def _serve_session(self, session, timeout, **kwargs):
            ready[self.name].set()
            await release[self.name].wait()

    async def scenario():
        servers = {name: HeldServer(name) for name in release}
        tasks = {name: asyncio.create_task(server._run_stdio({
            "command": sys.executable, "args": ["-c", _SPAWN_HELPER, str(tmp_path / f"{name}.pid")]}))
            for name, server in servers.items()}
        helpers = {}
        try:
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in ready.values())), 30)
            for _ in range(200):
                if all((tmp_path / f"{n}.pid").exists() for n in servers):
                    break
                await asyncio.sleep(0.05)
            helpers = {n: psutil.Process(int((tmp_path / f"{n}.pid").read_text())) for n in servers}
            tracked = {n: set(s._stdio_child_pids) for n, s in servers.items()}
            release["alpha"].set()
            await asyncio.wait_for(tasks["alpha"], 30)
            await asyncio.sleep(2.5)  # past the escaped-descendant grace
            return tracked, helpers["beta"].is_running(), not tasks["beta"].done()
        finally:
            for event in release.values():
                event.set()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            for proc in helpers.values():
                if proc.is_running():
                    proc.kill()

    tracked, beta_helper_alive, beta_running = asyncio.run(scenario())
    assert not (tracked["alpha"] & tracked["beta"]), tracked
    assert beta_running and beta_helper_alive
