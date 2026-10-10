#!/usr/bin/env python3
import json,statistics as st,collections,sys
rows=[json.loads(l) for l in open(sys.argv[1] if len(sys.argv) > 1 else "calib.jsonl")]
def band(e): return (e//100)*100
for cls in (('Rapid','Classical'),('Blitz',)):
    sel=[r for r in rows if any(c in r['ev'] for c in cls) and r['n']>=10]
    print('==',cls,len(sel),'player-games')
    g=collections.defaultdict(list)
    for r in sel: g[band(r['elo'])].append(r)
    print('band   N   meanACPL medACPL meanAcc medAcc  I/100mv M/100mv B/100mv  I/g M/g B/g')
    for b in sorted(g):
        L=g[b]
        if len(L)<50: continue
        mv=sum(r['n'] for r in L)
        print(f"{b:5d} {len(L):5d} {st.mean(r['acpl'] for r in L):7.1f} {st.median(r['acpl'] for r in L):7.1f} {st.mean(r['acc'] for r in L):7.1f} {st.median(r['acc'] for r in L):6.1f} {100*sum(r['I'] for r in L)/mv:7.2f} {100*sum(r['M'] for r in L)/mv:7.2f} {100*sum(r['B'] for r in L)/mv:7.2f} {st.mean(r['I'] for r in L):4.1f} {st.mean(r['M'] for r in L):4.1f} {st.mean(r['B'] for r in L):4.1f}")
