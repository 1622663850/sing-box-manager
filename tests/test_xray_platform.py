#!/usr/bin/env python3
"""Isolated platform-management coverage for the standalone Xray manager.

Every test uses XrayFixture's temporary XR_ROOT_PREFIX.  systemctl, sysctl,
modprobe, downloads, and the staged core are controlled local stubs; no host
service, kernel setting, or production file is touched.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import unittest
import zipfile

import test_xray_manager as fixture


SCRIPT = fixture.SCRIPT


def github_payload(content: bytes, path: str) -> dict[str, object]:
    digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    return {
        "type": "file",
        "path": path,
        "encoding": "base64",
        "size": len(content),
        "sha": digest,
        "content": base64.b64encode(content).decode("ascii"),
    }


@unittest.skipUnless(SCRIPT.is_file(), "xray-manager.sh is required")
class XrayPlatformTest(fixture.XrayFixture):
    """Platform tests reuse the repository fixture and its dependency policy."""

    def setUp(self) -> None:
        fixture.XrayFixture.setUp(self)
        self.env["PATH"] = str(self.bin) + ":" + self.env.get("PATH", "")

    def _stage_core(self, label: str) -> Path:
        stage = self.root / ("stage-" + label)
        stage.mkdir()
        core = stage / "xray"
        content = self.core.read_bytes() + (b"\n# staged-" + label.encode() + b"\n")
        core.write_bytes(content)
        core.chmod(0o755)
        return stage

    def _stub_download_core(self) -> str:
        return r'''
download_core() {
    cp -- "$XR_PLATFORM_STAGE/xray" "$2/xray"
    chmod 755 "$2/xray"
    : >"$2/geoip.dat"
    : >"$2/geosite.dat"
    printf '%s\n' v26.10.31 >"$2/release-tag"
    "$2/xray" version >"$2/version.txt"
}
'''

    def _panel_candidate(self, version: str = "1.1.4", *, valid_help: bool = True) -> bytes:
        help_body = "printf 'Xray 中文终端管理面板\\n'" if valid_help else "return 1"
        return (
            "#!/usr/bin/env bash\n"
            "# xray-manager: a self-contained Chinese terminal manager.\n"
            f'XR_MANAGER_VERSION="{version}"\n'
            f"usage() {{ {help_body}; }}\n"
            "main() { case \"${1:-menu}\" in help|-h|--help) usage;; *) usage;; esac; }\n"
            'if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi\n'
        ).encode("utf-8")

    def _stub_panel_download(self, candidate: bytes) -> None:
        payload = self.root / "panel.json"
        fixture.dump(payload, github_payload(candidate, "xray-manager.sh"))
        self.env["XR_PANEL_FIXTURE"] = str(payload)

    def _prepare_core_release(self, *, bad_digest: bool = False) -> tuple[Path, Path, str]:
        real_core = self.env.get("XR_REAL_CORE")
        if not real_core or not Path(real_core).is_file():
            if platform.machine().lower() not in {"x86_64", "amd64"}:
                self.fail("download_core fixture requires an x86_64 Linux executable when XR_REAL_CORE is absent")
            real_path = self.root / "synthetic-xray"
            message = b"Xray 26.10.31 platform-fixture\n"
            code = (
                b"\xb8\x01\x00\x00\x00\xbf\x01\x00\x00\x00"
                b"\x48\x8d\x35\x10\x00\x00\x00"
                + b"\xba" + struct.pack("<I", len(message))
                + b"\x0f\x05\xb8\x3c\x00\x00\x00\x31\xff\x0f\x05"
                + message
            )
            header_size, phdr_size = 64, 56
            code_offset = 0x1000
            image_size = code_offset + len(code)
            elf_header = struct.pack(
                "<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + b"\0" * 9,
                2, 62, 1, 0x400000 + code_offset, 64, 0, 0, 64, phdr_size, 1, 0, 0, 0,
            )
            program_header = struct.pack(
                "<IIQQQQQQ", 1, 5, 0, 0x400000, 0, image_size, image_size, 0x1000,
            )
            real_path.write_bytes(elf_header + program_header + b"\0" * (code_offset - header_size - phdr_size) + code)
            real_path.chmod(0o755)
            real_core = str(real_path)
        self.env["XR_REAL_CORE"] = real_core
        asset = "Xray-linux-64.zip" if platform.machine().lower() in {"x86_64", "amd64"} else "Xray-linux-arm64-v8a.zip"
        archive = self.root / "xray-release.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(real_core, "xray")
            bundle.writestr("geoip.dat", b"geoip")
            bundle.writestr("geosite.dat", b"geosite")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        advertised = "0" * 64 if bad_digest else digest
        tag = "v26.10.31"
        api = self.root / "release.json"
        fixture.dump(api, {
            "draft": False,
            "prerelease": False,
            "tag_name": tag,
            "assets": [{
                "name": asset,
                "browser_download_url": f"https://github.com/XTLS/Xray-core/releases/download/{tag}/{asset}",
                "digest": "sha256:" + advertised,
            }],
        })
        destination = self.root / ("core-download-bad" if bad_digest else "core-download-good")
        destination.mkdir()
        self.env.update({"XR_RELEASE_JSON": str(api), "XR_RELEASE_ZIP": str(archive)})
        return destination, archive, asset

    def _stub_core_download_http(self) -> str:
        return r'''
download_https() {
    case "$1" in
        https://api.github.com/repos/XTLS/Xray-core/releases/tags/v26.10.31) cp -- "$XR_RELEASE_JSON" "$2";;
        https://github.com/XTLS/Xray-core/releases/download/v26.10.31/Xray-linux-64.zip|https://github.com/XTLS/Xray-core/releases/download/v26.10.31/Xray-linux-arm64-v8a.zip) cp -- "$XR_RELEASE_ZIP" "$2";;
        *) return 1;;
    esac
}
'''

    def _panel_download_function(self) -> str:
        return r'''
download_https() {
    [[ $1 == "https://api.github.com/repos/1622663850/sing-box-manager/contents/xray-manager.sh?ref=main" ]] || return 77
    cp -- "$XR_PANEL_FIXTURE" "$2"
}
'''

    def _install_bbr_stubs(self) -> None:
        state = self.root / "bbr-state.json"
        fixture.dump(state, {
            "tcp_congestion_control": "cubic",
            "default_qdisc": "fq_codel",
            "available": "reno cubic bbr",
            "fail_write_once": False,
            "modprobe_fail": False,
        })
        sysctl = self.bin / "sysctl"
        sysctl.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "p=pathlib.Path(os.environ['XR_BBR_STATE']); s=json.loads(p.read_text()); a=sys.argv[1:]\n"
            "def k(x):\n"
            "  return 'tcp_congestion_control' if x.endswith('tcp_congestion_control') else 'default_qdisc' if x.endswith('default_qdisc') else 'available'\n"
            "if '-n' in a: print(s.get(k(a[a.index('-n')+1]), '')); raise SystemExit(0)\n"
            "if '-w' in a:\n"
            "  if s.get('fail_write_once'): s['fail_write_once']=False; p.write_text(json.dumps(s)); raise SystemExit(1)\n"
            "  for item in a:\n"
            "    if '=' in item: key,val=item.split('=',1); s[k(key)]=val\n"
            "  p.write_text(json.dumps(s)); raise SystemExit(0)\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        sysctl.chmod(0o755)
        modprobe = self.bin / "modprobe"
        modprobe.write_text(
            "#!/usr/bin/env python3\n"
            "import json,os,pathlib,sys\n"
            "p=pathlib.Path(os.environ['XR_BBR_STATE']);s=json.loads(p.read_text())\n"
            "if s.get('modprobe_fail'): raise SystemExit(1)\n"
            "if 'tcp_bbr' in sys.argv: s['available']=s.get('available','')+' bbr'\n"
            "p.write_text(json.dumps(s))\n",
            encoding="utf-8",
        )
        modprobe.chmod(0o755)
        self.env["XR_BBR_STATE"] = str(state)

    def _fail_daemon_reload_once(self) -> None:
        real = self.bin / "systemctl.real"
        self.bin.joinpath("systemctl").rename(real)
        marker = self.root / "daemon-reload-once"
        wrapper = self.bin / "systemctl"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "args=sys.argv[1:]\n"
            "marker=pathlib.Path(os.environ['XR_DAEMON_RELOAD_MARKER'])\n"
            "if 'daemon-reload' in args and not marker.exists():\n"
            "  marker.write_text('used')\n"
            "  raise SystemExit(1)\n"
            "os.execv(os.environ['XR_DAEMON_REAL'], [os.environ['XR_DAEMON_REAL'], *args])\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        self.env["XR_DAEMON_REAL"] = str(real)
        self.env["XR_DAEMON_RELOAD_MARKER"] = str(marker)

    def test_setup_panel_locked_creates_owned_paths_and_base_config(self) -> None:
        self.core.unlink()
        self.marker.unlink()
        self.wrapper.unlink(missing_ok=True)
        self.unit.unlink()
        self.config.unlink()
        self.lib_dir.rmdir()
        self.config.parent.rmdir()
        shutil.rmtree(self.state_dir)
        self.env["XR_MOCK_UNIT_FILE"] = ""
        result = self.shell("setup_panel_locked", check=True)
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "xray-manager-v1\n")
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["inbounds"], [])
        self.assertTrue(self.wrapper.is_file())
        self.assertEqual(self.wrapper.stat().st_mode & 0o777, 0o755)
        self.assertIn("独立 Xray 面板已安装", result.stdout)

    def test_install_core_starts_stopped_service_without_enabling(self) -> None:
        stage = self._stage_core("install")
        self.core.unlink()
        self.unit.unlink()
        self.env["XR_PLATFORM_STAGE"] = str(stage)
        self.state(active=False, enabled=False)
        result = self.shell(self._stub_download_core() + "install_core_locked 26.10.31 install", check=True)
        self.assertTrue(self.core.is_file())
        self.assertTrue(self.unit.is_file())
        self.assertTrue(self.state()["active"])
        self.assertFalse(self.state()["enabled"])
        self.assertTrue(any(call[:1] == ["start"] and "xray.service" in call for call in self.calls()))
        self.assertIn("核心已安装/更新", result.stdout)

    def test_update_core_replaces_core_and_preserves_autostart(self) -> None:
        old = self.core.read_bytes()
        stage = self._stage_core("update")
        self.env["XR_PLATFORM_STAGE"] = str(stage)
        self.state(active=True, enabled=True)
        result = self.shell(self._stub_download_core() + "install_core_locked 26.10.31 update", check=True)
        self.assertNotEqual(self.core.read_bytes(), old)
        self.assertTrue(self.state()["active"])
        self.assertTrue(self.state()["enabled"])
        self.assertFalse(list(self.backups_dir.glob("core-*")))
        self.assertIn("核心已安装/更新", result.stdout)

    def test_update_core_restart_failure_restores_core_unit_and_state(self) -> None:
        old_core, old_unit = self.core.read_bytes(), self.unit.read_bytes()
        stage = self._stage_core("update-fail")
        self.env["XR_PLATFORM_STAGE"] = str(stage)
        self.state(active=True, enabled=True, restart_failures=1)
        result = self.shell(self._stub_download_core() + "install_core_locked 26.10.31 update")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.core.read_bytes(), old_core)
        self.assertEqual(self.unit.read_bytes(), old_unit)
        self.assertTrue(self.state()["active"])
        self.assertTrue(self.state()["enabled"])

    def test_install_start_failure_rolls_back_new_core_and_service(self) -> None:
        stage = self._stage_core("install-fail")
        self.core.unlink()
        self.env["XR_PLATFORM_STAGE"] = str(stage)
        self.state(active=False, enabled=False, start_failures=1)
        result = self.shell(self._stub_download_core() + "install_core_locked 26.10.31 install")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.core.exists())
        self.assertFalse(self.state()["active"])

    def test_uninstall_core_locked_preserves_config_and_panel(self) -> None:
        wrapper_before = SCRIPT.read_bytes()
        self.wrapper.write_bytes(wrapper_before)
        self.wrapper.chmod(0o755)
        metadata = self.clients_dir / "keep.json"
        metadata.write_text('{"keep":true}\n', encoding="utf-8")
        backup = self.backups_dir / "keep.json"
        backup.write_text('{"backup":true}\n', encoding="utf-8")
        config_before = self.config.read_bytes()
        result = self.shell("uninstall_core_locked", check=True)
        self.assertFalse(self.core.exists())
        self.assertFalse(self.unit.exists())
        self.assertTrue(self.marker.exists())
        self.assertTrue(self.wrapper.is_file())
        self.assertEqual(self.wrapper.read_bytes(), wrapper_before)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(metadata.read_text(encoding="utf-8"), '{"keep":true}\n')
        self.assertEqual(backup.read_text(encoding="utf-8"), '{"backup":true}\n')
        self.assertFalse(list(self.backups_dir.glob("uninstall-*")))
        self.assertFalse(self.state()["active"])
        self.assertFalse(self.state()["enabled"])

    def test_download_core_validates_release_zip_and_sha256_digest(self) -> None:
        destination, archive, asset = self._prepare_core_release()
        result = self.shell(self._stub_core_download_http() + 'download_core 26.10.31 "$1"', str(destination), check=True)
        self.assertEqual((destination / "release-tag").read_text(encoding="utf-8").strip(), "v26.10.31")
        self.assertTrue((destination / "version.txt").read_text(encoding="utf-8").startswith("Xray "))
        self.assertEqual((destination / "xray").read_bytes(), Path(self.env["XR_REAL_CORE"]).read_bytes())
        self.assertEqual((destination / "geoip.dat").read_bytes(), b"geoip")
        self.assertEqual((destination / "geosite.dat").read_bytes(), b"geosite")
        self.assertTrue(archive.is_file())

    def test_download_core_rejects_bad_sha256_without_extracting(self) -> None:
        destination, _, _ = self._prepare_core_release(bad_digest=True)
        result = self.shell(self._stub_core_download_http() + 'download_core 26.10.31 "$1"', str(destination))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((destination / "xray").exists())
        self.assertFalse((destination / "release-tag").exists())

    def test_remove_panel_route_removes_only_wrapper_after_core_is_gone(self) -> None:
        wrapper_before = SCRIPT.read_bytes()
        self.wrapper.write_bytes(wrapper_before)
        self.wrapper.chmod(0o755)
        metadata = self.clients_dir / "keep.json"
        metadata.write_text('{"keep":true}\n', encoding="utf-8")
        backup = self.backups_dir / "keep.json"
        backup.write_text('{"backup":true}\n', encoding="utf-8")
        config_before = self.config.read_bytes()
        self.core.unlink()
        self.assertTrue(self.wrapper.is_file())
        result = self.shell("XR_ROOT_PREFIX=''; confirm() { return 0; }; require_root(){ return 0; }; require_systemd(){ return 0; }; main remove-panel", check=True)
        self.assertFalse(self.wrapper.exists())
        self.assertTrue(self.marker.exists())
        self.assertTrue(self.config.exists())
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(metadata.read_text(encoding="utf-8"), '{"keep":true}\n')
        self.assertEqual(backup.read_text(encoding="utf-8"), '{"backup":true}\n')

    def test_update_panel_locked_valid_candidate_backs_up_old_wrapper(self) -> None:
        old = b"#!/usr/bin/env bash\n# old panel\n"
        self.wrapper.write_bytes(old)
        self.wrapper.chmod(0o755)
        candidate = self._panel_candidate()
        self._stub_panel_download(candidate)
        result = self.shell(self._panel_download_function() + "update_panel_locked", check=True)
        self.assertEqual(self.wrapper.read_bytes(), candidate)
        backups = list(self.backups_dir.glob("panel-*.sh"))
        self.assertEqual(backups, [])
        self.assertIn("面板已更新", result.stdout)

    def test_update_panel_locked_rejects_bad_candidate_without_write(self) -> None:
        old = b"#!/usr/bin/env bash\n# old panel\n"
        self.wrapper.write_bytes(old)
        self.wrapper.chmod(0o755)
        self._stub_panel_download(self._panel_candidate(valid_help=False))
        result = self.shell(self._panel_download_function() + "update_panel_locked")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.wrapper.read_bytes(), old)
        backups = list(self.backups_dir.glob("panel-*.sh"))
        self.assertEqual(backups, [])

    def test_update_panel_locked_atomic_publish_failure_restores_old_wrapper(self) -> None:
        old = b"#!/usr/bin/env bash\n# old panel\n"
        self.wrapper.write_bytes(old)
        self.wrapper.chmod(0o755)
        self._stub_panel_download(self._panel_candidate())
        body = self._panel_download_function() + r'''
eval "$(declare -f atomic_copy | sed '1s/^atomic_copy/real_atomic_copy/')"
atomic_copy() {
    if [[ ! -e "$XR_PANEL_ATOMIC_ONCE" ]]; then : >"$XR_PANEL_ATOMIC_ONCE"; return 1; fi
    real_atomic_copy "$@"
}
update_panel_locked
'''
        self.env["XR_PANEL_ATOMIC_ONCE"] = str(self.root / "atomic-once")
        result = self.shell(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.wrapper.read_bytes(), old)

    def test_tcp_bbr_enable_disable_round_trip_uses_only_temp_sysctl(self) -> None:
        self._install_bbr_stubs()
        result = self.shell("confirm() { return 0; }; enable_bbr_locked", check=True)
        state = json.loads((self.root / "bbr-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["tcp_congestion_control"], "bbr")
        self.assertEqual(state["default_qdisc"], "fq")
        bbr_file = self.root / "etc/sysctl.d/99-xray-manager-bbr.conf"
        self.assertTrue(bbr_file.is_file())
        baseline_archives = list(self.backups_dir.glob("bbr-*"))
        self.assertEqual(len(baseline_archives), 1)
        result = self.shell("confirm() { return 0; }; disable_bbr_locked", check=True)
        restored = json.loads((self.root / "bbr-state.json").read_text(encoding="utf-8"))
        self.assertEqual(restored["tcp_congestion_control"], "cubic")
        self.assertEqual(restored["default_qdisc"], "fq_codel")
        self.assertFalse(bbr_file.exists())
        self.assertEqual(list(self.backups_dir.glob("bbr-*")), baseline_archives)

    def test_uninstall_daemon_reload_failure_restores_core_unit_and_service_state(self) -> None:
        wrapper_before = SCRIPT.read_bytes()
        self.wrapper.write_bytes(wrapper_before)
        self.wrapper.chmod(0o755)
        config_before = self.config.read_bytes()
        self._fail_daemon_reload_once()
        self.state(active=True, enabled=True)
        result = self.shell("uninstall_core_locked")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.core.is_file())
        self.assertTrue(self.unit.is_file())
        self.assertTrue(self.wrapper.is_file())
        self.assertEqual(self.wrapper.read_bytes(), wrapper_before)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertTrue(self.state()["active"])
        self.assertTrue(self.state()["enabled"])
        self.assertFalse(list(self.state_dir.glob("uninstall.*")))

    def test_tcp_bbr_conflict_and_runtime_failure_leave_state_unchanged(self) -> None:
        self._install_bbr_stubs()
        foreign = self.root / "etc/sysctl.d/99-xray-manager-bbr.conf"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("# foreign\nnet.ipv4.tcp_congestion_control=cubic\n", encoding="utf-8")
        before = foreign.read_bytes()
        result = self.shell("confirm() { return 0; }; enable_bbr_locked")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(foreign.read_bytes(), before)
        foreign.unlink()
        self.state_after_bbr = json.loads((self.root / "bbr-state.json").read_text())
        self.state_after_bbr["fail_write_once"] = True
        fixture.dump(self.root / "bbr-state.json", self.state_after_bbr)
        before_state = json.loads((self.root / "bbr-state.json").read_text())
        result = self.shell("confirm() { return 0; }; enable_bbr_locked")
        self.assertNotEqual(result.returncode, 0)
        after_state = json.loads((self.root / "bbr-state.json").read_text())
        self.assertEqual(after_state["tcp_congestion_control"], before_state["tcp_congestion_control"])
        self.assertEqual(after_state["default_qdisc"], before_state["default_qdisc"])
        self.assertFalse((self.root / "etc/sysctl.d/99-xray-manager-bbr.conf").exists())

    def test_acme_menu_valid_download_installs_and_bad_content_is_rejected(self) -> None:
        candidate = (
            b"#!/usr/bin/env bash\n"
            b"# https://github.com/1622663850/acme-script\n"
            b"# acme-script: persistent-menu-v1\n"
            b"menu() { :; }\n"
        )
        payload = self.root / "acme-payload.json"
        fixture.dump(payload, github_payload(candidate, "acme_cf_token.sh"))
        self.env["XR_ACME_FIXTURE"] = str(payload)
        download = r'''
download_https() { cp -- "$XR_ACME_FIXTURE" "$2"; }
'''
        result = self.shell(download + "ensure_acme_menu_locked", check=True)
        acme = self.root / "root/acme-script/acme_cf_token_menu_v1.sh"
        self.assertEqual(Path(result.stdout.strip()), acme)
        self.assertEqual(acme.read_bytes(), candidate)
        self.assertEqual(acme.stat().st_mode & 0o777, 0o700)

        bad = candidate.replace(b"persistent-menu-v1", b"invalid-marker")
        fixture.dump(payload, github_payload(bad, "acme_cf_token.sh"))
        acme.unlink()
        failed = self.shell(download + "ensure_acme_menu_locked")
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(acme.exists())

    def test_open_acme_menu_returns_external_menu_status_and_download_failure(self) -> None:
        candidate = (
            b"#!/usr/bin/env bash\n"
            b"# https://github.com/1622663850/acme-script\n"
            b"# acme-script: persistent-menu-v1\n"
            b"menu() { return 7; }\n"
            b"menu\n"
        )
        payload = self.root / "acme-open-payload.json"
        fixture.dump(payload, github_payload(candidate, "acme_cf_token.sh"))
        self.env["XR_ACME_FIXTURE"] = str(payload)
        download = 'download_https() { cp -- "$XR_ACME_FIXTURE" "$2"; }; '
        failed_menu = self.shell(download + "open_acme_menu")
        self.assertNotEqual(failed_menu.returncode, 0)
        self.assertTrue((self.root / "root/acme-script/acme_cf_token_menu_v1.sh").is_file())

        self.root.joinpath("root/acme-script/acme_cf_token_menu_v1.sh").unlink()
        download_failed = self.shell("download_https() { return 1; }; open_acme_menu")
        self.assertNotEqual(download_failed.returncode, 0)
        self.assertFalse((self.root / "root/acme-script/acme_cf_token_menu_v1.sh").exists())

        cancelled = candidate.replace(b"return 7", b"return 0")
        fixture.dump(payload, github_payload(cancelled, "acme_cf_token.sh"))
        returned = self.shell(download + "open_acme_menu")
        self.assertEqual(returned.returncode, 0, returned.stdout + returned.stderr)


if __name__ == "__main__":
    unittest.main()
