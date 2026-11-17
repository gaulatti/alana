#!/usr/bin/env python3
"""Run the isolated software display-to-display benchmark; never production."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import tempfile
import uuid

from report import SCHEMA, table, validate

ROOT = Path(__file__).resolve().parents[2]
SERVER = 'livekit/livekit-server:v1.13.4'
SDK_VERSION = '2.21.0'


def command(*args, **kwargs):
    try:
        return subprocess.run(list(args), check=True, capture_output=True, text=True, **kwargs).stdout.strip()
    except subprocess.CalledProcessError as error:
        # The worker emits only our bounded fixture failure diagnostics. Never
        # echo Docker/server stderr or arbitrary subprocess output.
        for line in (error.stdout or '').splitlines():
            if re.fullmatch(r'Latency case failed: [a-zA-Z0-9 ,_=.-]+', line):
                print(line, flush=True)
        raise


def private_file(path, value):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Image built from this repository Dockerfile')
    parser.add_argument('--sdk', required=True, type=Path, help='Existing livekit-client 2.21.0 dist/livekit-client.umd.js')
    parser.add_argument('--seconds', type=int, default=30)
    parser.add_argument('--warmup', type=int, default=10)
    parser.add_argument('--outputs', type=int, nargs='+', choices=(1, 3, 5), default=[1, 3, 5])
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.seconds < 4 or args.warmup < 0:
        parser.error('Use at least four measurement seconds and a nonnegative warmup')
    sdk = args.sdk.resolve(strict=True)
    package = json.loads((sdk.parent.parent / 'package.json').read_text())
    if sdk.name != 'livekit-client.umd.js' or package['name'] != 'livekit-client' or package['version'] != SDK_VERSION:
        parser.error('Supply the existing, pinned livekit-client 2.21.0 UMD artifact')
    if platform.system() == 'Darwin' and command('docker', 'context', 'show') != 'colima':
        parser.error('Use the shared default Colima Docker context; no extra VM is created')
    image = json.loads(command('docker', 'image', 'inspect', args.image))[0]
    server_image = json.loads(command('docker', 'image', 'inspect', SERVER))[0]
    # Verify the actual publisher boundary has not drifted from this checkout.
    runtime_files = ['startup.sh', 'validate-config.sh', 'healthcheck.sh', 'control-server.py',
                     'alana_metrics.py', 'metrics-event.py', 'recording.py']
    expected = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in runtime_files}
    actual = command('docker', 'run', '--rm', '--entrypoint', 'sha256sum', args.image,
                     *['/usr/local/bin/' + name for name in runtime_files])
    if {Path(line.split()[1]).name: line.split()[0] for line in actual.splitlines()} != expected:
        parser.error('Image publisher source differs from this checkout')
    versions = command('docker', 'run', '--rm', '--entrypoint', '/bin/bash', args.image, '-lc',
                       'ffmpeg -version | head -1; chromium --version; lk --version; python3 --version')
    docker_info = json.loads(command('docker', 'info', '--format', '{{json .}}'))
    metadata = {
        'kind': 'software-display',
        'measurement': 'software-rendered source pixels to decoded receiver framebuffer pixels',
        'physical_display_latency_measured': False,
        'source_commit': command('git', '-C', str(ROOT), 'rev-parse', 'HEAD'),
        'dirty_checkout': bool(command('git', '-C', str(ROOT), 'status', '--porcelain')),
        'host': platform.platform(),
        'host_machine': platform.machine(),
        'docker_architecture': docker_info['Architecture'],
        'docker_cpus': docker_info['NCPU'], 'docker_memory_bytes': docker_info['MemTotal'],
        'image_id': image['Id'], 'server_image_id': server_image['Id'], 'server': SERVER,
        'sdk_version': SDK_VERSION, 'sdk_sha256': hashlib.sha256(sdk.read_bytes()).hexdigest(),
        'software': versions.splitlines(),
        'profile': {'width': 640, 'height': 360, 'fps': 30, 'encoder': 'libx264',
                    'publisher_arguments': 'unchanged startup.sh defaults except documented local resolution/outputs'},
        'measurement_seconds': args.seconds, 'warmup_seconds': args.warmup,
        'cpu_boundary': 'publisher process session including browser, capture and all encoders; 100% = one CPU core',
        'observer': '30fps X11 framebuffer capture; duplicate source timestamps excluded; nearest-rank percentiles',
    }
    if platform.system() == 'Darwin':
        metadata['host_model'] = command('sysctl', '-n', 'hw.model')
        metadata['host_cpu'] = command('sysctl', '-n', 'machdep.cpu.brand_string')
    rows = []
    task = 'alana28-' + uuid.uuid4().hex[:10]
    network = task + '-net'
    server_name = task + '-server'
    worker_name = task + '-worker'
    # Colima shares the checkout under HOME, not arbitrary macOS /var/folders.
    # Git's private directory is untracked and also works for Git worktrees.
    private_root = Path(command('git', '-C', str(ROOT), 'rev-parse', '--absolute-git-dir'))
    with tempfile.TemporaryDirectory(prefix='alana28-', dir=private_root) as temporary:
        private = Path(temporary)
        # Ephemeral local credentials go straight into private temporary stores.
        # They are never printed, committed, persisted in the baseline or reused.
        private_file(private / 'key', secrets.token_urlsafe(18))
        private_file(private / 'secret', secrets.token_urlsafe(32))
        private_file(private / 'control-token', secrets.token_urlsafe(32))
        key = (private / 'key').read_text()
        secret = (private / 'secret').read_text()
        private_file(private / 'livekit.yaml',
                     f'port: 7880\nrtc:\n  tcp_port: 7881\n  udp_port: 7882\n  use_external_ip: false\nkeys:\n  {key}: {secret}\n')
        private_file(private / 'worker.env', f'LIVEKIT_URL=ws://{server_name}:7880\nLIVEKIT_API_KEY={key}\nLIVEKIT_API_SECRET={secret}\n')
        result = private / 'result'
        result.mkdir()
        try:
            command('docker', 'network', 'create', '--internal', network)
            command('docker', 'run', '--detach', '--name', server_name, '--network', network,
                    '--volume', f'{private}:/private:ro', SERVER,
                    '--config', '/private/livekit.yaml', '--bind', '0.0.0.0')
            for outputs in args.outputs:
                command('docker', 'run', '--rm', '--name', worker_name, '--network', network,
                        '--shm-size', '1g', '--env-file', str(private / 'worker.env'),
                        '--volume', f'{ROOT / "test/latency"}:/bench:ro',
                        '--volume', f'{sdk}:/sdk/livekit-client.umd.js:ro',
                        '--volume', f'{private}:/private:ro', '--volume', f'{result}:/result',
                        '--entrypoint', 'python3', args.image, '/bench/worker.py',
                        '--outputs', str(outputs), '--seconds', str(args.seconds), '--warmup', str(args.warmup),
                        timeout=args.seconds + args.warmup + 100)
                case = json.loads((result / 'rows.json').read_text())
                rows.extend(case)
                print(f'Completed {outputs} RTMP outputs plus the concurrent LiveKit leg', flush=True)
        finally:
            for name in (worker_name, server_name):
                subprocess.run(['docker', 'rm', '--force', name], capture_output=True)
            subprocess.run(['docker', 'network', 'rm', network], capture_output=True)
    report = validate({'schema': SCHEMA, 'metadata': metadata, 'rows': rows})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(table(report))


if __name__ == '__main__':
    try:
        main()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # Docker/SDK diagnostics may contain credentials. Do not echo captured
        # command arguments, logs, environment, JWTs or stderr to the terminal.
        raise SystemExit('Local benchmark failed; no baseline was written. All task containers/networks were removed.')
