"""auto_fix — the diagnose-and-brief half of the self-improve loop (user: '搭 auto-fix agent 骨架').

Today the loop is: capture(auto) → classify(auto) → rank(auto) → FIX(manual) → re-measure(auto).
This automates the diagnose→brief step so a fix-agent (next layer) can act without a human
reading every screenshot. It turns runs/failures/*.jsonl + per-field traces into a ranked list
of FIX BRIEFS: 'the biggest failure class is COMMIT on custom selects, N times, here are the
evidence PNGs, most likely oa_observe_act.py:_commit_from_options, try X.'

INGEST → CATEGORIZE (by trace stage) → CLUSTER → RANK (freq x burned-secs) → BRIEF.

Usage: python auto_fix.py               # brief from the default run dirs
       python auto_fix.py <results.json...>   # also fold in per-field traces from these

Read-only. Emits runs/auto_fix_briefs.json + a console table. No code is changed here — a brief
is the INPUT to the fix-agent, kept separate so a wrong auto-fix can never land unreviewed.
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# trace-fragment → (stage, symptom). Order matters: first match wins.
_STAGE_RULES: list[tuple[str, str, str]] = [
    (r"no-control", "OBSERVE", "locate found no control for the field"),
    (r"mark=-1|no Add control|apply click failed|named affordance.*not found", "ACTION", "affordance not located/clicked"),
    (r"Add clicked -> 0 new fields", "ACTION", "Add click produced no row"),
    (r"text-type-refused", "COMMIT", "text input refused the value (React controlled)"),
    (r"commit-failed|recommit-verdict:EMPTY|commit-cap", "COMMIT", "option/value did not commit or verify"),
    (r"select-filter|no-delta->search", "COMMIT", "custom dropdown filter/search did not resolve"),
    (r"HARD-FIELD-TIMEOUT|EXC:", "RUN", "field wall-clock timeout / exception"),
    (r"L1.fill=MISS", "OBSERVE", "every rung missed the field (locate/commit both failed)"),
    (r"L1.read_back=MISS|L3.read_back=MISS", "COMMIT", "filled but read-back could not confirm"),
    (r"form not reachable|no fillable fields|found no fillable", "REACH", "form not reached on the page"),
    (r"did not advance|STEP_STALLED", "RUN", "wizard step did not advance"),
]

# stage → the engine file+function most likely responsible (points the fix-agent)
_STAGE_OWNER = {
    "OBSERVE": "oa_perception.locate_field_tiered / oa_observe_act._s1_locate",
    "ACTION": "oa_repeater._visual_click_add / ats_engine._try_apply_click",
    "COMMIT": "oa_observe_act._commit_from_options / oa_cdp_action.cdp_type",
    "RUN": "oa_singlepage._fill_form (per-field wall clock) / ats_engine.set_job_deadline",
    "COVERAGE": "oa_complete.audit / oa_planner.plan_page (denominator + section detect)",
    "REACH": "ats_engine._try_apply_click / oa_singlepage generic reach (iframe-hop, apply, HITL)",
    "UNKNOWN": "inspect the evidence PNGs — add a _STAGE_RULES pattern",
}


def _stage_of(trace_str: str) -> tuple[str, str]:
    for pat, stage, symptom in _STAGE_RULES:
        if re.search(pat, trace_str, re.I):
            return stage, symptom
    return "UNKNOWN", trace_str[:60]


def _ingest_failures(fail_dir: Path) -> list[dict]:
    out = []
    fj = fail_dir / "failures.jsonl"
    if fj.exists():
        for line in fj.read_text().splitlines():
            with __import__("contextlib").suppress(Exception):
                r = json.loads(line)
                stage, symptom = _stage_of(str(r.get("reason", "")) + " " + str(r.get("extra", "")))
                # a BLOCKED page kind is a COVERAGE/REACH signal, not a field stage
                kind = (r.get("triage") or {}).get("kind", "")
                if kind in ("CAPTCHA_OR_ANTIBOT", "LOGIN_OR_VERIFY", "CAREERS_LANDING", "JOB_DESCRIPTION"):
                    stage, symptom = "REACH", f"page blocked: {kind}"
                out.append({"stage": stage, "symptom": symptom, "png": r.get("png"),
                            "secs": 0.0, "tag": r.get("tag", ""), "src": "failcap"})
    return out


def _ingest_traces(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        with __import__("contextlib").suppress(Exception):
            d = json.loads(Path(p).read_text())
            rows = d if isinstance(d, list) else [d]
            for rec in rows:
                for f in (rec.get("results") or []):
                    if f.get("outcome") in ("ESCALATE", "FAIL"):
                        stage, symptom = _stage_of(" ".join(f.get("trace") or []))
                        out.append({"stage": stage, "symptom": symptom, "png": rec.get("screenshot"),
                                    "secs": 0.0, "tag": f.get("label", "")[:30], "src": "trace"})
    return out


def _ingest_corpus(corpus: Path) -> list[dict]:
    out = []
    if corpus.exists():
        for line in corpus.read_text().splitlines():
            with __import__("contextlib").suppress(Exception):
                r = json.loads(line)
                if r.get("success") is False:
                    out.append({"stage": "COMMIT", "symptom": "L3 agent could not complete field",
                                "png": None, "secs": r.get("secs") or 0.0, "tag": r.get("tag", ""), "src": "l3"})
    return out


def main(trace_paths: list[str]) -> None:
    base = Path("runs")
    fails = _ingest_failures(base / "failures") + _ingest_corpus(base / "l3_learn" / "corpus.jsonl") + _ingest_traces(trace_paths)
    if not fails:
        print("no failures captured yet — run some fills first (failcap writes runs/failures/failures.jsonl)")
        return

    clusters: dict[tuple[str, str], dict] = defaultdict(lambda: {"count": 0, "secs": 0.0, "pngs": [], "tags": set()})
    for f in fails:
        k = (f["stage"], f["symptom"])
        c = clusters[k]
        c["count"] += 1
        c["secs"] += f.get("secs") or 0.0
        if f.get("png"):
            c["pngs"].append(f["png"])
        c["tags"].add(f["tag"])

    briefs = []
    for (stage, symptom), c in sorted(clusters.items(), key=lambda kv: -(kv[1]["count"] + kv[1]["secs"] / 60)):
        briefs.append({
            "stage": stage, "symptom": symptom, "count": c["count"], "burned_secs": round(c["secs"], 1),
            "likely_owner": _STAGE_OWNER.get(stage, "?"),
            "evidence_pngs": c["pngs"][:3], "example_fields": sorted(c["tags"])[:5],
        })

    (base / "auto_fix_briefs.json").write_text(json.dumps(briefs, indent=2))
    print(f"=== FIX BRIEFS (ranked, {len(fails)} failures -> {len(briefs)} classes) ===")
    print(f"{'stage':<9}{'count':>6}{'burn_s':>8}  symptom / likely-owner")
    for b in briefs:
        print(f"{b['stage']:<9}{b['count']:>6}{b['burned_secs']:>8}  {b['symptom'][:46]}")
        print(f"{'':<23}  -> fix in {b['likely_owner']}")
    print(f"\nwrote {base / 'auto_fix_briefs.json'}  (each brief = one generic fix for a fix-agent to write)")


if __name__ == "__main__":
    main(sys.argv[1:])
