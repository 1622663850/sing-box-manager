#!/usr/bin/env python3
"""Real-core outbound binding smoke test on loopback only.

The test creates a private manager root, a local mixed-inbound core and a
second VLESS TCP core whose direct outbound is bound to 127.0.0.2.  The HTTP
target reports only its peer address; credentials and core diagnostics never
reach stdout.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from smoke_core import LOOPBACK, bash_json, check, fetch_through_socks, free_port, write_json
from smoke_multiusers import quiet_core


def manager_call(manager: Path, core: Path, root: Path, function: str, *args: str) -> str:
    result = subprocess.run(
        [
            "bash", "-c",
            'source "$1"; CORE_BIN="$2"; shift 2; confirm() { return 0; }; service_is_active() { return 1; }; "$@"',
            "outbound-routes", str(manager), str(core), function, *args,
        ],
        env={**os.environ, "SB_ROOT_PREFIX": str(root)},
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("manager operation failed")
    return result.stdout


@contextmanager
def peer_http_target() -> object:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = self.client_address[0].encode("ascii")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer((LOOPBACK, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run(core: Path, manager: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="sing-box-outbound-routes-") as temporary:
        work = Path(temporary)
        root = work / "manager-root"
        state = root / "var/lib/sing-box-manager"
        config_dir = root / "etc/sing-box"
        clients = state / "clients"
        for directory in (config_dir, clients, state / "backups", root / "usr/local/lib/sing-box-manager"):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        (root / "usr/local/lib/sing-box-manager/.managed").write_text("sing-box-manager-v1\n", encoding="utf-8")
        config = config_dir / "config.json"

        uuid = subprocess.check_output([str(core), "generate", "uuid"], text=True).strip()
        remote_port = free_port()
        one_port, two_port = free_port(), free_port()
        while one_port == remote_port:
            one_port = free_port()
        while two_port in {one_port, remote_port}:
            two_port = free_port()

        remote_inbound = {
            "type": "vless", "tag": "remote-vless", "listen": LOOPBACK,
            "listen_port": remote_port, "users": [{"uuid": uuid}],
        }
        remote_config = work / "remote.json"
        write_json(remote_config, {
            "log": {"level": "warn"},
            "inbounds": [remote_inbound],
            "outbounds": [{"type": "direct", "tag": "direct", "inet4_bind_address": "127.0.0.2"}],
            "route": {"final": "direct"},
        })
        check(core, remote_config, work)

        local_config = {
            "log": {"level": "warn"},
            "inbounds": [
                {"type": "mixed", "tag": "one", "listen": LOOPBACK, "listen_port": one_port},
                {"type": "mixed", "tag": "two", "listen": LOOPBACK, "listen_port": two_port},
            ],
            "outbounds": [{"type": "direct", "tag": "direct"}],
            "route": {"final": "direct"},
        }
        write_json(config, local_config)

        outbound = {
            "type": "vless", "tag": "proxy-egress", "server": LOOPBACK,
            "server_port": remote_port, "uuid": uuid,
        }
        outbound_file = work / "outbounds.json"
        write_json(outbound_file, {"outbounds": [outbound]})

        def call(function: str, *args: str) -> str:
            return manager_call(manager, core, root, function, *args)

        call("add_outbounds_file_locked", str(outbound_file))
        call("bind_outbound_locked", "one", "proxy-egress")
        check(core, config, work)

        with peer_http_target() as target_port:
            with quiet_core(core, remote_config, remote_port, work, "outbound-remote"):
                with quiet_core(core, config, one_port, work, "outbound-local"):
                    # The local mixed listener is reached through its own port;
                    # quiet_core only waits for the first listener, so the
                    # second is probed by the SOCKS request below.
                    one_peer = fetch_through_socks(one_port, target_port, "/one")
                    two_peer = fetch_through_socks(two_port, target_port, "/two")
                    if one_peer != b"127.0.0.2" or two_peer != b"127.0.0.1":
                        raise RuntimeError("bound and direct outbound peers did not match")
        print("PASS outbound bind: one used 127.0.0.2 and two used 127.0.0.1", flush=True)

        call("bind_outbound_locked", "one", "-")
        check(core, config, work)
        with peer_http_target() as target_port:
            with quiet_core(core, remote_config, remote_port, work, "outbound-remote-unbound"):
                with quiet_core(core, config, one_port, work, "outbound-local-unbound"):
                    peer = fetch_through_socks(one_port, target_port, "/one-unbound")
                    if peer != b"127.0.0.1":
                        raise RuntimeError("unbind did not restore direct outbound")
        value = json.loads(config.read_text(encoding="utf-8"))
        if [item["tag"] for item in value["inbounds"]] != ["one", "two"]:
            raise RuntimeError("inbounds changed while binding outbound")
        print("PASS outbound unbind: one restored direct and other rules remained", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--manager", required=True, type=Path)
    args = parser.parse_args()
    try:
        run(args.core, args.manager)
    except Exception:
        print("FAIL outbound route smoke", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
