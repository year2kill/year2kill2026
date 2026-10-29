#!/usr/bin/env python3
from __future__ import annotations

import json,re,sys,unicodedata
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin,urlparse

import feedparser,requests
from bs4 import BeautifulSoup
from ftfy import fix_text

ROOT=Path(__file__).resolve().parents[1]
CONFIG_PATH=ROOT/'sources.json'
WIRE_PATH=ROOT/'wire.json'
HEADERS={'User-Agent':'Mozilla/5.0 (compatible; Y2KWire/2.0; +https://year2kill.github.io/year2kill2026/)'}
CHICAGO_TERMS={'chicago','cook county','cta','south side','west side','north side','loop','bronzeville','hyde park','englewood','wicker park','logan square','illinois','chicago police','cpd','chicagoland'}
CRIME_TERMS={'shot','shooting','stabbed','stabbing','killed','murder','homicide','robbery','robbed','arrested','arrest','police','crime','charged','carjacking','burglary','theft','dead','death'}
LOW_VALUE={'horoscope','shopping guide','coupon','deal of the day','newsletter','sponsored','advertisement','gift guide','quiz'}

@dataclass
class Story:
    headline:str; url:str; source:str; category:str; published:str; score:float=0.0

def now_utc(): return datetime.now(timezone.utc)

def repair(value):
    text=fix_text('' if value is None else str(value))
    text=unicodedata.normalize('NFC',text).replace('\ufffd','')
    text=''.join(ch for ch in text if ch in '\t\n\r' or unicodedata.category(ch)[0] != 'C')
    return re.sub(r'\s+',' ',text).strip()

def clean(title,max_chars=118):
    title=repair(title)
    title=re.sub(r'^(watch|video|photos?|exclusive|breaking):\s*','',title,flags=re.I)
    title=re.sub(r'\s+\|\s+[^|]{2,50}$','',title)
    title=re.sub(r'\bEverything You Need to Know\b|\bHere\'s What We Know\b','',title,flags=re.I)
    title=repair(title).strip(' -–—:')
    if len(title)>max_chars:
        title=title[:max_chars+1].rsplit(' ',1)[0].rstrip(' ,;:-')+'…'
    return repair(title).upper()

def safe_dt(value):
    if not value:return None
    try: dt=parsedate_to_datetime(str(value))
    except Exception:
        try: dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        except Exception:return None
    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def fetch_rss(src):
    r=requests.get(src['url'],headers=HEADERS,timeout=20); r.raise_for_status()
    feed=feedparser.parse(r.content); out=[]
    for e in feed.entries[:80]:
        title,url=repair(e.get('title','')),repair(e.get('link',''))
        if not title or not url: continue
        if e.get('published_parsed'): dt=datetime(*e.published_parsed[:6],tzinfo=timezone.utc)
        elif e.get('updated_parsed'): dt=datetime(*e.updated_parsed[:6],tzinfo=timezone.utc)
        else: dt=safe_dt(e.get('published') or e.get('updated'))
        out.append(Story(title,url,src['name'],src['category'],(dt or now_utc()).isoformat()))
    return out

def fetch_html(src):
    r=requests.get(src['url'],headers=HEADERS,timeout=20); r.raise_for_status()
    enc=r.apparent_encoding or r.encoding or 'utf-8'
    soup=BeautifulSoup(fix_text(r.content.decode(enc,errors='replace')),'html.parser')
    source_host=urlparse(src['url']).netloc.lower().removeprefix('www.')
    out=[]; seen=set()
    for sel in src.get('selectors',['article a','h2 a','h3 a']):
        for a in soup.select(sel):
            title=repair(a.get_text(' ',strip=True)); href=a.get('href')
            if not title or not href or len(title)<20: continue
            url=urljoin(src['url'],href); host=urlparse(url).netloc.lower().removeprefix('www.')
            if host and source_host and not(host==source_host or host.endswith('.'+source_host)): continue
            if url in seen: continue
            seen.add(url); out.append(Story(title,url,src['name'],src['category'],now_utc().isoformat()))
            if len(out)>=60:return out
    return out

def relevant(story,src):
    text=story.headline.casefold()
    if any(t in text for t in LOW_VALUE): return False
    if src.get('require_chicago') and not any(t in text for t in CHICAGO_TERMS): return False
    if src.get('require_crime') and not any(t in text for t in CRIME_TERMS): return False
    return True

def age_ok(story,hours):
    dt=safe_dt(story.published)
    return True if dt is None else now_utc()-dt<=timedelta(hours=hours)

def fingerprint(title):
    stop={'the','a','an','to','of','and','in','on','for','with','at','is','are','from','by','as','new'}
    return {w for w in re.findall(r'[a-z0-9]+',repair(title).casefold()) if len(w)>2 and w not in stop}

def similarity(a,b):
    aa,bb=fingerprint(a.headline),fingerprint(b.headline)
    return 0 if not aa or not bb else len(aa&bb)/len(aa|bb)

def dedupe(stories):
    kept=[]
    for s in stories:
        if any(s.url==k.url or similarity(s,k)>=.56 for k in kept): continue
        kept.append(s)
    return kept

def score(story):
    dt=safe_dt(story.published) or now_utc(); age=max(0,(now_utc()-dt).total_seconds()/3600)
    return max(0,100-1.5*age)+(8 if 35<=len(story.headline)<=105 else 0)

def interleave(config,pools,total):
    weights=config['weights']; cats=list(weights); wanted={c:total*weights[c]/100 for c in cats}
    targets={c:min(int(wanted[c]),len(pools.get(c,[]))) for c in cats}; remaining=total-sum(targets.values())
    while remaining>0:
        candidates=[c for c in cats if targets[c]<len(pools.get(c,[]))]
        if not candidates: break
        c=max(candidates,key=lambda x:(wanted[x]-targets[x],weights[x])); targets[c]+=1; remaining-=1
    used={c:0 for c in cats}; out=[]
    while len(out)<total:
        candidates=[c for c in cats if used[c]<targets[c]]
        if not candidates: break
        c=min(candidates,key=lambda x:(used[x]/max(targets[x],1))-weights[x]/100)
        out.append(pools[c][used[c]]); used[c]+=1
    return out

def main():
    config=json.loads(CONFIG_PATH.read_text(encoding='utf-8')); gathered=[]; errors=[]
    for src in config['sources']:
        if not src.get('enabled',True): continue
        try:
            items=fetch_rss(src) if src['type']=='rss' else fetch_html(src)
            accepted=[i for i in items if relevant(i,src)]; gathered.extend(accepted)
            print(f"[ok] {src['name']}: {len(items)} fetched / {len(accepted)} accepted")
        except Exception as exc:
            msg=f"{src['name']}: {type(exc).__name__}: {exc}"; errors.append(repair(msg)); print('[warn] '+msg,file=sys.stderr)
    filtered=[]
    for s in gathered:
        cfg=config['categories'][s.category]
        if not age_ok(s,cfg['max_age_hours']): continue
        s.headline=clean(s.headline)
        if not s.headline: continue
        s.source=repair(s.source); s.score=score(s); filtered.append(s)
    filtered=dedupe(sorted(filtered,key=lambda s:s.score,reverse=True))
    pools={c:[s for s in filtered if s.category==c][:cfg['pool_limit']] for c,cfg in config['categories'].items()}
    final=interleave(config,pools,int(config['story_limit']))
    if len(final)<int(config.get('minimum_publish_count',8)):
        print(f"Only {len(final)} qualifying stories; preserving existing wire.json.",file=sys.stderr); return 0
    output={'version':2,'updated':now_utc().isoformat(),'story_count':len(final),'stories':[{'headline':repair(s.headline),'url':s.url,'source':repair(s.source),'category':s.category,'published':s.published} for s in final],'source_errors':errors[:20]}
    tmp=WIRE_PATH.with_suffix('.json.tmp'); tmp.write_text(json.dumps(output,indent=2,ensure_ascii=False)+'\n',encoding='utf-8'); tmp.replace(WIRE_PATH)
    print(f"Wrote {len(final)} stories to {WIRE_PATH}")
    return 0

if __name__=='__main__': raise SystemExit(main())
