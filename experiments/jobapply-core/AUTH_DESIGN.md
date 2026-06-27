# AUTH & MULTI-PAGE DESIGN — Workday / wizard ATS filler

Status: **DESIGN (deferred track)**. The deterministic single-page engine
(Greenhouse / Lever / Ashby) is built and verified live. `WorkdayAdapter` +
`run_wizard` already reach the live Create-Account wall and read the 7-step
`progressBar`, then halt `AUTH_FAILED`. This document specifies the auth +
email-verification + credential infrastructure needed to cross that wall and
resume the wizard, grounded in a live read-only Workday auth probe (NVIDIA wd5,
Autodesk wd1, Blue Origin wd5; Intel wd1 as an expired control).

Scope binding — everything here plugs into the **existing** contract in
`ats_engine.py`, no rewrite:

```python
@dataclass
class Credentials:        # ats_engine.py:65
    email: str
    password: str         # never via CLI args — env / secret bootstrap

@dataclass
class AuthResult:         # ats_engine.py:71
    ok: bool
    needs_verification: bool = False   # emailed link/code follows -> HITL halt
    reason: str = ""

class ATSAdapter:         # ats_engine.py:97
    async def authenticate(self, session, page, creds: Credentials | None) -> AuthResult: ...
```

`run_wizard` already routes the result (`ats_engine.py:604-608`):

```python
auth = await adapter.authenticate(session, page, creds)
if not auth.ok:               return _wizard_halt(... "AUTH_FAILED", auth.reason ...)
if auth.needs_verification:   return _wizard_halt(... "EMAIL_VERIFICATION_REQUIRED", auth.reason ...)
# else: fall through to the per-step fill loop (My Information ... -> Review STOP)
```

The design's job is to (1) make `authenticate` actually authenticate instead of
halting, (2) turn `needs_verification` from a halt into an **in-process poll**
that returns `ok=True` once the code is consumed, and (3) supply `creds` from a
per-(user, tenant) store via a no-CLI-args secret bootstrap.

---

## 0. The hard facts (from the live probe) — what the design must respect

These are not assumptions; they were observed read-only on live tenants. They
shape every decision below.

| Fact | Evidence | Design consequence |
|------|----------|-------------------|
| **Account creation/sign-in is the hard gate for BOTH apply paths.** | For every tenant, `/apply/autofillWithResume` rendered the **byte-identical** auth screen as `/apply/applyManually` (same aids, same `<title>`). NO `formField-*` (My Information) elements exist in the DOM pre-auth. There is no anonymous/guest apply. | Auth is unconditional and unavoidable. "Autofill with Resume" does **not** bypass it — drop any hope of a bypass path. |
| **Two auth archetypes, tenant-dependent.** | (A) NVIDIA: `/applyManually` lands on title "Sign In" with ONLY `GoogleSignInButton` + `SignInWithEmailButton` (an SSO chooser, no fields yet); must click `SignInWithEmailButton` to reveal the native form, which defaults to **sign-in** mode. (B) Autodesk/Blue Origin: lands directly on title "Create Account" with `email`/`password`/`verifyPassword` mounted, no SSO buttons. | `authenticate` must branch: if `SignInWithEmailButton` exists, click it first; else the native form is already present. Then detect create-vs-signin mode by the presence of `verifyPassword`. |
| **No interactive CAPTCHA observed** on any Apply/Create-Account/Sign-In screen across all three tenants. | Explicit negative HTML scan for hcaptcha / recaptcha|grecaptcha / turnstile|cf-challenge / funcaptcha|arkose / perimeterx and for challenge iframe srcs: ALL FALSE. | CAPTCHA is **not** on the common path. But this was a passive single-load read — Workday may inject a challenge adaptively **on submit** or under bot heuristics (not exercised). Treat as a possible-but-improbable HITL halt, not a baked-in step. |
| **`noCaptchaWrapper` is NOT a captcha.** | On Autodesk a `div data-automation-id="noCaptchaWrapper"` wraps an overlay `click_filter` div + the real `createAccountSubmitButton`. The string "captcha" in body text comes only from this aid + the honeypot field name. | Do not treat `noCaptchaWrapper` / `anyCaptchaText` as a CAPTCHA signal. The real CAPTCHA detector must scan for the vendor signatures above, not the substring "captcha". |
| **Honeypot on EVERY native form.** | `<input type="text" data-automation-id="beecatcher" name="website">`. On Autodesk it computes `display:block / visibility:visible / offsetParent != null` (hidden via off-screen position/clip, **not** `display:none`) — so a naive "fill every visible input" filler WOULD fill it. | The filler must **explicitly skip** `aid=beecatcher` and any `input[name="website"]`, and must never enumerate auth-screen inputs generically. |
| **aids are stable across pods** (wd1 == wd5). | Identical aid vocabulary across tenants; only WHICH archetype renders varies. | One aid-keyed state machine works cross-tenant. Per-tenant handling = small branches, not separate adapters. |
| **Dead-job detection is required.** | Intel (expired) fell back to title "Intel Careers", no `adventureButton`, `/applyManually` showed `aid="invalid-content"` and zero auth fields. | Before asserting "gate", the adapter must distinguish a real auth wall from a dead/expired posting (`invalid-content` / missing `adventureButton`) and halt `JOB_EXPIRED`, not `AUTH_FAILED`. |

aid vocabulary (captured live, stable cross-tenant):

```
SSO chooser (NVIDIA pre-form):  GoogleSignInButton, SignInWithEmailButton
Create-Account form:            email, password, verifyPassword,
                                createAccountSubmitButton, createAccountCheckbox,
                                signInLink, forgotPasswordLink, beecatcher (HONEYPOT)
Sign-In form:                   email, password, signInSubmitButton,
                                createAccountLink, forgotPasswordLink, beecatcher (HONEYPOT)
Email verification (next wall): verificationCode, emailVerification, verifyCodeButton*
Cookie/legal modal (e.g. Visa): legalNoticeAcceptButton
Apply menu:                     adventureButton -> [applyManually, autofillWithResume, ...]
Submit wrapper (NOT a captcha): noCaptchaWrapper > click_filter + createAccountSubmitButton
```
*`verifyCodeButton` aid name inferred — confirm against a live post-submit screen; the verification submit is the one unprobed aid (the probe was read-only and never submitted).

> Note the asymmetry that the current adapter does NOT yet handle: the
> **switch-to-register** link is `createAccountLink` on the sign-in screen but
> `signInLink` on the create-account screen. Mode detection must use
> `verifyPassword` presence (reliable), not the link name.

---

## 1. End-to-end auth flow for an autonomous Workday application

### 1.1 The path

```
navigate(jobUrl)
   │  (dead-job guard: title/invalid-content/adventureButton)
   ▼
open_form: dismiss legal modal -> deep-link /apply/applyManually
   │
   ▼
authenticate(session, page, creds)  ──────────────────────────────────────────┐
   │                                                                           │
   ├─ A. SSO chooser present? (SignInWithEmailButton)  ── yes ─> click it      │
   │        (NEVER GoogleSignInButton — guardrail)                             │
   │                                                                           │
   ├─ B. CAPTCHA / Cloudflare challenge present?  ── yes ─> HALT CAPTCHA_HITL  │
   │                                                                           │
   ├─ C. native form mounted? detect mode by verifyPassword presence:         │
   │        verifyPassword exists  -> CREATE-ACCOUNT mode                      │
   │        else                   -> SIGN-IN mode                             │
   │     reconcile with creds.account_status (stored=sign-in, generated=create)│
   │     -> if the screen disagrees with intent, flip via signInLink /         │
   │        createAccountLink to the intended sub-form                         │
   │                                                                           │
   ├─ D. fill native fields (email, password [, verifyPassword],              │
   │        [createAccountCheckbox]); NEVER touch beecatcher                    │
   │     submit (createAccountSubmitButton | signInSubmitButton)               │
   │                                                                           │
   ├─ E. classify the resulting screen:                                        │
   │        still on email/password + error  -> AuthResult(ok=False, reason)   │
   │        CAPTCHA injected on submit        -> HALT CAPTCHA_HITL              │
   │        verificationCode / emailVerification mounted -> go to F            │
   │        progressBar / formField-* mounted -> AuthResult(ok=True)           │
   │                                                                           │
   └─ F. EMAIL VERIFICATION: poll the mailbox for the OTP/link (§2),          │
            enter code -> submit -> re-classify:                               │
              code accepted, form mounted   -> AuthResult(ok=True)             │
              code rejected / timed out     -> AuthResult(ok=True,             │
                                                 needs_verification=True)  ────┘
                                                 (HITL fallback path)
   ▼
run_wizard fill loop: My Information -> My Experience -> ... -> Review (STOP, never Submit)
```

### 1.2 Auth-state classification (one DOM probe, mirrors the existing pattern)

The adapter already has `_active_step_name` and aid probes. The design adds one
JS classifier that returns **exactly one** state — the same shape the in-tree
`_probe_generated_auth_state()` pattern uses. States map 1:1 onto the
credential-store `account_status` and onto `AuthResult`:

```python
# states the classifier emits (observable page states, not credential states)
SSO_CHOOSER      # GoogleSignInButton/SignInWithEmailButton present, no email input
CREATE_ACCOUNT   # email + password + verifyPassword present
SIGN_IN          # email + password, NO verifyPassword, signInSubmitButton present
VERIFICATION     # verificationCode / emailVerification present
CAPTCHA          # vendor challenge signature present (see §4 detector)
AUTHED           # progressBar OR any formField-* present, no auth inputs
AUTH_ERROR       # auth inputs still present + an error/aria-invalid node
DEAD             # invalid-content / no adventureButton / title != job title
```

### 1.3 Pseudocode — bound to `authenticate(...) -> AuthResult`

This replaces the body of `WorkdayAdapter.authenticate` (`ats_workday.py:56-97`).
It keeps the same signature and return type; `creds` is now richer (carries
`account_status` / `intent`, see §3) but stays a superset of the current
`Credentials(email, password)`.

```python
async def authenticate(self, session, page, creds) -> AuthResult:
    state = await self._auth_state(page)               # §1.2 single JS classifier

    if state == "DEAD":
        return AuthResult(ok=False, reason="JOB_EXPIRED: invalid-content / no apply menu")
    if state == "AUTHED":
        return AuthResult(ok=True)                      # already past the gate (resumed session)
    if creds is None:
        return AuthResult(ok=False, reason=(
            "Workday account gate is mandatory and no credentials were provided. "
            "Needs a per-(user,tenant) account + a reachable inbox for verification."))

    # A. SSO chooser -> reveal native form (NVIDIA archetype). NEVER Google SSO.
    if state == "SSO_CHOOSER":
        await self._click_aid(page, "SignInWithEmailButton")
        await self._settle(page)
        state = await self._auth_state(page)

    # B. CAPTCHA before we even submit -> HITL halt, do not auto-solve (§4)
    if state == "CAPTCHA":
        return AuthResult(ok=False, reason="CAPTCHA_HITL: human challenge on auth screen")

    # C. reconcile screen mode with credential intent
    want_create = (creds.account_status in ("pending", "failed"))   # §3 status->intent
    on_create   = (state == "CREATE_ACCOUNT")
    if want_create and not on_create:
        await self._click_aid(page, "createAccountLink")   # sign-in screen -> register
        await self._settle(page); on_create = True
    elif (not want_create) and on_create:
        await self._click_aid(page, "signInLink")          # create screen -> sign-in
        await self._settle(page); on_create = False

    # D. fill native fields ONLY (never beecatcher / input[name=website])
    await self._fill_aid(page, "email", creds.email)
    await self._fill_aid(page, "password", creds.password)
    if on_create:
        await self._fill_aid(page, "verifyPassword", creds.password)
        await self._click_aid(page, "createAccountCheckbox")   # consent (Visa et al.) — only if present
        await self._click_aid(page, "createAccountSubmitButton")
    else:
        await self._click_aid(page, "signInSubmitButton")
    await self._settle(page)

    # E. classify the post-submit screen
    state = await self._auth_state(page)
    if state == "CAPTCHA":
        return AuthResult(ok=False, reason="CAPTCHA_HITL: challenge injected on submit")
    if state == "AUTH_ERROR":
        # one repair attempt for sign-in: bad/rotated password -> re-create not safe (dup profile);
        # surface for the store to mark `failed` and (optionally) trigger a reset flow.
        return AuthResult(ok=False, reason="AUTH_ERROR: credentials rejected / validation failed")
    if state == "AUTHED":
        return AuthResult(ok=True)

    # F. email verification wall
    if state == "VERIFICATION":
        code = await self._poll_verification(creds)        # §2 mailbox interface (in-process)
        if code is None:                                   # timeout / no inbox -> HITL fallback
            return AuthResult(ok=True, needs_verification=True,
                              reason="EMAIL_VERIFICATION_REQUIRED: code not retrieved within TTL")
        await self._fill_aid(page, "verificationCode", code)
        await self._click_aid(page, "verifyCodeButton") or await self._submit_verification(page)
        await self._settle(page)
        state = await self._auth_state(page)
        if state == "AUTHED":
            return AuthResult(ok=True)                      # crossed the wall fully autonomously
        return AuthResult(ok=True, needs_verification=True,
                          reason="EMAIL_VERIFICATION_REQUIRED: code rejected, needs human")

    return AuthResult(ok=False, reason=f"unexpected auth state: {state}")
```

Key behavioral changes vs. the current adapter:

1. **SSO chooser handled** (`SignInWithEmailButton`) before touching fields — the
   current code probes for it but never clicks it.
2. **Create-vs-sign-in is decided by `verifyPassword` + credential intent**, not by
   blindly clicking both submit buttons (the current `createAccountSubmitButton OR
   signInSubmitButton` line).
3. **Honeypot is never touched** — fills are aid-scoped (`email`/`password`/
   `verifyPassword`), never "every visible input".
4. **Email verification becomes an in-process poll** that can return `ok=True`
   autonomously; `needs_verification=True` is now a *fallback*, not the only outcome.
5. **`JOB_EXPIRED` and `CAPTCHA_HITL`** are distinct halts, not lumped into
   `AUTH_FAILED`.

---

## 2. Email verification — mailbox approach + poll interface

### 2.1 Recommended approach: dedicated catch-all domain + IMAP, unique full localpart

**Primary:** own one or two reputable-looking, warmed, aged domains (e.g. Migadu
for unlimited flat-fee aliases, or self-hosted Mailcow), enable **catch-all**, and
have the worker mint a **deterministic unique full localpart per (user, tenant)** —
**NOT plus-addressing**. Example: `u8231-nvidia-a7k2@apply.ourdomain.com`.

Why this and not the alternatives:

- **Plus-addressing is disqualified.** Workday is documented to (a) reject `+` as
  invalid on some tenants and (b) spawn **duplicate candidate profiles** when a
  `+alias` diverges from a base address. It fails the "works across many tenants
  unattended" bar.
- **Managed test-mailbox APIs (Mailosaur / MailSlurp)** are the **hot fallback**,
  not the default: cleanest OTP-extraction ergonomics and an instant alternate
  domain when one of ours gets flagged, but on their shared/default domains you
  risk disposable-email blocklists (the vendors themselves recommend a custom
  domain for policy-sensitive use), they cost tens-to-hundreds of $/mo at volume,
  and a third party then holds applicants' verification mail.
- **Applicant's-own-email via OAuth (Gmail/Graph)** is the only path with perfect
  deliverability and true account ownership, but `gmail.readonly` is a Google
  RESTRICTED scope dragging in CASA Tier-3 + annual pen tests (thousands/yr) and a
  human consent step — wrong fit for a hands-off worker. Revisit only as a future
  premium "real applicant-owned accounts" tier.

Implementation guardrails for the primary path:

- Keep the domain off disposable-email blocklists: normal-looking name, age it,
  warm it, set SPF/DKIM/DMARC **even though receive-only** (some ATS validate MX /
  deliverability).
- Poll IMAP **gently** or use **IDLE** — high-volume catch-all connections can look
  like a directory-harvest attack upstream.
- Match strictly on the exact **To / Delivered-To** address for idempotency (the
  unique localpart is the join key).
- Persist full **RFC822 bytes** + parsed body for re-parse and audit.
- **Rotate across 2–3 domains** so a single block doesn't halt the fleet.

### 2.2 Poll interface (what `authenticate` calls)

A small `VerificationInbox` protocol, implementation-swappable (IMAP primary,
managed API fallback). It is the only mailbox surface `authenticate` knows about.

```python
class VerificationInbox(Protocol):
    def address_for(self, user_id: str, tenant: str) -> str:
        """Deterministic unique full localpart, e.g. u8231-nvidia-a7k2@apply.ourdomain.com.
        Same (user, tenant) ALWAYS yields the same address (re-apply reuses the account)."""

    async def poll_code(self, address: str, *, since: datetime,
                        timeout_s: float = 120, interval_s: float = 5) -> str | None:
        """IDLE/poll for a message TO `address` newer than `since`. Extract the OTP
        (regex \\b\\d{4,8}\\b near 'verification'/'code') or the verify LINK. Return the
        code/token, or None on timeout. Matches strictly on To/Delivered-To == address."""
```

`WorkdayAdapter._poll_verification(creds)` is a thin wrapper:

```python
async def _poll_verification(self, creds) -> str | None:
    addr = self.inbox.address_for(creds.user_id, creds.tenant)  # == creds.email already
    # `since` = the moment we clicked submit, so we never re-read a stale code
    return await self.inbox.poll_code(addr, since=creds.submitted_at, timeout_s=120)
```

The mailbox address **is** `creds.email` — the store mints it at account-reservation
time (§3), so the address typed into Workday and the address polled here are the
same value by construction.

### 2.3 HITL fallback

When `poll_code` returns `None` (no deliverable mail within TTL, or a link-only
flow we can't auto-click safely), `authenticate` returns
`AuthResult(ok=True, needs_verification=True)`. `run_wizard` already converts that
into a `EMAIL_VERIFICATION_REQUIRED` halt (`ats_engine.py:607-608`). The HITL
contract on top:

- Emit `{credentialId, tenant, user_id, address, jobUrl, authState:"verification"}`
  to VALET (no secret) so a human (or the applicant in the Desktop app) can paste
  the code.
- Keep the browser **session alive** (`keep_alive=True` is already set) so the
  resumed run can submit the human-supplied code without re-authenticating.
- The resumed run re-enters `authenticate`, finds `state == VERIFICATION` with a
  human code injected via the Desktop bridge, submits, and proceeds.

---

## 3. Credential store — data model + per-(user, tenant) + secret bootstrap

### 3.1 The store key is the **tenant**, not the platform

Workday uses **per-tenant accounts** — each company's Workday instance requires
separate registration (the `WORKDAY_AUTH_FLOW` invariant). Re-applying to the same
company must **reuse** the account. The key is the Workday tenant derived from the
host:

```python
# ghosthands/security/tenant.py
def tenant_key(url: str) -> str:
    """nvidia.wd5.myworkdayjobs.com  ->  'workday:nvidia'
       nvidia.wd1.myworkdayjobs.com  ->  'workday:nvidia'   (wd1==wd5, same tenant)
    First DNS label of *.myworkdayjobs.com; registrable domain for non-Workday ATS."""
```

This replaces today's fuzzy `domain ILIKE '%...%'` match in `load_credentials`.

### 3.2 Data model (reuse `gh_user_credentials`; VALET DB is source of truth)

```sql
ALTER TABLE gh_user_credentials
  ADD COLUMN tenant          text NOT NULL,           -- 'workday:nvidia'
  ADD COLUMN account_status  text NOT NULL DEFAULT 'pending',
  ADD COLUMN secret_version  int  NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX ux_cred_user_tenant
  ON gh_user_credentials (user_id, tenant);           -- the re-apply guarantee
```

`account_status` lifecycle maps 1:1 onto the auth states the classifier emits, and
**derives** `credential_source` (no more manual env var):

| status | meaning | set when (auth state) | drives intent |
|---|---|---|---|
| `pending` | row reserved, account not yet created | before run | **create-account** |
| `active` | account exists, sign-in works | `AUTHED` after sign-in | **sign-in** |
| `pending_verification` | created, email-verify wall | `VERIFICATION` | await-verification |
| `failed` | create/sign-in broke | `AUTH_ERROR` | **create-account** (repair) |

Re-apply flow: second job to tenant T finds an `active` row -> intent = sign-in ->
`authenticate` takes the SIGN_IN branch and signs in (never re-creates -> avoids
Workday's duplicate-profile bug). First job: no row -> reserve `pending` -> intent
= create-account.

### 3.3 Password generation (strict, class-guaranteed, server-side)

The existing inline generator (`worker/executor.py:200`) does **not** guarantee
character-class coverage — over N chars it can yield a password with no digit or no
special and intermittently fail Workday's policy. Replace it with a CSPRNG,
class-guaranteed generator, generated **server-side in VALET** at `pending`
reservation (so the secret never originates in a model-facing process):

```python
# ghosthands/security/password.py — strictest common Workday policy: >=12, all 4 classes
_UPPER="ABCDEFGHJKLMNPQRSTUVWXYZ"; _LOWER="abcdefghijkmnpqrstuvwxyz"
_DIGIT="23456789"; _SPECIAL="!@#$%^&*-_=+"          # no quotes/backslash/space (break fills/shell)

def generate_password(length: int = 16) -> str:
    pools=[_UPPER,_LOWER,_DIGIT,_SPECIAL]
    chars=[secrets.choice(p) for p in pools]
    union="".join(pools)
    chars+=[secrets.choice(union) for _ in range(length-len(pools))]
    for i in range(len(chars)-1,0,-1):              # Fisher-Yates via secrets (not random.shuffle)
        j=secrets.randbelow(i+1); chars[i],chars[j]=chars[j],chars[i]
    return "".join(chars)
```

Length 16 (>12 floor) for margin against tenants requiring 14; excludes ambiguous
and fill/shell-breaking characters.

### 3.4 Secret bootstrap — no CLI args, two transports (both already in-tree)

**Hard rule (project memory):** secrets never go through `argv` (visible via
`ps aux`). Two transports already exist:

**Path A — worker/DB mode** (current `executor.py`): secrets arrive as
`encrypted_secret` rows decrypted **in-process** via `decrypt_credentials()`
(AES-256-GCM, key `GH_CREDENTIAL_ENCRYPTION_KEY`). Plaintext lives only in the
in-memory `credentials` dict. Only change: key the lookup on `tenant`, not
`domain ILIKE`.

**Path B — Desktop bridge mode (V2 claim/secrets split) — stdin bootstrap.** The
`bridge/protocol.py` stdin loop already exists; add one message type, written
**before** the agent starts:

```jsonc
// Desktop -> Hand-X, first stdin line, one JSON object
{"type":"bootstrap","credentials":[
  {"credentialId":"...","tenant":"workday:nvidia","user_id":"u8231",
   "loginIdentifier":"u8231-nvidia-a7k2@apply.ourdomain.com",
   "secret":"<plaintext>","accountStatus":"active"}],
 "expiresAt":"..."}
```

```python
async def read_bootstrap(timeout: float = 30.0) -> dict[str, Credentials]:
    """First stdin line MUST be the bootstrap envelope (V2). Returns {tenant: Credentials}.
    Secrets kept ONLY in this dict — never echoed to stdout, never in GH_USER_PROFILE_TEXT,
    never logged (structlog already redacts password keys)."""
```

V2 flow: Desktop claims job -> gets `credentialRefs` (no secret) -> calls
`POST /jobs/:jobId/secrets` (accessToken + sessionToken + leaseId) -> VALET
resolves only this lease's secrets, generating+storing a `pending` password
(§3.3) if `usage=create_account_pending` -> Desktop spawns Hand-X and writes the
`bootstrap` line to **stdin**. `Credentials` is extended to carry
`user_id`, `tenant`, `account_status`, `submitted_at` so `authenticate` and the
inbox can do their work without any extra lookups.

### 3.5 Confirmation back to VALET (no secret leaves the worker)

After `authenticate`, Hand-X emits **metadata only**:

```jsonc
{"credentialId":"...","tenant":"workday:nvidia",
 "loginIdentifier":"u8231-nvidia-a7k2@apply.ourdomain.com",
 "authState":"AUTHED"}   // or "VERIFICATION" | "AUTH_ERROR" | "CAPTCHA"
```

VALET promotes status: `AUTHED` -> `active` (next apply signs in), `VERIFICATION`
-> `pending_verification`, `AUTH_ERROR`/`CAPTCHA` -> `failed`. The plaintext
password and the OTP never leave the worker process.

---

## 4. CAPTCHA — detection + HITL-halt policy

### 4.1 Likelihood (from the probe)

**Low on the common path, non-zero on submit.** Across NVIDIA, Autodesk, Blue
Origin, an explicit negative scan found **no** hcaptcha / reCAPTCHA / Turnstile /
Cloudflare-challenge / FunCaptcha-Arkose / PerimeterX on any Apply / Create-Account
/ Sign-In screen. **But** the probe was a passive, read-only, single-load read that
**never submitted** — Workday is known to inject reCAPTCHA / Cloudflare adaptively
on actual submit or under bot heuristics. So: don't bake a CAPTCHA step into the
happy path, but **detect on every state classification**, especially post-submit.

False-positive trap to avoid: the `noCaptchaWrapper` aid and the `beecatcher`
honeypot name both put the substring "captcha" in the DOM **without any real
challenge**. The detector must match vendor signatures, never the substring.

### 4.2 Detector (runs inside `_auth_state`, returns `state == "CAPTCHA"`)

```python
CAPTCHA_SIGNATURES = [
  # script/iframe srcs and global objects, NOT body text
  'iframe[src*="hcaptcha.com"]', '.h-captcha', 'script[src*="hcaptcha.com"]',
  'iframe[src*="recaptcha"]', '.g-recaptcha', 'script[src*="recaptcha/api.js"]',
  'iframe[src*="challenges.cloudflare.com"]', '.cf-turnstile', 'div#cf-challenge-running',
  'iframe[src*="arkoselabs"]', 'iframe[src*="funcaptcha"]',
  'script[src*="perimeterx"]', 'script[src*="px-cdn"]',
]
# Explicitly EXCLUDE: [data-automation-id="noCaptchaWrapper"], input[name="website"]
```

### 4.3 Policy: **never auto-solve. Pause + notify.**

- On any `state == "CAPTCHA"`, `authenticate` returns `AuthResult(ok=False,
  reason="CAPTCHA_HITL: ...")`. `run_wizard` halts. (We surface this as a distinct
  status, not `AUTH_FAILED`.)
- **No third-party solver, no audio-solve, no token-injection.** Auto-solving a
  CAPTCHA is exactly the abuse signal that gets the domain + IP fleet blocked, and
  it's an ethical/ToS line. Treat CAPTCHA as a hard "a human must take over here."
- Notify the user with `{tenant, jobUrl, screenshot, authState:"captcha"}`, keep
  the session alive, and let a human complete the challenge in the Desktop app (or
  abandon the application). Because CAPTCHA is rare on Workday, the HITL rate this
  introduces should be low.

---

## 5. Per-tenant auth-screen handling — small branches off the shared machine

The aid vocabulary is identical across tenants; only **which archetype renders**
varies. So per-tenant handling is **a few conditionals inside the one
`authenticate` state machine**, not separate code paths.

| Tenant behavior | Signal | Branch (already in §1.3 pseudocode) |
|---|---|---|
| **SSO chooser first** (NVIDIA) | `SignInWithEmailButton` present, no `email` input | Click `SignInWithEmailButton`; **never** `GoogleSignInButton`; re-classify. |
| **Direct native form** (Autodesk, Blue Origin) | `email`/`password` mounted, no SSO buttons | Skip the chooser branch; go straight to mode detection. |
| **Sign-in default** (NVIDIA email path) | native form, no `verifyPassword` | mode = SIGN_IN; flip to create via `createAccountLink` only if intent says create. |
| **Create-account default** (Autodesk/Blue Origin) | `verifyPassword` present | mode = CREATE_ACCOUNT; flip to sign-in via `signInLink` only if intent says sign-in. |
| **Consent checkbox** (Visa, Autodesk) | `createAccountCheckbox` present | click it ON before create-submit; **only if present** (`_click_aid` no-ops when absent). |
| **Cookie/legal modal first** (Visa) | `legalNoticeAcceptButton` present | dismiss in `open_form` (already done at `ats_workday.py:35`) and re-check before classify. |
| **Honeypot** (ALL native forms) | `beecatcher` / `input[name="website"]` | **NEVER fill.** Fills are aid-scoped to `email`/`password`/`verifyPassword` only; add an assertion that `beecatcher.value == ""` before submit. |
| **Expired/dead job** (Intel control) | `invalid-content`, no `adventureButton`, title mismatch | classify `DEAD` -> `AuthResult(ok=False, reason="JOB_EXPIRED")`; do **not** assume a gate. |

Each branch is a guard that no-ops when its signal is absent, so the same function
handles every tenant. New tenants that reuse the aid vocabulary need **zero** new
code; only a genuinely new archetype (new aid) would add a branch.

---

## 6. HONEST go/no-go

### 6.1 What autonomous Workday costs

To cross the wall **fully unattended** you must stand up and operate three pieces
of infrastructure that the single-page engine never needed:

1. **A managed receive-mail system** — owned + warmed + SPF/DKIM/DMARC'd
   domain(s), catch-all, IMAP poller, OTP/link parser, domain rotation, plus a
   managed-API fallback for blocked domains. This is an **ongoing ops surface**
   (deliverability, blocklist monitoring), not a one-time build.
2. **A per-(user, tenant) credential store** — schema change, unique index,
   server-side strong password gen, AES-256-GCM at rest, status lifecycle, and the
   stdin secret-bootstrap wiring. Plus you are now **custodian of applicant ATS
   accounts and their verification mail** — a real privacy/compliance surface.
3. **A HITL path for CAPTCHA + verification failures** — notification, session
   keep-alive, resume protocol, and a human (or the applicant) in the loop.

Against the **per-application economics**: the deterministic single-page fill is
**~$0.002/application**. Workday adds amortized mail-infra + store + HITL overhead
and a per-job verification poll. The marginal *LLM* cost stays tiny (the per-step
map call is the same cheap structured call), but the **fixed** infra + ops + human
fallback cost is what you're buying.

### 6.2 What it unlocks

Workday is one of the highest-volume enterprise ATSes — a large share of
big-company postings (NVIDIA, Autodesk, Blue Origin, Visa, Intel, and thousands
more) are Workday. The single-page engine covers Greenhouse/Lever/Ashby, which
skew startup/mid-market. **Workday is the gateway to enterprise coverage**, and
once `run_wizard` + auth land, the same machinery extends to other account-gated
wizards (iCIMS, Taleo, SuccessFactors) — the auth/inbox/store investment amortizes
across all of them, not just Workday.

### 6.3 Verdict: **GO, but phased — HITL-assisted first, full-auto later.**

The infra is justified by the coverage *only if you stage it*, because the two
biggest risks (mail deliverability/blocklisting, and account-creation under bot
heuristics) are exactly the things you cannot validate from a read-only probe —
they only surface when you actually submit. So:

**Phase 1 — HITL-assisted (minimum viable, ship first).**
Build the credential store (§3) + the auth state machine (§1) + CAPTCHA detection
(§4) + per-tenant branches (§5). For email verification, **start with the HITL
fallback only** (§2.3): authenticate up to the verification wall, then hand the OTP
step to the applicant via the Desktop app. This proves account creation works
on live tenants, exercises the duplicate-profile/honeypot/SSO branches for real,
and gathers the **actual** CAPTCHA-on-submit rate — **without** standing up mail
infra. Smallest surface that produces a submitted-to-Review Workday application.

**Phase 2 — automate verification (the mail track).**
Once Phase 1 confirms account creation is reliable and the CAPTCHA rate is
tolerable, stand up the catch-all domain + IMAP poller (§2.1–2.2) and flip
`_poll_verification` from "always HITL" to "poll first, HITL on timeout." This is
where the bulk of the ops cost lives — defer it until Phase 1 has proven the rest.

**Phase 3 — scale + harden.**
Domain rotation, managed-API fallback, blocklist monitoring, and (optionally, much
later) the applicant-owned-email OAuth premium tier for users who want real
account ownership and perfect deliverability.

Rationale (第一性原理): the unknowns that could **kill** autonomous Workday
(submit-time CAPTCHA, bot-heuristic account-creation blocks, deliverability) are
unfalsifiable until you submit. Phase 1 buys that evidence cheaply by deferring the
single most expensive, ops-heavy component (mail). If Phase 1 shows account
creation is routinely CAPTCHA-walled or blocked, you've spent the *least* to learn
Workday isn't worth full automation — and you still have a working HITL-assisted
flow.

---

## 7. Risks & unknowns

| # | Risk / unknown | Why it matters | Mitigation / how to retire it |
|---|---|---|---|
| R1 | **CAPTCHA / Cloudflare on submit** — never exercised (probe was read-only). | If Workday challenges most account-creation submits, full autonomy is impossible regardless of mail infra. | Phase 1 measures the real rate on live submits before any mail spend. HITL-halt policy (§4) makes a challenge a safe pause, not a failure. |
| R2 | **Bot-heuristic blocks on account creation** — headless + datacenter IP + brand-new-domain email is a triple risk signal. | Submits could be silently rejected or shadow-failed even without a visible CAPTCHA. | Use residential/warmed proxies, a warmed/aged domain (§2.1), and human-like timing. Detect `AUTH_ERROR` and mark `failed`; alarm on a rising failure rate. |
| R3 | **Email deliverability / blocklisting** — ATS may validate MX/deliverability or flag newly-registered domains. | A blocked domain halts the whole fleet's verification. | SPF/DKIM/DMARC even receive-only; age + warm; **rotate 2–3 domains**; managed-API fallback as hot spare (§2.1). |
| R4 | **Duplicate candidate profiles** — Workday spawns dupes if the email diverges per tenant or `+` aliasing is used. | Corrupts the applicant's record; can break re-apply. | Unique **full-localpart** per (user, tenant) — never plus-addressing; `UNIQUE(user_id, tenant)` index forces account reuse (§3.2). |
| R5 | **`verifyCodeButton` / verification-submit aid is unconfirmed** — never reached (read-only). | The verification submit is the one unprobed control; wrong aid = stuck on the verify screen. | First live submit-run must capture the verification screen's aids; the pseudocode falls back to `_submit_verification(page)` (text/Enter) until confirmed. |
| R6 | **Adaptive auth archetypes beyond the 3 probed** — other tenants may add MFA, a different SSO chooser, or a corporate IdP redirect. | An unrecognized screen would dead-end the state machine. | The classifier emits an explicit `unknown` state -> `AuthResult(ok=False)` halt with a screenshot, never a wrong guess. New archetype = one new branch (§5). |
| R7 | **Session-resume after HITL** — the keep-alive session must survive a human round-trip without re-auth. | If the session dies mid-wait, the resumed run re-creates an account -> dup profile (R4). | `keep_alive=True` (already set); on resume, re-classify and take the `AUTHED`/`VERIFICATION` branch — never re-submit create if already `active`. |
| R8 | **Compliance / data custody** — you now hold applicant ATS passwords + their verification mail. | Legal/privacy exposure; a breach is high-severity. | AES-256-GCM at rest, VALET as single source of truth, secrets only via stdin bootstrap (never argv/logs), redaction in structlog; consider the OAuth-owned-email tier (§2.1 (d)) for users who'd rather own the account. |
| R9 | **`enable_default_extensions=True` is fatal in this env** — uBlock Lite download fails SSL (`CERTIFICATE_VERIFY_FAILED`) and aborts CDP startup. | Any headless Workday run here breaks at session start with the default config. | Pass `enable_default_extensions=False` (or fix the cert chain) for all Workday runs; document in the run config. |
| R10 | **ToS / ethics of automated account creation** — creating ATS accounts on an applicant's behalf at scale may violate some tenants' terms. | Reputational + access risk if tenants detect and block the fleet. | Per-applicant accounts (not fake/shared), real applicant data, human-owned credentials where possible; respect robots/ToS; the CAPTCHA HITL line is also the "tenant clearly doesn't want bots here — stop" line. |

---

## Appendix — files touched (grounding)

- **New:** `ghosthands/security/tenant.py` (tenant key, §3.1),
  `ghosthands/security/password.py` (strong gen, §3.3),
  a `VerificationInbox` impl (§2.2).
- **Edit:** `ats_workday.py:56-97` `authenticate` (the §1.3 state machine + §4
  detector + §5 branches); `ghosthands/integrations/database.py` `load_credentials`
  keyed on `tenant`, `+ reserve_pending`, `+ promote_status`;
  `ghosthands/bridge/protocol.py` `+ read_bootstrap` (§3.4 Path B);
  `ghosthands/worker/executor.py:200` -> call `security.password.generate_password`.
- **Reuse unchanged:** `ats_engine.py` wizard contract (`Credentials`, `AuthResult`,
  `run_wizard`, `_wizard_halt`), `integrations/credentials.py` (AES-256-GCM decrypt),
  `platforms/workday.py` (guardrails: native-only, never Google SSO).
