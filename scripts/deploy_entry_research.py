"""Install prospective research on the existing VPS; never mutate the pilot."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

FILES = ('gptsalov/entry_research.py','gptsalov/market_scanner.py',
         'gptsalov/exit_shadow.py','tests/test_exit_shadow.py',
         'tests/test_entry_research.py','ENTRY_RESEARCH.md',
         'deploy/gptsalov-entry-research.service','deploy/gptsalov-entry-research.timer',
         'scripts/deploy_entry_research.py')
EXPECTED = {'gptsalov/market_scanner.py':'c8d8bcd17fbc4ec6828a5bfe8a2c52d0c04bf9b90405941719ea34b2df364b3b',
            'gptsalov/core.py':'a5b0e5b348cf397b54469b3bf0651b7d0120d8158a3f8239cf071afd679e3a08'}


def ctl(*args,check=True):
    return subprocess.run(['systemctl','--user',*args],check=check,capture_output=True,text=True).stdout.strip()


def deploy(stage):
    stage=Path(stage).resolve(); home=Path.home(); base=home/'GPTsalov'
    source=Path(ctl('show','gptsalov-market-scanner.service','-p','WorkingDirectory','--value'))
    if not source.is_relative_to(base/'releases'):
        raise ValueError('Unexpected scanner release')
    for name,digest in EXPECTED.items():
        if hashlib.sha256((source/name).read_bytes()).hexdigest()!=digest:
            raise ValueError('Source drift: '+name)
    for name in FILES:
        if not (stage/name).is_file():
            raise ValueError('Missing staged file '+name)
    stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())
    release=base/'releases'/('entry-research-'+stamp)
    shutil.copytree(source,release,ignore=shutil.ignore_patterns('__pycache__','.git'))
    for name in FILES:
        (release/name).parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(stage/name,release/name)
    subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_entry_research.py','-q'],cwd=release,check=True)
    subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_exit_shadow.py','-q'],cwd=release,check=True)
    subprocess.run([sys.executable,'-m','compileall','-q','gptsalov'],cwd=release,check=True)
    units=home/'.config/systemd/user'
    override=units/'gptsalov-market-scanner.service.d/zzzzzzzzzzzzzzzz-entry-research.conf'
    db=base/'data/entry-research-v1/research.db'
    pilot=base/'data/multiagent-testnet-v8/pilot.db'
    contents={override:'[Service]\nWorkingDirectory='+str(release)+'\nExecStart=\nExecStart=/usr/bin/python3 -m gptsalov.market_scanner --pilot-db '+str(pilot)+' --research-db '+str(db)+'\n'}
    for name in ('gptsalov-entry-research.service','gptsalov-entry-research.timer'):
        contents[units/name]=(release/'deploy'/name).read_text()
    saved={str(p):p.read_text() if p.exists() else None for p in contents}
    link=base/'entry-research-current'
    if link.exists() and not link.is_symlink():
        raise ValueError('Research link is not symlink')
    previous=os.readlink(link) if link.is_symlink() else None
    scanner_timer_active=ctl('is-active','gptsalov-market-scanner.timer',check=False)=='active'
    if not scanner_timer_active:
        raise ValueError('Existing scanner timer is not active')
    pilot_before=ctl('show','gptsalov-testnet-pilot.service','-p','WorkingDirectory','-p','MainPID','-p','ExecStart')
    backup=base/'data'/('entry-research-deploy-'+stamp);backup.mkdir()
    (backup/'rollback.json').write_text(json.dumps(dict(files=saved,previous_link=previous,release=str(release),source=str(source)),indent=2))
    try:
        ctl('stop','gptsalov-market-scanner.timer')
        if ctl('is-active','gptsalov-market-scanner.service',check=False) in ('active','activating','deactivating'):
            raise ValueError('Scanner busy; retry after it finishes')
        for p,text in contents.items():
            p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        temp=base/('entry-research-link-'+stamp);temp.symlink_to(release);os.replace(temp,link)
        ctl('daemon-reload')
        if ctl('show','gptsalov-market-scanner.service','-p','WorkingDirectory','--value')!=str(release):
            raise ValueError('Scanner override did not activate')
        # Initialize/validate schema through the no-signal evaluator before activation.
        subprocess.run([sys.executable,'-m','gptsalov.entry_research','evaluate','--db',str(db)],cwd=release,check=True)
        ctl('enable','--now','gptsalov-entry-research.timer')
        ctl('start','gptsalov-market-scanner.timer')
        ctl('start','--no-block','gptsalov-market-scanner.service')
        if ctl('is-active','gptsalov-entry-research.timer')!='active':
            raise ValueError('Outcome timer inactive')
        if ctl('show','gptsalov-testnet-pilot.service','-p','WorkingDirectory','-p','MainPID','-p','ExecStart')!=pilot_before:
            raise ValueError('Pilot runtime changed during deployment; investigate')
    except Exception:
        ctl('disable','--now','gptsalov-entry-research.timer',check=False)
        for name,text in saved.items():
            p=Path(name)
            if text is None:p.unlink(missing_ok=True)
            else:p.write_text(text)
        if link.is_symlink():link.unlink()
        if previous is not None:link.symlink_to(previous)
        ctl('daemon-reload');ctl('start','gptsalov-market-scanner.timer')
        raise
    print(json.dumps(dict(deployed=True,release=str(release),backup=str(backup),database=str(db),pilot_runtime_unchanged=True)))

if __name__=='__main__':deploy(sys.argv[1])
