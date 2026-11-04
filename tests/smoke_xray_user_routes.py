#!/usr/bin/env python3
"""Real Xray per-user outbound routing smoke test on loopback only."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import smoke_core as smoke
import smoke_xray as xray_smoke
import smoke_xray_management as management

LOOPBACK = "127.0.0.1"
USER_A_HOST = "127.0.0.2"
USER_B_HOST = "127.0.0.3"
USER_A_PAYLOAD = b"XRAY_USER_ROUTE_A"
USER_B_PAYLOAD = b"XRAY_USER_ROUTE_B"
USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def write_json(path: Path, value: dict) -> None:
    smoke.write_json(path, value)


@contextmanager
def tagged_http_target(host: str, payload: bytes):
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

    server = ThreadingHTTPServer((host, 0), Handler)
    server.daemon_threads = True
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def manager_call(manager: Path, core: Path, root: Path, service: str,
                 function: str, *args: str, locked: bool,
                 environment: dict[str, str]):
    return management.xray_manager_call(
        manager, core, root, service, function, *args,
        locked=locked, environment=environment,
    )


def manager_json(manager: Path, core: Path, root: Path, service: str,
                 function: str, *args: str, locked: bool,
                 environment: dict[str, str]) -> dict:
    result = manager_call(manager, core, root, service, function, *args,
                          locked=locked, environment=environment)
    require(result.returncode == 0, function + " failed")
    value = json.loads(result.stdout)
    require(isinstance(value, dict), function + " returned a non-object")
    return value


def route_rule(config: dict, user: str, outbound: str) -> dict | None:
    for rule in config.get("routing", {}).get("rules", []):
        if rule.get("type") == "field" and rule.get("user") == [user] and rule.get("outboundTag") == outbound:
            return rule
    return None


def request_user(core: Path, outbound: dict, target_port: int, work: Path, label: str) -> bytes:
    proxy_port = smoke.free_port()
    client_file = work / (label + "-client.json")
    write_json(client_file, xray_smoke.client_config(outbound, proxy_port))
    xray_smoke.check_xray(core, client_file, work)
    with xray_smoke.running_xray(core, client_file, work, proxy_port, label + "-client"):
        return smoke.fetch_through_socks_retry(proxy_port, target_port, "/" + label)


def run(core: Path, manager: Path, protocol: str) -> None:
    require(os.geteuid() == 0 and Path("/run/systemd/system").is_dir(), "requires root with systemd")
    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    service = "xray-user-route-" + uuid.uuid4().hex + ".service"
    unit = Path("/run/systemd/system") / service
    require(not unit.exists() and not unit.is_symlink(), "temporary unit already exists")
    created = False
    unit_bytes = b""
    with tempfile.TemporaryDirectory(prefix="xray-user-routes-") as temporary:
        work = Path(temporary)
        root = work / "root"
        state = root / "var/lib/xray-manager"
        clients = state / "clients"
        config = root / "etc/xray/config.json"
        lib = root / "usr/local/lib/xray-manager"
        for directory in (clients, state / "backups", config.parent, lib, root / "etc/systemd/system"):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        marker = lib / ".managed"
        marker.write_text("xray-manager-v1\n", encoding="utf-8")
        marker.chmod(0o600)
        env = dict(os.environ, XR_ROOT_PREFIX=str(root), CORE_BIN=str(core))
        try:
            certificate, private_key = smoke.tls_material(work) if protocol in {"trojan", "hysteria"} else (Path(""), Path(""))
            with tagged_http_target(USER_A_HOST, USER_A_PAYLOAD) as target_a_port, \
                 tagged_http_target(USER_B_HOST, USER_B_PAYLOAD) as target_b_port:
                allow_a = [
                    {"action": "allow", "network": "tcp", "ip": ["127.0.0.1/32"], "port": str(target_a_port)},
                    {"action": "allow", "network": "tcp", "ip": [USER_A_HOST + "/32"], "port": str(target_a_port)},
                ]
                allow_b = [
                    {"action": "allow", "network": "tcp", "ip": ["127.0.0.1/32"], "port": str(target_a_port)},
                    {"action": "allow", "network": "tcp", "ip": [USER_B_HOST + "/32"], "port": str(target_b_port)},
                ]
                initial = {
                    "log": {"loglevel": "warning"}, "inbounds": [],
                    "outbounds": [
                        {"protocol": "blackhole", "tag": "block", "settings": {}},
                        {"protocol": "freedom", "tag": "out-a", "settings": {"redirect": f"{USER_A_HOST}:{target_a_port}", "finalRules": allow_a}},
                        {"protocol": "freedom", "tag": "out-b", "settings": {"redirect": f"{USER_B_HOST}:{target_b_port}", "finalRules": allow_b}},
                    ],
                    "routing": {"domainStrategy": "AsIs", "rules": []},
                }
                write_json(config, initial)
                xray_smoke.check_xray(core, config, work)
                unit_bytes = management.unit_source(core, state, config)
                with unit.open("xb") as stream:
                    stream.write(unit_bytes)
                    created = True
                unit.chmod(0o644)
                management.systemd(systemctl, "daemon-reload")
                template = {"vless": "vless-tcp", "vmess": "vmess-tcp", "trojan": "trojan", "hysteria": "hysteria"}[protocol]
                credential_a = USER_A if protocol in {"vless", "vmess"} else "alice-route-password"
                credential_b = USER_B if protocol in {"vless", "vmess"} else "bob-route-password"
                tls_name = "localhost" if protocol in {"trojan", "hysteria"} else ""
                inbound = manager_json(
                    manager, core, root, service, "generate_inbound", template, "user-route",
                    str(smoke.free_port()), credential_a, "", "", "", tls_name,
                    str(certificate), str(private_key), "", "", "none", "",
                    locked=False, environment=env,
                )
                inbound["listen"] = LOOPBACK
                user_key = {"vless": "id", "vmess": "id", "trojan": "password", "hysteria": "auth"}[protocol]
                inbound["settings"]["users"] = [{user_key: credential_a, "email": "alice"}, {user_key: credential_b, "email": "bob"}]
                inbound_file = work / "user-route-inbound.json"
                write_json(inbound_file, inbound)
                added = manager_call(manager, core, root, service, "add_managed_inbound", str(inbound_file), LOOPBACK, "", locked=True, environment=env)
                require(added.returncode == 0, "initial multi-user inbound add failed")
                management.active_pid(systemctl, service)
                user_a = manager_json(manager, core, root, service, "generate_client", str(inbound_file), LOOPBACK, "", "1", locked=False, environment=env)
                user_b = manager_json(manager, core, root, service, "generate_client", str(inbound_file), LOOPBACK, "", "2", locked=False, environment=env)
                if protocol in {"trojan", "hysteria"}:
                    xray_smoke.prepare_tls_outbound(user_a, certificate, protocol + "-alice")
                    xray_smoke.prepare_tls_outbound(user_b, certificate, protocol + "-bob")
                before_metadata = json.loads((clients / "user-route.json").read_text(encoding="utf-8"))

                def bind_user(index: int, outbound: str) -> None:
                    result = manager_call(manager, core, root, service, "bind_user_outbound_locked", "user-route", str(index), outbound, locked=True, environment=env)
                    require(result.returncode == 0, "per-user bind failed")

                def bind_inbound(outbound: str) -> None:
                    result = manager_call(manager, core, root, service, "bind_outbound_locked", "user-route", outbound, locked=True, environment=env)
                    require(result.returncode == 0, "inbound bind failed")

                bind_inbound("out-a")
                before_metadata = json.loads((clients / "user-route.json").read_text(encoding="utf-8"))
                bind_user(2, "out-b")
                current = json.loads(config.read_text(encoding="utf-8"))
                bob_rule = route_rule(current, "bob", "out-b")
                require(bob_rule is not None and current["routing"]["rules"].index(bob_rule) == 0, "bob route was not prioritized")
                require(request_user(core, user_a, target_a_port, work, "alice-a") == USER_A_PAYLOAD, "alice did not use out-a")
                require(request_user(core, user_b, target_a_port, work, "bob-b") == USER_B_PAYLOAD, "bob did not use out-b")
                print("PASS per-user routes: distinct users reached distinct outbound payloads", flush=True)
                bind_inbound("out-a")
                require(request_user(core, user_a, target_a_port, work, "alice-global") == USER_A_PAYLOAD, "alice changed after global bind")
                require(request_user(core, user_b, target_a_port, work, "bob-user") == USER_B_PAYLOAD, "bob rule was shadowed by global bind")
                bind_user(1, "out-b")
                require(request_user(core, user_a, target_a_port, work, "alice-b") == USER_B_PAYLOAD, "alice did not switch to out-b")
                require(request_user(core, user_b, target_a_port, work, "bob-both") == USER_B_PAYLOAD, "bob changed when alice switched")
                bind_user(2, "-")
                require(request_user(core, user_a, target_a_port, work, "alice-b-after-bob-unbind") == USER_B_PAYLOAD, "alice changed when bob unbound")
                require(request_user(core, user_b, target_a_port, work, "bob-after") == USER_A_PAYLOAD, "bob did not fall back after unbind")
                bind_user(1, "-")
                require(request_user(core, user_a, target_a_port, work, "alice-after-unbind") == USER_A_PAYLOAD, "alice did not fall back after unbind")
                print("PASS route precedence: user rule survived global bind and unbind restored fallback", flush=True)
                current = json.loads(config.read_text(encoding="utf-8"))
                require(route_rule(current, "alice", "out-a") is None, "unbinding bob changed alice")
                metadata = json.loads((clients / "user-route.json").read_text(encoding="utf-8"))
                require(metadata["inbound"]["settings"]["users"] == before_metadata["inbound"]["settings"]["users"], "credentials changed")
                for key in set(before_metadata) | set(metadata):
                    if key != "user_outbound_routes":
                        require(metadata.get(key) == before_metadata.get(key), "non-route metadata changed")
                require(not list(state.joinpath("backups").iterdir()), "per-user operations created automatic backup")
                print("PASS user route isolation: credentials and backup directory preserved", flush=True)
        finally:
            if created:
                management.systemd(systemctl, "stop", service, check=False)
                require(management.systemd(systemctl, "is-active", "--quiet", service, check=False).returncode != 0, "temporary service remained active")
                management.systemd(systemctl, "reset-failed", service, check=False)
                require(unit.is_file() and not unit.is_symlink() and unit.read_bytes() == unit_bytes, "temporary unit changed")
                unit.unlink()
                management.systemd(systemctl, "daemon-reload")
        require(not unit.exists(), "temporary unit cleanup failed")
    print("PASS user-route smoke cleanup", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--manager", required=True, type=Path)
    parser.add_argument("--protocol", choices=("vless", "vmess", "trojan", "hysteria"), default="vless")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("requires root on Linux with running systemd")
    try:
        run(args.core.resolve(), args.manager.resolve(), args.protocol)
    except Exception as error:
        locations = ",".join(Path(f.filename).name + ":" + str(f.lineno) for f in traceback.extract_tb(error.__traceback__)[-3:])
        print(f"FAIL user-route smoke: {type(error).__name__} at {locations}", file=sys.stderr)
        if type(error) is RuntimeError:
            print("FAIL reason: " + str(error)[:160], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
