"""Standalone-node JSON-RPC client and node lifecycle controller.

Everything here talks to a local `xrpld --standalone` process over its admin
JSON-RPC port. Nothing in this module trusts the node's own invariant checks:
it only moves bytes and reads committed ledger data back.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Sequence


class RpcError(RuntimeError):
    pass


class RpcClient:
    def __init__(self, url: str, timeout: float = 60.0):
        self.url = url
        self.timeout = timeout

    def call(self, method: str, **params: Any) -> Dict[str, Any]:
        body = json.dumps({"method": method, "params": [params]}).encode()
        req = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode())
        result = payload.get("result", payload)
        if result.get("status") == "error":
            raise RpcError(f"{method} failed: {result}")
        return result


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


CONFIG_TEMPLATE = """\
[server]
port_rpc_admin_local

[port_rpc_admin_local]
port = {port}
ip = 127.0.0.1
admin = 127.0.0.1
protocol = http

[node_size]
tiny

[node_db]
type=NuDB
path={workdir}/db/nudb

[database_path]
{workdir}/db

[debug_logfile]
{workdir}/debug.log

[ssl_verify]
0
{features_section}"""


def _features_section(features) -> str:
    if not features:
        return ""
    # An amendment ID is the first 32 bytes of SHA-512 of its name; "[amendments]" makes the
    # node vote for them, and a fresh standalone genesis enables everything the node desires.
    import hashlib
    lines = ["", "[amendments]"] + [
        f"{hashlib.sha512(n.encode()).digest()[:32].hex().upper()} {n}" for n in features
    ] + [""]
    return chr(10).join(lines)


class NodeController:
    """Start/stop a FRESH standalone node (new genesis ledger) and wait for RPC."""

    def __init__(self, binary: str, keep_dir: bool = False, startup_timeout: float = 120.0,
                 features: Sequence[str] = (), env: Optional[Dict[str, str]] = None):
        self.binary = binary
        self.keep_dir = keep_dir
        self.features = list(features)
        self.env = env
        self.startup_timeout = startup_timeout
        self.workdir: Optional[str] = None
        self.proc: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self.client: Optional[RpcClient] = None

    def __enter__(self) -> "NodeController":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> RpcClient:
        self.workdir = tempfile.mkdtemp(prefix="nullsum-node-")
        os.makedirs(f"{self.workdir}/db", exist_ok=True)
        self.port = _free_port()
        self.config_path = f"{self.workdir}/xrpld.cfg"
        with open(self.config_path, "w") as f:
            f.write(CONFIG_TEMPLATE.format(
                port=self.port, workdir=self.workdir,
                features_section=_features_section(self.features),
            ))
        self._log = open(f"{self.workdir}/stdout.log", "wb")
        # -a: standalone, --start: fresh genesis ledger
        self.proc = subprocess.Popen(
            [self.binary, "-a", "--start", "--conf", self.config_path],
            stdout=self._log,
            stderr=subprocess.STDOUT,
            env=({**os.environ, **self.env} if self.env else None),
        )
        self.client = RpcClient(f"http://127.0.0.1:{self.port}/", timeout=60.0)
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"node exited early rc={self.proc.returncode}; see {self.workdir}/stdout.log"
                )
            try:
                info = self.client.call("server_info")
                if info.get("info"):
                    return self.client
            except (urllib.error.URLError, ConnectionError, OSError, RpcError):
                pass
            time.sleep(0.2)
        raise RuntimeError(f"node RPC not ready after {self.startup_timeout}s")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.client.call("stop")
            except Exception:
                pass
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.send_signal(signal.SIGKILL)
                self.proc.wait()
        if getattr(self, "_log", None):
            self._log.close()
        if self.workdir and not self.keep_dir:
            shutil.rmtree(self.workdir, ignore_errors=True)
