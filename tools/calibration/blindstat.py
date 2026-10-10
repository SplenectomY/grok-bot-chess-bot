#!/usr/bin/env python3
import json,glob,re,sys
import os; exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'metrics.py')).read())
for tag in sys.argv[1:]:
    bt=0;bchosen=0;tot=0; bj={}; srcj={}; blindvals=[]
    for f in glob.glob(tag+'/g*.scored.json'):
        d=json.load(open(f)); g=d['game']; P={p['ply']:p for p in d['plies']}
        for r in g['recs']:
            tot+=1; p=P[r['ply']]; j=judge(p['before'],p['after'])
            ev=' '.join(r['events'] or [])
            k=r['source']
            if 'blind-turn' in ev:
                bt+=1
                m=re.search(r"blind-turn: refutation overlooked for \[(.*?)\]",ev)
                items=[x.strip("' ") for x in m.group(1).split(',')] if m and m.group(1) else []
                blindvals+= [float(x.split('~')[1]) for x in items if '~' in x]
                if r['san'] in [x.split('~')[0] for x in items]:
                    bchosen+=1; k='blind'
            srcj.setdefault(k,{}); srcj[k][j]=srcj[k].get(j,0)+1
    import statistics
    print(tag,'moves',tot,'blindTurns',bt,'blindChosen',bchosen, 'median blind loss',statistics.median(blindvals) if blindvals else None)
    for k,v in srcj.items(): print('  ',k,v)
