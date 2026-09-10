"""Optional observation only. Never returns a trading decision to PaperEngine."""
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
import json
import sqlite3

from .core import D, dec, encode, size

THRESHOLDS = (D('1'), D('1.5'), D('2'))  # Experiments, not optimized settings.


def reward_risk(position, cfg):
    """Use the simulator's rounded entry/target, adverse exit, fees and reserve."""
    entry, qty = dec(position['entry']), dec(position['qty'])
    fill = dec(position['target'])*(1-position['side']*cfg.slippage_bps/10000)
    gross = position['side']*(fill-entry)*qty
    exit_fee = fill*qty*cfg.fee_bps/10000
    reward = gross-exit_fee-dec(position['entry_fee'])-dec(position['funding_reserve'])
    risk = dec(position['modeled_risk'])
    if risk <= 0 or qty <= 0:
        raise ValueError('INVALID_RESEARCH_RISK')
    ratio = reward/risk
    return {'target_exit_estimate': str(fill), 'target_gross_pnl': str(gross),
            'target_exit_fee': str(exit_fee), 'net_reward': str(reward),
            'modeled_risk': str(risk), 'net_rr': str(ratio),
            'passes': {str(t): ratio >= t for t in THRESHOLDS}}


class ResearchRecorder:
    """Bounded SQLite sidecar. A failure disables recording, never paper trading.

    Record only after a successful ledger commit. A crash between commits can
    leave gaps; no backfilled entry assessments are presented as prospective.
    """
    def __init__(self, path, ledger_path, cfg):
        self.path = Path(path).resolve()
        if self.path == Path(ledger_path).resolve():
            raise ValueError('Research database must differ from paper ledger')
        if self.path.exists() and Path(ledger_path).exists() and self.path.samefile(ledger_path):
            raise ValueError('Research database aliases paper ledger')
        self.cfg, self.disabled, self.last_stamp = cfg, False, None

    def observe(self, snapshot, before, after, events):
        if self.disabled:
            return None
        # Repeated polls update the live ledger heartbeat, not duplicate research.
        if snapshot.latest_close == before['last_close_ms']:
            return None
        try:
            self._record(snapshot, before, after, events)
        except Exception as exc:
            self.disabled = True
            return {'status': 'RESEARCH_DISABLED', 'error_type': type(exc).__name__,
                    'reason': 'Sidecar write failed; inspect storage and permissions',
                    'paper_trading_unchanged': True}
        return None

    def _record(self, snapshot, before, after, events):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=0.2)) as db, db:
            # About 128 MiB for DB pages; do not grow without bound on a small VPS.
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            db.execute(f'PRAGMA max_page_count={134217728//page_size}')
            db.execute('CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY, data TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS bars (symbol TEXT, close_ms INTEGER, observed_ms INTEGER, data TEXT, PRIMARY KEY(symbol,close_ms))')
            db.execute('CREATE TABLE IF NOT EXISTS cycles (close_ms INTEGER PRIMARY KEY, observed_ms INTEGER, data TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS candidates (signal_id TEXT PRIMARY KEY, observed_ms INTEGER, data TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS entries (signal_id TEXT PRIMARY KEY, open_ms INTEGER, observed_ms INTEGER, data TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS outcomes (signal_id TEXT PRIMARY KEY, close_ms INTEGER, observed_ms INTEGER, data TEXT)')
            identity = {'schema': 1, 'source': snapshot.source, 'config_hash': self.cfg.fingerprint,
                        'config': asdict(self.cfg), 'thresholds': [str(t) for t in THRESHOLDS],
                        'mode': 'OBSERVE_ONLY', 'bars_retention_days': 30}
            old = db.execute('SELECT data FROM meta WHERE id=1').fetchone()
            if old and json.loads(old[0]) != json.loads(encode(identity)):
                raise ValueError('Research identity mismatch; use a new sidecar')
            db.execute('INSERT OR IGNORE INTO meta VALUES (1,?)', (encode(identity),))
            for symbol, bars in snapshot.bars.items():
                latest = db.execute('SELECT MAX(close_ms) FROM bars WHERE symbol=?', (symbol,)).fetchone()[0]
                db.executemany('INSERT OR IGNORE INTO bars VALUES (?,?,?,?)',
                    [(symbol, b.close_ms, snapshot.now_ms, encode(asdict(b))) for b in bars
                     if latest is None or b.close_ms > latest])
            intent_ids = {d['signal']['symbol'] for _, k, d in events if k == 'PAPER_INTENT'}
            rejected = {d.get('symbol'): d.get('reason') for _, k, d in events if k == 'REJECT'}
            for signal in snapshot.signals:
                assessment = None
                sizing_error = None
                try:
                    plan = size(signal, signal.reference, dec(after['equity']), snapshot.rules[signal.symbol], self.cfg)
                    assessment = reward_risk({'entry': plan.entry, 'qty': plan.qty, 'target': plan.target,
                        'side': signal.side, 'entry_fee': plan.notional*self.cfg.fee_bps/10000,
                        'funding_reserve': plan.notional*self.cfg.funding_reserve_bps/10000,
                        'modeled_risk': plan.risk}, self.cfg)
                except (ValueError, KeyError) as exc:
                    sizing_error = str(exc)
                if signal.symbol in intent_ids:
                    reason = 'BASELINE_INTENT'
                elif signal.symbol in rejected:
                    reason = rejected[signal.symbol]
                elif after['hard_lock'] or after['daily_locked']:
                    reason = 'RISK_LOCK'
                elif after['position']:
                    reason = 'POSITION_OPEN'
                elif after['pending']:
                    reason = 'PENDING_OR_HIGHER_RANKED_SIGNAL'
                else:
                    reason = 'NOT_SELECTED'
                db.execute('INSERT OR IGNORE INTO candidates VALUES (?,?,?)',
                    (signal.key, snapshot.now_ms, encode({'signal': asdict(signal),
                     'baseline_reason': reason, 'reference_only_assessment': assessment,
                     'sizing_error': sizing_error, 'rules': asdict(snapshot.rules[signal.symbol])
                     if signal.symbol in snapshot.rules else None})))
            for timestamp, kind, data in events:
                if kind == 'PAPER_OPEN':
                    # Only actual modeled fills determine experimental pass/fail.
                    db.execute('INSERT OR IGNORE INTO entries VALUES (?,?,?,?)',
                        (data['signal_id'], timestamp, snapshot.now_ms,
                         encode({'position': data, 'assessment': reward_risk(data, self.cfg)})))
                elif kind == 'PAPER_CLOSE':
                    db.execute('INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?)',
                        (data['signal_id'], timestamp, snapshot.now_ms, encode(data)))
            db.execute('INSERT OR IGNORE INTO cycles VALUES (?,?,?)',
                (snapshot.latest_close, snapshot.now_ms, encode({'scan': snapshot.audit,
                 'events': [{'time_ms': t, 'kind': k, 'data': d} for t,k,d in events],
                 'equity': after['equity'], 'position': after['position'],
                 'pending': after['pending'], 'daily_locked': after['daily_locked'],
                 'hard_lock': after['hard_lock']})))
            cutoff = snapshot.now_ms-30*86400000
            db.execute('DELETE FROM bars WHERE close_ms<?', (cutoff,))
            db.execute('DELETE FROM cycles WHERE close_ms<?', (cutoff,))


def research_report(path):
    """Conditional baseline trade subsets, never a counterfactual equity curve."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
        meta = json.loads(db.execute('SELECT data FROM meta WHERE id=1').fetchone()[0])
        rows = db.execute('SELECT e.data,o.data FROM entries e JOIN outcomes o USING(signal_id)').fetchall()
        total = db.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0]
        last = db.execute('SELECT MAX(observed_ms) FROM cycles').fetchone()[0]
        candidates = db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0]
        entries = db.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
    results = []
    for threshold in meta['thresholds']:
        passed, blocked = [], []
        for entry, outcome in rows:
            e, o = json.loads(entry), json.loads(outcome)
            (passed if e['assessment']['passes'][threshold] else blocked).append(dec(o['net_pnl']))
        results.append({'min_net_rr': threshold, 'passed_closed_trades': len(passed),
                        'passed_net_pnl': str(sum(passed, D(0))),
                        'blocked_closed_trades': len(blocked),
                        'blocked_net_pnl': str(sum(blocked, D(0)))})
    return {'mode': 'OBSERVE_ONLY', 'last_observed_ms': last, 'candidates': candidates,
            'entries': entries, 'matched_closed_trades': len(rows),
            'closed_without_entry_assessment': total-len(rows), 'comparisons': results,
            'warning': 'Conditional subsets of baseline fills, not a strategy backtest. '
                       'Skipped trades change future availability, sizing and locks. '
                       'Unexecuted signals have no simulated outcome. No historical backfill.'}
