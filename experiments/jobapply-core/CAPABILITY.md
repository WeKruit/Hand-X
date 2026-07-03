# 能投递的类型 — Deliverable Application Types (2026-07-02)

Fill-only to Review / pre-submit. Submit stays human-gated everywhere. Evidence: 116-attempt
sweep + 22-tenant frozen Workday sample + live smoke; scale300 (n=300) running.

## A. 全自动可投 (auto, proven)

| # | Type | Success | Cost/time | Evidence |
|---|------|---------|-----------|----------|
| 1 | Greenhouse job-boards direct | 86% -> ~96% w/ retry+rungs | $0.002 / ~45s | 49 live, misses all non-fill |
| 2 | Ashby hosted | 100% [89,100] | $0.003 / ~38s | 33 live |
| 3 | Lever hosted | 95% [76,99] | $0.002 / ~40s | 14 live |
| 4 | GH redirect -> job page w/ Apply button | NEW rung live (n26 clicked to /apply) | +2s | smoke |
| 5 | GH iframe embed (toast class) | NEW src-hop landed; live proof = scale300 | — | commit 632dda97f |
| 6 | Workday, account creates clean | auth 64% x fill 79% (fill fixes landed, retest queued) | ~$0.01 / 5-25min | 22-tenant frozen |

Pool coverage: the 20,980 feasible pool IS types 1-5 (GH 12,861 / Ashby 6,569 / Lever 1,550)
-> ~92% today, ~96% expected with the new rungs. Workday inventory tracked separately (62 tenants).

## B. HITL-辅助可投 (one human touch, patterns already live as NEEDS_HUMAN)

| # | Type | Human touch | Automation status |
|---|------|-------------|-------------------|
| 7 | Workday email-verify gate | paste code in-app | gmail+alias IMAP MVP = 0.5d to full-auto |
| 8 | Workday create-account CAPTCHA | solve once | detector live; kla/nike class |
| 9 | Anti-bot wall (Cloudflare press&hold, WAF 403) | complete in browser | report-and-stop live |
| 10 | Foreign ATS after redirect (FirstStage etc.) | fill manually or wait for adapter | NEEDS_HUMAN bundle live |

## C. 正确拒投 (auto-declined with reason)

| # | Type | Behavior |
|---|------|----------|
| 11 | Dead posting (404 / closed) | NOT_FOUND, skip + reason (correct) |
| 12 | Un-adapted ATS (Taleo / iCIMS / SuccessFactors...) | NO_ADAPTER, skip (future adapters) |

## Workday 修理 priority (不完全占用浏览器 — 每片 ≤1h,scale 管线优先)

- **P0 done (browser-free, today):** autosave-resume rung · agent wall-clock budget ·
  iframe src-hop · gmail profile emails · AUTH-failure cause split (2 stale URL / 4
  creation-blocked=mailinator-blocklist suspect / 2 stale tracked accounts)
- **P1 (~45min slice after scale300):** retest chewy + stryker + n26 + toast — proves the
  P0 rungs live
- **P2 (0.5d code browser-free + 1h slice):** gmail+alias IMAP verify-code reader ->
  converts the creation-blocked + verify-gated auth losses; rotate+recreate for stale accounts
- **P3 (only if scale data demands):** OOPIF second-session iframe drill
