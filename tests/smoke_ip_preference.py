#!/usr/bin/env python3
"""Real-core IPv4/IPv6 outbound preference smoke test on localhost only."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Iterator

from smoke_core import LOOPBACK, bash_json, check, free_port, receive_exact, write_json


LOOPBACK6 = "::1"


class _IPv6HTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


@contextmanager
def family_http_target(port: int, payload: bytes, ipv6: bool) -> Iterator[None]:
    server_class = _IPv6HTTPServer if ipv6 else ThreadingHTTPServer
    address = (LOOPBACK6 if ipv6 else LOOPBACK, port)

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

    server = server_class(address, Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def dual_http_targets(payload4: bytes, payload6: bytes, enable_ipv6: bool) -> Iterator[int]:
    port = free_port()
    with family_http_target(port, payload4, False):
        if enable_ipv6:
            with family_http_target(port, payload6, True):
                yield port
        else:
            yield port


def quiet_wait_for_listener(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("core process exited before listening")
        try:
            with socket.create_connection((LOOPBACK, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("core listener did not become ready")


@contextmanager
def quiet_core(core: Path, config: Path, port: int, work: Path) -> Iterator[None]:
    log_path = work / "ip-preference-core.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(core), "run", "-D", str(work), "-c", str(config)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        )
        try:
            quiet_wait_for_listener(process, port)
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def fetch_domain(proxy_port: int, target_port: int) -> bytes:
    with socket.create_connection((LOOPBACK, proxy_port), timeout=5) as sock:
        sock.sendall(b"\x05\x01\x00")
        if receive_exact(sock, 2) != b"\x05\x00":
            raise RuntimeError("SOCKS5 negotiation failed")
        host = b"localhost"
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + target_port.to_bytes(2, "big"))
        header = receive_exact(sock, 4)
        if header[:3] != b"\x05\x00\x00":
            raise RuntimeError("SOCKS5 domain connect failed")
        if header[3] == 1:
            receive_exact(sock, 4)
        elif header[3] == 4:
            receive_exact(sock, 16)
        elif header[3] == 3:
            receive_exact(sock, receive_exact(sock, 1)[0])
        else:
            raise RuntimeError("unexpected SOCKS5 address type")
        receive_exact(sock, 2)
        sock.sendall(
            f"GET /ip-preference HTTP/1.1\r\nHost: localhost:{target_port}\r\n"
            "Connection: close\r\n\r\n".encode("ascii"),
        )
        response = bytearray()
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            response.extend(chunk)
    marker = bytes(response).split(b"\r\n\r\n", 1)[-1]
    if b"200 OK" not in response[:256]:
        raise RuntimeError("HTTP target returned a non-success status")
    return marker


def generated_config(
    core: Path,
    manager: Path,
    environment: dict[str, str],
    work: Path,
    preference: str,
    proxy_port: int,
) -> Path:
    source = {
        "log": {"level": "warn"},
        "dns": {
            "servers": [{"tag": "local", "type": "local"}],
            "final": "local",
        },
        "inbounds": [{"type": "mixed", "tag": "local", "listen": LOOPBACK, "listen_port": proxy_port}],
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "route": {"final": "direct", "default_domain_resolver": "local"},
    }
    source_path = work / (preference + "-source.json")
    write_json(source_path, source)
    value = bash_json(manager, environment, "generate_outbound_preference", str(source_path), preference)
    config_path = work / (preference + "-config.json")
    write_json(config_path, value)
    check(core, config_path, work)
    return config_path


def run_preference(
    core: Path,
    manager: Path,
    environment: dict[str, str],
    work: Path,
    preference: str,
    target_port: int,
    expected: bytes,
) -> None:
    proxy_port = free_port()
    config_path = generated_config(core, manager, environment, work, preference, proxy_port)
    with quiet_core(core, config_path, proxy_port, work):
        received = fetch_domain(proxy_port, target_port)
    if received != expected:
        raise RuntimeError("unexpected address-family target selected")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--manager", type=Path, default=Path(__file__).resolve().parents[1] / "sing-box-manager.sh")
    args = parser.parse_args()
    core, manager = args.core.expanduser().resolve(), args.manager.expanduser().resolve()
    if not core.is_file() or not manager.is_file():
        print("FAIL ip-preference", file=sys.stderr)
        return 1
    try:
        addresses = socket.getaddrinfo("localhost", 0, socket.AF_UNSPEC, socket.SOCK_STREAM)
        families = {item[0] for item in addresses}
        if socket.AF_INET6 not in families or socket.AF_INET not in families:
            print("SKIP ip-preference: localhost is not dual-stack")
            return 0
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
                probe.bind((LOOPBACK6, 0))
        except OSError:
            print("SKIP ip-preference: IPv6 loopback unavailable")
            return 0
        with tempfile.TemporaryDirectory(prefix="sing-box-ip-preference-") as temporary:
            work = Path(temporary)
            environment = os.environ.copy()
            environment["SB_ROOT_PREFIX"] = str(work / "source-prefix")
            payload4, payload6 = b"IPV4_TARGET", b"IPV6_TARGET"
            with dual_http_targets(payload4, payload6, True) as port:
                run_preference(core, manager, environment, work, "prefer_ipv6", port, payload6)
                print("PASS ip-preference prefer_ipv6: IPv6 target selected")
                run_preference(core, manager, environment, work, "prefer_ipv4", port, payload4)
                print("PASS ip-preference prefer_ipv4: IPv4 target selected")
            with dual_http_targets(payload4, payload6, False) as port:
                run_preference(core, manager, environment, work, "prefer_ipv6", port, payload4)
                print("PASS ip-preference prefer_ipv6 fallback: IPv4 target selected")
    except Exception:
        print("FAIL ip-preference", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
