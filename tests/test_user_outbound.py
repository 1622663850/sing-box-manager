#!/usr/bin/env python3
"""Focused tests for per-user outbound bindings on sing-box inbounds."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_manager as fixture


UUID1 = "b7efdc96-87aa-4910-81b4-374b2eecc737"
UUID2 = "0325ba41-4946-4ad5-b6b3-476eb50c37cb"
UUID3 = "4d8f9f0e-29f1-4d31-9c1d-fcfb74000001"
UUID4 = "4d8f9f0e-29f1-4d31-9c1d-fcfb74000002"


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class UserOutboundTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)
        self.marker = self.core.parent / ".managed"
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)
        self._tls: tuple[Path, Path] | None = None

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def calls(self) -> list[list[str]]:
        return fixture.ManagerTest.calls(self, self.system_log)

    def generate_inbound(self, *args: str, **kwargs: object) -> dict:
        return fixture.ManagerTest.generate_inbound(self, *args, **kwargs)

    def _certificate(self) -> tuple[Path, Path]:
        if self._tls is None:
            self._tls = fixture.ManagerTest.tls_material(self, "example.com")
        return self._tls

    def _node(
        self,
        protocol: str,
        tag: str,
        port: int,
        names: tuple[object, ...] = ("alice", "bob"),
    ) -> dict:
        if protocol == "vless":
            node = self.generate_inbound("vless-tcp", UUID1)
            users = [{"uuid": value} for value in (UUID1, UUID2, UUID3, UUID4)]
        elif protocol == "vmess":
            node = self.generate_inbound("vmess-tcp", UUID1)
            users = [{"uuid": value} for value in (UUID1, UUID2, UUID3, UUID4)]
        elif protocol == "hysteria2":
            certificate, private_key = self._certificate()
            node = self.generate_inbound(
                "hysteria2", "hy-password", tls_server_name="example.com",
                certificate=certificate, key=private_key,
            )
            users = [{"password": "hy-" + name} for name in ("alice", "bob", "charlie", "delta")]
        elif protocol == "tuic":
            certificate, private_key = self._certificate()
            node = self.generate_inbound(
                "tuic", UUID1, tls_server_name="example.com", certificate=certificate,
                key=private_key, tuic_password="tuic-password",
            )
            users = [
                {"uuid": value, "password": "tuic-" + name}
                for value, name in zip((UUID1, UUID2, UUID3, UUID4), ("alice", "bob", "charlie", "delta"))
            ]
        elif protocol == "anytls":
            certificate, private_key = self._certificate()
            node = self.generate_inbound(
                "anytls", "anytls-password", tls_server_name="example.com",
                certificate=certificate, key=private_key,
            )
            users = [{"password": "any-" + name} for name in ("alice", "bob", "charlie", "delta")]
        else:
            raise ValueError(protocol)
        node = copy.deepcopy(node)
        node.update({"tag": tag, "listen": "127.0.0.1", "listen_port": port})
        users = users[:len(names)]
        for user, name in zip(users, names):
            if name is not _MISSING:
                user["name"] = name
        node["users"] = users
        return node

    def _write_config(
        self,
        nodes: list[dict],
        *,
        rules: list[dict] | None = None,
        outbounds: list[dict] | None = None,
        comments: bool = False,
    ) -> None:
        value = copy.deepcopy(self.original)
        value["inbounds"] = nodes
        value["outbounds"] = [{"type": "direct", "tag": "direct"}] + list(outbounds or [])
        value["route"] = {"rules": list(rules or [])}
        if comments:
            self.config.write_text(
                "// keep user outbound comment\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            fixture.dump(self.config, value)

    def _metadata(self, node: dict, *, tag: str | None = None, **extra: object) -> Path:
        name = tag or node["tag"]
        path = self.state_dir / "clients" / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        value: dict = {"server": "example.com", "public_key": "", "inbound": copy.deepcopy(node)}
        value.update(extra)
        fixture.dump(path, value)
        return path

    def _config(self) -> dict:
        return json.loads(self.shell("config_jq .", check=True).stdout)

    def _meta(self, tag: str) -> dict:
        return json.loads((self.state_dir / "clients" / (tag + ".json")).read_text(encoding="utf-8"))

    def _outbounds(self, *tags: str) -> list[dict]:
        return [{"type": "direct", "tag": tag} for tag in tags]

    def _bind(
        self,
        tag: str,
        index: int | str,
        outbound: str,
        expected: Path | None = None,
        *,
        confirm: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        body = "confirm() { return %d; }; bind_user_outbound_locked \"$@\"" % (0 if confirm else 1)
        args = [tag, str(index), outbound]
        if expected is not None:
            args.append(str(expected))
        return self.shell(body, *args)

    def _bundle(self) -> Path:
        result = self.shell("create_backup", check=True)
        path = Path(result.stdout.strip())
        self.assertTrue(path.is_file())
        return path

    def _bundle_validate(self, bundle: Path, tag: str) -> dict:
        result = self.shell("user_outbound_bundle validate \"$1\" \"$2\"", str(bundle), tag, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(set(value), {"rules", "names"})
        return value

    def _credential(self, user: dict) -> dict:
        return {key: value for key, value in user.items() if key != "name"}

    def test_bind_two_users_for_all_supported_protocols_preserves_credentials_and_metadata(self) -> None:
        protocols = ("vless", "vmess", "hysteria2", "tuic", "anytls")
        nodes = [
            self._node(protocol, "node-" + protocol, 25000 + index, ("alice", _MISSING))
            for index, protocol in enumerate(protocols)
        ]
        originals = {node["tag"]: copy.deepcopy(node["users"]) for node in nodes}
        metadata = {node["tag"]: self._metadata(node, note="keep-me") for node in nodes}
        self._write_config(nodes, rules=[{"domain_suffix": ["external.example"], "outbound": "direct"}], outbounds=self._outbounds("proxy-a", "proxy-b"), comments=True)

        for node in nodes:
            with self.subTest(protocol=node["type"], tag=node["tag"]):
                first = self._bind(node["tag"], 1, "proxy-a")
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                second = self._bind(node["tag"], 2, "proxy-b")
                self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

        saved = self._config()
        user_rules = [rule for rule in saved["route"]["rules"] if "auth_user" in rule]
        self.assertEqual(len(user_rules), len(nodes) * 2)
        for node in nodes:
            tag = node["tag"]
            current = next(item for item in saved["inbounds"] if item["tag"] == tag)
            self.assertEqual(self._credential(current["users"][0]), self._credential(originals[tag][0]))
            self.assertEqual(self._credential(current["users"][1]), self._credential(originals[tag][1]))
            self.assertEqual(current["users"][0]["name"], "alice")
            self.assertTrue(current["users"][1]["name"].startswith("user-route-"))
            meta = self._meta(tag)
            self.assertEqual(meta["note"], "keep-me")
            self.assertEqual(meta["inbound"], current)
            record = meta["user_outbound_routes"]
            self.assertEqual(record["format"], "sing-box-manager-user-outbound-route-v1")
            self.assertEqual([binding["name"] for binding in record["bindings"]], ["alice", current["users"][1]["name"]])
        self.assertIn("// keep user outbound comment", self.config.read_text(encoding="utf-8"))
        self.assertFalse(any(any(action in call for action in ("start", "restart", "stop")) for call in self.calls()))

    def test_unbind_restores_missing_null_empty_names_and_preserves_external_rules(self) -> None:
        node = self._node("vless", "names-node", 25101, (_MISSING, None, "", "keep"))
        external = [
            {"inbound": [node["tag"]], "auth_user": ["external-user"], "action": "route", "outbound": "external"},
            {"inbound": ["other-node"], "action": "route", "outbound": "external"},
        ]
        self._write_config([node], rules=external, outbounds=self._outbounds("proxy-a", "external"), comments=True)
        metadata = self._metadata(node, note="preserve")
        for index in (1, 2, 3):
            self.assertEqual(self._bind(node["tag"], index, "proxy-a").returncode, 0)
        bound = self._config()["inbounds"][0]
        generated = [bound["users"][i]["name"] for i in range(3)]
        self.assertEqual(len(set(generated)), 3)
        self.assertEqual(self._bind(node["tag"], 2, "-").returncode, 0)
        self.assertEqual(self._bind(node["tag"], 1, "-").returncode, 0)
        self.assertEqual(self._bind(node["tag"], 3, "-").returncode, 0)
        saved = self._config()["inbounds"][0]
        self.assertNotIn("name", saved["users"][0])
        self.assertIsNone(saved["users"][1]["name"])
        self.assertEqual(saved["users"][2]["name"], "")
        self.assertEqual(saved["users"][3]["name"], "keep")
        self.assertEqual(self._config()["route"]["rules"], external)
        after_meta = self._meta(node["tag"])
        self.assertEqual(after_meta["note"], "preserve")
        self.assertNotIn("user_outbound_routes", after_meta)
        self.assertEqual(after_meta["inbound"], saved)
        self.assertTrue(metadata.is_file())

    def test_missing_metadata_unbind_is_noop_then_created_record_is_removed(self) -> None:
        node = self._node("vless", "created-meta-node", 25151)
        self._write_config([node], outbounds=self._outbounds("proxy-a"))
        self.assertEqual(self._bind(node["tag"], 1, "-").returncode, 0)
        self.assertFalse((self.state_dir / "clients" / (node["tag"] + ".json")).exists())
        self.assertEqual(self._bind(node["tag"], 1, "proxy-a").returncode, 0)
        metadata = self.state_dir / "clients" / (node["tag"] + ".json")
        self.assertTrue(metadata.is_file())
        self.assertTrue(self._meta(node["tag"])["user_outbound_routes"]["created_metadata"])
        self.assertEqual(self._bind(node["tag"], 1, "-").returncode, 0)
        self.assertFalse(metadata.exists())

    def test_existing_metadata_without_inbound_keeps_its_shape_after_bind_unbind(self) -> None:
        node = self._node("vless", "metadata-shape-node", 25161)
        self._write_config([node], outbounds=self._outbounds("proxy-a"))
        metadata = self.state_dir / "clients" / (node["tag"] + ".json")
        metadata.parent.mkdir(parents=True, exist_ok=True)
        original = {"server": "example.com", "public_key": "", "note": "no-inbound"}
        fixture.dump(metadata, original)
        self.assertEqual(self._bind(node["tag"], 1, "proxy-a").returncode, 0)
        self.assertNotIn("inbound", self._meta(node["tag"]))
        self.assertEqual(self._bind(node["tag"], 1, "-").returncode, 0)
        self.assertEqual(json.loads(metadata.read_text(encoding="utf-8")), original)

    def test_collection_snapshot_rejects_external_user_name_swap_for_missing_metadata_shapes(self) -> None:
        node = self._node("vless", "collection-cas-node", 25166, ("alice", "bob"))
        original_credentials = [self._credential(user) for user in node["users"]]
        metadata_path = self.state_dir / "clients" / (node["tag"] + ".json")
        for original_metadata in (None, {"server": "example.com", "public_key": ""}):
            with self.subTest(metadata=original_metadata is not None):
                self._write_config([node], outbounds=self._outbounds("proxy-a"))
                if self.state_dir.joinpath("clients").exists():
                    shutil.rmtree(self.state_dir / "clients")
                if original_metadata is not None:
                    metadata_path.parent.mkdir(parents=True, exist_ok=True)
                    fixture.dump(metadata_path, original_metadata)
                self.assertEqual(self._bind(node["tag"], 1, "proxy-a").returncode, 0)
                bound_meta = self._meta(node["tag"])
                self.assertEqual(bound_meta["user_outbound_routes"]["inbound"], self._config()["inbounds"][0])
                before_metadata = metadata_path.read_bytes()
                changed = self._config()
                changed["inbounds"][0]["users"][0]["name"], changed["inbounds"][0]["users"][1]["name"] = (
                    changed["inbounds"][0]["users"][1]["name"], changed["inbounds"][0]["users"][0]["name"],
                )
                self.assertEqual(
                    [self._credential(user) for user in changed["inbounds"][0]["users"]],
                    original_credentials,
                )
                fixture.dump(self.config, changed)
                before_config = self.config.read_bytes()
                changed_bundle = self._bundle()
                validate = self.shell("user_outbound_bundle validate \"$1\" \"$2\"", str(changed_bundle), node["tag"])
                self.assertNotEqual(validate.returncode, 0)
                self.assertNotEqual(self._bind(node["tag"], 1, "proxy-a").returncode, 0)
                self.assertNotEqual(self._bind(node["tag"], 1, "-").returncode, 0)
                self.assertEqual(self.config.read_bytes(), before_config)
                self.assertEqual(metadata_path.read_bytes(), before_metadata)

    def test_bulk_first_and_user_first_remove_only_created_metadata_after_cross_unbind(self) -> None:
        node = self._node("vless", "created-order-node", 25171)
        outbounds = self._outbounds("proxy-user", "proxy-bulk")

        def run_order(*actions: str) -> None:
            self._write_config([node], outbounds=outbounds)
            clients = self.state_dir / "clients"
            if clients.exists():
                shutil.rmtree(clients)
            for action in actions:
                if action == "bulk-bind":
                    self.assertEqual(self.shell('confirm() { return 0; }; bind_outbound_locked "$1" proxy-bulk', node["tag"]).returncode, 0)
                elif action == "user-bind":
                    self.assertEqual(self._bind(node["tag"], 1, "proxy-user").returncode, 0)
                elif action == "bulk-unbind":
                    self.assertEqual(self.shell('confirm() { return 0; }; bind_outbound_locked "$1" -', node["tag"]).returncode, 0)
                elif action == "user-unbind":
                    self.assertEqual(self._bind(node["tag"], 1, "-").returncode, 0)
                else:
                    raise AssertionError(action)
            self.assertFalse((clients / (node["tag"] + ".json")).exists())

        run_order("bulk-bind", "user-bind", "user-unbind", "bulk-unbind")
        run_order("user-bind", "bulk-bind", "bulk-unbind", "user-unbind")

    def test_user_outbound_bundle_validate_and_expected_bundle_bind(self) -> None:
        node = self._node("vless", "bundle-node", 25201)
        original_rules = [{"domain_suffix": ["bundle.example"], "outbound": "direct"}]
        self._write_config([node], rules=original_rules, outbounds=self._outbounds("proxy-a"))
        self._metadata(node)
        bundle = self._bundle()
        report = self._bundle_validate(bundle, node["tag"])
        self.assertEqual(report["rules"], [])
        self.assertEqual(report["names"], [])
        bound = self._bind(node["tag"], 1, "proxy-a", bundle)
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        after_snapshot = self._bundle()
        report = self._bundle_validate(after_snapshot, node["tag"])
        self.assertEqual(report["rules"], [{"inbound": [node["tag"]], "auth_user": ["alice"], "action": "route", "outbound": "proxy-a"}])
        self.assertEqual(report["names"], ["alice"])
        after = json.loads(self.shell("user_outbound_bundle bind \"$1\" \"$2\" 2 proxy-a", str(after_snapshot), node["tag"], check=True).stdout)
        self.assertIn({"inbound": [node["tag"]], "auth_user": ["alice"], "action": "route", "outbound": "proxy-a"}, after["config"]["route"]["rules"])
        self.assertIn({"inbound": [node["tag"]], "auth_user": ["bob"], "action": "route", "outbound": "proxy-a"}, after["config"]["route"]["rules"])

    def test_external_metadata_mutation_duplicate_rules_and_cas_are_rejected(self) -> None:
        node = self._node("vless", "integrity-node", 25301)
        self._write_config([node], rules=[{"domain_suffix": ["safe.example"], "outbound": "direct"}], outbounds=self._outbounds("proxy-a", "proxy-b"))
        metadata = self._metadata(node)
        self.assertEqual(self._bind(node["tag"], 1, "proxy-a").returncode, 0)
        before_config = self.config.read_bytes()
        current_meta = self._meta(node["tag"])
        good_meta = copy.deepcopy(current_meta)
        current_meta["user_outbound_routes"]["bindings"][0]["rule"]["outbound"] = "proxy-b"
        fixture.dump(metadata, current_meta)
        result = self._bind(node["tag"], 1, "-" )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)

        fixture.dump(metadata, good_meta)
        duplicate = self._config()
        duplicate["route"]["rules"].insert(0, copy.deepcopy(good_meta["user_outbound_routes"]["bindings"][0]["rule"]))
        fixture.dump(self.config, duplicate)
        duplicate_bytes = self.config.read_bytes()
        result = self._bind(node["tag"], 1, "-")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), duplicate_bytes)

        self._write_config([node], rules=[{"domain_suffix": ["safe.example"], "outbound": "direct"}], outbounds=self._outbounds("proxy-a", "proxy-b"))
        self._metadata(node)
        bundle = self._bundle()
        changed = self._config()
        changed["inbounds"][0]["listen_port"] += 1
        fixture.dump(self.config, changed)
        before = self.config.read_bytes()
        result = self._bind(node["tag"], 1, "proxy-a", bundle)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_generated_name_referenced_by_external_rule_cannot_be_unbound(self) -> None:
        node = self._node("vless", "generated-reference-node", 25311, (_MISSING, "bob"))
        self._write_config([node], outbounds=self._outbounds("proxy-a", "external"))
        self._metadata(node)
        self.assertEqual(self._bind(node["tag"], 1, "proxy-a").returncode, 0)
        generated = self._config()["inbounds"][0]["users"][0]["name"]
        changed = self._config()
        changed["route"]["rules"].append({
            "inbound": [node["tag"]], "auth_user": [generated], "action": "route", "outbound": "external",
        })
        fixture.dump(self.config, changed)
        before = self.config.read_bytes()
        result = self._bind(node["tag"], 1, "-")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self._config()["inbounds"][0]["users"][0]["name"], generated)

    def test_tampered_named_record_cannot_be_treated_as_auto_name(self) -> None:
        node = self._node("vless", "named-record-node", 25321, ("alice", "bob"))
        self._write_config([node], outbounds=self._outbounds("proxy-a"))
        metadata = self._metadata(node)
        self.assertEqual(self._bind(node["tag"], 1, "proxy-a").returncode, 0)
        tampered = self._meta(node["tag"])
        tampered["user_outbound_routes"]["bindings"][0]["name_before_present"] = False
        tampered["user_outbound_routes"]["bindings"][0]["name_before"] = None
        fixture.dump(metadata, tampered)
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        result = self._bind(node["tag"], 1, "-")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)

    def test_bulk_rule_moved_ahead_rejects_same_target_rebind_without_restart(self) -> None:
        node = self._node("vless", "priority-race-node", 25331)
        self._write_config([node], outbounds=self._outbounds("proxy-user", "proxy-bulk"))
        self._metadata(node)
        self.assertEqual(self._bind(node["tag"], 1, "proxy-user").returncode, 0)
        self.assertEqual(self.shell('confirm() { return 0; }; bind_outbound_locked "$1" proxy-bulk', node["tag"]).returncode, 0)
        changed = self._config()
        user_rule = next(rule for rule in changed["route"]["rules"] if "auth_user" in rule)
        bulk_rule = next(rule for rule in changed["route"]["rules"] if rule.get("outbound") == "proxy-bulk")
        changed["route"]["rules"] = [bulk_rule, user_rule]
        fixture.dump(self.config, changed)
        before = self.config.read_bytes()
        call_count = len(self.calls())
        result = self._bind(node["tag"], 1, "proxy-user")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(any("restart" in call for call in self.calls()[call_count:]))

    def test_bulk_inbound_binding_keeps_user_rules_ahead_of_bulk_rule(self) -> None:
        node = self._node("vless", "bulk-node", 25401)
        self._write_config([node], rules=[{"domain_suffix": ["external.example"], "outbound": "direct"}], outbounds=self._outbounds("proxy-user", "proxy-bulk"))
        self._metadata(node)
        self.assertEqual(self._bind(node["tag"], 1, "proxy-user").returncode, 0)
        self.assertEqual(self.shell('confirm() { return 0; }; bind_outbound_locked "$1" proxy-bulk', node["tag"]).returncode, 0)
        rules = self._config()["route"]["rules"]
        self.assertEqual(rules[0]["auth_user"], ["alice"])
        self.assertEqual(rules[0]["outbound"], "proxy-user")
        self.assertEqual(rules[1], {"inbound": [node["tag"]], "action": "route", "outbound": "proxy-bulk"})
        self.assertEqual(self.shell('confirm() { return 0; }; bind_outbound_locked "$1" -', node["tag"]).returncode, 0)
        self.assertEqual(self._config()["route"]["rules"][0]["auth_user"], ["alice"])

        self._write_config([node], rules=[{"domain_suffix": ["external.example"], "outbound": "direct"}], outbounds=self._outbounds("proxy-user", "proxy-bulk"))
        self._metadata(node)
        self.assertEqual(self.shell('confirm() { return 0; }; bind_outbound_locked "$1" proxy-bulk', node["tag"]).returncode, 0)
        self.assertEqual(self._bind(node["tag"], 1, "proxy-user").returncode, 0)
        rules = self._config()["route"]["rules"]
        self.assertEqual(rules[0]["auth_user"], ["alice"])
        self.assertEqual(rules[1], {"inbound": [node["tag"]], "action": "route", "outbound": "proxy-bulk"})

    def test_delete_bound_user_is_rejected_but_unbound_user_delete_preserves_binding(self) -> None:
        node = self._node("vless", "delete-user-node", 25501)
        self._write_config([node], outbounds=self._outbounds("proxy-user"))
        metadata = self._metadata(node)
        self.assertEqual(self._bind(node["tag"], 1, "proxy-user").returncode, 0)
        original = self._config()["inbounds"][0]
        remove_bound = copy.deepcopy(original)
        del remove_bound["users"][0]
        candidate = self.root / "remove-bound.json"
        fixture.dump(candidate, remove_bound)
        # Use the actual original snapshot as the CAS input for the guarded delete.
        expected = self.root / "delete-expected.json"
        fixture.dump(expected, original)
        before = self.config.read_bytes()
        result = self.shell('replace_inbound_file "$1" "$2" example.com "" "$3"', node["tag"], str(candidate), str(expected))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)

        remove_unbound = copy.deepcopy(original)
        del remove_unbound["users"][1]
        candidate = self.root / "remove-unbound.json"
        fixture.dump(candidate, remove_unbound)
        self.assertEqual(self.shell('replace_inbound_file "$1" "$2" example.com "" "$3"', node["tag"], str(candidate), str(expected), check=True).returncode, 0)
        saved = self._config()["inbounds"][0]
        self.assertEqual(len(saved["users"]), 1)
        self.assertEqual(self._config()["route"]["rules"][0]["auth_user"], ["alice"])
        self.assertEqual(self._meta(node["tag"])["inbound"], saved)
        self.assertTrue(metadata.is_file())

    def test_editing_user_credentials_preserves_binding(self) -> None:
        node = self._node("vless", "credential-edit-node", 25601)
        self._write_config([node], comments=True, outbounds=self._outbounds("proxy-user"))
        self._metadata(node, note="keep")
        self.assertEqual(self._bind(node["tag"], 1, "proxy-user").returncode, 0)
        original = self._config()["inbounds"][0]
        edited = copy.deepcopy(original)
        edited["users"][0]["uuid"] = "4d8f9f0e-29f1-4d31-9c1d-fcfb74000001"
        candidate = self.root / "credential-edited.json"
        fixture.dump(candidate, edited)
        expected = self.root / "credential-original.json"
        fixture.dump(expected, original)
        self.assertEqual(self.shell('replace_inbound_file "$1" "$2" example.com "" "$3"', node["tag"], str(candidate), str(expected), check=True).returncode, 0)
        saved = self._config()["inbounds"][0]
        self.assertEqual(saved["users"][0]["uuid"], edited["users"][0]["uuid"])
        self.assertEqual(self._config()["route"]["rules"][0]["auth_user"], ["alice"])
        self.assertEqual(self._meta(node["tag"])["note"], "keep")
        self.assertEqual(self._meta(node["tag"])["inbound"], saved)
        self.assertIn("// keep user outbound comment", self.config.read_text(encoding="utf-8"))

    def test_same_target_noop_cancel_invalid_index_and_unsupported_protocol_do_not_write(self) -> None:
        node = self._node("vless", "noop-user-node", 25701)
        self._write_config([node], outbounds=self._outbounds("proxy-a", "proxy-b"))
        metadata = self._metadata(node)
        self.assertEqual(self._bind(node["tag"], 1, "proxy-a").returncode, 0)
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        backups = list((self.state_dir / "backups").glob("bundle-*.json"))
        result = self._bind(node["tag"], 1, "proxy-a", confirm=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)
        self.assertEqual(list((self.state_dir / "backups").glob("bundle-*.json")), backups)
        for index in ("0", "3", "$(touch bad-index)"):
            result = self._bind(node["tag"], index, "proxy-b")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.config.read_bytes(), before_config)
        unsupported = self._node("vless", "unsupported-user-node", 25702)
        unsupported["type"] = "shadowsocks"
        unsupported["users"] = [{"password": "one"}, {"password": "two"}]
        self._write_config([unsupported], outbounds=self._outbounds("proxy-a"))
        self._metadata(unsupported)
        self.assertNotEqual(self._bind(unsupported["tag"], 1, "proxy-a").returncode, 0)

    def test_active_restart_failure_rolls_back_user_binding_and_metadata(self) -> None:
        node = self._node("vless", "rollback-user-node", 25801)
        self._write_config([node], outbounds=self._outbounds("proxy-a"))
        metadata = self._metadata(node)
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        self.state(active=True, restart_failures=1)
        result = self._bind(node["tag"], 1, "proxy-a")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)
        self.assertTrue(self.state()["active"])
        self.assertFalse(any("start" in call for call in self.calls()))

    def test_wizard_selects_node_user_outbound_and_unbind_skips_outbound(self) -> None:
        node = self._node("vless", "wizard-node", 25851)
        self._write_config([node], outbounds=self._outbounds("proxy-user"))
        self._metadata(node)
        for action, expected_target in (("bind", "proxy-user"), ("unbind", "-")):
            with self.subTest(action=action):
                log = self.root / (action + "-wizard-calls")
                snapshot = self.root / (action + "-wizard-snapshot")
                body = r'''
TEST_LOG="$1"; TEST_SNAPSHOT="$2"; TEST_ACTION="$3"
select_inbound() { printf 'node\n' >>"$TEST_LOG"; printf -v "$1" '%s' wizard-node; }
select_inbound_user() { printf 'user\n' >>"$TEST_LOG"; printf -v "$1" '%s' 2; }
select_outbound() { printf 'outbound\n' >>"$TEST_LOG"; printf -v "$1" '%s' proxy-user; }
bind_user_outbound_locked() { printf '%s:%s:%s\n' "$1" "$2" "$3" >>"$TEST_LOG"; cp -- "$4" "$TEST_SNAPSHOT"; }
user_outbound_wizard_locked "$TEST_ACTION"
'''
                result = self.shell(body, str(log), str(snapshot), action)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(log.read_text().splitlines(), ["node", "user"] +
                                 (["outbound"] if action == "bind" else []) +
                                 ["wizard-node:2:" + expected_target])
                self.assertEqual(json.loads(snapshot.read_text())["config"], self._config())

    def test_wizard_cancel_at_each_selection_never_binds(self) -> None:
        node = self._node("vless", "cancel-wizard", 25861)
        self._write_config([node], outbounds=self._outbounds("proxy-user"))
        metadata = self._metadata(node)
        before = (self.config.read_bytes(), metadata.read_bytes())
        for stage in ("node", "user", "outbound"):
            with self.subTest(stage=stage):
                marker = self.root / (stage + "-must-not-bind")
                body = r'''
TEST_CANCEL="$1"; TEST_MARKER="$2"
select_inbound() { [[ $TEST_CANCEL != node ]] || { printf -v "$1" ''; return; }; printf -v "$1" '%s' cancel-wizard; }
select_inbound_user() { [[ $TEST_CANCEL != user ]] || { printf -v "$1" ''; return; }; printf -v "$1" '%s' 1; }
select_outbound() { [[ $TEST_CANCEL != outbound ]] || { printf -v "$1" ''; return; }; printf -v "$1" '%s' proxy-user; }
bind_user_outbound_locked() { printf 'unexpected\n' >"$TEST_MARKER"; }
user_outbound_wizard_locked bind
'''
                result = self.shell(body, stage, str(marker))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(marker.exists())
                self.assertEqual((self.config.read_bytes(), metadata.read_bytes()), before)

    def test_user_wizard_and_cli_route_preserve_literal_index_and_outbound(self) -> None:
        marker = self.root / "user-route-calls"
        body = r'''
SB_ROOT_PREFIX=
require_root() { return 0; }
require_systemd() { return 0; }
safe_paths() { return 0; }
is_managed() { return 0; }
with_lock() { "$@"; }
bind_user_outbound_locked() { printf '%s:%s:%s\n' "$1" "$2" "$3" >"$TEST_ROUTE_MARKER"; }
main outbound-user-bind literal-tag 2 -
'''
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; TEST_ROUTE_MARKER="$2"; ' + body, "user-route", str(fixture.SCRIPT), str(marker)],
            env=self.env,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "literal-tag:2:-\n")


_MISSING = object()


if __name__ == "__main__":
    unittest.main()
