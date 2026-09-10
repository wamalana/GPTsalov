from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import sys
import time

from . import __version__
from .core import Config, encode
from .market import BinanceFeed, BinancePublic, DemoFeed, MarketError, SnapshotExpired
from .paper import PaperEngine, Store, report
from .research import ResearchRecorder, research_report


def print_json(value):
    print(json.dumps(value, default=str, ensure_ascii=False, indent=2), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="GPTsalov research-only paper trading; no API key needed")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-access", help="One public Binance server-time GET; no account access")
    scan = commands.add_parser("scan", help="Read-only scan; never creates positions")
    scan.add_argument("--source", choices=("demo", "binance"), default="demo")
    scan.add_argument("--config", default=None)
    run = commands.add_parser("run", help="Forward candle simulation with persistent ledger")
    run.add_argument("--source", choices=("demo", "binance"), default="demo")
    run.add_argument("--config", default=None)
    run.add_argument("--db", required=True)
    run.add_argument("--research-db", help="Optional separate observation-only SQLite database")
    run.add_argument("--cycles", type=int, default=180, help="0=continuous for Binance, max 380 for demo")
    status = commands.add_parser("status", help="Read-only ledger report; JSON includes data timestamp")
    status.add_argument("--db", required=True)
    status.add_argument("--json", action="store_true")
    research = commands.add_parser("research-status", help="Read-only net reward/risk experiment report")
    research.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "research-status":
            print_json(research_report(args.db))
            return 0
        if args.command == "check-access":
            print_json({"mode": "READ_ONLY", "binance": BinancePublic().get("/fapi/v1/time")})
            return 0
        if args.command == "status":
            data = report(args.db)
            if args.json:
                print_json(data)
            else:
                print(f"GPTsalov {__version__} | PAPER ONLY | {data['source']}")
                print(f"Equity estimate: {data['equity']} USDT | Net estimate: {data['net_pnl_estimate']}")
                print(f"Closed trades: {data['closed_trades']} | DD: {data['drawdown_fraction']}")
                print(f"Position: {encode(data['position'])}")
                print(f"Pending: {encode(data['pending'])}")
                print(f"Daily lock: {data['daily_locked']} | Review lock: {data['hard_lock']}")
                print(f"Last market time (UTC milliseconds): {data['as_of_ms']}")
                print(f"Last error: {data['last_error']}")
                print(f"Health: {data['health']} | Data age ms: {data['data_age_ms']}")
                print("Synthetic data / fixed funding reserve are not proof of profit.")
            return 0
        cfg = Config.load(args.config)
        feed = DemoFeed() if args.source == "demo" else BinanceFeed(cfg)
        if args.command == "scan":
            snapshot = feed.snapshot()
            print_json({"mode": "READ_ONLY", "source": snapshot.source, "as_of_ms": snapshot.now_ms,
                        "signals": [asdict(s) for s in snapshot.signals], "audit": snapshot.audit,
                        "note": "Heuristic score, not win probability; no orders submitted"})
            return 0
        if args.cycles < 0 or (args.source == "demo" and not 1 <= args.cycles <= 380):
            raise ValueError("Demo cycles must be 1–380; Binance cycles must be >= 0")
        source = "synthetic-demo" if args.source == "demo" else "binance-public"
        recorder = ResearchRecorder(args.research_db, args.db, cfg) if args.research_db else None
        with Store(args.db, cfg, source) as store:
            if args.source == "demo" and store.state["last_close_ms"] is not None:
                # Resume deterministic generator from the persisted last candle.
                feed.index = (store.state["last_close_ms"]-feed.start+1)//900_000
            engine = PaperEngine(cfg, store)
            count = 0
            while args.cycles == 0 or count < args.cycles:
                try:
                    snapshot = feed.snapshot(engine.required_symbols())
                    before = store.state
                    events = engine.step(snapshot)
                except SnapshotExpired as exc:
                    engine.fault(str(exc), int(time.time()*1000))
                    count += 1
                    print(encode({"cycle": count, "status": "SKIPPED_EXPIRED_SNAPSHOT",
                                  "error": str(exc), "real_orders": 0}), flush=True)
                    if args.source == "binance" and (args.cycles == 0 or count < args.cycles):
                        time.sleep(cfg.poll_seconds)
                    continue
                except (MarketError, ValueError) as exc:
                    engine.fault(str(exc), int(time.time()*1000))
                    raise
                if recorder:
                    warning = recorder.observe(snapshot, before, store.state, events)
                    if warning:
                        print(encode(warning), file=sys.stderr, flush=True)
                count += 1
                if events or count == 1 or count % 20 == 0:
                    print(encode({"cycle": count, "source": source, "equity": store.state["equity"],
                                  "events": [{"kind": kind, "time_ms": stamp, "data": data}
                                             for stamp, kind, data in events]}), flush=True)
                if args.source == "binance" and (args.cycles == 0 or count < args.cycles):
                    time.sleep(cfg.poll_seconds)
        print(encode({"completed_cycles": count, "database": args.db, "real_orders": 0}))
        return 0
    except KeyboardInterrupt:
        print("Stopped. Paper ledger retained; no real orders exist.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(encode({"status": "STOPPED", "error": str(exc), "type": type(exc).__name__,
                      "real_orders": 0}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
