import json, math, glob, statistics as st, sys
CEIL=1000
def clamp(v): return max(-CEIL,min(CEIL,v))
def wc(cp): return 2/(1+math.exp(-0.00368208*cp))-1
def wp(cp): return 50+50*wc(clamp(cp))
def acc(b,a):
    if a>=b: return 100.0
    return max(0,min(100,103.1668100711649*math.exp(-0.04354415386753951*(b-a))-3.166924740191411+1))
def ismate(v): return abs(v)>=50000
def judge(before,after):
    # mover POV
    if ismate(before) or ismate(after):
        if not ismate(before) and ismate(after) and after<0:  # mate created against mover
            return 'I' if before<-999 else 'M' if before<-700 else 'B'
        if ismate(before) and before>0 and (not ismate(after) or after<0):
            a=after if not ismate(after) else 0
            return 'I' if a>999 else 'M' if a>700 else 'B'
        return None
    d=wc(before)-wc(after)
    return 'B' if d>=.3 else 'M' if d>=.2 else 'I' if d>=.1 else None
def game_acc(plies, color):
    # lichess gameAccuracy: volatility-weighted mean + harmonic mean
    cps=[clamp(p['after']) * (1 if p['mover']=='w' else -1) for p in plies]  # white POV after each ply
    allwp=[wp(15)]+[wp(c) for c in cps]   # lichess initial eval ~ +15? see note
    n=len(cps); ws=max(2,min(8,n//10))
    windows=[allwp[:ws]]*(min(ws,len(allwp))-2)+[allwp[i:i+ws] for i in range(len(allwp)-ws+1)]
    weights=[max(0.5,min(12,st.pstdev(w))) for w in windows]
    accs=[]
    for i in range(n):
        mover='w' if i%2==0 else 'b'
        if mover!=color: continue
        p,q=allwp[i],allwp[i+1]
        a = acc(p,q) if mover=='w' else acc(100-p,100-q)
        accs.append((a,weights[i]))
    wm=sum(a*w for a,w in accs)/sum(w for a,w in accs)
    hm=len(accs)/sum(1/max(a,1e-9) for a,_ in accs)
    return (wm+hm)/2
def summarize(plies, color, skip_plies=0):
    mv=[p for p in plies if p['mover']==color and p['nlegal']>1 and p['ply']>skip_plies]
    cpl=[max(0,clamp(p['before'])-clamp(p['after'])) for p in mv]
    j=[judge(p['before'],p['after']) for p in mv]
    return dict(n=len(mv), acpl=sum(cpl)/len(cpl) if cpl else None, I=j.count('I'),M=j.count('M'),B=j.count('B'), cpl=cpl, moves=mv, j=j)
