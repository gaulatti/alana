"""Container-only source -> unmodified Alana -> actual receiver framebuffer."""
import argparse
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time

from timecode import Samples, beacon

ROOT = Path(__file__).parent
WIDTH = 640
HEIGHT = 360
FPS = 30


def token(key, secret, room):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')
    now = int(time.time())
    data = encode({'alg': 'HS256', 'typ': 'JWT'}) + b'.' + encode({
        'iss': key, 'sub': 'latency-receiver', 'nbf': now - 5, 'exp': now + 600,
        'video': {'roomJoin': True, 'room': room, 'canSubscribe': True, 'canPublish': False},
    })
    return (data + b'.' + base64.urlsafe_b64encode(hmac.new(secret.encode(), data, hashlib.sha256).digest()).rstrip(b'=')).decode()


def cpu_ticks(session):
    total = 0
    for stat in Path('/proc').glob('[0-9]*/stat'):
        try:
            # The command name in parentheses can contain spaces.
            fields = stat.read_text().rsplit(')', 1)[1].split()
            if int(fields[3]) == session:
                total += sum(int(fields[i]) for i in (11, 12, 13, 14))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return total


def capture(process, width, names, samples):
    size = width * 24
    try:
        while True:
            frame = bytearray()
            while len(frame) < size:
                part = process.stdout.read(size - len(frame))
                if not part:
                    return
                frame.extend(part)
            received = time.time_ns() / 1_000_000
            for index, name in enumerate(names):
                stamp = beacon(frame, width, index * WIDTH)
                if stamp is not None:
                    samples.put((name, stamp, received))
    finally:
        process.stdout.close()


def run(outputs, seconds, warmup):
    processes = []
    sessions = []
    diagnostics = []
    key = os.environ['LIVEKIT_API_KEY']
    secret = os.environ['LIVEKIT_API_SECRET']
    room = f'harbor-light-{outputs}'
    config = {'url': os.environ['LIVEKIT_URL'], 'token': token(key, secret, room)}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            routes = {'/source': ROOT / 'source.html', '/receiver': ROOT / 'receiver.html',
                      '/sdk.js': Path('/sdk/livekit-client.umd.js')}
            if self.path == '/config':
                body = json.dumps(config).encode()
                content_type = 'application/json'
            elif self.path in routes:
                body = routes[self.path].read_bytes()
                content_type = 'text/javascript' if self.path == '/sdk.js' else 'text/html; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 18000), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def launch(command, **kwargs):
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   start_new_session=True, **kwargs)
        def drain():
            for line in process.stderr:
                # Print only a setting NAME from our validator, never a value,
                # URL, token, raw Chromium/FFmpeg/LiveKit or daemon diagnostic.
                match = re.match(rb'\[config\] ([A-Z_]+)', line)
                if match:
                    diagnostics.append('config-' + match.group(1).decode())
                elif b'command not found' in line:
                    diagnostics.append('missing-runtime-command')
            process.stderr.close()
        threading.Thread(target=drain, daemon=True).start()
        processes.append(process)
        sessions.append(process.pid)
        return process

    def wait_display(display):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if subprocess.run(['xdpyinfo', '-display', display], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0:
                return
            time.sleep(.1)
        raise RuntimeError('Receiver display did not become ready')

    try:
        launch(['Xvfb', ':99', '-screen', '0', '1920x720x24', '-nolisten', 'tcp'])
        launch(['Xvfb', ':100', '-screen', '0', '640x360x24', '-nolisten', 'tcp'])
        wait_display(':99')
        wait_display(':100')
        urls = []
        for index in range(outputs):
            url = f'rtmp://127.0.0.1:{19350 + index}/live/latency'
            urls.append(url)
            launch(['ffplay', '-hide_banner', '-loglevel', 'quiet', '-listen', '1',
                    '-an', '-noborder', '-x', '640', '-y', '360', url],
                   env={**os.environ, 'DISPLAY': ':99',
                        'SDL_VIDEO_WINDOW_POS': f'{index % 3 * WIDTH},{index // 3 * HEIGHT}'})
        launch(['chromium', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                '--no-first-run', '--disable-background-networking', '--disable-renderer-backgrounding',
                '--autoplay-policy=no-user-gesture-required', '--kiosk', '--window-size=640,360',
                '--user-data-dir=/tmp/latency-receiver', 'http://127.0.0.1:18000/receiver'],
               env={**os.environ, 'DISPLAY': ':100'})
        source = launch(['/usr/local/bin/startup.sh'], env={
            **os.environ, 'PROGRAM_ID': 'harbor-light', 'CHANNEL_BROWSER_URL': 'http://127.0.0.1:18000/source',
            'RESOLUTION': '640x360', 'WINDOW_SIZE': '640x360', 'FPS': str(FPS),
            'VIDEO_ENCODER': 'libx264', 'DISABLE_CHROME_GPU': '1', 'CHROME_DISABLE_DEV_SHM_USAGE': '1',
            'RTMP_OUTPUTS': ','.join(urls), 'LIVEKIT_ENABLED': '1', 'LIVEKIT_ROOM': room,
            'LIVEKIT_IDENTITY': 'harbor-light-source', 'CROCCANTE_CONTROL_URL': 'http://127.0.0.1:18000',
            'PIPELINE_READY_TIMEOUT': '90', 'CONTROL_RETRY_SECONDS': '5',
            'ALANA_CONTROL_TOKEN_FILE': '/private/control-token',
            'CROCCANTE_CONTROL_TOKEN_FILE': '/private/control-token',
        })
        samples = queue.Queue()
        for row in range((outputs + 2) // 3):
            names = [f'rtmp-{i + 1}' for i in range(row * 3, min(outputs, row * 3 + 3))]
            width = len(names) * WIDTH
            process = subprocess.Popen(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'quiet',
                '-f', 'x11grab', '-draw_mouse', '0', '-video_size', f'{width}x24', '-framerate', str(FPS),
                '-i', f':99.0+0,{row * HEIGHT}', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            processes.append(process)
            threading.Thread(target=capture, args=(process, width, names, samples), daemon=True).start()
        process = subprocess.Popen(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'quiet',
            '-f', 'x11grab', '-draw_mouse', '0', '-video_size', '640x24', '-framerate', str(FPS),
            '-i', ':100.0', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        processes.append(process)
        threading.Thread(target=capture, args=(process, WIDTH, ['livekit'], samples), daemon=True).start()
        names = [f'rtmp-{i + 1}' for i in range(outputs)] + ['livekit']
        seen = {}
        deadline = time.monotonic() + 60
        # Read actual beacons from every rendered receiver before warming up.
        while len(seen) != len(names):
            if time.monotonic() > deadline or source.poll() is not None:
                raise RuntimeError('Missing receivers ' + ','.join(name for name in names if name not in seen) + ' publisher_exit=' + str(source.poll()) + ' child_exits=' + ','.join(str(p.poll()) for p in processes) + ' diagnostics=' + ','.join(diagnostics))
            try:
                name, stamp, received = samples.get(timeout=.5)
                if 0 <= received - stamp < 60_000:
                    seen[name] = stamp
            except queue.Empty:
                pass
        beginning = time.monotonic() + warmup
        finish = beginning + seconds
        collected = Samples(names, seen)
        before = None
        while time.monotonic() < finish:
            if source.poll() is not None:
                raise RuntimeError('Publisher ended during measurement')
            if before is None and time.monotonic() >= beginning:
                before = (time.monotonic(), cpu_ticks(source.pid))
            try:
                name, stamp, received = samples.get(timeout=.5)
            except queue.Empty:
                continue
            collected.add(name, stamp, received, measure=time.monotonic() >= beginning)
        elapsed = time.monotonic() - before[0]
        cpu = (cpu_ticks(source.pid) - before[1]) / os.sysconf('SC_CLK_TCK') / elapsed * 100
        return collected.rows(outputs, seconds * FPS / 4, cpu)
    finally:
        server.shutdown()
        for sid in reversed(sessions):
            try:
                os.killpg(sid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--outputs', type=int, choices=(1, 3, 5), required=True)
    parser.add_argument('--seconds', type=int, required=True)
    parser.add_argument('--warmup', type=int, required=True)
    args = parser.parse_args()
    try:
        result = run(args.outputs, args.seconds, args.warmup)
        Path('/result/rows.json').write_text(json.dumps(result))
    except Exception as error:
        # Never repeat runtime URLs, JWTs or server credentials in diagnostics.
        detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(f'Latency case failed: {detail}', flush=True)
        raise SystemExit(1)
