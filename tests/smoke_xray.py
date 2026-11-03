#!/usr/bin/env python3
"""Real Xray loopback smoke test for the standalone Xray manager.

The manager is sourced only for its pure protocol generators.  Every Xray
server and client is started with a temporary configuration and all network
endpoints are loopback-only.  The server's Freedom outbound has a blackhole
fallback and an exact route for the temporary HTTP/UDP target, so this smoke
test cannot accidentally use a production private address.

Example (Linux):
    python3 tests/smoke_xray.py --core /usr/local/bin/xray

The optional VLESS REALITY case is attempted when the Xray binary exposes the
``x25519`` command and is reported as a fixed ``SKIP`` otherwise.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from typing import Iterator

from smoke_core import (
    LOOPBACK,
    check_udp_through_socks,
    fetch_through_socks_retry,
    free_port,
    http_target,
    tls_material,
    udp_echo_target,
    write_json,
)


PROJECT = Path(__file__).resolve().parents[1]
MANAGER_DEFAULT = PROJECT / "xray-manager.sh"
HTTP_PAYLOAD = b"XRAY_LOOPBACK_SMOKE_OK"


class OptionalRealityUnavailable(RuntimeError):
    """The optional REALITY fixture cannot run on this Xray build."""


def _manager_result(
    manager: Path,
    environment: dict[str, str],
    function: str,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; shift; "$@"',
            "xray-smoke",
            str(manager),
            function,
            *args,
        ],
        env=environment,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    if result.returncode:
        # Do not include stderr: manager diagnostics can contain credentials.
        raise RuntimeError("Xray manager function failed")
    return result


def manager_json(
    manager: Path,
    environment: dict[str, str],
    function: str,
    *args: str,
) -> dict:
    result = _manager_result(manager, environment, function, *args)
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise RuntimeError("Xray manager returned invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("Xray manager returned a non-object")
    return value


def manager_link(
    manager: Path,
    environment: dict[str, str],
    outbound_file: Path,
    label: str,
    expected_prefix: str,
) -> None:
    result = _manager_result(manager, environment, "client_link", str(outbound_file), label)
    link = result.stdout.strip()
    if not link.startswith(expected_prefix) or "\n" in link or "\r" in link:
        raise RuntimeError("Xray manager returned an invalid client link")


def check_xray(core: Path, config: Path, work: Path) -> None:
    result = subprocess.run(
        [str(core), "run", "-test", "-config", str(config)],
        cwd=work,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Xray configuration check failed")


def _wait_tcp(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Xray process exited before its listener was ready")
        try:
            with socket.create_connection((LOOPBACK, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("Xray listener did not become ready")


@contextmanager
def running_xray(
    core: Path,
    config: Path,
    work: Path,
    probe_port: int | None,
    label: str,
) -> Iterator[None]:
    log_path = work / (label + ".log")
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(core), "run", "-config", str(config)],
            cwd=work,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            if probe_port is None:
                # Hysteria listens on UDP; a TCP connect is not a readiness
                # probe.  Keep this bounded while still catching fast exits.
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Xray UDP process exited before readiness")
                    time.sleep(0.05)
            else:
                _wait_tcp(process, probe_port)
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@contextmanager
def quiet_tls_target(certificate: Path, private_key: Path, port: int, work: Path) -> Iterator[None]:
    log_path = work / (f"reality-target-{port}.log")
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [
                "openssl", "s_server", "-accept", f"{LOOPBACK}:{port}",
                "-tls1_3", "-cert", str(certificate), "-key", str(private_key),
                "-alpn", "h2,http/1.1", "-quiet", "-ign_eof",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_tcp(process, port)
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def server_config(inbound: dict, target_ports: tuple[int, ...]) -> dict:
    if not target_ports:
        raise RuntimeError("server target port list is empty")
    port_spec = ",".join(str(port) for port in target_ports)
    # The first outbound is the safe fallback.  Only the exact loopback target
    # ports are routed to Freedom; no other private destination is opened.
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [inbound],
        "outbounds": [
            {
                "tag": "block-private",
                "protocol": "blackhole",
                "settings": {"response": {"type": "none"}},
            },
            {
                "tag": "direct",
                "protocol": "freedom",
                "settings": {
                    # The fixed Xray build blocks private destinations by
                    # default.  Permit only this smoke target, separately for
                    # TCP and UDP; routing rules alone must not bypass it.
                    "finalRules": [
                        {
                            "action": "allow",
                            "network": "tcp",
                            "ip": ["127.0.0.1/32"],
                            "port": port_spec,
                        },
                        {
                            "action": "allow",
                            "network": "udp",
                            "ip": ["127.0.0.1/32"],
                            "port": port_spec,
                        },
                    ],
                },
                "streamSettings": {"sockopt": {"domainStrategy": "UseIPv4"}},
            },
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {
                    "type": "field",
                    "ip": ["127.0.0.1/32"],
                    "port": ",".join(str(port) for port in target_ports),
                    "outboundTag": "direct",
                },
            ],
        },
    }


def client_config(outbound: dict, proxy_port: int) -> dict:
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "local-socks",
                "listen": LOOPBACK,
                "port": proxy_port,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
            },
        ],
        "outbounds": [outbound],
        "routing": {"domainStrategy": "AsIs"},
    }


def prepare_tls_outbound(outbound: dict, certificate: Path, expected: str) -> None:
    stream = outbound.get("streamSettings")
    if not isinstance(stream, dict) or stream.get("security") != "tls":
        raise RuntimeError(f"{expected} did not export Xray TLS stream settings")
    tls = stream.get("tlsSettings")
    if not isinstance(tls, dict):
        raise RuntimeError(f"{expected} did not export Xray TLS settings")
    if tls.get("allowInsecure") is True:
        raise RuntimeError(f"{expected} disabled TLS verification")
    tls["serverName"] = "localhost"
    tls.pop("allowInsecure", None)
    tls["disableSystemRoot"] = True
    # Xray accepts usage=verify for a temporary trust anchor.  This keeps the
    # test certificate-verified rather than hiding errors with allowInsecure.
    tls["certificates"] = [{"certificateFile": str(certificate), "usage": "verify"}]


def validate_outbound(outbound: dict, expected_protocol: str) -> None:
    if not isinstance(outbound, dict):
        raise RuntimeError("Xray client export is not an object")
    if outbound.get("tag") != "proxy" or outbound.get("protocol") != expected_protocol:
        raise RuntimeError("Xray client export is not a native proxy outbound")
    if not isinstance(outbound.get("settings"), dict):
        raise RuntimeError("Xray client export has invalid settings")


def _uuid() -> str:
    return str(uuid.uuid4())


def run_case(
    core: Path,
    manager: Path,
    environment: dict[str, str],
    work: Path,
    label: str,
    template: str,
    expected_protocol: str,
    credential: str,
    extra: str,
    tls: bool,
    udp: bool,
    http_port: int,
    udp_port: int | None,
    certificate: Path | None,
    private_key: Path | None,
) -> None:
    server_port, proxy_port = free_port(), free_port()
    while proxy_port == server_port:
        proxy_port = free_port()
    if udp and udp_port is None:
        raise RuntimeError("UDP case has no target port")
    if tls and (certificate is None or private_key is None):
        raise RuntimeError("TLS case has no certificate material")

    inbound = manager_json(
        manager,
        environment,
        "generate_inbound",
        template,
        "xray-" + label,
        str(server_port),
        credential,
        extra,
        "",
        "",
        "localhost" if tls else "",
        str(certificate or ""),
        str(private_key or ""),
        "",
        "",
    )
    if not isinstance(inbound.get("protocol"), str):
        raise RuntimeError("Xray manager returned an invalid inbound")
    if template == "hysteria" and inbound.get("settings", {}).get("version") != 2:
        raise RuntimeError("Xray Hysteria inbound is not version 2")
    inbound["listen"] = LOOPBACK
    inbound_file = work / (label + "-inbound.json")
    write_json(inbound_file, inbound)

    outbound = manager_json(
        manager,
        environment,
        "generate_client",
        str(inbound_file),
        LOOPBACK,
        "",
        "1",
    )
    validate_outbound(outbound, expected_protocol)
    if template == "hysteria" and outbound.get("settings", {}).get("version") != 2:
        raise RuntimeError("Xray Hysteria outbound is not version 2")
    if tls:
        assert certificate is not None
        prepare_tls_outbound(outbound, certificate, label)
    outbound_file = work / (label + "-outbound.json")
    write_json(outbound_file, outbound)
    link_prefix = {
        "vless": "vless://",
        "vmess": "vmess://",
        "shadowsocks": "ss://",
        "trojan": "trojan://",
        "hysteria": "hysteria2://",
    }[expected_protocol]
    manager_link(manager, environment, outbound_file, "xray-" + label, link_prefix)

    target_ports = (http_port,) if udp_port is None else (http_port, udp_port)
    server_file = work / (label + "-server.json")
    client_file = work / (label + "-client.json")
    write_json(server_file, server_config(inbound, target_ports))
    write_json(client_file, client_config(outbound, proxy_port))
    check_xray(core, server_file, work)
    check_xray(core, client_file, work)

    server_probe = None if template == "hysteria" else server_port
    with running_xray(core, server_file, work, server_probe, label + "-server"):
        with running_xray(core, client_file, work, proxy_port, label + "-client"):
            received = fetch_through_socks_retry(proxy_port, http_port, "/" + label)
            if received != HTTP_PAYLOAD:
                raise RuntimeError("Xray HTTP loopback payload mismatch")
            if udp and udp_port is not None:
                check_udp_through_socks(proxy_port, udp_port)
    print(f"PASS xray {label}: native server/client forwarded loopback traffic", flush=True)


def _x25519(core: Path) -> tuple[str, str]:
    result = subprocess.run(
        [str(core), "x25519"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode:
        raise OptionalRealityUnavailable("Xray x25519 is unavailable")
    private_match = re.search(r"(?im)^\s*private\s*key\s*:\s*(\S+)", result.stdout)
    public_match = re.search(r"(?im)^\s*public\s*key\s*:\s*(\S+)", result.stdout)
    if not private_match or not public_match:
        raise OptionalRealityUnavailable("Xray x25519 output is unavailable")
    return private_match.group(1), public_match.group(1)


def _set_reality_target(inbound: dict, target_port: int) -> None:
    stream = inbound.get("streamSettings")
    reality = stream.get("realitySettings") if isinstance(stream, dict) else None
    if not isinstance(reality, dict):
        raise OptionalRealityUnavailable("manager did not export REALITY settings")
    target = f"{LOOPBACK}:{target_port}"
    if "dest" in reality:
        reality["dest"] = target
    elif "target" in reality:
        reality["target"] = target
    else:
        raise OptionalRealityUnavailable("manager REALITY target field is unavailable")
    reality["serverNames"] = ["localhost"]


def run_reality_case(
    core: Path,
    manager: Path,
    environment: dict[str, str],
    work: Path,
    certificate: Path,
    private_key: Path,
    http_port: int,
) -> None:
    private, public = _x25519(core)
    server_port, proxy_port, camouflage_port = free_port(), free_port(), free_port()
    while len({server_port, proxy_port, camouflage_port, http_port}) != 4:
        server_port, proxy_port, camouflage_port = free_port(), free_port(), free_port()
    inbound = manager_json(
        manager,
        environment,
        "generate_inbound",
        "vless-reality",
        "xray-vless-reality",
        str(server_port),
        _uuid(),
        "localhost",
        private,
        "0123456789abcdef",
        "localhost",
        "",
        "",
        "",
        "",
    )
    inbound["listen"] = LOOPBACK
    _set_reality_target(inbound, camouflage_port)
    inbound_file = work / "vless-reality-inbound.json"
    write_json(inbound_file, inbound)
    outbound = manager_json(
        manager,
        environment,
        "generate_client",
        str(inbound_file),
        LOOPBACK,
        public,
        "1",
    )
    validate_outbound(outbound, "vless")
    outbound_file = work / "vless-reality-outbound.json"
    write_json(outbound_file, outbound)
    manager_link(manager, environment, outbound_file, "xray-vless-reality", "vless://")
    server_file = work / "vless-reality-server.json"
    client_file = work / "vless-reality-client.json"
    write_json(server_file, server_config(inbound, (http_port,)))
    write_json(client_file, client_config(outbound, proxy_port))
    check_xray(core, server_file, work)
    check_xray(core, client_file, work)
    with quiet_tls_target(certificate, private_key, camouflage_port, work):
        with running_xray(core, server_file, work, server_port, "vless-reality-server"):
            with running_xray(core, client_file, work, proxy_port, "vless-reality-client"):
                if fetch_through_socks_retry(proxy_port, http_port, "/vless-reality") != HTTP_PAYLOAD:
                    raise RuntimeError("Xray REALITY loopback payload mismatch")
    print("PASS xray vless-reality: Vision loopback handshake and forwarding", flush=True)


def run(core: Path, manager: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="xray-loopback-") as temporary:
        work = Path(temporary)
        root_prefix = work / "manager-prefix"
        environment = os.environ.copy()
        environment["SB_ROOT_PREFIX"] = str(root_prefix)
        environment["CORE_BIN"] = str(core)
        certificate, private_key = tls_material(work)
        with http_target(HTTP_PAYLOAD) as http_port:
            cases = (
                ("vless-raw", "vless-tcp", "vless", _uuid(), "", False, False),
                ("vmess-raw", "vmess-tcp", "vmess", _uuid(), "", False, False),
                (
                    "ss2022", "shadowsocks", "shadowsocks",
                    base64.b64encode(os.urandom(16)).decode("ascii"),
                    "", False, True,
                ),
                ("trojan-tls", "trojan", "trojan", "xray-trojan-password", "", True, False),
                ("vless-ws-tls", "vless-ws-tls", "vless", _uuid(), "/xray-ws", True, False),
                ("vless-grpc-tls", "vless-grpc-tls", "vless", _uuid(), "XrayGrpc", True, False),
                ("vless-xhttp-tls", "vless-xhttp-tls", "vless", _uuid(), "/xray-xhttp", True, False),
                ("hysteria2", "hysteria", "hysteria", "xray-hysteria-password", "", True, True),
            )
            for label, template, protocol, credential, extra, use_tls, use_udp in cases:
                if use_udp:
                    with udp_echo_target() as udp_port:
                        run_case(
                            core, manager, environment, work, label, template, protocol,
                            credential, extra, use_tls, use_udp, http_port, udp_port,
                            certificate, private_key,
                        )
                else:
                    run_case(
                        core, manager, environment, work, label, template, protocol,
                        credential, extra, use_tls, use_udp, http_port, None,
                        certificate, private_key,
                    )
            try:
                run_reality_case(core, manager, environment, work, certificate, private_key, http_port)
            except OptionalRealityUnavailable:
                print("SKIP xray vless-reality: optional x25519/REALITY fixture unavailable", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path, help="Xray v26.10.31 executable")
    parser.add_argument("--manager", type=Path, default=MANAGER_DEFAULT)
    args = parser.parse_args()
    core, manager = args.core.expanduser().resolve(), args.manager.expanduser().resolve()
    if not core.is_file() or not os.access(core, os.X_OK) or not manager.is_file():
        print("FAIL xray smoke: invalid core or manager path", file=sys.stderr)
        return 1
    try:
        run(core, manager)
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        locations = ",".join(Path(frame.filename).name + ":" + str(frame.lineno) for frame in frames[-3:])
        print(f"FAIL xray smoke: {type(error).__name__} at {locations}", file=sys.stderr)
        return 1
    print("PASS xray smoke: all required loopback protocols passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
