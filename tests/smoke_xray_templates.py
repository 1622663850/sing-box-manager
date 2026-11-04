#!/usr/bin/env python3
"""Add, connect, and delete every Xray template using one isolated systemd unit."""
from pathlib import Path
import argparse
import base64
import json
import os
import shutil
import sys
import tempfile
import uuid

import smoke_xray as proxy
import smoke_xray_management as management
import smoke_xray_pq as pq

TEMPLATES = (
    ('vless-reality', ''), ('vless-tcp', ''), ('vless-tcp-tls', ''),
    ('vless-ws', '/test-ws'), ('vless-ws-tls', '/test-ws'),
    ('vless-grpc-tls', 'TestGrpc'), ('vless-grpc-reality', 'TestGrpc'),
    ('vless-xhttp-tls', '/test-xhttp'), ('vless-xhttp-reality', '/test-xhttp'),
    ('vmess-tcp', ''), ('vmess-tcp-tls', ''), ('vmess-ws', '/test-ws'),
    ('vmess-ws-tls', '/test-ws'), ('vmess-grpc-tls', 'TestGrpc'),
    ('trojan', ''), ('shadowsocks', ''), ('hysteria', ''),
    ('vless-xhttp-reality-vision', '/test-xhttp'),
)
PAYLOAD = b'XRAY_ALL_TEMPLATES_LIFECYCLE_OK'


def run(core, manager, only=None):
    systemctl = shutil.which('systemctl')
    assert systemctl and os.geteuid() == 0 and Path('/run/systemd/system').is_dir()
    service = 'xray-templates-' + uuid.uuid4().hex + '.service'
    unit = Path('/run/systemd/system') / service
    assert not unit.exists() and not unit.is_symlink()
    created = False
    ports = []
    with tempfile.TemporaryDirectory(prefix='xray-templates-') as temporary:
        work = Path(temporary)
        root = work / 'root'
        state = root / 'var/lib/xray-manager'
        clients = state / 'clients'
        config = root / 'etc/xray/config.json'
        lib = root / 'usr/local/lib/xray-manager'
        for directory in (clients, state / 'backups', config.parent, lib, root / 'etc/systemd/system'):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        (lib / '.managed').write_text('xray-manager-v1\n')
        env = dict(os.environ, XR_ROOT_PREFIX=str(root))

        def call(function, *args, locked=False):
            result = management.xray_manager_call(manager, core, root, service, function,
                *map(str, args), locked=locked, environment=env)
            if result.returncode:
                raise RuntimeError(function + ' failed')
            return result.stdout

        certificate, key = pq._large_chain_material(work)
        reality_private, reality_public = proxy._x25519(core)
        kem = json.loads(call('pq_crypto', 'mlkem768'))
        camouflage_port = proxy.free_port()
        try:
            with proxy.http_target(PAYLOAD) as target_port, pq.large_pq_tls_target(
                core, certificate, key, camouflage_port, work
            ):
                initial = proxy.server_config({}, (target_port,))
                initial['inbounds'] = []
                proxy.write_json(config, initial)
                unit_bytes = management.unit_source(core, state, config)
                with unit.open('xb') as stream:
                    stream.write(unit_bytes)
                    created = True
                unit.chmod(0o644)
                management.systemd(systemctl, 'daemon-reload')
                for index, (template, extra) in enumerate(TEMPLATES, 1):
                    if only and template not in only:
                        continue
                    port = proxy.free_port()
                    while port in ports or port in {target_port, camouflage_port}:
                        port = proxy.free_port()
                    ports.append(port)
                    tag = 'template-' + str(index)
                    credential = str(uuid.uuid4()) if template.startswith(('vless-', 'vmess-')) else base64.b64encode(os.urandom(16)).decode()
                    encrypted = template == 'vless-xhttp-reality-vision'
                    inbound = json.loads(call('generate_inbound', template, tag, port, credential,
                        extra, reality_private, '0123456789abcdef', 'localhost', certificate, key,
                        '', 'fixture-salamander' if template == 'hysteria' else '',
                        pq._decryption_value(kem['private']) if encrypted else 'none', ''))
                    inbound['listen'] = proxy.LOOPBACK
                    if template == 'vless-tcp':
                        inbound['streamSettings']['method'] = 'tcp'
                    if 'reality' in template:
                        inbound['streamSettings']['realitySettings']['target'] = f'{proxy.LOOPBACK}:{camouflage_port}'
                    node_file = work / (tag + '.json')
                    proxy.write_json(node_file, inbound)
                    call('add_managed_inbound', node_file, proxy.LOOPBACK,
                         reality_public if 'reality' in template else '', locked=True)
                    management.active_pid(systemctl, service)
                    saved = json.loads(config.read_text())['inbounds']
                    assert len(saved) == 1 and saved[0] == inbound, 'add did not persist the requested template'
                    metadata = clients / (tag + '.json')
                    assert json.loads(metadata.read_text())['inbound'] == inbound
                    if template == 'hysteria':
                        call('quic_bbr_change_locked', 'enable', tag, locked=True)
                        changed = json.loads(config.read_text())['inbounds'][0]
                        assert changed['streamSettings']['finalmask']['quicParams']['congestion'] == 'bbr'
                    outbound = json.loads(call('show_client', tag, proxy.LOOPBACK, 'json-only', '1'))
                    link = call('show_client', tag, proxy.LOOPBACK, 'link-only', '1').strip()
                    assert link.startswith(('vless://', 'vmess://', 'trojan://', 'ss://', 'hysteria2://'))
                    if outbound.get('streamSettings', {}).get('security') == 'tls':
                        proxy.prepare_tls_outbound(outbound, certificate, template)
                    proxy_port = proxy.free_port()
                    client = work / (tag + '-client.json')
                    proxy.write_json(client, proxy.client_config(outbound, proxy_port))
                    proxy.check_xray(core, client, work)
                    with proxy.running_xray(core, client, work, proxy_port, tag + '-client'):
                        assert proxy.fetch_through_socks_retry(proxy_port, target_port, '/' + tag) == PAYLOAD
                    if template == 'hysteria':
                        call('quic_bbr_change_locked', 'disable', tag, locked=True)
                        assert json.loads(config.read_text())['inbounds'][0] == inbound
                        print('PASS Hy2 Salamander forwarding and explicit QUIC BBR enable/restore', flush=True)
                    call('delete_inbound', tag, locked=True)
                    assert json.loads(config.read_text())['inbounds'] == [], 'node survived deletion'
                    assert not metadata.exists() and not list(clients.iterdir()), 'client metadata survived deletion'
                    listeners = management.run_command(['ss', '-H', '-ln' + ('u' if template == 'hysteria' else 't'),
                        f'sport = :{port}'], 'listener cleanup').stdout
                    assert not listeners.strip(), 'deleted node is still listening'
                    print(f'PASS template {index:02d}/18 {template}: added, exported, forwarded, deleted', flush=True)
        finally:
            if created:
                management.systemd(systemctl, 'stop', service, check=False)
                assert management.systemd(systemctl, 'is-active', '--quiet', service, check=False).returncode != 0
                management.systemd(systemctl, 'reset-failed', service, check=False)
                assert unit.is_file() and not unit.is_symlink() and unit.read_bytes() == unit_bytes
                unit.unlink()
                management.systemd(systemctl, 'daemon-reload')
        assert not unit.exists()
    assert not work.exists()
    print(f'PASS {len(only) if only else 18} templates; temporary unit, processes, configs, keys and backups removed', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core', type=Path, required=True)
    parser.add_argument('--manager', type=Path, default=Path(__file__).resolve().parents[1] / 'xray-manager.sh')
    parser.add_argument('--only', nargs='+', choices=[name for name, _ in TEMPLATES])
    args = parser.parse_args()
    os.umask(0o077)
    try:
        run(args.core.resolve(), args.manager.resolve(), set(args.only) if args.only else None)
    except Exception as error:
        # Generated keys and configuration objects must never appear in failure output.
        import traceback
        locations = ','.join(f'{Path(f.filename).name}:{f.lineno}' for f in traceback.extract_tb(error.__traceback__)[-3:])
        print(f'FAIL templates: {type(error).__name__} at {locations}', file=sys.stderr)
        raise SystemExit(1)
