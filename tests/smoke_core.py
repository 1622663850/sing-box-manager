#!/usr/bin/env python3
"""Exercise generated configs with a real sing-box core, entirely on loopback.

Example:
    python3 tests/smoke_core.py --core /absolute/path/to/sing-box

No root privileges, package installation, host service changes, or Internet
connections are used. An HTTP target, server core, and client core bind only to
127.0.0.1. TCP, WebSocket, and Shadowsocks 2022 must forward the exact response.
REALITY server/client templates receive real-core schema checks; its TLS
handshake is intentionally not claimed as an end-to-end test.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Iterator


PROJECT = Path(__file__).resolve().parents[1]
LOOPBACK = "127.0.0.1"


def command(args: list[str], env: dict[str, str] | None = None) -> str:
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {args[0]} {args[1]}\n"
            + result.stdout + result.stderr
        )
    return result.stdout


def bash_json(manager: Path, environment: dict[str, str], function: str, *args: str) -> dict:
    text = command(
        ["bash", "-c", 'source "$1"; shift; "$@"', "smoke-core", str(manager), function, *args],
        env=environment,
    )
    value = json.loads(text)
    if not isinstance(value, dict):
        raise RuntimeError(f"{function} did not produce a JSON object")
    return value


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def free_port() -> int:
    # Reserve both transports while choosing a port; Shadowsocks uses TCP/UDP.
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
            tcp.bind((LOOPBACK, 0))
            port = tcp.getsockname()[1]
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                try:
                    udp.bind((LOOPBACK, port))
                except OSError:
                    continue
                return port
    raise RuntimeError("Could not allocate a loopback port")


def receive_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        data = sock.recv(length - len(chunks))
        if not data:
            raise RuntimeError("Unexpected end of SOCKS5 response")
        chunks.extend(data)
    return bytes(chunks)


def fetch_through_socks(proxy_port: int, target_port: int, path: str) -> bytes:
    with socket.create_connection((LOOPBACK, proxy_port), timeout=10) as sock:
        sock.sendall(b"\x05\x01\x00")
        if receive_exact(sock, 2) != b"\x05\x00":
            raise RuntimeError("Mixed inbound did not accept SOCKS5 without authentication")
        sock.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(LOOPBACK) + struct.pack("!H", target_port))
        header = receive_exact(sock, 4)
        if header[:3] != b"\x05\x00\x00":
            raise RuntimeError(f"SOCKS5 CONNECT failed: {header.hex()}")
        if header[3] == 1:
            receive_exact(sock, 4)
        elif header[3] == 4:
            receive_exact(sock, 16)
        elif header[3] == 3:
            receive_exact(sock, receive_exact(sock, 1)[0])
        else:
            raise RuntimeError("Unexpected SOCKS5 address type")
        receive_exact(sock, 2)
        request = f"GET {path} HTTP/1.1\r\nHost: {LOOPBACK}:{target_port}\r\nConnection: close\r\n\r\n"
        sock.sendall(request.encode("ascii"))
        response = http.client.HTTPResponse(sock)
        response.begin()
        if response.status != 200:
            raise RuntimeError(f"Unexpected HTTP response: {response.status}")
        return response.read(1024 * 1024)


@contextmanager
def http_target(payload: bytes) -> Iterator[int]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer((LOOPBACK, 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def wait_for_listener(process: subprocess.Popen, port: int, log_path: Path) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Core exited before listening:\n{log_path.read_text(encoding='utf-8')}")
        try:
            with socket.create_connection((LOOPBACK, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"Core did not listen on loopback port {port}:\n{log_path.read_text(encoding='utf-8')}")


@contextmanager
def running_core(core: Path, config: Path, port: int, work: Path) -> Iterator[None]:
    log_path = config.with_suffix(".log")
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(core), "run", "-D", str(work), "-c", str(config)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            wait_for_listener(process, port, log_path)
            yield
        except Exception:
            log.flush()
            print(f"--- {log_path.name} ---\n{log_path.read_text(encoding='utf-8')}", file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def server_config(inbound: dict) -> dict:
    return {
        "log": {"level": "warn"},
        "inbounds": [inbound],
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "route": {"final": "direct"},
    }


def client_config(outbound: dict, port: int) -> dict:
    return {
        "log": {"level": "warn"},
        "inbounds": [{"type": "mixed", "tag": "local", "listen": LOOPBACK, "listen_port": port}],
        "outbounds": [outbound],
        "route": {"final": "proxy"},
    }


def check(core: Path, config: Path, work: Path) -> None:
    command([str(core), "check", "-D", str(work), "-c", str(config)])


def run_smoke(core: Path, manager: Path) -> None:
    print(command([str(core), "version"]).splitlines()[0], flush=True)
    with tempfile.TemporaryDirectory(prefix="sing-box-loopback-") as temporary:
        work = Path(temporary)
        environment = os.environ.copy()
        environment["SB_ROOT_PREFIX"] = str(work / "unused-source-prefix")
        payload = ("SING_BOX_LOOPBACK_SMOKE:" + secrets.token_hex(32)).encode("ascii")
        with http_target(payload) as target_port:
            for protocol in ("vless-tcp", "vless-ws", "shadowsocks"):
                server_port, proxy_port = free_port(), free_port()
                while proxy_port == server_port:
                    proxy_port = free_port()
                credential = command(
                    [str(core), "generate", "rand", "--base64", "16"] if protocol == "shadowsocks"
                    else [str(core), "generate", "uuid"]
                ).strip()
                inbound = bash_json(
                    manager, environment, "generate_inbound", protocol, "smoke-server", str(server_port),
                    credential, "/smoke-ws" if protocol == "vless-ws" else "", "", "",
                )
                inbound["listen"] = LOOPBACK
                inbound_file = work / (protocol + "-inbound.json")
                write_json(inbound_file, inbound)
                outbound = bash_json(manager, environment, "generate_client", str(inbound_file), LOOPBACK)
                server_file, client_file = work / (protocol + "-server.json"), work / (protocol + "-client.json")
                write_json(server_file, server_config(inbound))
                write_json(client_file, client_config(outbound, proxy_port))
                check(core, server_file, work)
                check(core, client_file, work)
                with running_core(core, server_file, server_port, work):
                    with running_core(core, client_file, proxy_port, work):
                        received = fetch_through_socks(proxy_port, target_port, "/probe/" + protocol)
                        if received != payload:
                            raise RuntimeError(f"{protocol}: forwarded HTTP response did not match")
                print(f"PASS {protocol}: real server + client forwarded exact HTTP payload over loopback", flush=True)

        # No TLS target is contacted: REALITY is explicitly schema-only here.
        pair = command([str(core), "generate", "reality-keypair"])
        private_match = re.search(r"PrivateKey:\s*(\S+)", pair)
        public_match = re.search(r"PublicKey:\s*(\S+)", pair)
        if not private_match or not public_match:
            raise RuntimeError("Could not parse core-generated REALITY keypair")
        inbound = bash_json(
            manager, environment, "generate_inbound", "vless-reality", "smoke-reality", str(free_port()),
            command([str(core), "generate", "uuid"]).strip(), "localhost",
            private_match.group(1), secrets.token_hex(8),
        )
        inbound["listen"] = LOOPBACK
        inbound["tls"]["reality"]["handshake"] = {"server": LOOPBACK, "server_port": 443}
        inbound_file = work / "reality-inbound.json"
        write_json(inbound_file, inbound)
        outbound = bash_json(
            manager, environment, "generate_client", str(inbound_file), LOOPBACK, public_match.group(1),
        )
        server_file, client_file = work / "reality-server.json", work / "reality-client.json"
        write_json(server_file, server_config(inbound))
        write_json(client_file, client_config(outbound, free_port()))
        check(core, server_file, work)
        check(core, client_file, work)
        print("PASS vless-reality: real core accepted server and client configs (schema only)", flush=True)
    print("All smoke checks passed; temporary files and child processes cleaned up.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--core", required=True, type=Path, help="Trusted sing-box executable to run")
    parser.add_argument("--manager", type=Path, default=PROJECT / "sing-box-manager.sh")
    args = parser.parse_args()
    core, manager = args.core.expanduser().resolve(), args.manager.expanduser().resolve()
    if not core.is_file() or not os.access(core, os.X_OK):
        parser.error("--core must name an executable file")
    if not manager.is_file():
        parser.error("--manager must name the Bash manager script")
    try:
        run_smoke(core, manager)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
