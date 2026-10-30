#!/usr/bin/env python3
"""Validate management transactions against an isolated real systemd service.

Run as root on Linux with --core /absolute/path/to/a/trusted/sing-box.
All configurations and credentials stay in a private temporary directory, and
only a uniquely named transient unit is created, restarted and stopped. The
production sing-box.service, configuration and firewall are never accessed.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

import smoke_core as smoke


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def execute(args: list[str], label: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=45)
    # Never include captured output or command arguments: either may contain
    # generated connection credentials or an upstream configuration diagnostic.
    require(result.returncode == 0, f"{label} failed (exit {result.returncode})")
    return result.stdout


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def run(core: Path, manager: Path) -> None:
    unit = "sing-box-manager-smoke-" + uuid.uuid4().hex + ".service"
    require(unit != "sing-box.service", "refusing the production service name")
    runtime_dir = Path("/run/systemd/system")
    runtime_unit = runtime_dir / unit

    def systemd_quote(value: Path) -> str:
        text = str(value)
        require("\n" not in text and "\r" not in text, "runtime path contains a newline")
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    with tempfile.TemporaryDirectory(prefix="sing-box-management-") as temporary:
        work = Path(temporary)
        root = work / "fixture"
        state = root / "var/lib/sing-box-manager"
        clients = state / "clients"
        config = root / "etc/sing-box/config.json"
        for directory in (clients, state / "backups", config.parent):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        env = dict(os.environ, SB_ROOT_PREFIX=str(root))

        def call(function: str, *args: str) -> str:
            return execute([
                "bash", "-c",
                'source "$1" || exit; CORE_BIN=$2; SERVICE=$3; shift 3; '
                '[[ $SERVICE == sing-box-manager-smoke-*.service && '
                '$SERVICE != sing-box.service && -n $SB_ROOT_PREFIX && '
                '$CONFIG_FILE == "$SB_ROOT_PREFIX/etc/sing-box/config.json" ]] || exit 90; '
                'set -o pipefail; umask 077; confirm() { return 0; }; with_lock "$@"',
                "management-smoke", str(manager), str(core), unit, function, *args,
            ], function, env)

        def pid() -> int:
            execute(["systemctl", "is-active", "--quiet", unit], "isolated service health")
            value = execute(["systemctl", "show", unit, "--property=MainPID", "--value"], "isolated PID")
            result = int(value.strip())
            require(result > 0, "isolated service has no main process")
            return result

        def fixture_config() -> dict:
            return json.loads(call("config_jq", "."))

        def check_restarted(previous: int) -> None:
            require(pid() != previous, "configuration change did not restart isolated service")

        def verify_forwarding(tag: str) -> None:
            outbound = json.loads(call("show_client", tag, smoke.LOOPBACK, "json-only"))
            outbound["tag"] = "proxy"
            proxy_port = smoke.free_port()
            client_file = work / "probe-client.json"
            smoke.write_json(client_file, smoke.client_config(outbound, proxy_port))
            execute([str(core), "check", "-D", str(work), "-c", str(client_file)], "probe client validation")
            payload = ("MANAGEMENT_SMOKE:" + uuid.uuid4().hex).encode("ascii")
            # The shared helper prints core logs on error; capture those because
            # smoke failures must not expose credentials in the calling terminal.
            try:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with smoke.http_target(payload) as target_port:
                        with smoke.running_core(core, client_file, proxy_port, work):
                            response = smoke.fetch_through_socks_retry(proxy_port, target_port, "/management")
                            require(response == payload, "HTTP forwarding payload mismatch")
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                raise RuntimeError("probe HTTP forwarding failed; sensitive diagnostics suppressed") from None

        def node(tag: str, port: int) -> dict:
            return {
                "type": "vless", "tag": tag, "listen": smoke.LOOPBACK,
                "listen_port": port, "users": [{"uuid": str(uuid.uuid4())}],
            }

        baseline = node("baseline", smoke.free_port())
        baseline_config = smoke.server_config(baseline)
        baseline_metadata = {"server": "", "public_key": "", "inbound": baseline}
        smoke.write_json(config, baseline_config)
        commented = config.read_text(encoding="utf-8")
        commented = commented.replace(f'"listen_port": {baseline["listen_port"]},',
                                      f'"listen_port": {baseline["listen_port"]}, // baseline port')
        config.write_text('// JSONC fixture: preserve comments when saving\n' + commented, encoding='utf-8')
        smoke.write_json(clients / "baseline.json", baseline_metadata)
        default_file = state / "default-server"
        default_file.write_text(smoke.LOOPBACK + "\n", encoding="utf-8")
        default_file.chmod(0o600)
        unit_started = False
        unit_created = False
        runtime_unit_source = b""
        try:
            execute([str(core), "check", "-D", str(state), "-c", str(config)], "fixture validation")
            require(not runtime_unit.exists() and not runtime_unit.is_symlink(), "runtime unit already exists")
            runtime_unit_source = (
                "[Unit]\n"
                f"Description=sing-box-manager isolated smoke {unit}\n"
                "[Service]\n"
                "Type=simple\n"
                "UMask=0077\n"
                f"WorkingDirectory={state}\n"
                f"ExecStart={systemd_quote(core)} run -D {systemd_quote(state)} -c {systemd_quote(config)}\n"
                "StandardOutput=null\n"
                "StandardError=null\n"
            ).encode("utf-8")
            with runtime_unit.open("xb") as stream:
                unit_created = True
                stream.write(runtime_unit_source)
            os.chmod(runtime_unit, 0o644)
            execute(["systemctl", "daemon-reload"], "isolated unit daemon-reload")
            # Mark before start so a partially registered unit is always stopped.
            unit_started = True
            execute([
                "systemctl", "start", unit,
            ], "isolated unit start")
            time.sleep(0.3)
            pid()
            verify_forwarding("baseline")
            print("PASS isolated baseline: real service and exact HTTP forwarding", flush=True)

            original_commented = config.read_bytes()
            previous = pid()
            call("service_action", "restart")
            check_restarted(previous)
            require(config.read_bytes() == original_commented, "restart rewrote JSONC source")
            verify_forwarding("baseline")
            print("PASS JSONC restart: original comments preserved and exact HTTP forwarding", flush=True)

            backup = Path(call("create_backup").strip())
            require(backup.parent == state / "backups", "backup escaped fixture")
            bundle = read_json(backup)
            require(bundle["config"] == baseline_config, "backup configuration mismatch")
            require(bundle["config_source"] == config.read_text(encoding="utf-8"), "backup lost original comments")
            require(bundle["clients"] == {"baseline": baseline_metadata}, "backup metadata mismatch")
            require(bundle["default_server"] == smoke.LOOPBACK, "backup default address mismatch")
            require(backup.stat().st_mode & 0o777 == 0o600, "backup permissions mismatch")
            print("PASS backup: configuration, metadata, default address and private permissions", flush=True)

            # Verify the new activation path while the isolated service is stopped.
            execute(["systemctl", "stop", unit], "isolated service stop before add")
            stopped_added = node("stopped-added", smoke.free_port())
            stopped_added_file = work / "stopped-added.json"
            smoke.write_json(stopped_added_file, stopped_added)
            call("add_managed_inbound", str(stopped_added_file), smoke.LOOPBACK)
            pid()
            verify_forwarding("stopped-added")
            print("PASS stopped add: add_managed_inbound started the isolated service and forwarded HTTP", flush=True)
            # Return to the baseline bundle before the active-service transaction checks below.
            call("restore_backup", str(backup))
            pid()
            require(fixture_config() == baseline_config, "stopped-add cleanup did not restore baseline")

            added = node("added", smoke.free_port())
            added_file = work / "added.json"
            smoke.write_json(added_file, added)
            previous = pid()
            call("add_managed_inbound", str(added_file), smoke.LOOPBACK)
            check_restarted(previous)
            require(fixture_config()["inbounds"] == [baseline, added], "add changed other nodes")
            require("// baseline port" in config.read_text(encoding="utf-8"), "add lost existing comments")
            require(read_json(clients / "added.json")["inbound"] == added, "add metadata mismatch")
            verify_forwarding("added")
            print("PASS add: real restart, metadata synchronization and HTTP forwarding", flush=True)

            edited = dict(added, listen_port=smoke.free_port())
            edited_file = work / "edited.json"
            smoke.write_json(edited_file, edited)
            previous = pid()
            call("replace_inbound_file", "added", str(edited_file), smoke.LOOPBACK, "", str(added_file))
            check_restarted(previous)
            require(fixture_config()["inbounds"] == [baseline, edited], "edit changed other nodes")
            require("// baseline port" in config.read_text(encoding="utf-8"), "edit lost existing comments")
            verify_forwarding("added")
            print("PASS edit: real restart, preserved other node and HTTP forwarding", flush=True)

            paths = {kind: work / ("export-" + kind) for kind in ("links", "base64", "json")}
            previous = pid()
            for kind, path in paths.items():
                call("export_clients", kind, str(path))
                require(path.stat().st_mode & 0o777 == 0o600, "export permissions mismatch")
            links = paths["links"].read_text(encoding="utf-8")
            require(base64.b64decode(paths["base64"].read_bytes(), validate=True).decode("utf-8") == links,
                    "base64 export differs from links")
            parsed = [urlsplit(link) for link in links.splitlines()]
            require(len(parsed) == 2 and all(link.scheme == "vless" and link.hostname == smoke.LOOPBACK for link in parsed),
                    "export links malformed")
            require([link.port for link in parsed] == [baseline["listen_port"], edited["listen_port"]],
                    "export links have incorrect ports")
            require([link.username for link in parsed] == [baseline["users"][0]["uuid"], edited["users"][0]["uuid"]],
                    "export links have incorrect authentication")
            outbounds = read_json(paths["json"])["outbounds"]
            require([item["tag"] for item in outbounds] == ["baseline", "added"], "JSON export tags mismatch")
            require([item["server_port"] for item in outbounds] == [baseline["listen_port"], edited["listen_port"]],
                    "JSON export ports mismatch")
            require([item["uuid"] for item in outbounds] == [baseline["users"][0]["uuid"], edited["users"][0]["uuid"]],
                    "JSON export authentication mismatch")
            require(pid() == previous, "export unexpectedly restarted service")
            print("PASS exports: links/base64/JSON agree, permissions 600, no restart", flush=True)

            previous = pid()
            call("delete_inbound", "added")
            check_restarted(previous)
            require(fixture_config() == baseline_config, "delete did not preserve baseline")
            require("// baseline port" in config.read_text(encoding="utf-8"), "delete lost remaining node comments")
            require(not (clients / "added.json").exists(), "delete left node metadata")
            require(read_json(clients / "baseline.json") == baseline_metadata, "delete altered other metadata")
            print("PASS delete: real restart, selected node removed, baseline preserved", flush=True)

            # Change all restored categories so restore tests more than a no-op.
            changed_baseline = dict(baseline, listen_port=smoke.free_port())
            changed_file = work / "changed-baseline.json"
            smoke.write_json(changed_file, changed_baseline)
            call("replace_inbound_file", "baseline", str(changed_file), smoke.LOOPBACK)
            call("set_default_server", "changed.example")
            previous = pid()
            call("restore_backup", str(backup))
            check_restarted(previous)
            require(fixture_config() == baseline_config, "restore configuration mismatch")
            require(config.read_bytes() == original_commented, "restore did not recover original comments and layout")
            require(sorted(path.name for path in clients.iterdir()) == ["baseline.json"], "restore metadata set mismatch")
            require(read_json(clients / "baseline.json") == baseline_metadata, "restore metadata mismatch")
            require(default_file.read_text(encoding="utf-8") == smoke.LOOPBACK + "\n", "restore default address mismatch")
            verify_forwarding("baseline")
            print("PASS restore: real restart, exact baseline state and HTTP forwarding", flush=True)

            before = (config.read_bytes(), (clients / "baseline.json").read_bytes(), default_file.read_bytes(), pid())
            call("diagnose")
            after = (config.read_bytes(), (clients / "baseline.json").read_bytes(), default_file.read_bytes(), pid())
            require(before == after, "diagnose modified configuration or restarted service")
            print("PASS diagnose: healthy isolated service and no configuration/process changes", flush=True)
        finally:
            if unit_started:
                subprocess.run(["systemctl", "stop", unit], capture_output=True, timeout=30)
                status = subprocess.run(["systemctl", "is-active", "--quiet", unit], capture_output=True, timeout=15)
                require(status.returncode != 0, "isolated unit is still active after cleanup")
                subprocess.run(["systemctl", "reset-failed", unit], capture_output=True, timeout=15)
            if unit_created:
                require(runtime_unit.is_file() and not runtime_unit.is_symlink(), "runtime unit was replaced or removed")
                require(runtime_unit.read_bytes() == runtime_unit_source, "runtime unit changed during smoke")
                runtime_unit.unlink()
                execute(["systemctl", "daemon-reload"], "isolated unit daemon-reload after cleanup")
    print("PASS cleanup: isolated unit stopped; all temporary credentials and files removed", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--manager", type=Path, default=Path(__file__).resolve().parents[1] / "sing-box-manager.sh")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("requires root on Linux with running systemd")
    if not Path("/run/systemd/system").is_dir() or not all(shutil.which(name) for name in ("bash", "jq", "ss", "systemctl")):
        parser.error("requires systemd, bash, jq and ss")
    core, manager = args.core.resolve(), args.manager.resolve()
    if not core.is_file() or not os.access(core, os.X_OK) or not manager.is_file():
        parser.error("core must be executable and manager must be a file")
    os.umask(0o077)
    try:
        run(core, manager)
    except RuntimeError as error:
        # Only this file's fixed operation labels reach this output; helpers
        # which include core logs are replaced by a generic failure below.
        text = str(error)
        if "\n" in text or len(text) > 180:
            text = "isolated operation failed; sensitive diagnostics suppressed"
        print("FAIL management smoke: " + text, file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print("FAIL management smoke: isolated operation failed; sensitive diagnostics suppressed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
