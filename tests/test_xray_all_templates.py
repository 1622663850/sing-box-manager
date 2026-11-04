#!/usr/bin/env python3
"""Exercise every interactive Xray inbound template through save/export/delete."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest
from urllib.parse import urlsplit

import test_xray_manager as fixture
import test_xray_pq as pq_fixture


UUID = "b7efdc96-87aa-4910-81b4-374b2eecc737"
SS_PASSWORD = "8JCsPssfgS8tiRwiMlhARg=="


# This driver deliberately goes through choose_template -> node_wizard_locked,
# add_managed_inbound, show_client, and delete_inbound.  It does not call the
# pure generate_inbound helper directly.
WIZARD_DRIVER = r'''
set -u
source "$1"
root=$2
fixture_menu_choice=$3
fixture_tag=$4
fixture_port=$5
fixture_certificate=$6
fixture_tls_key=$7
fixture_edit_port=$8
wait_healthy() { service_is_active; }
cleaned=0
mkdir -p "$root"
cleanup() {
    if [[ $cleaned != 1 ]]; then
        delete_inbound "$fixture_tag" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

prompt() {
    local variable=$1 message default value
    message=${2:-}
    default=${3:-}
    value=$default
    case "$variable" in
        protocol_choice) value=$fixture_menu_choice ;;
        tag) value=$fixture_tag ;;
        port) [[ -n ${old:-} ]] && value=$fixture_edit_port || value=$fixture_port ;;
        tls_name|server) value=example.com ;;
        certificate) value=$fixture_certificate ;;
        tls_key) value=$fixture_tls_key ;;
        extra)
            case "$protocol" in
                *-ws|*-ws-tls) value=/template-test ;;
                *-grpc-*) value=TemplateService ;;
                *-xhttp-*) value=/template-xhttp ;;
                *) value=$default ;;
            esac
            ;;
        obfs_choice) value=1 ;;
        choice)
            case "$message" in
                *"VLESS 后量子混合加密"*)
                    [[ $protocol == vless-xhttp-reality-vision ]] && value=2 || value=1 ;;
                *"REALITY 密钥"*) value=1 ;;
                *"REALITY ML-DSA-65"*) value=1 ;;
                *) value=$default ;;
            esac
            ;;
    esac
    printf -v "$variable" '%s' "$value"
}
prompt_secret() {
    local variable=$1 default value
    default=${3:-}
    value=$default
    case "$variable" in
        credential)
            case "$protocol" in
                vless-*|vmess-*) value='b7efdc96-87aa-4910-81b4-374b2eecc737' ;;
                shadowsocks) value='8JCsPssfgS8tiRwiMlhARg==' ;;
                trojan) value=template-trojan-password ;;
                hysteria) value=template-hysteria-password ;;
            esac
            ;;
        sid) value=01234567 ;;
        obfs) value=template-obfs ;;
    esac
    printf -v "$variable" '%s' "$value"
}
confirm() { return 0; }

add_wizard_locked || exit $?
config_jq --arg t "$fixture_tag" '[.inbounds[]?|select(.tag==$t)]|if length==1 then .[0] else error("added node missing") end' >"$root/inbound-before.json" || exit 1
edit_wizard_locked "$fixture_tag" || exit $?
config_jq --arg t "$fixture_tag" '[.inbounds[]?|select(.tag==$t)]|if length==1 then .[0] else error("edited node missing") end' >"$root/inbound.json" || exit 1
show_client "$fixture_tag" example.com json-only 1 >"$root/outbound.json" || exit 1
show_client "$fixture_tag" example.com link-only 1 >"$root/link.txt" || exit 1
delete_inbound "$fixture_tag" >"$root/delete.log" 2>&1 || exit 1
cleaned=1
[[ $(config_jq --arg t "$fixture_tag" '[.inbounds[]?|select(.tag==$t)]|length') == 0 ]] || exit 1
[[ ! -e "$CLIENT_DIR/$fixture_tag.json" && ! -L "$CLIENT_DIR/$fixture_tag.json" ]] || exit 1
if find "$BACKUP_DIR" -type f -print -quit | grep -q .; then exit 1; fi
printf 'PASS wizard %s/%s\n' "$fixture_menu_choice" "$fixture_tag"
'''


IMPORT_DRIVER = r'''
set -u
source "$1"
root=$2
fixture_choice=$3
fixture_file=$4
fixture_tag=$5
fixture_original=$6
cleaned=0
mkdir -p "$root"
wait_healthy() { service_is_active; }
cleanup() {
    if [[ $cleaned != 1 ]]; then
        delete_inbound "$fixture_tag" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT
prompt() {
    local variable=$1 message default value
    message=${2:-}
    default=${3:-}
    value=$default
    case "$variable" in
        protocol_choice) value=$fixture_choice ;;
        tag) value=$fixture_tag ;;
        port) value=23000 ;;
        file) value=$fixture_file ;;
    esac
    printf -v "$variable" '%s' "$value"
}
prompt_secret() { printf -v "$1" '%s' "${3:-}"; }
confirm() { return 0; }
add_wizard_locked || exit $?
if [[ $fixture_choice == 18 ]]; then
    config_jq --arg t "$fixture_tag" '[.inbounds[]?|select(.tag==$t)]|if length==1 then .[0] else error("imported node missing") end' >"$root/imported.json" || exit 1
    [[ -f "$CLIENT_DIR/$fixture_tag.json" && ! -L "$CLIENT_DIR/$fixture_tag.json" ]] || exit 1
    if find "$BACKUP_DIR" -type f -print -quit | grep -q .; then exit 1; fi
    delete_inbound "$fixture_tag" >/dev/null 2>&1 || exit 1
    cleaned=1
    [[ $(config_jq --arg t "$fixture_tag" '[.inbounds[]?|select(.tag==$t)]|length') == 0 ]] || exit 1
    [[ ! -e "$CLIENT_DIR/$fixture_tag.json" && ! -L "$CLIENT_DIR/$fixture_tag.json" ]] || exit 1
    printf 'PASS import %s/%s\n' "$fixture_choice" "$fixture_tag"
else
    config_jq . >"$root/imported-before-restore.json" || exit 1
    if find "$BACKUP_DIR" -type f -print -quit | grep -q .; then exit 1; fi
    import_config "$fixture_original" || exit 1
    cmp -s "$CONFIG_FILE" "$fixture_original" || exit 1
    [[ $(config_jq '[.inbounds[]?|select(.tag=="import-full")]|length') == 0 ]] || exit 1
    cleaned=1
    printf 'PASS import %s/%s\n' "$fixture_choice" "$fixture_tag"
fi
'''


TEMPLATES = {
    1: ("vless", "raw", "reality", "vless-reality", "vless", "tcp"),
    2: ("vless", "raw", "none", "vless-tcp", "vless", "tcp"),
    3: ("vless", "raw", "tls", "vless-tcp-tls", "vless", "tcp"),
    4: ("vless", "websocket", "none", "vless-ws", "vless", "ws"),
    5: ("vless", "websocket", "tls", "vless-ws-tls", "vless", "ws"),
    6: ("vless", "grpc", "tls", "vless-grpc-tls", "vless", "grpc"),
    7: ("vless", "grpc", "reality", "vless-grpc-reality", "vless", "grpc"),
    8: ("vless", "xhttp", "tls", "vless-xhttp-tls", "vless", "xhttp"),
    9: ("vless", "xhttp", "reality", "vless-xhttp-reality", "vless", "xhttp"),
    10: ("vmess", "raw", "none", "vmess-tcp", "vmess", "tcp"),
    11: ("vmess", "raw", "tls", "vmess-tcp-tls", "vmess", "tcp"),
    12: ("vmess", "websocket", "none", "vmess-ws", "vmess", "ws"),
    13: ("vmess", "websocket", "tls", "vmess-ws-tls", "vmess", "ws"),
    14: ("vmess", "grpc", "tls", "vmess-grpc-tls", "vmess", "grpc"),
    15: ("trojan", "raw", "tls", "trojan", "trojan", "tcp"),
    16: ("shadowsocks", "raw", "none", "shadowsocks", "shadowsocks", "tcp,udp"),
    17: ("hysteria", "hysteria", "tls", "hysteria", "hysteria", "udp"),
    20: ("vless", "xhttp", "reality", "vless-xhttp-reality-vision", "vless", "xhttp"),
}


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class XrayAllTemplatesTest(fixture.XrayFixture):
    """One real wizard/save/export/delete transaction per menu template."""

    def setUp(self) -> None:
        super().setUp()
        if not shutil.which("openssl"):
            self.skipTest("openssl is required for TLS templates")
        certificate = self.root / "template-cert.pem"
        private_key = self.root / "template-key.pem"
        result = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
             "-subj", "/CN=example.com", "-addext", "subjectAltName=DNS:example.com"],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, "temporary certificate generation failed")
        self.certificate = certificate
        self.private_key = private_key
        # The normal fixture mock intentionally has no PQ commands.  This
        # deterministic core supports only the commands needed by template 20.
        self.core.write_text(pq_fixture.PQ_CORE, encoding="utf-8")
        self.core.chmod(0o755)

    def run_template(self, menu_choice: int) -> tuple[dict, dict, str, str]:
        tag = f"template-{menu_choice}"
        root = self.root / f"result-{menu_choice}"
        result = subprocess.run(
            ["bash", "-c", WIZARD_DRIVER, "template-test", str(fixture.SCRIPT),
             str(root), str(menu_choice), tag, str(21000 + menu_choice),
             str(self.certificate), str(self.private_key), str(22000 + menu_choice)],
            env=self.env, stdin=subprocess.DEVNULL, text=True,
            capture_output=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, f"wizard menu {menu_choice} failed with exit code {result.returncode}")
        inbound_before = json.loads((root / "inbound-before.json").read_text(encoding="utf-8"))
        inbound = json.loads((root / "inbound.json").read_text(encoding="utf-8"))
        outbound = json.loads((root / "outbound.json").read_text(encoding="utf-8"))
        link = (root / "link.txt").read_text(encoding="utf-8").strip()
        delete_log = (root / "delete.log").read_text(encoding="utf-8")
        self.assertTrue(link)
        self.assertIn("配置及连接信息已保存", result.stdout)
        self.assertIn("配置及连接信息已保存", delete_log)
        self.assertEqual(inbound["port"], 22000 + menu_choice)
        self.assertEqual(inbound_before["port"], 21000 + menu_choice)
        self.assertEqual(inbound["protocol"], inbound_before["protocol"])
        self.assertEqual(inbound.get("streamSettings", {}), inbound_before.get("streamSettings", {}))
        self.assertEqual(inbound["settings"], inbound_before["settings"])
        self.assertEqual(list(self.backups_dir.iterdir()), [])
        return inbound, outbound, link, delete_log

    def run_import(self, menu_choice: int) -> tuple[dict, Path, Path]:
        tag = "import-node" if menu_choice == 18 else "import-full"
        root = self.root / f"result-import-{menu_choice}"
        node = self.write_node(tag=tag, port=23000 + menu_choice, protocol="vless", method="tcp")
        node_path = self.root / f"{tag}.json"
        node_path.write_text(json.dumps(node, ensure_ascii=False) + "\n", encoding="utf-8")
        full = dict(self.original)
        full["inbounds"] = [node]
        full_path = self.root / "full-import.json"
        full_path.write_text(json.dumps(full, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        original_path = self.root / "original-import.json"
        original_path.write_bytes(self.config.read_bytes())
        source = node_path if menu_choice == 18 else full_path
        result = subprocess.run(
            ["bash", "-c", IMPORT_DRIVER, "import-test", str(fixture.SCRIPT),
             str(root), str(menu_choice), str(source), tag, str(original_path)],
            env=self.env, stdin=subprocess.DEVNULL, text=True,
            capture_output=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, f"import menu {menu_choice} failed with exit code {result.returncode}")
        expected_message = "配置及连接信息已保存" if menu_choice == 18 else "配置已保存"
        self.assertIn(expected_message, result.stdout + result.stderr)
        self.assertEqual(list(self.backups_dir.iterdir()), [])
        if menu_choice == 18:
            imported = json.loads((root / "imported.json").read_text(encoding="utf-8"))
            self.assertEqual(imported["tag"], tag)
        else:
            imported = json.loads((root / "imported-before-restore.json").read_text(encoding="utf-8"))
            self.assertEqual(imported["inbounds"][0]["tag"], tag)
            self.assertEqual(self.config.read_bytes(), original_path.read_bytes())
        return imported, source, root

    def test_every_add_menu_template_saves_exports_and_cleans(self) -> None:
        self.assertEqual(set(TEMPLATES), {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 20})
        for menu_choice, (protocol, method, security, _label, export_protocol, link_type) in TEMPLATES.items():
            with self.subTest(menu_choice=menu_choice):
                inbound, outbound, link, _ = self.run_template(menu_choice)
                self.assertEqual(inbound["protocol"], protocol)
                inbound_stream = inbound.get("streamSettings", {})
                outbound_stream = outbound.get("streamSettings", {})
                self.assertEqual(inbound_stream.get("method", "raw"), method)
                self.assertEqual(inbound_stream.get("security", "none"), security)
                self.assertEqual(outbound["protocol"], export_protocol)
                self.assertEqual(outbound_stream.get("method", "raw"), method)
                self.assertEqual(outbound_stream.get("security", "none"), security)
                self.assertEqual(urlsplit(link).scheme, {"vless": "vless", "vmess": "vmess",
                                 "trojan": "trojan", "shadowsocks": "ss", "hysteria": "hysteria2"}[export_protocol])
                if method in {"websocket", "xhttp"}:
                    settings = inbound_stream.get("wsSettings") or inbound_stream.get("xhttpSettings")
                    self.assertEqual(settings["path"], "/template-test" if method == "websocket" else "/template-xhttp")
                if method == "grpc":
                    self.assertEqual(inbound_stream["grpcSettings"]["serviceName"], "TemplateService")
                if menu_choice in {1, 20}:
                    self.assertEqual(inbound["settings"]["users"][0]["flow"], "xtls-rprx-vision")
                if menu_choice == 20:
                    self.assertTrue(inbound["settings"]["decryption"].startswith("mlkem768x25519plus."))
                else:
                    self.assertEqual(inbound["settings"].get("decryption", "none"), "none")
                if menu_choice == 16:
                    self.assertEqual(inbound["settings"]["network"], "tcp,udp")
                    self.assertNotIn("streamSettings", outbound)
                if security in {"tls", "reality"}:
                    settings = inbound_stream.get("tlsSettings") or inbound_stream.get("realitySettings")
                    self.assertTrue(settings)
                print(f"PASS wizard {menu_choice}/{inbound['protocol']}", flush=True)

    def test_import_single_inbound_json_saves_and_deletes(self) -> None:
        imported, _, root = self.run_import(18)
        self.assertEqual(imported["protocol"], "vless")
        self.assertEqual(imported["streamSettings"]["method"], "tcp")
        self.assertTrue((root / "imported.json").is_file())
        self.assertEqual(self.config.read_text(encoding="utf-8").count("import-node"), 0)
        print("PASS import 18/inbound", flush=True)

    def test_import_full_xray_json_replaces_and_restores(self) -> None:
        imported, _, root = self.run_import(19)
        self.assertEqual(imported["inbounds"][0]["tag"], "import-full")
        self.assertTrue((root / "imported-before-restore.json").is_file())
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["inbounds"], [])
        print("PASS import 19/full", flush=True)


if __name__ == "__main__":
    unittest.main()
