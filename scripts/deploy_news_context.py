"""Activate staged news releases on the existing Testnet VPS, with config rollback."""
from pathlib import Path
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time


def systemctl(*args):
    return subprocess.run(['systemctl', '--user', *args], check=True, capture_output=True, text=True).stdout


def deploy(release, office):
    release, office = Path(release).resolve(), Path(office).resolve()
    home = Path.home()
    base = home/'GPTsalov'
    sys.path.insert(0, str(release))
    from gptsalov.news_analysis import read
    ai = read()
    if ai.get('status') != 'COMPLETED' or not ai.get('llm_connected'):
        raise ValueError('Fresh validated AI result required before activation')
    pilot_path = base/'data/multiagent-testnet-v8/pilot.db'
    with sqlite3.connect(pilot_path.as_uri()+'?mode=ro', uri=True) as db:
        state = json.loads(db.execute('SELECT data FROM state WHERE id=1').fetchone()[0])
        assert state['policy']['environment'] == 'testnet'
        assert not state.get('active') and state.get('lock') == 'PILOT_BATCH_COMPLETE'
        backup = base/'data'/('news-deploy-'+time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
        backup.mkdir()
        with sqlite3.connect(backup/'pilot.db') as dst:
            db.backup(dst)
    units = home/'.config/systemd/user'
    name = 'zzzzzzzzzzzz-news-context.conf'
    files = {}
    for unit in ('gptsalov-testnet-pilot','gptsalov-market-scanner','gptsalov-multiagent-shadow','gptsalov-monitor'):
        directory = office if unit == 'gptsalov-monitor' else release
        text = '[Service]\nWorkingDirectory='+str(directory)+'\n'
        if unit == 'gptsalov-market-scanner':
            text += 'ExecStart=\nExecStart=/usr/bin/python3 -m gptsalov.market_scanner --pilot-db '+str(pilot_path)+'\n'
        if unit == 'gptsalov-multiagent-shadow':
            text += ('ExecStart=\nExecStart=/usr/bin/python3 -u -m gptsalov.multiagent_shadow --pilot-db '
                     +str(pilot_path)+' --output-db '+str(base/'data/multiagent-shadow-news-v1/observations.db')
                     +' --symbols BTCUSDT ETHUSDT --cycles 0\n')
        files[units/(unit+'.service.d')/name] = text
    for filename in ('gptsalov-news-ai.service','gptsalov-news-ai.timer'):
        files[units/filename] = (release/'deploy'/filename).read_text()
    saved = {str(p): p.read_text() if p.exists() else None for p in files}
    link = base/'news-ai-current'
    if link.exists() and not link.is_symlink():
        raise ValueError('news-ai-current must be a symlink or absent')
    previous_link = os.readlink(link) if link.is_symlink() else None
    manifest = dict(files=saved, previous_link=previous_link, release=str(release), office=str(office))
    (backup/'rollback.json').write_text(json.dumps(manifest, indent=2))
    restart = ['gptsalov-testnet-pilot.service','gptsalov-multiagent-shadow.service','gptsalov-monitor.service']
    try:
        for p, text in files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        if link.is_symlink(): link.unlink()
        link.symlink_to(release)
        systemctl('daemon-reload')
        for unit in restart + ['gptsalov-market-scanner.service']:
            expected = office if unit == 'gptsalov-monitor.service' else release
            actual = systemctl('show', unit, '-p', 'WorkingDirectory', '--value').strip()
            assert actual == str(expected), (unit, actual)
        systemctl('restart', *restart)
        for unit in restart:
            assert systemctl('is-active', unit).strip() == 'active'
        systemctl('enable', '--now', 'gptsalov-news-ai.timer')
    except Exception:
        for path, old in saved.items():
            p = Path(path)
            if old is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(old)
        if link.is_symlink(): link.unlink()
        if previous_link is not None: link.symlink_to(previous_link)
        systemctl('daemon-reload')
        systemctl('restart', *restart)
        raise
    print(json.dumps({'deployed':True, 'backup':str(backup), 'lock_preserved':state['lock'],
                      'equity':state['equity'], 'ai_status':ai['status']}))


if __name__ == '__main__':
    deploy(*sys.argv[1:])
