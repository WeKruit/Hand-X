import json, glob, os, re, collections
VOL = re.compile(r'veteran|race|ethnic|hispanic|disab|sexual orientation|transgender|self-identif|__system', re.I)
P = collections.defaultdict(lambda: {'has':0,'done':0})
repair = 0; residual_dd_ashby = 0
for j in glob.glob('runs/newats/freshmatrix4/*.json'):
    prof = os.path.basename(j).split('_',1)[1].rsplit('.',1)[0]
    try: d = json.load(open(j))
    except: continue
    ashby = 'ashbyhq' in (d.get('url') or '')
    if d.get('page_kind')=='NOT_FOUND' or d.get('fill_rate')==0.0: continue
    a = P[prof]
    for r in d.get('results', []):
        tr = r.get('trace') or []
        if any('choice-btn-repair' in str(t) for t in tr): repair += 1
        if r.get('outcome')=='DONE' and ashby and any('choice-dom-direct' in str(t) for t in tr): residual_dd_ashby += 1
        if VOL.search(str(r.get('label',''))): continue
        if str(r.get('value') or '').strip():
            a['has'] += 1; a['done'] += (r.get('outcome')=='DONE')
prev = {'rich_profile':97.9,'rich_profile2':96.9,'profile_intl':97.4,'profile_minimal':98.1,'profile_veteran':96.3}
print(f'choice-btn-repair fired (false-greens fixed to real button-click): {repair}')
print(f'residual choice-dom-direct DONE on ashby (still hidden-input, potential false-green): {residual_dd_ashby}')
allok=True
for prof in sorted(P):
    a=P[prof]; e=100*a['done']/a['has'] if a['has'] else 0; pv=prev.get(prof,0)
    ok='✅' if e>=95 else 'BELOW'; allok=allok and e>=95
    print(f'  {prof:16} {e:5.1f}%  (n={a["has"]})  was(inflated) {pv} ({"+" if e-pv>=0 else ""}{e-pv:.1f})  {ok}')
print('ALL 5 >=95% (CORRECTED, repair engine):', allok)
