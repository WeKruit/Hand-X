import json, glob, os, re, collections
VOL = re.compile(r'veteran|race|ethnic|hispanic|disab|sexual orientation|transgender|self-identif|__system', re.I)
P = collections.defaultdict(lambda: {'has':0,'done':0})
fired = 0
for j in glob.glob('runs/newats/freshmatrix3/*.json'):
    prof = os.path.basename(j).split('_',1)[1].rsplit('.',1)[0]
    try: d = json.load(open(j))
    except: continue
    if d.get('page_kind')=='NOT_FOUND' or d.get('fill_rate')==0.0: continue
    a = P[prof]
    for r in d.get('results', []):
        if any('choice-btn-by-label' in str(t) for t in (r.get('trace') or [])): fired += 1
        if VOL.search(str(r.get('label',''))): continue
        if str(r.get('value') or '').strip():
            a['has'] += 1; a['done'] += (r.get('outcome')=='DONE')
base = {'rich_profile':95.4,'rich_profile2':96.2,'profile_intl':95.6,'profile_minimal':94.6,'profile_veteran':96.0}
print('choice-btn-by-label fired:', fired)
allok = True
for prof in sorted(P):
    a=P[prof]; e=100*a['done']/a['has'] if a['has'] else 0; b=base.get(prof,0)
    ok = '✅' if e>=95 else 'BELOW'; allok = allok and e>=95
    print(f'  {prof:16} {e:5.1f}%  (n={a["has"]})  vs {b} ({"+" if e-b>=0 else ""}{e-b:.1f})  {ok}')
print('ALL 5 PROFILES >=95%:', allok)
