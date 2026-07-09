import json, glob, os, re, collections

VOL = re.compile(r'veteran|race|ethnic|hispanic|disab|sexual orientation|transgender|self-identif|__system', re.I)

btn_fired = 0
sample = None          # a run+field where btn-by-label committed, to eyeball
sog = 0                # S_OTHER_GUARD remaining
cc = 0                 # commit-cap remaining
cohort_has = 0
cohort_done = 0

for lg in glob.glob('runs/newats/failcohort/*.log'):
    try:
        txt = open(lg, errors='ignore').read()
    except Exception:
        continue
    n = txt.count('choice-btn-by-label')
    btn_fired += n
    if n and sample is None:
        sample = lg[:-4] + '.png'

for j in glob.glob('runs/newats/failcohort/*.json'):
    try:
        d = json.load(open(j))
    except Exception:
        continue
    if d.get('page_kind') == 'NOT_FOUND' or d.get('fill_rate') == 0.0:
        continue
    for r in d.get('results', []):
        tr = r.get('trace') or []
        if any('S_OTHER_GUARD' in str(t) for t in tr) and r.get('outcome') != 'DONE':
            sog += 1
        if any('commit-cap' in str(t) for t in tr) and r.get('outcome') != 'DONE':
            cc += 1
        if VOL.search(str(r.get('label', ''))):
            continue
        if str(r.get('value') or '').strip():
            cohort_has += 1
            cohort_done += (r.get('outcome') == 'DONE')

print(f'choice-btn-by-label FIRED (committed a pill): {btn_fired}')
print(f'remaining escalations: S_OTHER_GUARD={sog}  commit-cap={cc}')
eng = 100 * cohort_done / cohort_has if cohort_has else 0
print(f'cohort engine-completion (req w/ value): {eng:.1f}%  (n={cohort_has})')
if sample:
    print(f'EYEBALL this (btn-by-label committed a pill): {sample}')
