#!/usr/bin/env python3
"""Exercise generated configs with a real sing-box core, entirely on loopback.

Example:
    python3 tests/smoke_core.py --core /absolute/path/to/sing-box

No root privileges, package installation, host service changes, or Internet
connections are used. An HTTP target, server core, and client core bind only to
127.0.0.1. Every supported template forwards the exact response through a real
client/server pair. REALITY additionally uses a local TLS 1.3 camouflage target;
Hysteria 2, TUIC and Shadowsocks 2022 also forward real UDP echo datagrams through
SOCKS5 UDP ASSOCIATE. No public endpoint is contacted.
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


def fetch_through_socks_retry(proxy_port: int, target_port: int, path: str) -> bytes:
    """Retry the first request while a QUIC handshake is settling."""
    last_error: Exception | None = None
    for _ in range(20):
        try:
            return fetch_through_socks(proxy_port, target_port, path)
        except (OSError, RuntimeError) as error:
            last_error = error
            time.sleep(0.25)
    raise RuntimeError(f"Proxy request did not complete after retries: {last_error}")


@contextmanager
def udp_echo_target() -> Iterator[int]:
    """A reply prefix proves the packet reached the target, not just the relay."""
    stop = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind((LOOPBACK, 0))
        server.settimeout(0.2)

        def echo() -> None:
            while not stop.is_set():
                try:
                    data, peer = server.recvfrom(65535)
                    server.sendto(b"UDP_ECHO:" + data, peer)
                except socket.timeout:
                    continue

        thread = threading.Thread(target=echo, daemon=True)
        thread.start()
        try:
            yield server.getsockname()[1]
        finally:
            stop.set()
            thread.join(timeout=2)


def check_udp_through_socks(proxy_port: int, target_port: int) -> None:
    # RFC 1928: the TCP association must remain open while relaying datagrams.
    with socket.create_connection((LOOPBACK, proxy_port), timeout=5) as control:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind((LOOPBACK, 0))
            udp.settimeout(2)
            control.sendall(b"\x05\x01\x00")
            if receive_exact(control, 2) != b"\x05\x00":
                raise RuntimeError("SOCKS5 UDP authentication negotiation failed")
            control.sendall(
                b"\x05\x03\x00\x01" + socket.inet_aton(LOOPBACK)
                + struct.pack("!H", udp.getsockname()[1]),
            )
            header = receive_exact(control, 4)
            if header[:3] != b"\x05\x00\x00" or header[3] != 1:
                raise RuntimeError("SOCKS5 UDP ASSOCIATE did not return an IPv4 relay")
            relay_host = socket.inet_ntoa(receive_exact(control, 4))
            relay_port = struct.unpack("!H", receive_exact(control, 2))[0]
            if relay_host not in (LOOPBACK, "0.0.0.0") or not relay_port:
                raise RuntimeError("SOCKS5 UDP relay is not on the expected loopback interface")
            udp.connect((LOOPBACK, relay_port))
            address = b"\x01" + socket.inet_aton(LOOPBACK) + struct.pack("!H", target_port)
            for sequence in range(3):
                payload = f"UDP_PROBE:{sequence}:{secrets.token_hex(32)}".encode("ascii")
                request = b"\x00\x00\x00" + address + payload
                expected = b"\x00\x00\x00" + address + b"UDP_ECHO:" + payload
                for _attempt in range(3):
                    udp.send(request)
                    response = b""
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        udp.settimeout(max(0.01, deadline - time.monotonic()))
                        try:
                            response = udp.recv(65535)
                        except socket.timeout:
                            break
                        if response == expected:
                            break
                    if response == expected:
                        break
                else:
                    raise RuntimeError("UDP echo through SOCKS5 did not return the exact target reply")


def run_udp_smoke(proxy_port: int, protocol: str) -> None:
    with udp_echo_target() as target_port:
        check_udp_through_socks(proxy_port, target_port)
    print(f"PASS {protocol}: SOCKS5 UDP ASSOCIATE forwarded 3 exact UDP echo replies", flush=True)


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


def wait_for_listener(
    process: subprocess.Popen, port: int, log_path: Path, *, tcp_probe: bool = True,
) -> None:
    deadline = time.monotonic() + (10 if tcp_probe else 1)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Core exited before listening:\n{log_path.read_text(encoding='utf-8')}")
        if not tcp_probe:
            # QUIC listeners do not accept a TCP connect.  A bounded liveness
            # check still catches immediate schema/startup failures.
            time.sleep(0.1)
            continue
        try:
            with socket.create_connection((LOOPBACK, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    if not tcp_probe and process.poll() is None:
        return
    raise RuntimeError(f"Core did not listen on loopback port {port}:\n{log_path.read_text(encoding='utf-8')}")


@contextmanager
def running_core(
    core: Path, config: Path, port: int, work: Path, *, tcp_probe: bool = True,
) -> Iterator[None]:
    log_path = config.with_suffix(".log")
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(core), "run", "-D", str(work), "-c", str(config)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            wait_for_listener(process, port, log_path, tcp_probe=tcp_probe)
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


def tls_material(work: Path) -> tuple[Path, Path]:
    certificate, private_key = work / "smoke-cert.pem", work / "smoke-key.pem"
    result = subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ], capture_output=True, text=True, timeout=20,
    )
    if result.returncode:
        raise RuntimeError("Could not create temporary TLS certificate: " + result.stderr)
    return certificate, private_key


def check(core: Path, config: Path, work: Path) -> None:
    command([str(core), "check", "-D", str(work), "-c", str(config)])


@contextmanager
def running_tls_target(certificate: Path, private_key: Path, port: int, work: Path) -> Iterator[None]:
    """Run a local TLS 1.3 camouflage target for REALITY handshakes."""
    log_path = work / f"reality-target-{port}.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [
                "openssl", "s_server", "-accept", f"{LOOPBACK}:{port}", "-tls1_3",
                "-cert", str(certificate), "-key", str(private_key),
                "-alpn", "h2,http/1.1", "-quiet", "-ign_eof",
            ],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            try:
                wait_for_listener(process, port, log_path)
            except Exception:
                log.flush()
                detail = log_path.read_text(encoding="utf-8", errors="replace")
                raise RuntimeError(f"Local TLS target failed for REALITY handshake:\n{detail}") from None
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def run_reality_loopback_smoke(
    core: Path, manager: Path, environment: dict[str, str], work: Path, payload: bytes,
) -> None:
    """Exercise TCP Vision and gRPC REALITY against a local TLS 1.3 target."""
    certificate, private_key = tls_material(work)
    pair = command([str(core), "generate", "reality-keypair"])
    private_match = re.search(r"PrivateKey:\s*(\S+)", pair)
    public_match = re.search(r"PublicKey:\s*(\S+)", pair)
    if not private_match or not public_match:
        raise RuntimeError("Could not parse core-generated REALITY keypair")

    with http_target(payload) as target_http_port:
        for protocol, service_name in (("vless-reality", ""), ("vless-grpc-reality", "RealityGrpc")):
            target_tls_port, server_port, proxy_port = free_port(), free_port(), free_port()
            while server_port in {target_tls_port, proxy_port}:
                server_port = free_port()
            while proxy_port in {target_tls_port, server_port}:
                proxy_port = free_port()
            extra = "localhost" if protocol == "vless-reality" else service_name
            inbound = bash_json(
                manager, environment, "generate_inbound", protocol, "smoke-" + protocol,
                str(server_port), command([str(core), "generate", "uuid"]).strip(),
                extra, private_match.group(1), "0123456789abcdef", "localhost",
            )
            inbound["listen"] = LOOPBACK
            inbound["tls"]["reality"]["handshake"] = {
                "server": LOOPBACK, "server_port": target_tls_port,
            }
            inbound_file = work / (protocol + "-loopback-inbound.json")
            server_file = work / (protocol + "-loopback-server.json")
            client_file = work / (protocol + "-loopback-client.json")
            write_json(inbound_file, inbound)
            outbound = bash_json(
                manager, environment, "generate_client", str(inbound_file), LOOPBACK, public_match.group(1),
            )
            if protocol == "vless-grpc-reality":
                if "flow" in outbound or outbound.get("transport", {}).get("type") != "grpc":
                    raise RuntimeError("VLESS gRPC REALITY client export must omit Vision flow")
            write_json(server_file, server_config(inbound))
            write_json(client_file, client_config(outbound, proxy_port))
            check(core, server_file, work)
            check(core, client_file, work)
            with running_tls_target(certificate, private_key, target_tls_port, work):
                with running_core(core, server_file, server_port, work):
                    with running_core(core, client_file, proxy_port, work):
                        received = fetch_through_socks_retry(
                            proxy_port, target_http_port, "/probe/" + protocol,
                        )
                        if received != payload:
                            raise RuntimeError(
                                f"{protocol}: REALITY loopback response did not match",
                            )
            print(
                f"PASS {protocol}: REALITY handshake and HTTP forwarding succeeded against local TLS 1.3 target",
                flush=True,
            )


def run_extended_smoke(core: Path, manager: Path, environment: dict[str, str], work: Path, payload: bytes) -> None:
    """Exercise every added transport with a real server/client loopback pair.

    QUIC templates get a short liveness check because they do not accept a TCP
    probe, then the same SOCKS HTTP request below proves the actual forwarding.
    """
    certificate, private_key = tls_material(work)
    uuid = command([str(core), "generate", "uuid"]).strip()
    pair = command([str(core), "generate", "reality-keypair"])
    private_match = re.search(r"PrivateKey:\s*(\S+)", pair)
    public_match = re.search(r"PublicKey:\s*(\S+)", pair)
    if not private_match or not public_match:
        raise RuntimeError("Could not parse core-generated REALITY keypair")
    templates = {
        "hysteria2": ("hy2-password", "", "", "", "localhost", certificate, private_key, ""),
        "tuic": (uuid, "", "", "", "localhost", certificate, private_key, "tuic-password"),
        "anytls": ("anytls-password", "", "", "", "localhost", certificate, private_key, ""),
        "vless-grpc-tls": (uuid, "SmokeGrpc", "", "", "localhost", certificate, private_key, ""),
        "vless-ws-tls": (uuid, "/smoke-vless-tls", "", "", "localhost", certificate, private_key, ""),
        "vless-grpc-reality": (uuid, "SmokeGrpcReality", private_match.group(1), "0123456789abcdef", "localhost", None, None, ""),
        "vmess-tcp": (uuid, "", "", "", "", None, None, ""),
        "vmess-tcp-tls": (uuid, "", "", "", "localhost", certificate, private_key, ""),
        "vmess-ws": (uuid, "/smoke-vmess", "", "", "", None, None, ""),
        "vmess-ws-tls": (uuid, "/smoke-vmess-tls", "", "", "localhost", certificate, private_key, ""),
        "vmess-grpc-tls": (uuid, "SmokeVmessGrpc", "", "", "localhost", certificate, private_key, ""),
    }
    with http_target(payload) as target_port:
        for protocol, fields in templates.items():
            credential, extra, server_private, short_id, server_name, cert, key, password = fields
            server_port, proxy_port = free_port(), free_port()
            while proxy_port == server_port:
                proxy_port = free_port()
            inbound = bash_json(
                manager, environment, "generate_inbound", protocol, "smoke-" + protocol, str(server_port),
                credential, extra, server_private, short_id, server_name, str(cert or ""), str(key or ""), password,
            )
            inbound["listen"] = LOOPBACK
            inbound_file = work / (protocol + "-extended-inbound.json")
            write_json(inbound_file, inbound)
            server_file = work / (protocol + "-extended-server.json")
            write_json(server_file, server_config(inbound))
            check(core, server_file, work)
            if protocol == "vless-grpc-reality":
                # The separate local TLS target test exercises the handshake.
                outbound = bash_json(
                    manager, environment, "generate_client", str(inbound_file), LOOPBACK, public_match.group(1),
                )
                if "flow" in outbound or outbound.get("transport", {}).get("type") != "grpc":
                    raise RuntimeError("VLESS gRPC REALITY client export must omit Vision flow")
                if outbound.get("tls", {}).get("alpn") != ["h2"]:
                    raise RuntimeError("VLESS gRPC REALITY client export must use h2 ALPN")
                reality_client_file = work / "vless-grpc-reality-extended-client.json"
                write_json(reality_client_file, client_config(outbound, free_port()))
                check(core, reality_client_file, work)
                print("PASS vless-grpc-reality: real core accepted generated server/client schema", flush=True)
                continue

            outbound = bash_json(manager, environment, "generate_client", str(inbound_file), LOOPBACK)
            if inbound.get("tls", {}).get("enabled"):
                # Trust only the temporary certificate; insecure=true is never
                # used, which keeps this an actual certificate verification.
                if outbound.get("tls", {}).get("insecure", False):
                    raise RuntimeError(f"{protocol}: generated client must not use insecure TLS")
                outbound.setdefault("tls", {})["certificate"] = certificate.read_text(encoding="utf-8")
            client_file = work / (protocol + "-extended-client.json")
            write_json(client_file, client_config(outbound, proxy_port))
            check(core, client_file, work)
            quic = protocol in {"hysteria2", "tuic"}
            with running_core(core, server_file, server_port, work, tcp_probe=not quic):
                with running_core(core, client_file, proxy_port, work):
                    received = fetch_through_socks_retry(proxy_port, target_port, "/probe/" + protocol)
                    if received != payload:
                        raise RuntimeError(f"{protocol}: forwarded HTTP response did not match")
                    if quic:
                        run_udp_smoke(proxy_port, protocol)
            print(f"PASS {protocol}: real server + client forwarded exact HTTP payload over loopback", flush=True)


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
                        if protocol == "shadowsocks":
                            run_udp_smoke(proxy_port, protocol)
                print(f"PASS {protocol}: real server + client forwarded exact HTTP payload over loopback", flush=True)

        # Check schemas first; the local TLS target test below checks forwarding.
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
        run_extended_smoke(core, manager, environment, work, payload)
        run_reality_loopback_smoke(core, manager, environment, work, payload)
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
