"""Run interactively on the VPS. Only use a dedicated Binance Demo/Testnet key."""
import getpass
import json
import os
from pathlib import Path
path=Path.home()/'.config/gptsalov/testnet.json'
if path.exists() or path.is_symlink():
    raise SystemExit('Existing file retained; review before replacing')
path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
key=getpass.getpass('Binance TESTNET API key (hidden): ').strip()
secret=getpass.getpass('Binance TESTNET secret (hidden): ').strip()
if not key or not secret:
    raise SystemExit('Empty credentials; no file written')
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:
    json.dump(dict(environment='testnet',api_key=key,api_secret=secret),f)
print('Testnet credentials saved. No API calls or orders made.')
