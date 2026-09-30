"""Free RSS collection and read-only headline context; no LLM or trading imports."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

FEEDS = {
    'CoinDesk': ('https://www.coindesk.com/arc/outboundfeeds/rss/', 'coindesk.com'),
    'Cointelegraph': ('https://cointelegraph.com/rss', 'cointelegraph.com'),
}
DEFAULT_DB = '/home/wamalana/GPTsalov/data/news-v1/news.db'
RULES = {
    'BTC': r'\b(bitcoin|btc)\b',
    'ETH': r'\b(ethereum|ether|eth)\b',
    'Binance': r'\bbinance\b',
    'Security': r'\b(hack\w*|exploit\w*|breach\w*|stolen|theft)\b',
    'Regulation': r'\b(sec|cftc|regulat\w*|lawsuit\w*|senate|congress)\b',
    'Macro': r'\b(fed|fomc|inflation|interest rate\w*|cpi|jobs report)\b',
    'ETF': r'\betf\w*\b',
}
def now_ms():
    return int(time.time()*1000)

def safe_url(url, domain):
    try:
        u = urllib.parse.urlsplit(url.strip())
        if u.scheme != 'https' or u.username or u.password or u.port not in (None,443):
            return None
        host = u.hostname or ''
        if host != domain and not host.endswith('.'+domain):
            return None
        return urllib.parse.urlunsplit((u.scheme,u.netloc,u.path,u.query,''))
    except (ValueError, AttributeError):
        return None

def parse_feed(raw, domain, stamp):
    if len(raw)>2_000_000 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('unsupported_xml')
    root = ET.fromstring(raw)
    if root.tag not in ('rss','{http://www.w3.org/2005/Atom}feed'):
        raise ValueError('not_feed')
    items = root.findall('./channel/item')
    if not items:
        raise ValueError('empty_feed')
    result=[]
    for item in items[:100]:
        title=' '.join(html.unescape(re.sub('<[^>]*>', '', item.findtext('title') or '')).split())[:400]
        link=safe_url(item.findtext('link') or '',domain)
        try:
            date=parsedate_to_datetime(item.findtext('pubDate') or '')
            published=int(date.timestamp()*1000) if date.tzinfo is not None else None
        except (ValueError,TypeError,OverflowError):
            published=None
        if not title or not link or published is None or not stamp-7*86400000 <= published <= stamp+300000:
            continue
        tags=[name for name,pattern in RULES.items() if re.search(pattern,title,re.I)]
        result.append({'id':hashlib.sha256(link.encode()).hexdigest(),'title':title,'url':link,
                       'published_ms':published,'tags':tags or ['Crypto']})
    return result

def fetch(name, stamp):
    url,domain=FEEDS[name]
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'GPTsalov-News/1.0 RSS reader','Accept':'application/rss+xml, application/xml'})
        with urllib.request.urlopen(req,timeout=20) as response:
            if not safe_url(response.geturl(),domain):
                raise ValueError('redirect_domain')
            items=parse_feed(response.read(2_000_001),domain,stamp)
        return name,items,None
    except Exception as exc:
        return name,[],type(exc).__name__

def collect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    stamp=now_ms()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda name:fetch(name,stamp),FEEDS))
    with closing(sqlite3.connect(path,timeout=5)) as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS articles(
            id TEXT PRIMARY KEY, source TEXT, title TEXT, url TEXT, published_ms INTEGER,
            first_seen_ms INTEGER, last_seen_ms INTEGER, tags TEXT);
            CREATE TABLE IF NOT EXISTS sources(name TEXT PRIMARY KEY,checked_ms INTEGER,
            success_ms INTEGER,error TEXT,item_count INTEGER);""")
        for name,items,error in results:
            db.execute("""INSERT INTO sources VALUES(?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET checked_ms=excluded.checked_ms,
                success_ms=CASE WHEN excluded.error IS NULL THEN excluded.success_ms ELSE sources.success_ms END,
                error=excluded.error,item_count=excluded.item_count""",
                (name,stamp,stamp if error is None else None,error,len(items)))
            for item in items:
                db.execute("""INSERT INTO articles VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title,last_seen_ms=excluded.last_seen_ms,
                    tags=excluded.tags""",
                    (item['id'],name,item['title'],item['url'],item['published_ms'],stamp,stamp,json.dumps(item['tags'])))
        db.execute('DELETE FROM articles WHERE published_ms < ?', (stamp-7*86400000,))
        db.commit()
    return snapshot(path)

def snapshot(path=DEFAULT_DB, stamp=None):
    stamp=now_ms() if stamp is None else stamp
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            sources=[{'name':n,'checked_at_ms':c,'success_at_ms':s,'error':e,'item_count':num,
                      'fresh':s is not None and 0<=stamp-s<=1800000}
                     for n,c,s,e,num in db.execute('SELECT * FROM sources ORDER BY name')]
            rows=db.execute('SELECT source,title,url,published_ms,tags FROM articles WHERE published_ms BETWEEN ? AND ? ORDER BY published_ms DESC LIMIT 30',
                            (stamp-86400000,stamp)).fetchall()
        articles=[];seen=set()
        for source,title,url,published,tags in rows:
            normalized=re.sub(r'\W+','',title.casefold())
            if normalized in seen:continue
            seen.add(normalized)
            articles.append({'source':source,'title':title,'url':url,'published_ms':published,'tags':json.loads(tags)})
        good=sum(s['fresh'] and not s['error'] for s in sources)
        status='current' if good==len(FEEDS) else 'partial' if good else 'stale'
        if status=='current' and not articles:status='no_recent_news'
        return {'status':status,'sources':sources,'articles':articles,'checked_at_ms':max((s['checked_at_ms'] for s in sources),default=None),
                'llm_connected':False,'advisory_only':True,'poll_minutes':15,
                'classification':'headline_keywords_not_sentiment','retention_days':7}
    except (sqlite3.Error,OSError,ValueError,TypeError):
        return {'status':'unavailable','sources':[],'articles':[],'llm_connected':False,'advisory_only':True}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',default=DEFAULT_DB)
    p.add_argument('--read',action='store_true')
    args=p.parse_args()
    result=snapshot(args.db) if args.read else collect(args.db)
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__': main()

