#!/usr/bin/env python3
"""Real-core Snell + ShadowTLS v3 chained loopback smoke test."""

from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path
import secrets
import sys
import tempfile

from smoke_core import (
    LOOPBACK,
    bash_json,
    check,
    check_udp_through_socks,
    command,
    free_port,
    http_target,
    running_tls_target,
    server_config,
    tls_material,
    udp_echo_target,
    write_json,
)
from smoke_multiusers import expect_rejected, quiet_core, request_with_retries


def make_wrong_certificate(work: Path) -> Path:
    certificate = work / "wrong-cert.pem"
    private_key = work / "wrong-key.pem"
    result = command(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
    )
    del result
    return certificate


def find_outbound(config: dict, outbound_type: str) -> dict:
    for outbound in config.get("outbounds", []):
        if outbound.get("type") == outbound_type:
            return outbound
    raise RuntimeError("expected outbound missing")


def group_client_config(group: dict, port: int) -> dict:
    return {
        "log": {"level": "warn"},
        "inbounds": [{"type": "mixed", "tag": "local", "listen": LOOPBACK, "listen_port": port}],
        "outbounds": group["outbounds"],
        "route": group["route"],
    }


def run_case(
    core: Path,
    manager: Path,
    environment: dict[str, str],
    work: Path,
    certificate: Path,
    private_key: Path,
) -> None:
    front_port, handshake_port, proxy_port = free_port(), free_port(), free_port()
    while handshake_port in {front_port, proxy_port}:
        handshake_port = free_port()
    while proxy_port == front_port:
        proxy_port = free_port()
    psk = "snell-shadow-" + secrets.token_urlsafe(16)
    shadow_password = "shadowtls-" + secrets.token_urlsafe(16)
    group = bash_json(
        manager, environment, "generate_snell_shadowtls", "snell-shadow", str(front_port),
        psk, shadow_password, LOOPBACK, str(handshake_port), "localhost",
    )
    if group.get("format") != "sing-box-manager-snell-shadowtls-v1":
        raise RuntimeError("unexpected Snell ShadowTLS group format")
    backend = group.get("backend", {})
    front = group.get("front", {})
    if backend.get("type") != "snell" or backend.get("listen") != LOOPBACK or backend.get("listen_port") != 0:
        raise RuntimeError("Snell backend must use a random loopback port")
    if front.get("type") != "shadowtls" or front.get("listen_port") != front_port:
        raise RuntimeError("ShadowTLS front listener does not match requested port")
    # Keep the temporary frontend loopback-only, too.
    front["listen"] = LOOPBACK
    server_file = work / "snell-shadowtls-server.json"
    group_file = work / "snell-shadowtls-group.json"
    write_json(group_file, group)
    write_json(server_file, {
        "log": {"level": "warn"},
        "inbounds": [backend, front],
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "route": {"final": "direct"},
    })
    client = bash_json(
        manager, environment, "generate_snell_shadowtls_clients", str(group_file), LOOPBACK, "snell-client",
    )
    if client.get("route", {}).get("final") != "snell-client":
        raise RuntimeError("Snell ShadowTLS client route is incomplete")
    snell = find_outbound(client, "snell")
    shadowtls = find_outbound(client, "shadowtls")
    if snell.get("detour") != "snell-client:shadowtls":
        raise RuntimeError("Snell client detour is incomplete")
    if shadowtls.get("password") != shadow_password:
        raise RuntimeError("ShadowTLS password was not preserved")
    shadowtls.setdefault("tls", {})["certificate_path"] = str(certificate)
    client_file = work / "snell-shadowtls-client.json"
    write_json(client_file, group_client_config(client, proxy_port))
    check(core, server_file, work)
    check(core, client_file, work)
    wrong_certificate = make_wrong_certificate(work)
    payload = b"SNELL_SHADOWTLS_LOOPBACK"
    with http_target(payload) as target_port:
        with udp_echo_target() as udp_target:
            with running_tls_target(certificate, private_key, handshake_port, work):
                with quiet_core(core, server_file, front_port, work, "snell-shadowtls-server"):
                    with quiet_core(core, client_file, proxy_port, work, "snell-shadowtls-client"):
                        if request_with_retries(proxy_port, target_port, "/snell-shadowtls") != payload:
                            raise RuntimeError("Snell ShadowTLS HTTP forwarding mismatch")
                        check_udp_through_socks(proxy_port, udp_target)

                    wrong_psk_client = copy.deepcopy(client)
                    find_outbound({"outbounds": wrong_psk_client["outbounds"]}, "snell")["psk"] = (
                        "X" if psk[:1] != "X" else "Y"
                    ) + psk[1:]
                    wrong_psk_file = work / "snell-shadowtls-wrong-psk.json"
                    wrong_psk_port = free_port()
                    write_json(wrong_psk_file, group_client_config(wrong_psk_client, wrong_psk_port))
                    check(core, wrong_psk_file, work)
                    with quiet_core(core, wrong_psk_file, wrong_psk_port, work, "snell-shadowtls-wrong-psk"):
                        expect_rejected(wrong_psk_port, target_port, "/wrong-psk")

                    wrong_shadow_client = copy.deepcopy(client)
                    find_outbound({"outbounds": wrong_shadow_client["outbounds"]}, "shadowtls")["password"] = (
                        "X" if shadow_password[:1] != "X" else "Y"
                    ) + shadow_password[1:]
                    wrong_shadow_file = work / "snell-shadowtls-wrong-shadow-password.json"
                    wrong_shadow_port = free_port()
                    write_json(wrong_shadow_file, group_client_config(wrong_shadow_client, wrong_shadow_port))
                    check(core, wrong_shadow_file, work)
                    with quiet_core(core, wrong_shadow_file, wrong_shadow_port, work, "snell-shadowtls-wrong-shadow"):
                        expect_rejected(wrong_shadow_port, target_port, "/wrong-shadow-password")

                    wrong_cert_client = copy.deepcopy(client)
                    find_outbound({"outbounds": wrong_cert_client["outbounds"]}, "shadowtls")["tls"]["certificate_path"] = str(wrong_certificate)
                    wrong_cert_file = work / "snell-shadowtls-wrong-certificate.json"
                    wrong_cert_port = free_port()
                    write_json(wrong_cert_file, group_client_config(wrong_cert_client, wrong_cert_port))
                    check(core, wrong_cert_file, work)
                    with quiet_core(core, wrong_cert_file, wrong_cert_port, work, "snell-shadowtls-wrong-cert"):
                        expect_rejected(wrong_cert_port, target_port, "/wrong-certificate")

                    with quiet_core(core, client_file, proxy_port, work, "snell-shadowtls-client-again"):
                        if request_with_retries(proxy_port, target_port, "/snell-shadowtls-again") != payload:
                            raise RuntimeError("correct Snell ShadowTLS client stopped working")
    print("PASS snell-shadowtls: chained HTTP/UDP, wrong PSK/password/certificate rejected", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--manager", type=Path, default=Path(__file__).resolve().parents[1] / "sing-box-manager.sh")
    args = parser.parse_args()
    core, manager = args.core.expanduser().resolve(), args.manager.expanduser().resolve()
    if not core.is_file() or not manager.is_file():
        print("FAIL snell-shadowtls smoke", file=sys.stderr)
        return 1
    try:
        with tempfile.TemporaryDirectory(prefix="sing-box-snell-shadowtls-") as temporary:
            work = Path(temporary)
            environment = os.environ.copy()
            environment["SB_ROOT_PREFIX"] = str(work / "source-prefix")
            certificate, private_key = tls_material(work)
            run_case(core, manager, environment, work, certificate, private_key)
    except Exception:
        print("FAIL snell-shadowtls smoke", file=sys.stderr)
        return 1
    print("PASS snell-shadowtls smoke: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
