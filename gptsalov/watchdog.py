"""Run on a SECOND host. Check health each minute; no periodic trading reports."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from .market import NoRedirect


def check(url, token, opener=None, timestamp=None):
    stamp = int(time.time()*1000) if timestamp is None else timestamp
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != '/healthz':
        raise ValueError('Use an HTTPS /healthz URL without credentials/query/fragment')
    if len(token) < 32:
        raise ValueError('Health token must contain at least 32 characters')
    opener = opener or build_opener(NoRedirect())
    try:
        with opener.open(Request(url, headers={'Authorization': 'Bearer ' + token}), timeout=15) as response:
            raw = response.read(4097)
            if len(raw) > 4096:
                return {'healthy': False, 'reason': 'invalid_response'}
            data = json.loads(raw)
        checked = data.get('checked_at_ms')
        if isinstance(checked, bool) or not isinstance(checked, (int, float)) or not 0 <= stamp-checked <= 90_000:
            return {'healthy': False, 'reason': 'stale_monitor'}
        ok = data.get('schema') == 1 and data.get('healthy') is True
        return {'healthy': ok, 'reason': 'ok' if ok else 'unhealthy'}
    except HTTPError as exc:
        return {'healthy': False, 'reason': 'authentication' if exc.code in (401,403) else 'unhealthy' if exc.code == 503 else 'http_error'}
    except Exception:
        return {'healthy': False, 'reason': 'unreachable_or_invalid'}


def transition(previous, result, threshold=3):
    failures = 0 if result['healthy'] else previous.get('failures', 0) + 1
    old = previous.get('state', 'unknown')
    state = 'up' if result['healthy'] else 'down' if failures >= threshold else old
    event = None
    if state == 'down' and old != 'down':
        event = 'GPTsalov needs attention: ' + result['reason']
    elif state == 'up' and old == 'down':
        event = 'GPTsalov recovered: service and data are current'
    return {'state': state, 'failures': failures, 'reason': result['reason']}, event


def notify(url, message):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Alert webhook must use HTTPS')
    # Endpoint and credentials stay server-side. Never print raw errors/URLs.
    request = Request(url, data=json.dumps({'text': message}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    with build_opener(NoRedirect()).open(request, timeout=15) as response:
        response.read(1024)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state', required=True)
    args = p.parse_args()
    state_path = Path(args.state)
    try:
        previous = json.loads(state_path.read_text())
    except (OSError, ValueError):
        previous = {}
    try:
        result = check(os.environ.get('GPTSALOV_HEALTH_URL',''), os.environ.get('GPTSALOV_HEALTH_TOKEN',''))
    except ValueError:
        raise SystemExit('Configure HTTPS health URL and health token on the external host')
    current, event = transition(previous, result)
    # Keep pending alerts for retry, but replace an old outage alert on recovery.
    pending = event or previous.get('pending_alert')
    webhook = os.environ.get('GPTSALOV_ALERT_WEBHOOK','')
    if pending:
        print(pending, flush=True)
        if webhook:
            try:
                notify(webhook, pending)
                pending = None
            except Exception:
                print('Alert delivery failed; retry on next check', flush=True)
        else:
            pending = None  # Journal only until a notification destination is configured.
    current.update({'checked_at_ms': int(time.time()*1000), 'pending_alert': pending})
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = state_path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(current, out)
    temp.replace(state_path)
    print(json.dumps({**result, 'state': current['state'], 'notification_configured': bool(webhook)}))


if __name__ == '__main__':
    main()
