"""l3_promote — mine the L3 escalation corpus for L1-promotion candidates (the auto-improve half
of the loop: 'download the script from browser use for our escalate & auto improve').

Reads runs/l3_learn/corpus.jsonl (written by ats_engine.capture_l3_history at every agent run) and
groups records by normalized tag. Any tag seen >= 2 times is, by definition, a hole in L1/L2 that
keeps costing an agent run — the report ranks them by (count x avg_secs) so the top line is always
the single most valuable generic fix. For each group it prints the dominant action sequence of the
SUCCESSFUL runs — that sequence is the promotion recipe (e.g. dateSignedOn: input,input,input =
'split the date into its 3 sub-inputs' -> became an L1 rule).

Usage: python l3_promote.py [corpus.jsonl]
"""

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


def _norm_tag(tag: str) -> str:
    """Collapse per-run noise so repeats group: strip repair error-text tails to their field word."""
    t = re.sub(r"^(repair_Errors?_Found(Error_)?|repair_Error_)", "repair_", tag)
    return t[:44]


def main(path: str) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for line in Path(path).read_text().splitlines():
        r = json.loads(line)
        if not r.get("actions"):
            continue  # empty capture (agent died before acting) — no learn signal
        groups[_norm_tag(r["tag"])].append(r)

    rows = []
    for tag, rs in groups.items():
        oks = [r for r in rs if r.get("success")]
        secs = [r.get("secs") or 0 for r in rs]
        # promotion recipe = most common action-sequence prefix among SUCCESSFUL runs
        recipe = ""
        if oks:
            seqs = Counter(",".join(a["name"] for a in r["actions"][:6]) for r in oks)
            recipe = seqs.most_common(1)[0][0]
        rows.append({
            "tag": tag, "n": len(rs), "ok": len(oks), "avg_secs": sum(secs) / len(secs),
            "burn": sum(secs), "recipe": recipe,
        })

    rows.sort(key=lambda r: -r["burn"])
    print(f"=== L3 -> L1 PROMOTION CANDIDATES ({path}) ===")
    print(f"{'tag':<44}{'n':>3}{'ok':>3}{'avg_s':>7}{'burn_s':>8}  successful recipe (first 6 actions)")
    for r in rows:
        flag = " <== PROMOTE" if r["n"] >= 2 else ""
        print(f"{r['tag']:<44}{r['n']:>3}{r['ok']:>3}{r['avg_secs']:>7.0f}{r['burn']:>8.0f}  {r['recipe']}{flag}")
    total = sum(r["burn"] for r in rows)
    repeat = sum(r["burn"] for r in rows if r["n"] >= 2)
    print(f"\ntotal agent seconds: {total:.0f} | in repeated tags (promotable): {repeat:.0f} ({repeat / max(total, 1):.0%})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "runs/l3_learn/corpus.jsonl")
