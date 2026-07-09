#!/usr/bin/env python3
"""Assemble the harsh playground from workflow-authored fixtures.

Input : harsh_fixtures.json  = [{id,kind,label,profile_value,expected,why_hard,html}, ...]
Output: playground.html          (page shell + every fixture fragment; fixtures carry their own inline JS)
        playground_values.json    ({normalized_label: profile_value}) for OA_FIXTURE_VALUES injection
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "harsh_fixtures.json")

SHELL_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Harsh Playground — offline fill fixtures</title>
<style>
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:720px;margin:24px auto;color:#111}
  .field{margin:30px 0;padding-bottom:8px;border-bottom:1px solid #eee}
  label,.q,legend{font-weight:600}
  input[type=text],input[type=email],input[type=tel],input[type=url],input[type=number],textarea,select{padding:8px;border:1px solid #bbb;border-radius:6px;font-size:15px}
  button{cursor:pointer}
  h1{font-size:20px}
  table{border-collapse:collapse}
  th,td{padding:6px 10px;text-align:left;vertical-align:top}
</style></head>
<body>
<h1>Harsh Playground</h1>
<form id="app">
"""
SHELL_TAIL = "\n</form>\n</body></html>\n"


def _norm(s):
    return " ".join(str(s or "").lower().replace("*", " ").replace("?", " ").split())


def main():
    fixtures = json.load(open(FIX))
    parts = [SHELL_HEAD]
    values = {}
    seen_kind = set()
    for i, fx in enumerate(fixtures):
        k = fx.get("kind") or f"fx{i}"
        if k in seen_kind:
            k = f"{k}_{i}"  # keep data-kind unique so the painted-dump maps 1:1
        seen_kind.add(k)
        html = fx.get("html", "")
        # ensure the emitted fragment's data-kind is the de-duped k (best-effort: only if it differs)
        if fx.get("kind") and k != fx.get("kind"):
            html = html.replace(f'data-kind="{fx["kind"]}"', f'data-kind="{k}"', 1)
        parts.append(f"\n<!-- {fx.get('id','')} | {fx.get('why_hard','')} -->\n{html}\n")
        if fx.get("label") and fx.get("profile_value") is not None:
            values[_norm(fx["label"])] = str(fx["profile_value"])
    parts.append(SHELL_TAIL)
    open(os.path.join(HERE, "playground.html"), "w").write("".join(parts))
    json.dump(values, open(os.path.join(HERE, "playground_values.json"), "w"), indent=1)
    print(f"built playground.html with {len(fixtures)} fixtures; {len(values)} injected values")


if __name__ == "__main__":
    main()
