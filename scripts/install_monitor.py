"""Install dashboard under the current VPS user, without touching trading services."""
import os
from pathlib import Path
import secrets
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
home = Path.home()
if sys.version_info < (3, 11):
    raise SystemExit('Python 3.11+ required')
if os.geteuid() == 0:
    raise SystemExit('Run as the existing trading user, not root')
if root != (home/'GPTsalov/monitor-current').resolve():
    raise SystemExit('First link ~/GPTsalov/monitor-current to this reviewed release')
ledger = home/'GPTsalov/data/binance-paper.db'
if not ledger.is_file():
    raise SystemExit('Existing paper ledger not found; inspect actual deployment before installing')
config = home/'.config/gptsalov'
config.mkdir(parents=True, exist_ok=True, mode=0o700)
env = config/'monitor.env'
if not env.exists():
    fd = os.open(env, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write('GPTSALOV_VIEW_TOKEN='+secrets.token_urlsafe(36)+'\n')
        out.write('GPTSALOV_HEALTH_TOKEN='+secrets.token_urlsafe(36)+'\n')
units = home/'.config/systemd/user'
units.mkdir(parents=True, exist_ok=True)
(units/'gptsalov-monitor.service').write_bytes((root/'deploy/gptsalov-monitor.service').read_bytes())
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
subprocess.run(['systemctl','--user','enable','--now','gptsalov-monitor.service'],check=True)
print('Dashboard installed on loopback port 8790. Configure HTTPS before remote access.')
print('Tokens stored in ~/.config/gptsalov/monitor.env; never paste them in chat or GitHub.')
print('Confirm Linger=yes for this user. External watchdog must be installed separately.')
