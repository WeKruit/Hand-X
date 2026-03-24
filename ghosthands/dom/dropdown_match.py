"""Reusable dropdown option matching — single source of truth for DomHand.

Matching passes (parity with in-page JS, GHOST-HANDS ``clickMatchingOption``):

+-------+---------------------------+-----------------------------------------------+
| Pass  | Rule                      | Rationale                                     |
+-------+---------------------------+-----------------------------------------------+
| 1     | Exact normalized match    | Ideal path; no ambiguity.                     |
| 2     | Prefix (either direction) | "Asian" prefix of "Asian (Not Hispanic…)".    |
| 3     | Contains (fwd + reverse)  | "Man" inside longer target "Male".            |
| 4     | Synonym equivalence       | "Male" ↔ "Man", "Decline" ↔ "Prefer not …".  |
| 5     | Word overlap (≥1 shared)  | "Computer Science" vs "Comp. Sci. & Eng.".    |
+-------+---------------------------+-----------------------------------------------+

Both the Python ``match_dropdown_option`` and the companion browser JS
``CLICK_DROPDOWN_OPTION_ENHANCED_JS`` implement the same ordered passes so that
behaviour in ``domhand_fill``, ``domhand_select`` (CDP), and in-page clicks stays
consistent.  When adding a new pass, add it in all three places.
"""

from __future__ import annotations

import re

from ghosthands.actions.views import normalize_name

_STOP_WORDS = frozenset({"the", "a", "an", "of", "for", "in", "to", "and", "or", "with", "at", "by"})

# ── Synonym groups ────────────────────────────────────────────────────
# Each frozenset contains **normalized** (lowercase, stripped) phrases that
# should be treated as semantically identical for dropdown matching.
# Keep groups tight to avoid false positives.

SYNONYM_GROUPS: list[frozenset[str]] = [
    # Gender
    frozenset({"male", "man", "m", "masculine"}),
    frozenset({"female", "woman", "f", "feminine"}),
    frozenset({"non-binary", "nonbinary", "non binary", "genderqueer", "gender non-conforming", "gender non conforming"}),
    # Decline / prefer-not-to-say
    frozenset({
        "i decline to self-identify",
        "i decline to self identify",
        "i dont wish to answer",
        "i do not wish to answer",
        "i don't wish to answer",
        "prefer not to say",
        "i prefer not to say",
        "prefer not to answer",
        "i prefer not to answer",
        "i prefer to not describe",
        "decline to answer",
        "decline to self-identify",
        "decline to self identify",
        "decline",
    }),
    # Disability (common dropdown phrasing)
    frozenset({
        "no",
        "no disability",
        "i do not have a disability",
        "i do not have a disability or chronic condition",
        "no i dont have a disability",
        "no i don't have a disability",
        "none",
    }),
    frozenset({
        "yes",
        "i have a disability",
        "i have a disability or chronic condition",
        "yes i have a disability",
    }),
    # Veteran
    frozenset({"i am not a protected veteran", "i am not a veteran", "not a veteran", "non-veteran", "non veteran"}),
    frozenset({"i identify as one or more of the classifications of protected veteran", "protected veteran", "veteran"}),
]

_SYNONYM_INDEX: dict[str, int] | None = None


def _get_synonym_index() -> dict[str, int]:
    global _SYNONYM_INDEX
    if _SYNONYM_INDEX is None:
        idx: dict[str, int] = {}
        for group_id, group in enumerate(SYNONYM_GROUPS):
            for phrase in group:
                idx[normalize_name(phrase)] = group_id
        _SYNONYM_INDEX = idx
    return _SYNONYM_INDEX


def are_synonyms(a: str, b: str) -> bool:
    """True when *a* and *b* belong to the same synonym group."""
    index = _get_synonym_index()
    a_id = index.get(normalize_name(a))
    b_id = index.get(normalize_name(b))
    if a_id is None or b_id is None:
        return False
    return a_id == b_id


def _meaningful_words(text: str) -> set[str]:
    return {w for w in normalize_name(text).split() if len(w) > 1 and w not in _STOP_WORDS}


_PHONE_CODE_SUFFIX_NORM_RE = re.compile(r"\s*\+?\d{1,4}\s*$")


def _strip_phone_code_norm(norm_text: str) -> str:
    """Remove trailing phone-code digits from a normalized country name.

    After normalization, "+1" becomes "1", "+44" becomes "44".
    """
    return _PHONE_CODE_SUFFIX_NORM_RE.sub("", norm_text).strip()


# ── Core matching API ─────────────────────────────────────────────────

def match_dropdown_option(
    target: str,
    options: list[str],
) -> str | None:
    """Return the best matching option label for *target*, or ``None``.

    *options* is a list of **visible option labels** (raw text from the page).
    The function implements the 5-pass cascade documented at module level.
    """
    target_norm = normalize_name(target)
    if not target_norm:
        return None

    normed_opts = [(opt, normalize_name(opt)) for opt in options]

    # Pass 1: Exact
    for opt, opt_norm in normed_opts:
        if opt_norm == target_norm:
            return opt

    # Pass 1.5: Phone code stripping — "United States +1" matches "United States"
    target_stripped = _strip_phone_code_norm(target_norm)
    for opt, opt_norm in normed_opts:
        opt_stripped = _strip_phone_code_norm(opt_norm)
        if opt_stripped == target_stripped and opt_stripped:
            return opt

    # Pass 2: Prefix (either direction)
    for opt, opt_norm in normed_opts:
        if not opt_norm:
            continue
        if opt_norm.startswith(target_norm) or target_norm.startswith(opt_norm):
            return opt

    # Pass 3: Contains (forward + reverse — GH pass 1+2)
    for opt, opt_norm in normed_opts:
        if not opt_norm:
            continue
        if target_norm in opt_norm or (len(opt_norm) > 2 and opt_norm in target_norm):
            return opt

    # Pass 4: Synonym
    for opt, opt_norm in normed_opts:
        if are_synonyms(target_norm, opt_norm):
            return opt

    # Pass 5: Word overlap (at least 1 shared meaningful word)
    target_words = _meaningful_words(target)
    if target_words:
        best_score = 0
        best_opt: str | None = None
        for opt, opt_norm in normed_opts:
            opt_words = _meaningful_words(opt)
            overlap = len(target_words & opt_words)
            if overlap > best_score:
                best_score = overlap
                best_opt = opt
        if best_opt is not None and best_score >= 1:
            return best_opt

    return None


def match_dropdown_option_dict(
    target: str,
    options: list[dict[str, str]],
    *,
    text_key: str = "text",
) -> dict[str, str] | None:
    """Same as ``match_dropdown_option`` but operates on dicts (``domhand_select`` shape).

    Returns the matched dict or ``None``.
    """
    labels = [str(opt.get(text_key) or opt.get("value") or "") for opt in options]
    matched_label = match_dropdown_option(target, labels)
    if matched_label is None:
        return None
    for opt, label in zip(options, labels):
        if label == matched_label:
            return opt
    return None


# ── In-page JS with same pass ordering ───────────────────────────────

CLICK_DROPDOWN_OPTION_ENHANCED_JS = r"""(text, synonymGroups) => {
    var lowerText = text.toLowerCase().trim();
    function qAll(sel) {
        if (window.__ff && window.__ff.queryAll) return window.__ff.queryAll(sel);
        return Array.from(document.querySelectorAll(sel));
    }
    var roleEls = qAll('[role="option"], [role="menuitem"], [role="treeitem"], [role="listitem"], [data-automation-id*="promptOption"], [data-automation-id*="selectOption"], [data-automation-id*="menuItem"]');
    var opts = [];
    for (var i = 0; i < roleEls.length; i++) {
        var o = roleEls[i];
        var rect = o.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        var t = (o.textContent || '').trim();
        if (t) opts.push({el: o, text: t, lower: t.toLowerCase().trim()});
    }
    // Pass 1: Exact
    for (var a = 0; a < opts.length; a++) {
        if (opts[a].lower === lowerText) { opts[a].el.click(); return JSON.stringify({clicked: true, text: opts[a].text, pass: 1}); }
    }
    // Pass 2: Prefix
    for (var b = 0; b < opts.length; b++) {
        var ol = opts[b].lower;
        if (ol.startsWith(lowerText) || lowerText.startsWith(ol)) { opts[b].el.click(); return JSON.stringify({clicked: true, text: opts[b].text, pass: 2}); }
    }
    // Pass 3: Contains (forward + reverse)
    for (var c = 0; c < opts.length; c++) {
        var ol2 = opts[c].lower;
        if (lowerText.indexOf(ol2) !== -1 || ol2.indexOf(lowerText) !== -1) { opts[c].el.click(); return JSON.stringify({clicked: true, text: opts[c].text, pass: 3}); }
    }
    // Pass 4: Synonym groups (each group is an array of lowercase strings)
    if (synonymGroups && synonymGroups.length > 0) {
        var targetGroup = -1;
        for (var g = 0; g < synonymGroups.length && targetGroup < 0; g++) {
            for (var gi = 0; gi < synonymGroups[g].length; gi++) {
                if (synonymGroups[g][gi] === lowerText) { targetGroup = g; break; }
            }
        }
        if (targetGroup >= 0) {
            for (var d = 0; d < opts.length; d++) {
                var grp = synonymGroups[targetGroup];
                for (var si = 0; si < grp.length; si++) {
                    if (grp[si] === opts[d].lower) { opts[d].el.click(); return JSON.stringify({clicked: true, text: opts[d].text, pass: 4}); }
                }
            }
        }
    }
    // Pass 5: Word overlap
    var stopW = {the:1,a:1,an:1,of:1,for:1,in:1,to:1,and:1,or:1};
    var tw = lowerText.split(/\s+/).filter(function(w){return w.length > 1 && !stopW[w];});
    if (tw.length > 0) {
        var bestIdx = -1, bestScore = 0;
        for (var e = 0; e < opts.length; e++) {
            var ow = opts[e].lower.split(/\s+/).filter(function(w){return w.length > 1 && !stopW[w];});
            var score = 0;
            for (var wi = 0; wi < tw.length; wi++) { if (ow.indexOf(tw[wi]) !== -1) score++; }
            if (score > bestScore) { bestScore = score; bestIdx = e; }
        }
        if (bestIdx >= 0 && bestScore >= 1) { opts[bestIdx].el.click(); return JSON.stringify({clicked: true, text: opts[bestIdx].text, pass: 5}); }
    }
    return JSON.stringify({clicked: false});
}"""


SCAN_VISIBLE_OPTIONS_JS = r"""() => {
    function qAll(sel) {
        if (window.__ff && window.__ff.queryAll) return window.__ff.queryAll(sel);
        return Array.from(document.querySelectorAll(sel));
    }
    var selectors = '[role="option"], [role="menuitem"], [role="treeitem"], [role="listitem"], [data-automation-id*="promptOption"], [data-automation-id*="selectOption"]';
    var els = qAll(selectors);
    var seen = {};
    var results = [];
    for (var i = 0; i < els.length; i++) {
        var rect = els[i].getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        var t = (els[i].textContent || '').trim();
        if (!t) continue;
        var key = t.toLowerCase();
        if (seen[key]) continue;
        seen[key] = 1;
        results.push(t);
    }
    return JSON.stringify(results);
}"""


def synonym_groups_for_js() -> list[list[str]]:
    """Serialize synonym groups as nested lists for passing to the enhanced JS click helper."""
    return [sorted(group) for group in SYNONYM_GROUPS]
