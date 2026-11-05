#!/usr/bin/env python3
"""Real-core acceptance for the Xray complete-purge uninstall path.

The manager is copied into a private XR_ROOT_PREFIX and sourced from that
copy.  A systemd facade maps the manager's fixed ``xray.service`` name to one
random unit under ``/run/systemd/system``; all other systemd calls delegate to
the real absolute systemctl binary.  Production Xray/sing-box state is
snapshotted before and after and is never addressed by a mutating command.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HERE = Path(__file__).resolve().parent
for candidate in (HERE, HERE.parent / "tests", Path.cwd() / "tests"):
    if (candidate / "smoke_core.py").is_file() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
        break

import smoke_core as smoke  # noqa: E402
import smoke_xray as xray_smoke  # noqa: E402
import smoke_xray_management as management  # noqa: E402


LOOPBACK = "127.0.0.1"
UUID_VALUE = "11111111-1111-4111-8111-111111111111"
PAYLOAD = b"XRAY_PURGE_LOOPBACK_OK"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run_command(args: list[str], *, env: dict[str, str] | None = None,
                timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL,
        timeout=timeout,
    )


def write_json(path: Path, value: dict) -> None:
    smoke.write_json(path, value)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def production_state(systemctl: str, core: Path) -> dict[str, object]:
    result: dict[str, object] = {}
    for service in ("xray.service", "sing-box.service"):
        output = run_command([
            systemctl, "show", service,
            "-p", "MainPID", "-p", "ActiveState", "-p", "UnitFileState",
        ])
        result[service] = (output.returncode, tuple(output.stdout.splitlines()))
    kernel = {}
    for key in (
        "net.ipv4.tcp_congestion_control",
        "net.core.default_qdisc",
        "net.ipv4.tcp_available_congestion_control",
    ):
        output = run_command(["/usr/sbin/sysctl", "-n", key])
        require(output.returncode == 0, "kernel state query failed")
        kernel[key] = output.stdout.strip()
    result["kernel"] = kernel
    installed_core = Path("/usr/local/lib/xray-manager/xray")
    result["core_sha256"] = digest(installed_core) if installed_core.is_file() else None
    result["test_core_sha256"] = digest(core)
    return result


def unit_from_manager(manager: Path, root: Path) -> bytes:
    command = 'source "$1" || exit 1; write_unit'
    result = run_command(
        ["bash", "-c", command, "xray-uninstall-smoke", str(manager)],
        env=dict(os.environ, XR_ROOT_PREFIX=str(root)), timeout=30,
    )
    require(result.returncode == 0, "write_unit failed")
    return result.stdout.encode("utf-8")


def make_systemctl_facade(directory: Path, real_systemctl: str,
                          runtime_unit: Path, virtual_unit: Path) -> Path:
    wrapper = directory / "systemctl"
    wrapper.write_text(
        r'''#!/usr/bin/env bash
set -u
runtime=${XRAY_PURGE_RUNTIME_UNIT:?}
virtual=${XRAY_PURGE_VIRTUAL_UNIT:?}
real=${XRAY_PURGE_REAL_SYSTEMCTL:?}
show_fragment=0
has_show=0
has_value=0
for arg in "$@"; do
    [[ $arg == show ]] && has_show=1
    [[ $arg == --property=FragmentPath || $arg == FragmentPath ]] && show_fragment=1
    [[ $arg == --value ]] && has_value=1
done
if ((has_show && show_fragment && has_value)); then
    printf '%s\n' "$virtual"
    exit 0
fi
args=()
for arg in "$@"; do
    if [[ $arg == xray.service ]]; then args+=("$runtime"); else args+=("$arg"); fi
done
exec "$real" "${args[@]}"
''', encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def make_sysctl_facade(directory: Path, real_sysctl: str, writes: Path) -> Path:
    wrapper = directory / "sysctl"
    wrapper.write_text(
        r'''#!/usr/bin/env bash
set -u
if [[ $# -eq 2 && $1 == -n ]]; then
    case "$2" in
        net.ipv4.tcp_congestion_control|net.core.default_qdisc|net.ipv4.tcp_available_congestion_control)
            exec "$XRAY_PURGE_REAL_SYSCTL" "$@";;
    esac
fi
printf '%s\n' "$*" >>"$XRAY_PURGE_SYSCTL_WRITES"
exit 97
''', encoding="utf-8")
    wrapper.chmod(0o755)
    writes.touch(mode=0o600)
    return wrapper


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
                 environment: dict[str, str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
    body = 'with_lock "$@"' if locked else '"$@"'
    command = (
        'source "$1" || exit 1; CORE_BIN="$2"; SERVICE="$3"; shift 3; ' + body
    )
    return run_command(
        ["bash", "-c", command, "xray-uninstall-smoke", str(manager),
         str(core), service, function, *args],
        env=dict(environment, XR_ROOT_PREFIX=str(root)), timeout=timeout,
    )


def manager_json(manager: Path, core: Path, root: Path, service: str,
                 function: str, *args: str, environment: dict[str, str]) -> dict:
    result = manager_call(manager, core, root, service, function, *args,
                          locked=False, environment=environment)
    require(result.returncode == 0, function + " failed")
    value = json.loads(result.stdout)
    require(isinstance(value, dict), function + " returned invalid JSON")
    return value


def config_for(port: int, target_port: int) -> dict:
    allow = [{
        "action": "allow", "network": "tcp", "ip": ["127.0.0.1/32"],
        "port": str(target_port),
    }]
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "protocol": "vless", "tag": "purge-node", "listen": LOOPBACK,
            "port": port,
            "settings": {"clients": [{"id": UUID_VALUE}], "decryption": "none"},
            "streamSettings": {"method": "raw", "security": "none"},
        }],
        "outbounds": [{
            "protocol": "freedom", "tag": "direct",
            "settings": {"finalRules": allow},
            "streamSettings": {"sockopt": {"domainStrategy": "UseIPv4"}},
        }],
        "routing": {"rules": [{
            "type": "field", "ip": ["127.0.0.1/32"],
            "port": str(target_port), "outboundTag": "direct",
        }]},
    }


def probe_client(manager: Path, core: Path, root: Path, service: str,
                 environment: dict[str, str], node_file: Path,
                 target_port: int, work: Path) -> None:
    outbound = manager_json(manager, core, root, service, "generate_client",
                            str(node_file), LOOPBACK, "", "1", environment=environment)
    require(outbound.get("protocol") == "vless", "generated client protocol invalid")
    proxy_port = smoke.free_port()
    client_file = work / "purge-client.json"
    write_json(client_file, xray_smoke.client_config(outbound, proxy_port))
    xray_smoke.check_xray(core, client_file, work)
    with xray_smoke.running_xray(core, client_file, work, proxy_port, "purge-client"):
        response = smoke.fetch_through_socks_retry(proxy_port, target_port, "/purge")
    require(response == PAYLOAD, "loopback forwarding failed")


def wait_listener(port: int, present: bool) -> None:
    deadline = __import__("time").monotonic() + 15
    while __import__("time").monotonic() < deadline:
        try:
            with socket.create_connection((LOOPBACK, port), timeout=0.2):
                connected = True
        except OSError:
            connected = False
        if connected == present:
            return
        __import__("time").sleep(0.1)
    raise RuntimeError("listener did not settle")


def run(core: Path, manager: Path) -> None:
    require(sys.platform == "linux" and os.geteuid() == 0,
            "requires Linux root")
    require(Path("/run/systemd/system").is_dir(), "requires running systemd")
    real_systemctl = "/usr/bin/systemctl"
    real_sysctl = "/usr/sbin/sysctl"
    require(Path(real_systemctl).is_file() and Path(real_sysctl).is_file(),
            "required system tools are missing")
    require(manager.is_file() and not manager.is_symlink(), "manager is not a regular file")
    require(core.is_file() and not core.is_symlink() and os.access(core, os.X_OK),
            "core is not a regular executable")

    before = production_state(real_systemctl, core)
    runtime_unit = Path("/run/systemd/system") / ("xray-purge-" + uuid.uuid4().hex + ".service")
    require(not runtime_unit.exists() and not runtime_unit.is_symlink(),
            "temporary runtime unit already exists")
    runtime_created = False

    with tempfile.TemporaryDirectory(prefix="xray-purge-smoke-") as temporary:
        work = Path(temporary)
        root = work / "root"
        lib = root / "usr/local/lib/xray-manager"
        wrapper = root / "usr/local/bin/xray"
        config_dir = root / "etc/xray"
        config = config_dir / "config.json"
        state = root / "var/lib/xray-manager"
        clients = state / "clients"
        backups = state / "backups"
        unit_dir = root / "etc/systemd/system"
        source = root / "staged/xray-manager.sh"
        bbr_dir = root / "etc/sysctl.d"
        bbr_file = bbr_dir / "99-xray-manager-bbr.conf"
        for directory in (lib, wrapper.parent, config_dir, clients, backups,
                          unit_dir, source.parent, bbr_dir, root / "etc/sing-box",
                          root / "var/lib/sing-box", root / "etc/ssl",
                          root / "root/.acme.sh", root / "root/acme-script"):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)

        shutil.copyfile(core, lib / "xray")
        (lib / "xray").chmod(0o755)
        shutil.copyfile(manager, wrapper)
        wrapper.chmod(0o755)
        shutil.copyfile(manager, source)
        source.chmod(0o600)
        marker = lib / ".managed"
        marker.write_text("xray-manager-v1\n", encoding="utf-8")
        marker.chmod(0o600)
        (lib / "geoip.dat").write_bytes(b"owned geoip\n")
        (lib / "geosite.dat").write_bytes(b"owned geosite\n")
        (state / "default-server").write_text("example.com\n", encoding="utf-8")
        (state / "default-server").chmod(0o600)
        bbr_file.write_text("# Managed by xray-manager: TCP BBR\nnet.core.default_qdisc=fq\n", encoding="utf-8")
        bbr_file.chmod(0o600)

        foreign = {
            root / "usr/local/bin/sing-box": b"foreign sing-box\n",
            root / "etc/sing-box/config.json": b"foreign sing-box config\n",
            root / "var/lib/sing-box/keep.json": b"foreign sing-box state\n",
            root / "etc/ssl/foreign-cert.pem": b"foreign certificate\n",
            root / "root/.acme.sh/example/fullchain.cer": b"foreign acme\n",
            root / "root/acme-script/sentinel": b"foreign acme menu\n",
        }
        for path, data in foreign.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        foreign_before = {str(path): digest(path) for path in foreign}

        fake_bin = work / "fake-bin"
        fake_bin.mkdir(mode=0o700)
        sysctl_writes = work / "sysctl-writes.log"
        make_systemctl_facade(fake_bin, real_systemctl, runtime_unit,
                              unit_dir / "xray.service")
        make_sysctl_facade(fake_bin, real_sysctl, sysctl_writes)
        modprobe = fake_bin / "modprobe"
        modprobe.write_text('#!/bin/sh\nprintf "module attempt\\n" >>"$XRAY_PURGE_SYSCTL_WRITES"\nexit 97\n',encoding="utf-8")
        modprobe.chmod(0o755)

        env = dict(os.environ)
        env.update({
            "XR_ROOT_PREFIX": str(root),
            "XRAY_PURGE_RUNTIME_UNIT": runtime_unit.name,
            "XRAY_PURGE_VIRTUAL_UNIT": str(unit_dir / "xray.service"),
            "XRAY_PURGE_REAL_SYSTEMCTL": real_systemctl,
            "XRAY_PURGE_REAL_SYSCTL": real_sysctl,
            "XRAY_PURGE_SYSCTL_WRITES": str(sysctl_writes),
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        })
        created = False
        unit_bytes = b""
        try:
            with tagged_http_target(LOOPBACK, PAYLOAD) as target_port:
                listen_port = smoke.free_port()
                write_json(config, config_for(listen_port, target_port))
                write_json(clients / "purge-node.json", {"server":LOOPBACK,"inbound":config_for(listen_port,target_port)["inbounds"][0]})
                write_json(backups / "purge-fixture.json", {"synthetic":True,"users":[UUID_VALUE]})
                xray_smoke.check_xray(lib / "xray", config, work)
                unit_bytes = unit_from_manager(wrapper, root)
                unit_dir.joinpath("xray.service").write_bytes(unit_bytes)
                unit_dir.joinpath("xray.service").chmod(0o644)
                with runtime_unit.open("xb") as stream:
                    stream.write(unit_bytes)
                runtime_unit.chmod(0o644)
                runtime_created = True
                created = True
                result = run_command([real_systemctl, "daemon-reload"], timeout=30)
                require(result.returncode == 0, "systemd daemon-reload failed")
                result = run_command([real_systemctl, "start", str(runtime_unit.name)], timeout=45)
                require(result.returncode == 0, "temporary Xray service did not start")
                require(run_command([real_systemctl, "is-active", "--quiet", str(runtime_unit.name)]).returncode == 0,
                        "temporary Xray service is not active")
                wait_listener(listen_port, present=True)
                node_file = work / "purge-node.json"
                write_json(node_file, config_for(listen_port, target_port)["inbounds"][0])
                probe_client(wrapper, lib / "xray", root, "xray.service", env,
                             node_file, target_port, work)
                print("PASS purge preflight: real Xray listened and forwarded loopback HTTP", flush=True)

                result = manager_call(wrapper, lib / "xray", root, "xray.service",
                                      "purge_install_locked", str(source), locked=True,
                                      environment=env, timeout=120)
                require(result.returncode == 0, "purge_install_locked failed")
                require(not config_dir.exists() and not state.exists() and not lib.exists(),
                        "owned Xray directories were not removed")
                require(not wrapper.exists() and not unit_dir.joinpath("xray.service").exists() and not source.exists(),
                        "owned Xray files were not removed")
                require(not bbr_file.exists(), "owned BBR persistence file remains")
                require(not list(state.parent.glob("xray-purge.*")), "temporary purge recovery entry remains")
                require(sysctl_writes.read_bytes() == b"", "purge attempted a real sysctl write")
                wait_listener(listen_port, present=False)
                for path, expected in foreign_before.items():
                    foreign_path = Path(path)
                    require(foreign_path.is_file() and digest(foreign_path) == expected,
                            "foreign sing-box/certificate/ACME state changed")
                print("PASS purge: owned Xray layout removed and foreign state preserved", flush=True)
        finally:
            if runtime_created:
                run_command([real_systemctl, "stop", str(runtime_unit.name)], timeout=30)
                require(run_command([real_systemctl, "is-active", "--quiet", str(runtime_unit.name)], timeout=15).returncode != 0,
                        "temporary runtime service remained active")
                run_command([real_systemctl, "reset-failed", str(runtime_unit.name)], timeout=15)
                require(runtime_unit.is_file() and not runtime_unit.is_symlink(),
                        "runtime unit disappeared before controlled cleanup")
                require(runtime_unit.read_bytes() == unit_bytes,
                        "runtime unit changed during purge")
                runtime_unit.unlink()
                run_command([real_systemctl, "daemon-reload"], timeout=30)
            require(not runtime_unit.exists(), "runtime unit cleanup failed")
            after = production_state(real_systemctl, core)
            require(before == after, "production Xray/sing-box/kernel state changed")
    print("PASS cleanup: temporary runtime unit and purge fixture removed", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--manager", required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("requires Linux root with systemd")
    try:
        run(args.core.resolve(), args.manager.resolve())
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        locations = ",".join(Path(frame.filename).name + ":" + str(frame.lineno)
                             for frame in frames[-3:])
        print(f"FAIL xray purge smoke: {type(error).__name__} at {locations}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
