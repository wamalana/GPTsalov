# GPTsalov monitoring without an always-on Mac

This change adds a read-only dashboard, VPS collector and a watchdog for a **second host**. It does not submit orders, restart trading, unlock risk state, use Binance keys or schedule four-hour reports. Installed on the VPS on 2026-09-16. See deployment record below.

## What is measured

- The existing schema-1 paper ledger, opened with SQLite `mode=ro` and `query_only`.
- `gptsalov-paper.service` and optional `gptsalov-forward.service` in the current user's systemd manager.
- An observation must be within 180 seconds, and the closed candle within one hour plus its permitted delay. A stopped/unknown primary service, missing DB, stale observation, synthetic data, errors or risk locks never produce healthy status.
- The collector refreshes every 30 seconds. Its own snapshot expires after 90 seconds. `/healthz` returns 503 for an unhealthy or stale collector.
- The six avatar desks represent components of the existing paper engine. They are not six independently running agents. News is disconnected. The separate Testnet pilot desk reads a strictly allowlisted local pilot ledger; this monitor does not contact the exchange. No order/account connectivity is inferred from public market data.
- Equity and P&L are paper figures; raw logs, configuration, error strings and API secrets are not exposed.

## Local checks

```bash
python3 -m unittest discover -s tests -v
python3 -m gptsalov.monitor --db /path/to/existing/paper.db --once
```

A missing ledger returns `unavailable` without creating a database. A locally opened dashboard cannot show the VPS until installed there. The frontend accepts a dashboard-only token, keeps it in memory, and clears it on logout or reload. It polls the backend every 30 seconds. This is health monitoring, not a four-hour report.

## Install on the trading VPS

Use GCP SSH-in-browser or another authenticated VPS connection, as the **existing trading user**. This source targets GPTsalov's documented `~/GPTsalov` layout, not the older `/opt/claudislav2` application. Verify which deployment is active before installing. No Binance key is needed.

1. Fetch the reviewed commit into a new immutable release directory using the VPS's existing GitHub authentication. Do not overwrite a dirty checkout or change `~/GPTsalov/current` (the trading release).
2. Set `~/GPTsalov/monitor-current` to that release, then run:

```bash
cd ~/GPTsalov/monitor-current
python3 scripts/install_monitor.py
systemctl --user status gptsalov-monitor.service --no-pager
loginctl show-user "$USER" -p Linger
```

If `Linger` is not `yes`, enable it through the host's permitted administration process. The dashboard needs read access to the ledger and existing WAL/SHM files; do not change trading database ownership or disable its locks. The service enforces a read-only data directory. If SQLite cannot open a read-only WAL on that deployment, it fails closed as `unavailable`; inspect and arrange a proper SQLite backup snapshot rather than enabling writes for monitoring.

3. Add a dedicated DNS hostname to the **existing** Caddy configuration using `deploy/Caddy.monitor.example`. Validate the full Caddy config before reloading. Do not replace existing routes or expose port 8790: the app binds only `127.0.0.1`. HTTPS terminates at Caddy. Firewall access is needed only for the existing HTTPS service, not a new public Python port.
4. Read the dashboard token privately from `~/.config/gptsalov/monitor.env` on the VPS and enter it in the HTTPS dashboard. The separate health token only reads `/healthz`; it cannot read `/api/status`. Never put either token in a URL, screenshot, chat or repository. Tokens are random, at least 32 characters, and can be rotated by updating the env file and restarting only the monitor service.
5. Confirm authenticated `/api/status` and `/healthz` through HTTPS, a 401 without the token, fresh timestamps, accurate service state, mobile rendering and no secrets. Validate loss of telemetry with a test fixture; do not stop the live trader merely to test the dashboard.

## External watchdog

Install on an **independent always-on host or managed runner**, not this VPS or the Mac. A watchdog on the trading VPS cannot detect that VPS losing power. A GCP managed HTTPS availability check is now provisioned (see below). The standalone watchdog is an optional alternative; no notification destination is configured.

Use the same code release at `~/GPTsalov/monitor-current` on the second host. Create `~/.config/gptsalov/watchdog.env` with mode 0600:

```dotenv
GPTSALOV_HEALTH_URL=https://YOUR-MONITOR-HOST/healthz
GPTSALOV_HEALTH_TOKEN=YOUR-SEPARATE-HEALTH-TOKEN
# Optional HTTPS endpoint accepting JSON {"text":"..."}; configure your destination first.
# GPTSALOV_ALERT_WEBHOOK=https://YOUR-ALERT-ENDPOINT
```

Do not enable the timer until the hostname, HTTPS and health token work. Copy the watchdog service/timer into `~/.config/systemd/user/`, reload the user manager, then:

```bash
systemctl --user enable --now gptsalov-watchdog.timer
systemctl --user list-timers gptsalov-watchdog.timer
journalctl --user -u gptsalov-watchdog.service -n 20 --no-pager
```

The check runs approximately every minute and flags failure after three consecutive failures (about 3–4 minutes including timeouts). It rejects redirects and stale “healthy” responses. The first down transition and recovery are written to the journal; an optional configured webhook gets those transitions only. No webhook or message destination is configured by default, and no messages have been sent. Failed webhook deliveries are retried. Check your destination's expected payload before enabling it. The watchdog host also needs supervision/linger to survive logout and reboot.

## Rollback

Stop/disable `gptsalov-monitor.service` on the VPS and `gptsalov-watchdog.timer` on the second host; remove only the dedicated Caddy hostname after validating its remaining config. Preserve the trading release, services, database and risk locks. No migration or trader restart is required by this change.


## Monitoring setup status

The authenticated external health check has been configured after explicit user authorization. It uses HTTPS with certificate validation, masked authorization headers, a 60-second interval and a 10-second timeout. The separate public availability check remains configured. Initial metric ingestion was still pending at verification time.

No notification destination or four-hour reports have been configured. No trading service, trading mode or risk lock was changed. Infrastructure identifiers, credentials and account figures are intentionally omitted from this record.
