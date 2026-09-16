"""Read-only observability. Never imports an engine, credentials or order client."""
from __future__ import annotations

import argparse
import hmac
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC = Path(__file__).with_name('dashboard')
SERVICES = ('gptsalov-paper.service', 'gptsalov-forward.service', 'gptsalov-tuning.service',
            'gptsalov-multiagent-shadow.service', 'gptsalov-testnet-pilot.service')
MAX_OBSERVATION_AGE_MS = 180_000
MAX_SNAPSHOT_AGE_MS = 90_000


def now_ms():
    return int(time.time() * 1000)


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError, OverflowError):
        return None


def service_status(unit):
    try:
        result = subprocess.run(
            ['systemctl', '--user', 'show', unit, '--property=LoadState,ActiveState,SubState'],
            capture_output=True, text=True, timeout=3, check=False)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if result.returncode or values.get('LoadState') != 'loaded':
            return {'status': 'unknown'}
        return {'status': 'active' if values.get('ActiveState') == 'active' else 'stopped'}
    except (OSError, subprocess.TimeoutExpired):
        return {'status': 'unknown'}


def ledger_status(path, timestamp):
    """Bounded read transaction; explicit allowlist prevents error/secret leakage."""
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            row = db.execute('SELECT data FROM state WHERE id=1').fetchone()
            if row is None or len(row[0]) > 2_000_000:
                raise ValueError('Missing/oversized state')
            s = json.loads(row[0])
            if s.get('schema') != 1 or s.get('mode') != 'paper':
                raise ValueError('Unsupported ledger')
            rows = db.execute("SELECT timestamp_ms,kind FROM events ORDER BY id DESC LIMIT 12").fetchall()
        observed = number(s.get('observed_at_ms'))
        market = number(s.get('as_of_ms'))
        age = timestamp - observed if observed is not None else None
        market_age = timestamp - market if market is not None else None
        max_age = number(s.get('max_data_age_ms'))
        max_age = max_age if max_age is not None and 0 <= max_age <= 3_600_000 else 120_000
        fresh = age is not None and 0 <= age <= MAX_OBSERVATION_AGE_MS
        fresh = fresh and market_age is not None and 0 <= market_age <= 3_600_000 + max_age
        error = bool(s.get('last_error'))
        locked = bool(s.get('hard_lock') or s.get('daily_locked'))
        source = s.get('source') if s.get('source') in ('binance', 'binance-public', 'synthetic-demo') else 'unknown'
        status = ('error' if error else 'locked' if locked else 'stale' if not fresh else
                  'synthetic' if source == 'synthetic-demo' else 'current' if source in ('binance', 'binance-public') else 'unknown')
        fields = {k: number(s.get(k)) for k in ('equity', 'initial_equity', 'balance', 'closed_trades', 'wins')}
        equity, initial = fields['equity'], fields['initial_equity']
        fields['pnl'] = equity - initial if equity is not None and initial is not None else None
        fields['win_rate'] = fields['wins'] / fields['closed_trades'] if fields['closed_trades'] and fields['wins'] is not None else None
        position = s.get('position')
        symbol = position.get('symbol') if isinstance(position, dict) else None
        if not isinstance(symbol, str) or not symbol.isascii() or not symbol.isalnum() or len(symbol) > 30:
            symbol = None
        known_events = {'PAPER_CLOSE', 'PAPER_OPEN', 'PAPER_INTENT', 'REJECT', 'DATA_ERROR', 'HALT', 'FLAT_RESYNC'}
        return {'status': status, 'mode': 'paper', 'source': source, 'observed_at_ms': observed,
                'as_of_ms': market, 'observation_age_ms': age, 'market_age_ms': market_age,
                'has_error': error, 'locked': locked, 'position_symbol': symbol,
                'pending': bool(s.get('pending')), 'metrics': fields,
                'events': [{'timestamp_ms': stamp, 'kind': kind if kind in known_events else 'EVENT'} for stamp, kind in rows]}
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError):
        return {'status': 'unavailable', 'mode': 'unknown', 'metrics': {}, 'events': []}


def pilot_status(path, timestamp):
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            row = db.execute('SELECT data FROM state WHERE id=1').fetchone()
        if not row or len(row[0]) > 2_000_000:
            raise ValueError('Invalid state')
        s = json.loads(row[0])
        if s.get('policy', {}).get('environment') != 'testnet':
            raise ValueError('Not a testnet ledger')
        checked = number(s.get('last_check_ms'))
        fresh = checked is not None and 0 <= timestamp - checked <= 180_000
        status = 'error' if s.get('error') else 'locked' if s.get('lock') else 'current' if fresh else 'stale'
        phase = s.get('phase', '')
        phase = phase if isinstance(phase, str) and phase.isascii() and len(phase) <= 40 and phase.replace('_','').isalnum() else 'unknown'
        return {'status': status, 'mode': 'testnet', 'checked_at_ms': checked, 'fresh': fresh,
                'locked': bool(s.get('lock')), 'has_error': bool(s.get('error')), 'phase': phase,
                'equity': number(s.get('equity')), 'closed_trades': number(s.get('closed_trades'))}
    except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
        return {'status': 'unavailable', 'mode': 'unknown'}


def collect(db_path, service_reader=service_status, timestamp=None):
    stamp = now_ms() if timestamp is None else timestamp
    ledger = ledger_status(db_path, stamp)
    services = {unit: service_reader(unit) for unit in SERVICES}
    pilot = pilot_status(Path(db_path).parent / 'testnet-pilot-v1/pilot.db', stamp)
    pilot_state = pilot['status']
    if services['gptsalov-testnet-pilot.service']['status'] != 'active' and pilot_state == 'current':
        pilot_state = 'unknown'
    paper_active = services[SERVICES[0]]['status'] == 'active'
    healthy = ledger['status'] == 'current' and paper_active
    working = paper_active and ledger['status'] == 'current'
    state = 'current' if working else ledger['status'] if ledger['status'] != 'current' else 'unknown'
    return {'schema': 1, 'checked_at_ms': stamp, 'healthy': healthy, 'ledger': ledger, 'services': services,
            'agents': [
                {'id': 'scanner', 'name': 'Market Scout', 'status': state, 'kind': 'paper_component'},
                {'id': 'strategy', 'name': 'Strategy Lab', 'status': state, 'kind': 'paper_component'},
                {'id': 'risk', 'name': 'Risk Guard', 'status': state, 'kind': 'paper_component'},
                {'id': 'execution', 'name': 'Paper Trader', 'status': state, 'kind': 'paper_component'},
                {'id': 'news', 'name': 'News Owl', 'status': 'not_connected', 'kind': 'unconnected'},
                {'id': 'testnet', 'name': 'Testnet Pilot', 'status': pilot_state, 'kind': 'testnet_ledger'}],
            'testnet': pilot, 'scheduled_reports': False}


class Snapshot:
    def __init__(self, db):
        self.db, self.lock = db, threading.Lock()
        self.data = {'schema': 1, 'checked_at_ms': 0, 'healthy': False}

    def refresh(self):
        data = collect(self.db)
        with self.lock:
            self.data = data

    def read(self):
        with self.lock:
            data = dict(self.data)
        age = now_ms() - data['checked_at_ms']
        data['monitor_fresh'] = 0 <= age <= MAX_SNAPSHOT_AGE_MS
        data['healthy'] = data['healthy'] and data['monitor_fresh']
        return data

    def loop(self):
        while True:
            try:
                self.refresh()
            except Exception:
                # Old snapshot remains stale; do not invent a healthy result.
                pass
            time.sleep(30)


def make_server(snapshot, view_token, health_token, port=8790):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log credentials or request URLs.

        def send(self, status, body, content_type='application/json'):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self, token):
            return hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode())

        def do_GET(self):
            if self.path in ('/api/status', '/healthz'):
                expected = health_token if self.path == '/healthz' else view_token
                if not self.authorized(expected):
                    self.send(401, {'error': 'unauthorized'})
                    return
                data = snapshot.read()
                if self.path == '/healthz':
                    self.send(200 if data['healthy'] else 503, {'schema': 1, 'healthy': data['healthy'], 'checked_at_ms': data['checked_at_ms']})
                else:
                    self.send(200, data)
                return
            assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if self.path not in assets:
                self.send(404, {'error': 'not_found'})
                return
            name, mime = assets[self.path]
            self.send(200, (STATIC / name).read_bytes(), mime)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', required=True)
    p.add_argument('--port', type=int, default=8790)
    p.add_argument('--once', action='store_true')
    args = p.parse_args()
    if args.once:
        print(json.dumps(collect(args.db), allow_nan=False))
        return
    view = os.environ.get('GPTSALOV_VIEW_TOKEN', '')
    health = os.environ.get('GPTSALOV_HEALTH_TOKEN', '')
    if min(len(view), len(health)) < 32 or view == health or not view.isascii() or not health.isascii():
        p.error('Set two different ASCII tokens of at least 32 characters in the environment')
    snapshot = Snapshot(args.db)
    threading.Thread(target=snapshot.loop, daemon=True).start()
    make_server(snapshot, view, health, args.port).serve_forever()


if __name__ == '__main__':
    main()
