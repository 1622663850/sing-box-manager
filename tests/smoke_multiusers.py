#!/usr/bin/env python3
"""Real-core multi-user authentication smoke tests on loopback only.

The script intentionally emits only fixed PASS/FAIL labels. Credentials,
configuration JSON, private keys and core logs stay inside one temporary
directory and are never printed.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Iterator

from smoke_core import (
    LOOPBACK,
    bash_json,
    check,
    check_udp_through_socks,
    client_config,
    command,
    fetch_through_socks,
    free_port,
    http_target,
    udp_echo_target,
    server_config,
    tls_material,
    write_json,
)


PROTOCOLS = ("vless-tcp", "vmess-tcp", "hysteria2", "tuic", "anytls")


def quiet_wait_for_listener(process: subprocess.Popen, port: int, tcp_probe: bool) -> None:
    deadline = time.monotonic() + (10 if tcp_probe else 1)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("core process exited before listening")
        if not tcp_probe:
            time.sleep(0.1)
            continue
        try:
            with socket.create_connection((LOOPBACK, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    if not tcp_probe and process.poll() is None:
        return
    raise RuntimeError("core listener did not become ready")


@contextmanager
def quiet_core(core: Path, config: Path, port: int, work: Path, label: str, tcp_probe: bool = True) -> Iterator[None]:
    log_path = work / (label + ".log")
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(core), "run", "-D", str(work), "-c", str(config)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        )
        try:
            quiet_wait_for_listener(process, port, tcp_probe)
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def request_with_retries(proxy_port: int, target_port: int, path: str) -> bytes:
    last_error: Exception | None = None
    for _ in range(8):
        try:
            return fetch_through_socks(proxy_port, target_port, path)
        except (OSError, RuntimeError) as error:
            last_error = error
            time.sleep(0.2)
    raise RuntimeError("valid user request did not complete")


def expect_rejected(proxy_port: int, target_port: int, path: str) -> None:
    try:
        fetch_through_socks(proxy_port, target_port, path)
    except (OSError, RuntimeError):
        return
    raise RuntimeError("invalid user was accepted")


def generated_server(
    core: Path,
    manager: Path,
    environment: dict[str, str],
    protocol: str,
    port: int,
    certificate: Path | None,
    private_key: Path | None,
) -> tuple[dict, list[dict], list[str]]:
    uuid1 = command([str(core), "generate", "uuid"]).strip()
    uuid2 = command([str(core), "generate", "uuid"]).strip()
    password1 = command([str(core), "generate", "rand", "--base64", "16"]).strip()
    password2 = command([str(core), "generate", "rand", "--base64", "16"]).strip()
    cert = str(certificate or "")
    key = str(private_key or "")
    if protocol in {"vless-tcp", "vmess-tcp"}:
        first_credential, first_extra, first_password = uuid1, "", ""
    elif protocol == "hysteria2":
        first_credential, first_extra, first_password = password1, "", ""
    elif protocol == "tuic":
        first_credential, first_extra, first_password = uuid1, "", password1
    elif protocol == "anytls":
        first_credential, first_extra, first_password = password1, "", ""
    else:
        raise RuntimeError("unsupported multi-user protocol")
    inbound = bash_json(
        manager, environment, "generate_inbound", protocol, "multi-" + protocol, str(port),
        first_credential, first_extra, "", "", "localhost" if certificate else "", cert, key, first_password,
    )
    if protocol in {"vless-tcp", "vmess-tcp"}:
        second = {"uuid": uuid2}
        if protocol == "vmess-tcp":
            second["alterId"] = 0
    elif protocol == "tuic":
        second = {"uuid": uuid2, "password": password2}
    else:
        second = {"password": password2}
    second["name"] = "multi-user-2"
    inbound["users"].append(second)
    if protocol == "hysteria2":
        inbound["obfs"] = {"type": "salamander", "password": "multi-salamander-password"}
        inbound["ignore_client_bandwidth"] = True
    elif protocol == "tuic":
        inbound["congestion_control"] = "bbr"
    inbound["listen"] = LOOPBACK
    credentials = [uuid1 if protocol in {"vless-tcp", "vmess-tcp", "tuic"} else password1,
                   uuid2 if protocol in {"vless-tcp", "vmess-tcp", "tuic"} else password2]
    if protocol == "tuic":
        credentials = [uuid1 + ":" + password1, uuid2 + ":" + password2]
    return inbound, [inbound["users"][0], second], credentials


def selected_outbound(
    manager: Path,
    environment: dict[str, str],
    inbound_path: Path,
    index: int,
    certificate: Path | None,
) -> dict:
    outbound = bash_json(manager, environment, "generate_client", str(inbound_path), LOOPBACK, "", str(index))
    if outbound.get("tls", {}).get("enabled"):
        if outbound.get("tls", {}).get("insecure", False):
            raise RuntimeError("generated client uses insecure TLS")
        outbound.setdefault("tls", {})["certificate"] = certificate.read_text(encoding="utf-8")
    return outbound


def wrong_outbound(outbound: dict, protocol: str) -> dict:
    value = json.loads(json.dumps(outbound))
    if protocol in {"vless-tcp", "vmess-tcp"}:
        value["uuid"] = "00000000-0000-4000-8000-000000000001"
    elif protocol == "tuic":
        value["uuid"] = "00000000-0000-4000-8000-000000000001"
        value["password"] = "wrong-multi-user-password"
    else:
        value["password"] = "wrong-multi-user-password"
    return value


def run_protocol(
    core: Path,
    manager: Path,
    environment: dict[str, str],
    work: Path,
    protocol: str,
    payload: bytes,
    certificate: Path | None,
    private_key: Path | None,
) -> None:
    server_port, proxy1, proxy2 = free_port(), free_port(), free_port()
    while proxy1 in {server_port, proxy2}:
        proxy1 = free_port()
    while proxy2 == server_port:
        proxy2 = free_port()
    inbound, _, _ = generated_server(core, manager, environment, protocol, server_port, certificate, private_key)
    inbound_path = work / (protocol + "-multi-inbound.json")
    server_path = work / (protocol + "-multi-server.json")
    write_json(inbound_path, inbound)
    write_json(server_path, server_config(inbound))
    check(core, server_path, work)
    client_paths: list[Path] = []
    clients: list[dict] = []
    for index, proxy_port in ((1, proxy1), (2, proxy2)):
        outbound = selected_outbound(manager, environment, inbound_path, index, certificate)
        clients.append(outbound)
        client_path = work / (protocol + f"-multi-client-{index}.json")
        write_json(client_path, client_config(outbound, proxy_port))
        check(core, client_path, work)
        client_paths.append(client_path)
    quic = protocol in {"hysteria2", "tuic"}
    with http_target(payload) as target_port:
        with udp_echo_target() as udp_target:
            with quiet_core(core, server_path, server_port, work, protocol + "-multi-server", tcp_probe=not quic):
                with quiet_core(core, client_paths[0], proxy1, work, protocol + "-multi-client-1"):
                    received = request_with_retries(proxy1, target_port, "/multi/" + protocol + "/1")
                    if received != payload:
                        raise RuntimeError("first user forwarding mismatch")
                    if quic:
                        check_udp_through_socks(proxy1, udp_target)
                with quiet_core(core, client_paths[1], proxy2, work, protocol + "-multi-client-2"):
                    received = request_with_retries(proxy2, target_port, "/multi/" + protocol + "/2")
                    if received != payload:
                        raise RuntimeError("second user forwarding mismatch")
                wrong_path = work / (protocol + "-multi-client-wrong.json")
                write_json(wrong_path, client_config(wrong_outbound(clients[1], protocol), free_port()))
                check(core, wrong_path, work)
                # Authentication rejection is exercised with a short single request.
                wrong_proxy_port = json.loads(wrong_path.read_text())["inbounds"][0]["listen_port"]
                with quiet_core(core, wrong_path, wrong_proxy_port, work, protocol + "-multi-client-wrong"):
                    expect_rejected(wrong_proxy_port, target_port, "/multi/" + protocol + "/wrong")
    print(f"PASS multi-user {protocol}: two credentials authenticated and wrong credential rejected", flush=True)

    if protocol == "vless-tcp":
        one_user = json.loads(json.dumps(inbound))
        one_user["users"] = one_user["users"][:1]
        delete_server = work / "vless-tcp-delete-second-server.json"
        write_json(delete_server, server_config(one_user))
        check(core, delete_server, work)
        with http_target(payload) as target_port:
            with quiet_core(core, delete_server, server_port, work, "vless-delete-server"):
                with quiet_core(core, client_paths[0], proxy1, work, "vless-delete-client-1"):
                    if request_with_retries(proxy1, target_port, "/multi/vless/delete/1") != payload:
                        raise RuntimeError("first user stopped working after second user removal")
                with quiet_core(core, client_paths[1], proxy2, work, "vless-delete-client-2"):
                    expect_rejected(proxy2, target_port, "/multi/vless/delete/2")
        print("PASS multi-user vless-tcp: removed second user, first remained valid", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--manager", type=Path, default=Path(__file__).resolve().parents[1] / "sing-box-manager.sh")
    args = parser.parse_args()
    core, manager = args.core.expanduser().resolve(), args.manager.expanduser().resolve()
    if not core.is_file() or not manager.is_file():
        print("FAIL multi-user smoke", file=sys.stderr)
        return 1
    try:
        with tempfile.TemporaryDirectory(prefix="sing-box-multiusers-") as temporary:
            work = Path(temporary)
            environment = os.environ.copy()
            environment["SB_ROOT_PREFIX"] = str(work / "source-prefix")
            payload = b"SING_BOX_MULTIUSER_SMOKE:" + b"x" * 64
            certificate, private_key = tls_material(work)
            for protocol in PROTOCOLS:
                run_protocol(core, manager, environment, work, protocol, payload, certificate, private_key)
    except Exception:
        print("FAIL multi-user smoke", file=sys.stderr)
        return 1
    print("PASS multi-user smoke: all protocols and deletion check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
