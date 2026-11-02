#!/usr/bin/env python3
"""Real-core Snell v5/v6 loopback smoke tests."""

from __future__ import annotations

import argparse
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
    client_config,
    free_port,
    http_target,
    server_config,
    udp_echo_target,
    write_json,
)
from smoke_multiusers import expect_rejected, quiet_core, request_with_retries


def random_psk(version: int) -> str:
    if version == 6:
        return "snell-v6-" + secrets.token_urlsafe(24)
    return "snell-v5-" + secrets.token_urlsafe(12)


def wrong_psk(psk: str) -> str:
    replacement = "X" if psk[:1] != "X" else "Y"
    return replacement + psk[1:]


def run_case(
    core: Path,
    manager: Path,
    environment: dict[str, str],
    work: Path,
    protocol: str,
    mode: str,
    live: bool,
) -> None:
    version = 5 if protocol == "snell" else 6
    psk = random_psk(version)
    server_port, proxy_port = free_port(), free_port()
    while proxy_port == server_port:
        proxy_port = free_port()
    inbound = bash_json(
        manager, environment, "generate_inbound", protocol, "smoke-" + protocol + "-" + mode,
        str(server_port), psk, mode,
    )
    inbound["listen"] = LOOPBACK
    inbound_file = work / (protocol + "-" + mode + "-inbound.json")
    server_file = work / (protocol + "-" + mode + "-server.json")
    client_file = work / (protocol + "-" + mode + "-client.json")
    write_json(inbound_file, inbound)
    write_json(server_file, server_config(inbound))
    if inbound.get("type") != "snell" or inbound.get("version") != version or inbound.get("psk") != psk:
        raise RuntimeError("Snell server template did not preserve version or PSK")
    if version == 5 and inbound.get("obfs_mode") != mode:
        raise RuntimeError("Snell v5 server template did not preserve obfs_mode")
    if version == 6 and inbound.get("mode") != mode:
        raise RuntimeError("Snell v6 server template did not preserve mode")
    outbound = bash_json(manager, environment, "generate_client", str(inbound_file), LOOPBACK)
    write_json(client_file, client_config(outbound, proxy_port))
    check(core, server_file, work)
    check(core, client_file, work)
    if outbound.get("version") != (4 if version == 5 else 6) or outbound.get("psk") != psk:
        raise RuntimeError("Snell client export did not preserve version or PSK")
    if version == 5 and outbound.get("obfs_mode") != mode:
        raise RuntimeError("Snell v5 client export did not preserve obfs_mode")
    if version == 6 and outbound.get("mode") != mode:
        raise RuntimeError("Snell v6 client export did not preserve mode")
    if not live:
        print(f"PASS snell {protocol} {mode}: real core check", flush=True)
        return

    wrong_file = work / (protocol + "-" + mode + "-wrong-client.json")
    wrong_outbound = dict(outbound)
    wrong_outbound["psk"] = wrong_psk(psk)
    wrong_proxy = free_port()
    write_json(wrong_file, client_config(wrong_outbound, wrong_proxy))
    check(core, wrong_file, work)
    payload = ("SNELL_SMOKE_" + protocol + "_" + mode).encode("ascii")
    with http_target(payload) as target_port:
        with udp_echo_target() as udp_target:
            with quiet_core(core, server_file, server_port, work, protocol + "-" + mode + "-server"):
                with quiet_core(core, client_file, proxy_port, work, protocol + "-" + mode + "-client"):
                    if request_with_retries(proxy_port, target_port, "/snell/" + mode) != payload:
                        raise RuntimeError("Snell HTTP forwarding mismatch")
                    check_udp_through_socks(proxy_port, udp_target)
                with quiet_core(core, wrong_file, wrong_proxy, work, protocol + "-" + mode + "-wrong"):
                    expect_rejected(wrong_proxy, target_port, "/snell/wrong-psk")
                with quiet_core(core, client_file, proxy_port, work, protocol + "-" + mode + "-original-again"):
                    if request_with_retries(proxy_port, target_port, "/snell/original-again") != payload:
                        raise RuntimeError("Original PSK stopped working after invalid authentication")
    print(f"PASS snell {protocol} {mode}: HTTP/UDP forwarding and wrong PSK rejection", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--manager", type=Path, default=Path(__file__).resolve().parents[1] / "sing-box-manager.sh")
    args = parser.parse_args()
    core, manager = args.core.expanduser().resolve(), args.manager.expanduser().resolve()
    if not core.is_file() or not manager.is_file():
        print("FAIL snell smoke", file=sys.stderr)
        return 1
    try:
        with tempfile.TemporaryDirectory(prefix="sing-box-snell-") as temporary:
            work = Path(temporary)
            environment = os.environ.copy()
            environment["SB_ROOT_PREFIX"] = str(work / "source-prefix")
            for mode in ("none", "http"):
                run_case(core, manager, environment, work, "snell", mode, live=True)
            for mode in ("default", "unshaped"):
                run_case(core, manager, environment, work, "snell-v6", mode, live=True)
            run_case(core, manager, environment, work, "snell-v6", "unsafe-raw", live=False)
    except Exception:
        print("FAIL snell smoke", file=sys.stderr)
        return 1
    print("PASS snell smoke: all requested modes passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
