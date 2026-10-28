"""Loopback-only JSON API and built React assets; no controller or database writes."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlparse

from demo_data import DemoData
from routing_state import read_routing_state,routing_json
from dashboard_evidence import load_package

ROOT = Path(__file__).resolve().parent
DEFAULT_REPLAY = (ROOT/'data/demo-replay.sqlite' if (ROOT/'data/demo-replay.sqlite').exists()
                  else ROOT/'data/research-20261015T180515Z/inference-71.sqlite')


def handler_for(demo, assets, evidence=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            if self.path.startswith('/api/') and args and str(args[1] if len(args) > 1 else '') == '200':
                return
            super().log_message(format, *args)

        def send_json(self, status, value):
            payload = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(payload)

        def allowed_host(self):
            host = urlparse('http://' + self.headers.get('Host', '')).hostname
            return host in ('127.0.0.1', 'localhost')

        def do_GET(self):
            if not self.allowed_host():
                return self.send_json(403, {'error': 'Local demo only'})
            path = urlparse(self.path).path
            if path.startswith('/api/'):
                if path == '/api/benchmark':
                    return self.send_json(200,evidence or dict(status='unavailable',reason='No portable benchmark data is configured.',trials=[],comparison={}))
                if path == '/api/routing':
                    # Actual persisted controller state; never a replay route
                    # preview, and never permission to issue OpenFlow updates.
                    return self.send_json(200, routing_json(dict(read_routing_state(demo.live_db), view_mode=demo.mode)))
                state = demo.snapshot()
                fields = {'/api/state': None, '/api/health': 'health', '/api/topology': 'topology',
                    '/api/telemetry': 'topology', '/api/predictions': 'topology', '/api/routes': 'routes', '/api/events': 'events'}
                if path not in fields:
                    return self.send_json(404, {'error': 'Unknown endpoint'})
                field = fields[path]
                value = state if field is None else state[field]
                return self.send_json(200, value)
            target = (assets / unquote(path).lstrip('/')).resolve()
            if not target.is_relative_to(assets.resolve()):
                return self.send_json(403, {'error': 'Invalid asset path'})
            if not target.is_file():
                target = assets / 'index.html' if '.' not in Path(path).name else target
            if not target.is_file():
                return self.send_json(404, {'error': 'Build the frontend with npm run build in frontend/'})
            payload = target.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(target)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-cache' if target.name == 'index.html' else 'public, max-age=3600')
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            origin = self.headers.get('Origin')
            allowed_origins = {f'http://{name}:{self.server.server_port}' for name in ('localhost', '127.0.0.1')}
            if not self.allowed_host() or (origin and origin not in allowed_origins):
                return self.send_json(403, {'error': 'Controls require the local same-origin dashboard'})
            if self.path != '/api/control':
                return self.send_json(404, {'error': 'Unknown control endpoint'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2048 or 'application/json' not in self.headers.get('Content-Type', ''):
                    raise ValueError('Controls require a small JSON request')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError('Expected a JSON object')
                self.send_json(200, demo.control(body))
            except (ValueError, TypeError, KeyError) as error:
                self.send_json(422, {'error': str(error)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('replay', 'live'), default='replay')
    parser.add_argument('--replay-db', type=Path, default=DEFAULT_REPLAY)
    parser.add_argument('--live-db', type=Path, default=ROOT/'data/demo-live.sqlite')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--evidence-package',type=Path,default=ROOT/'data/demo-dashboard/six-switch.json')
    args = parser.parse_args()
    try:
        demo = DemoData(args.replay_db, args.live_db, args.mode)
        server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(demo, ROOT/'frontend/dist',load_package(args.evidence_package)))
    except (OSError, ValueError) as error:
        parser.exit(1, f'FlowRider demo: {error}\n')
    print(f'FlowRider {args.mode.upper()} demo: http://localhost:{args.port}', flush=True)
    print('Ctrl+C stops only this API/dashboard. No network/controller is started or stopped.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
