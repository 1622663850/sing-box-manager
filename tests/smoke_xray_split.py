#!/usr/bin/env python3
"""Real-core loopback smoke for XHTTP upload/download separation.

All listeners are bound to the IPv4 loopback range (127.0.0.0/8).  The
candidate manager is sourced with an isolated ``XR_ROOT_PREFIX``.  Two TCP
relays forward the upload and download entries to one XHTTP inbound and count
connections, so a successful request proves that both entries were used.

Example (Linux):

    python3 tests/smoke_xray_split.py --core /tmp/xray --manager xray-manager.sh
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from typing import Iterator
from urllib.parse import urlsplit

from smoke_core import (
    LOOPBACK,
    free_port,
    receive_exact,
    tls_material,
    write_json,
)
from smoke_xray import (
    _x25519,
    check_xray,
    client_config,
    running_xray,
    server_config,
)


PROJECT = Path(__file__).resolve().parents[1]
MANAGER_DEFAULT = PROJECT / "xray-manager.sh"
UPLOAD_HOST = "127.0.0.2"
DOWNLOAD_HOST = "127.0.0.3"
UUID = "b7efdc96-87aa-4910-81b4-374b2eecc737"
REALITY_SHORT_ID = "0123456789abcdef"
PAYLOAD = b"XRAY_XHTTP_SPLIT_LOOPBACK_OK:" + (b"0123456789abcdef" * 16384)


def manager_call(
    manager: Path,
    environment: dict[str, str],
    function: str,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            "bash", "-c", 'source "$1"; shift; "$@"',
            "xhttp-split-smoke", str(manager), function, *args,
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=40,
    )
    return result


def manager_json(
    manager: Path,
    environment: dict[str, str],
    function: str,
    *args: str,
) -> dict:
    result = manager_call(manager, environment, function, *args)
    if result.returncode:
        raise RuntimeError(f"manager function failed: {function}")
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"manager function returned invalid JSON: {function}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"manager function returned a non-object: {function}")
    return value


def manager_root(environment: dict[str, str], core: Path) -> Path:
    root = Path(environment["XR_ROOT_PREFIX"])
    mock_bin = root / "mock-bin"
    lib = root / "usr/local/lib/xray-manager"
    config_dir = root / "etc/xray"
    clients = root / "var/lib/xray-manager/clients"
    backups = root / "var/lib/xray-manager/backups"
    for path in (mock_bin, lib, config_dir, clients, backups):
        path.mkdir(parents=True, exist_ok=True)
    core_target = lib / "xray"
    core_target.write_bytes(core.read_bytes())
    core_target.chmod(0o755)
    marker = lib / ".managed"
    marker.write_text("xray-manager-v1\n", encoding="utf-8")
    marker.chmod(0o600)
    systemctl_log = root / "systemctl.log"
    (mock_bin / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$XR_SPLIT_SYSTEMCTL_LOG\"\n"
        "case \" $* \" in *' is-active '*) exit 3;; *' is-enabled '*) exit 1;; esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (mock_bin / "systemctl").chmod(0o755)
    environment["XR_SPLIT_SYSTEMCTL_LOG"] = str(systemctl_log)
    environment["PATH"] = str(mock_bin) + os.pathsep + environment.get("PATH", "")
    return root


def mlkem_seed(core: Path) -> str:
    result = subprocess.run(
        [str(core), "mlkem768"], capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Xray core cannot generate ML-KEM material")
    match = re.search(r"(?im)^\s*seed\s*:\s*(\S+)", result.stdout)
    if not match:
        raise RuntimeError("Xray core returned no ML-KEM seed")
    return match.group(1)


class CountingRelay:
    """Loopback TCP forwarder that records accepted upload/download streams."""

    def __init__(self, listen_host: str, listen_port: int, target_port: int):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_port = target_port
        self.accepted = 0
        self.client_bytes = 0
        self.server_bytes = 0
        self._counter_lock = threading.Lock()
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._threads: list[threading.Thread] = []

    def __enter__(self) -> "CountingRelay":
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.listen_host, self.listen_port))
        server.listen(32)
        server.settimeout(0.2)
        self._server = server
        thread = threading.Thread(target=self._accept, daemon=True)
        thread.start()
        self._threads.append(thread)
        return self

    def _accept(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                client, _peer = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepted += 1
            thread = threading.Thread(target=self._forward, args=(client,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _forward(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection((LOOPBACK, self.target_port), timeout=10)
        except OSError:
            client.close()
            return
        with client, upstream:
            left = threading.Thread(target=self._pipe, args=(client, upstream, "client"), daemon=True)
            right = threading.Thread(target=self._pipe, args=(upstream, client, "server"), daemon=True)
            left.start()
            right.start()
            left.join(timeout=30)
            right.join(timeout=30)

    def _pipe(self, source: socket.socket, destination: socket.socket, direction: str) -> None:
        try:
            source.settimeout(1)
            while True:
                try:
                    data = source.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                with self._counter_lock:
                    if direction == "client":
                        self.client_bytes += len(data)
                    else:
                        self.server_bytes += len(data)
                destination.sendall(data)
        except OSError:
            pass
        finally:
            try:
                destination.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
        for thread in self._threads:
            thread.join(timeout=2)


class CountingUdpRelay:
    """Loopback UDP forwarder with per-client upstream sockets and counters."""

    def __init__(self, listen_host: str, listen_port: int, target_port: int):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_port = target_port
        self.accepted = 0
        self.client_bytes = 0
        self.server_bytes = 0
        self._counter_lock = threading.Lock()
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._peers: dict[tuple[str, int], socket.socket] = {}
        self._threads: list[threading.Thread] = []

    def __enter__(self) -> "CountingUdpRelay":
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.listen_host, self.listen_port))
        server.settimeout(0.2)
        self._server = server
        thread = threading.Thread(target=self._receive_clients, daemon=True)
        thread.start()
        self._threads.append(thread)
        return self

    def _receive_clients(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                data, client = self._server.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            upstream = self._peers.get(client)
            if upstream is None:
                try:
                    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    upstream.settimeout(0.2)
                    upstream.connect((LOOPBACK, self.target_port))
                except OSError:
                    if upstream is not None:
                        upstream.close()
                    continue
                self._peers[client] = upstream
                self.accepted += 1
                thread = threading.Thread(
                    target=self._receive_server,
                    args=(upstream, client),
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
            try:
                with self._counter_lock:
                    self.client_bytes += len(data)
                upstream.send(data)
            except OSError:
                pass

    def _receive_server(self, upstream: socket.socket, client: tuple[str, int]) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                data = upstream.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            try:
                with self._counter_lock:
                    self.server_bytes += len(data)
                self._server.sendto(data, client)
            except OSError:
                return

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
        for upstream in self._peers.values():
            upstream.close()
        for thread in self._threads:
            thread.join(timeout=2)


def run_counting_relay_self_test() -> None:
    """Exercise CountingRelay on any platform before requiring Bash/Xray."""
    target_port = free_port()
    relay_port = free_port()
    while relay_port == target_port:
        relay_port = free_port()
    stop = threading.Event()
    target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    target.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    target.bind((LOOPBACK, target_port))
    target.listen(1)
    target.settimeout(0.2)

    def echo() -> None:
        try:
            while not stop.is_set():
                try:
                    connection, _peer = target.accept()
                    break
                except socket.timeout:
                    continue
            else:
                return
            with connection:
                connection.settimeout(1)
                while not stop.is_set():
                    try:
                        data = connection.recv(65536)
                    except socket.timeout:
                        continue
                    if not data:
                        return
                    connection.sendall(data)
        except (OSError, socket.timeout):
            return

    thread = threading.Thread(target=echo, daemon=True)
    thread.start()
    body = b"counting-relay-self-test:" + (b"0123456789abcdef" * 4096)
    try:
        with CountingRelay(LOOPBACK, relay_port, target_port) as relay:
            with socket.create_connection((LOOPBACK, relay_port), timeout=5) as client:
                client.sendall(body)
                client.shutdown(socket.SHUT_WR)
                received = bytearray()
                while len(received) < len(body):
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    received.extend(chunk)
            if bytes(received) != body:
                raise RuntimeError("CountingRelay loopback self-test payload mismatch")
            if relay.client_bytes < len(body) or relay.server_bytes < len(body):
                raise RuntimeError("CountingRelay loopback self-test byte counters did not advance")
    finally:
        stop.set()
        target.close()
        thread.join(timeout=2)


def run_counting_udp_relay_self_test() -> None:
    """Exercise the UDP relay independently of Bash/Xray on Windows too."""
    target_port = free_port()
    relay_port = free_port()
    while relay_port == target_port:
        relay_port = free_port()
    stop = threading.Event()
    target = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    target.bind((LOOPBACK, target_port))
    target.settimeout(0.2)

    def echo() -> None:
        while not stop.is_set():
            try:
                data, peer = target.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            target.sendto(data, peer)

    thread = threading.Thread(target=echo, daemon=True)
    thread.start()
    body = b"counting-udp-relay-self-test:" + (b"0123456789abcdef" * 1024)
    try:
        with CountingUdpRelay(LOOPBACK, relay_port, target_port) as relay:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.settimeout(5)
                client.sendto(body, (LOOPBACK, relay_port))
                received, _peer = client.recvfrom(len(body) + 1024)
            if received != body:
                raise RuntimeError("CountingUdpRelay loopback self-test payload mismatch")
            if relay.client_bytes < len(body) or relay.server_bytes < len(body):
                raise RuntimeError("CountingUdpRelay loopback self-test byte counters did not advance")
    finally:
        stop.set()
        target.close()
        thread.join(timeout=2)


def safe_error_message(error: BaseException) -> str:
    """Keep CI diagnostics useful without echoing identifiers or key material."""
    message = str(error)
    message = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", "<REDACTED>", message)
    message = re.sub(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])", "<REDACTED>", message)
    message = re.sub(r"https?://\S+", "<REDACTED_URL>", message)
    return message[:180] or "no detail"


@contextmanager
def tls_target(certificate: Path, private_key: Path, port: int) -> Iterator[None]:
    """Local TLS target used only as REALITY camouflage."""
    log = tempfile.NamedTemporaryFile(prefix="xhttp-reality-target-", suffix=".log", delete=False)
    log_path = Path(log.name)
    log.close()
    handle = log_path.open("wb")
    process = subprocess.Popen(
        [
            "openssl", "s_server", "-accept", f"{LOOPBACK}:{port}", "-tls1_3",
            "-cert", str(certificate), "-key", str(private_key),
            "-alpn", "h2,http/1.1", "-quiet", "-ign_eof",
        ],
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("local REALITY camouflage target exited")
            try:
                with socket.create_connection((LOOPBACK, port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("local REALITY camouflage target did not listen")
        yield
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        handle.close()
        try:
            log_path.unlink()
        except OSError:
            pass


@contextmanager
def post_target(payload: bytes) -> Iterator[int]:
    """HTTP target that echoes a large POST body and serves the same GET body."""
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._reply(payload)

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            body = self.rfile.read(length)
            self._reply(body if body else payload)

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


def post_through_socks(proxy_port: int, target_port: int, body: bytes) -> bytes:
    with socket.create_connection((LOOPBACK, proxy_port), timeout=10) as sock:
        sock.sendall(b"\x05\x01\x00")
        if receive_exact(sock, 2) != b"\x05\x00":
            raise RuntimeError("SOCKS5 authentication negotiation failed")
        sock.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(LOOPBACK) + target_port.to_bytes(2, "big"))
        header = receive_exact(sock, 4)
        if header[:3] != b"\x05\x00\x00":
            raise RuntimeError("SOCKS5 CONNECT failed")
        if header[3] == 1:
            receive_exact(sock, 4)
        elif header[3] == 4:
            receive_exact(sock, 16)
        elif header[3] == 3:
            receive_exact(sock, receive_exact(sock, 1)[0])
        else:
            raise RuntimeError("unexpected SOCKS5 address type")
        receive_exact(sock, 2)
        request = (
            b"POST /echo HTTP/1.1\r\n"
            + f"Host: {LOOPBACK}:{target_port}\r\nContent-Length: {len(body)}\r\n"
            .encode("ascii")
            + b"Connection: close\r\n\r\n"
            + body
        )
        sock.sendall(request)
        response = http.client.HTTPResponse(sock)
        response.begin()
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP response: {response.status}")
        return response.read(len(body) + 1024)


def post_through_socks_retry(proxy_port: int, target_port: int, body: bytes) -> bytes:
    last: Exception | None = None
    for _ in range(20):
        try:
            return post_through_socks(proxy_port, target_port, body)
        except (OSError, RuntimeError) as error:
            last = error
            time.sleep(0.25)
    raise RuntimeError(f"POST through SOCKS5 did not complete: {last}")


def generate_inbound(
    manager: Path,
    env: dict[str, str],
    template: str,
    tag: str,
    port: int,
    extra: str,
    private: str,
    short_id: str,
    certificate: Path | None,
    private_key: Path | None,
    decryption: str = "none",
) -> dict:
    return manager_json(
        manager, env, "generate_inbound", template, tag, str(port), UUID,
        extra, private, short_id, "localhost", str(certificate or ""),
        str(private_key or ""), "", "", decryption, "",
    )


def prepare_client(manager: Path, env: dict[str, str], inbound: dict, work: Path, public_key: str) -> dict:
    source = work / "inbound.json"
    write_json(source, inbound)
    return manager_json(manager, env, "generate_client", str(source), UPLOAD_HOST, public_key, "1")


def attach_download(manager: Path, env: dict[str, str], outbound: dict, download: dict, work: Path) -> dict:
    out_file = work / "outbound.json"
    download_file = work / "download.json"
    write_json(out_file, outbound)
    write_json(download_file, download)
    return manager_json(manager, env, "xhttp_download_tools", "attach", str(out_file), str(download_file))


def tls_download(port: int, path: str, alpn: str = "h2") -> dict:
    fingerprint = "unsafe" if alpn == "http/1.1" else "chrome"
    return {
        "address": DOWNLOAD_HOST,
        "port": port,
        "network": "xhttp",
        "security": "tls",
        "tlsSettings": {
            "serverName": "localhost",
            "fingerprint": fingerprint,
            "alpn": [alpn],
            "allowInsecure": False,
        },
        "xhttpSettings": {"path": path, "mode": "auto"},
    }


def reality_download(port: int, path: str, public: str, short_id: str) -> dict:
    return {
        "address": DOWNLOAD_HOST,
        "port": port,
        "network": "xhttp",
        "security": "reality",
        "realitySettings": {
            "serverName": "localhost",
            "fingerprint": "chrome",
            "password": public,
            "shortId": short_id,
        },
        "xhttpSettings": {"path": path, "mode": "auto"},
    }


def prepare_runtime_tls(outbound: dict, certificate: Path) -> None:
    """Trust the temporary certificate while retaining verification."""
    stream = outbound["streamSettings"]
    if stream.get("security") == "tls":
        tls = stream["tlsSettings"]
        tls["serverName"] = "localhost"
        tls["disableSystemRoot"] = True
        tls["certificates"] = [{"certificateFile": str(certificate), "usage": "verify"}]
    download = stream["xhttpSettings"]["extra"]["downloadSettings"]
    if download.get("security") == "tls":
        tls = download["tlsSettings"]
        tls["disableSystemRoot"] = True
        tls["certificates"] = [{"certificateFile": str(certificate), "usage": "verify"}]


def manager_metadata_setup(
    manager: Path, env: dict[str, str], root: Path, inbound: dict,
    server_host: str, public_key: str, download: dict, work: Path,
) -> None:
    clients = root / "var/lib/xray-manager/clients"
    metadata = clients / f"{inbound['tag']}.json"
    write_json(metadata, {"server": server_host, "public_key": public_key, "inbound": inbound})
    metadata.chmod(0o600)
    download_file = work / "metadata-download.json"
    write_json(download_file, download)
    result = manager_call(manager, env, "set_xhttp_split_locked", inbound["tag"], str(download_file))
    if result.returncode:
        raise RuntimeError("setting XHTTP metadata failed")


def exported_clients(manager: Path, env: dict[str, str], tag: str, work: Path) -> tuple[dict, str]:
    json_result = manager_call(manager, env, "show_client", tag, UPLOAD_HOST, "json-only", "1")
    if json_result.returncode:
        raise RuntimeError("show_client JSON export failed")
    try:
        outbound = json.loads(json_result.stdout)
    except ValueError as error:
        raise RuntimeError("show_client returned invalid JSON") from error
    if not isinstance(outbound, dict):
        raise RuntimeError("show_client returned non-object JSON")
    link_result = manager_call(manager, env, "show_client", tag, UPLOAD_HOST, "link-only", "1")
    if link_result.returncode:
        raise RuntimeError("show_client link export failed")
    link = link_result.stdout.strip()
    if not link.startswith("vless://") or "\n" in link or "\r" in link:
        raise RuntimeError("show_client returned invalid VLESS URI")
    if "extra=" not in link:
        raise RuntimeError("show_client link did not include XHTTP extra")
    write_json(work / "exported.json", outbound)
    (work / "exported.txt").write_text(link + "\n", encoding="utf-8")
    return outbound, link


def parse_exported_link(manager: Path, env: dict[str, str], link: str, work: Path) -> dict:
    parsed = manager_call(manager, env, "parse_outbound_links", str(work / "exported.txt"))
    if parsed.returncode:
        raise RuntimeError("exported VLESS URI did not import")
    native = json.loads(parsed.stdout)
    if not isinstance(native, list) or len(native) != 1:
        raise RuntimeError("link import returned an unexpected number of outbounds")
    return native[0]


def batch_exports(manager: Path, env: dict[str, str], output_root: Path) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    for fmt, suffix in (("json", "json"), ("links", "txt"), ("base64", "b64")):
        target = output_root / f"batch.{suffix}"
        result = manager_call(manager, env, "exports", fmt, str(target))
        if result.returncode or not target.is_file():
            raise RuntimeError(f"batch {fmt} export failed")
        raw = target.read_bytes()
        if fmt == "json":
            value = json.loads(raw)
            if not value.get("outbounds") or "downloadSettings" not in json.dumps(value):
                raise RuntimeError("batch JSON export lost downloadSettings")
        elif fmt == "links":
            if b"vless://" not in raw or b"extra=" not in raw:
                raise RuntimeError("batch link export lost XHTTP extra")
        else:
            decoded = base64.b64decode(raw)
            if b"vless://" not in decoded or b"extra=" not in decoded:
                raise RuntimeError("batch base64 export lost XHTTP extra")


def runtime_case(
    core: Path,
    manager: Path,
    env: dict[str, str],
    root: Path,
    work: Path,
    label: str,
    security: str,
    include_vision: bool,
    certificate: Path,
    private_key: Path,
    upload_mode: str = "auto",
    download_alpn: str = "h2",
) -> None:
    server_port = free_port()
    download_port = free_port()
    while download_port == server_port:
        download_port = free_port()
    camouflage_port = free_port()
    path = "/xhttp-split-" + label
    public = ""
    private = ""
    decryption = "none"
    if security == "reality":
        private, public = _x25519(core)
        if include_vision:
            decryption = "mlkem768x25519plus.native.600s." + mlkem_seed(core)
        template = "vless-xhttp-reality-vision" if include_vision else "vless-xhttp-reality"
    else:
        template = "vless-xhttp-tls"
    inbound = generate_inbound(
        manager, env, template, "split-" + label, server_port, path,
        private, "0123456789abcdef" if security == "reality" else "",
        certificate if security == "tls" else None,
        private_key if security == "tls" else None,
        decryption,
    )
    inbound["listen"] = LOOPBACK
    inbound["streamSettings"]["xhttpSettings"]["mode"] = upload_mode
    if security == "tls":
        inbound["streamSettings"]["tlsSettings"]["alpn"] = (
            ["h3"] if download_alpn == "h3" else ["h2", "h3", "http/1.1"]
        )
    if security == "reality":
        reality = inbound["streamSettings"]["realitySettings"]
        reality["target"] = f"{LOOPBACK}:{camouflage_port}"
        reality["serverNames"] = ["localhost"]
    server_file = work / (label + "-server.json")
    with post_target(PAYLOAD) as http_port:
        # The manager config is the current server config for this case.  A
        # stale empty config makes metadata validation report a missing tag.
        manager_config = root / "etc/xray/config.json"
        write_json(manager_config, server_config(inbound, (http_port,)))
        clients_root = root / "var/lib/xray-manager/clients"
        for old_metadata in clients_root.glob("*.json"):
            old_metadata.unlink()
        write_json(server_file, server_config(inbound, (http_port,)))
        check_xray(core, server_file, work)
        if download_alpn == "h3":
            upload_relay = CountingUdpRelay(UPLOAD_HOST, server_port, server_port)
        else:
            upload_relay = CountingRelay(UPLOAD_HOST, server_port, server_port)
        if download_alpn == "h3":
            download_relay = CountingUdpRelay(DOWNLOAD_HOST, download_port, server_port)
        else:
            download_relay = CountingRelay(DOWNLOAD_HOST, download_port, server_port)
        target_context = tls_target(certificate, private_key, camouflage_port) if security == "reality" else _null_context()
        server_probe = None if download_alpn == "h3" else server_port
        with target_context, running_xray(core, server_file, work, server_probe, f"{label}-server"):
            with upload_relay, download_relay:
                metadata_download = reality_download(download_port, path, public, REALITY_SHORT_ID) if security == "reality" else tls_download(download_port, path, download_alpn)
                clients_root = root / "var/lib/xray-manager/clients"
                metadata = clients_root / f"{inbound['tag']}.json"
                write_json(metadata, {"server": UPLOAD_HOST, "public_key": public, "inbound": inbound})
                bad_download = json.loads(json.dumps(metadata_download))
                bad_download["xhttpSettings"]["path"] = "/wrong-path"
                bad_file = work / f"{label}-bad-download.json"
                write_json(bad_file, bad_download)
                before_config = (root / "etc/xray/config.json").read_bytes()
                rejected = manager_call(manager, env, "set_xhttp_split_locked", inbound["tag"], str(bad_file))
                if rejected.returncode == 0:
                    raise RuntimeError("invalid XHTTP download path was accepted")
                if "xhttp_split" in json.loads(metadata.read_text(encoding="utf-8")):
                    raise RuntimeError("invalid XHTTP metadata was persisted")
                if (root / "etc/xray/config.json").read_bytes() != before_config:
                    raise RuntimeError("invalid XHTTP metadata changed the server config")
                manager_metadata_setup(manager, env, root, inbound, UPLOAD_HOST, public, metadata_download, work)
                systemctl_log = root / "systemctl.log"
                if systemctl_log.exists():
                    control_actions = {"start", "restart", "stop", "enable", "disable", "reload", "daemon-reload"}
                    for line in systemctl_log.read_text(encoding="utf-8").splitlines():
                        words = line.split()
                        if any(word in control_actions for word in words):
                            raise RuntimeError("XHTTP metadata save called a service control action")
                exported, link = exported_clients(manager, env, inbound["tag"], work)
                if include_vision and not exported["settings"].get("encryption", "").startswith("mlkem768x25519plus."):
                    raise RuntimeError("exported VLESS Encryption field was lost")
                if "downloadSettings" not in json.dumps(exported):
                    raise RuntimeError("exported JSON omitted downloadSettings")
                imported = parse_exported_link(manager, env, link, work)
                if "downloadSettings" not in json.dumps(imported):
                    raise RuntimeError("imported link omitted downloadSettings")
                for name, outbound in (("json", exported), ("uri", imported)):
                    before_upload = (upload_relay.client_bytes, upload_relay.server_bytes)
                    before_download = (download_relay.client_bytes, download_relay.server_bytes)
                    if security == "tls":
                        prepare_runtime_tls(outbound, certificate)
                    client_file = work / f"{label}-{name}-client.json"
                    write_json(client_file, client_config(outbound, free_port()))
                    check_xray(core, client_file, work)
                    proxy_port = json.loads(client_file.read_text(encoding="utf-8"))["inbounds"][0]["port"]
                    with running_xray(core, client_file, work, proxy_port, f"{label}-{name}-client"):
                        received = post_through_socks_retry(proxy_port, http_port, PAYLOAD)
                        if received != PAYLOAD:
                            raise RuntimeError(f"{label} {name} payload mismatch")
                    if upload_relay.accepted < 1 or download_relay.accepted < 1:
                        raise RuntimeError(f"{label} {name} did not use both upload/download relays")
                    if upload_relay.client_bytes <= before_upload[0] or upload_relay.server_bytes <= before_upload[1]:
                        raise RuntimeError(f"{label} {name} upload relay carried no bidirectional bytes")
                    if download_relay.client_bytes <= before_download[0] or download_relay.server_bytes <= before_download[1]:
                        raise RuntimeError(f"{label} {name} download relay carried no bidirectional bytes")
                batch_exports(manager, env, work / f"{label}-exports")
                closed = manager_call(manager, env, "set_xhttp_split_locked", inbound["tag"], "-")
                if closed.returncode:
                    raise RuntimeError("closing XHTTP metadata failed")
                no_split = manager_call(manager, env, "show_client", inbound["tag"], UPLOAD_HOST, "json-only", "1")
                if no_split.returncode or "downloadSettings" in no_split.stdout:
                    raise RuntimeError("closing split did not restore normal export")
    transport_label = "dual-UDP" if download_alpn == "h3" else "loopback relays"
    print(f"PASS xhttp split {label}: JSON/URI export and {transport_label}", flush=True)


@contextmanager
def _null_context() -> Iterator[None]:
    yield


def run(core: Path, manager: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="xray-xhttp-split-") as temporary:
        work = Path(temporary)
        root = work / "manager-root"
        env = os.environ.copy()
        env["XR_ROOT_PREFIX"] = str(root)
        manager_root(env, core)
        config_path = root / "etc/xray/config.json"
        write_json(config_path, {"inbounds": [], "outbounds": [{"protocol": "freedom", "tag": "direct", "settings": {}}]})
        cert, key = tls_material(work)
        runtime_case(core, manager, env, root, work, "tls", "tls", False, cert, key)
        runtime_case(core, manager, env, root, work, "tls-packet-up", "tls", False, cert, key, upload_mode="packet-up")
        runtime_case(core, manager, env, root, work, "tls-http11", "tls", False, cert, key, download_alpn="http/1.1")
        runtime_case(core, manager, env, root, work, "tls-h3", "tls", False, cert, key, upload_mode="packet-up", download_alpn="h3")
        runtime_case(core, manager, env, root, work, "reality", "reality", False, cert, key)
        runtime_case(core, manager, env, root, work, "reality-vision", "reality", True, cert, key)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--manager", type=Path, default=MANAGER_DEFAULT)
    args = parser.parse_args()
    core = args.core.expanduser().resolve()
    manager = args.manager.expanduser().resolve()
    try:
        run_counting_relay_self_test()
        run_counting_udp_relay_self_test()
    except Exception as error:
        print(f"FAIL relay self-test: {safe_error_message(error)}", file=sys.stderr)
        return 1
    if not core.is_file() or not os.access(core, os.X_OK) or not manager.is_file():
        print("FAIL xhttp split smoke: invalid core or manager path", file=sys.stderr)
        return 1
    try:
        run(core, manager)
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        locations = ",".join(Path(frame.filename).name + ":" + str(frame.lineno) for frame in frames[-3:])
        print(f"FAIL xhttp split smoke: {type(error).__name__} at {locations}: {safe_error_message(error)}", file=sys.stderr)
        return 1
    print("PASS xhttp split smoke: TLS, REALITY, Vision, exports, and metadata", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
