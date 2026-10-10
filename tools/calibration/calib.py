#!/usr/bin/env python3
import zstandard, io, re, sys, json, urllib.request, math, collections
import os; exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'metrics.py')).read())
URL=sys.argv[1]; TARGET=int(sys.argv[2]); OUT=sys.argv[3]
req=urllib.request.urlopen(URL)
dctx=zstandard.ZstdDecompressor(max_window_size=2**31)
reader=io.TextIOWrapper(dctx.stream_reader(req),encoding='utf-8',errors='replace')
evre=re.compile(r'(\S+?)\s*\{ \[%eval (#?-?[\d.]+)\]')
tokre=re.compile(r'(\d+\.(?:\.\.)?\s*)?([^\s{}]+)\s*\{([^}]*)\}')
hdr={}; out=open(OUT,'w'); n=0; seen=0
def parse_ev(s):
    if s.startswith('#'): m=int(s[1:]); return 100000 if m>0 else -100000
    return int(round(float(s)*100))
for line in reader:
    if line.startswith('['):
        m=re.match(r'\[(\w+) "(.*)"\]',line); 
        if m: hdr[m.group(1)]=m.group(2)
        continue
    if line.startswith('1.'):
        seen+=1
        ev=hdr.get('Event','')
        if '[%eval' in line and ('Rapid' in ev or 'Classical' in ev or 'Blitz' in ev) and hdr.get('Termination')=='Normal' and 'Rated' in ev:
            toks=re.findall(r'([^\s{}]+)\s*\{([^}]*)\}', re.sub(r'(?<![\d.])\d+\.(?:\.\.)?(?=\s)','',line))
            evals=[]; ok=True; sans=[]
            for san,c in toks:
                m=re.search(r'%eval (#?-?[\d.]+)',c)
                sans.append(san)
                evals.append(parse_ev(m.group(1)) if m else None)
            # last move may lack eval if mate
            mm=re.search(r'#\s+(1-0|0-1)\s*$', line.strip())
            if mm: evals.append(100000 if mm.group(1)=='1-0' else -100000)
            if None in evals or len(evals)<20: ok=False
            if ok:
                # build plies: before/after mover POV, white POV evals; initial 15
                wpov=[15]+evals; plies=[]
                for i in range(len(evals)):
                    mover='w' if i%2==0 else 'b'; s=1 if mover=='w' else -1
                    plies.append(dict(ply=i+1,mover=mover,before=s*wpov[i],after=s*wpov[i+1],nlegal=2))
                try:
                    tc=hdr.get('TimeControl','')
                    for c,col in (('w','White'),('b','Black')):
                        su=summarize(plies,c); ga=game_acc(plies,c)
                        out.write(json.dumps(dict(elo=int(hdr.get(col+'Elo','0')),opp=int(hdr.get(('Black' if col=='White' else 'White')+'Elo','0')),ev=ev.split(' game')[0],tc=tc,n=su['n'],acpl=su['acpl'],I=su['I'],M=su['M'],B=su['B'],acc=ga))+'\n')
                    n+=1
                except Exception as e: pass
        hdr={}
        if n>=TARGET: break
print('games scanned',seen,'kept',n)
