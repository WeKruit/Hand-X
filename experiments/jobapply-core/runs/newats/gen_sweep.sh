#!/usr/bin/env bash
cd "/Users/adam/Desktop/WeKruit/VALET & GH/Hand-X/.claude/worktrees/observe-act-generic/experiments/jobapply-core" || exit 1
until ! pgrep -f "[m]s-playwright/chromium" >/dev/null; do sleep 5; done
set -a; . ./.env; set +a
export OA_NO_SANDBOX=1 OA_COMPLETE_AGENT=0 OA_PROC_CAP_S=600 PYTHONUNBUFFERED=1 TIMEOUT_TypeTextEvent=8 TIMEOUT_BrowserStateRequestEvent=8 TIMEOUT_ClickElementEvent=8
mkdir -p runs/newats/gen
i=0
while IFS= read -r u; do
  i=$((i+1)); [ -z "$u" ] && continue
  [ -s "runs/newats/gen/$i.json" ] && continue
  pkill -9 -f "[m]s-playwright/chromium" 2>/dev/null; sleep 3
  .venv/bin/python oa_singlepage.py --url "$u" --generic --profile fixtures/rich_profile.json --resume fixtures/test_resume.pdf --json "runs/newats/gen/$i.json" --screenshot "runs/newats/gen/$i.png" > "runs/newats/gen/$i.log" 2>&1
  python3 -c "
import json
try:
 d=json.load(open('runs/newats/gen/$i.json'));c=d.get('completeness') or {};res=d.get('results') or []
 dn=sum(1 for e in res if e.get('outcome')=='DONE');es=sum(1 for e in res if e.get('outcome')=='ESCALATE')
 print('GEN $i ->', 'PASS' if c.get('complete') else d.get('status'),'| filled',dn,'esc',es,'| cost \$%.4f'%(d.get('cost') or 0),'| %ss'%(d.get('secs') or 0),'| rev',c.get('choice_reverted'))
except Exception as e: print('GEN $i -> ERR',e)"
done < runs/newats/gen_urls.txt
pkill -9 -f "[m]s-playwright/chromium" 2>/dev/null
.venv/bin/python scripts/gen_report.py runs/newats/gen gen_sweep_newurls
echo GEN_DONE
