# Phase 12: VALET Prod Promotion - Context

**Gathered:** 2026-04-02
**Status:** Ready for planning
**Mode:** Auto-generated (infrastructure phase — discuss skipped)

<domain>
## Phase Boundary

VALET staging (289 commits including unified profile endpoint, DB migrations, resume parser) is safely promoted to production with verified DB compatibility and a recorded rollback path.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
All implementation choices are at Claude's discretion — pure infrastructure phase. Key constraints:
- Must verify prod DB `languages` column data before jsonb ALTER
- Full staging→main merge via PR (triggers CD)
- Record rollback SHA (81ce921) before merge
- Verify profile API endpoint responds on prod after deploy

</decisions>

<code_context>
## Existing Code Insights

### Key Files
- VALET repo: `packages/db/drizzle/0044_add_extended_application_profile_fields.sql` — migration adding 13 columns
- VALET repo: `packages/db/src/schema/user-application-profiles.ts` — languages column changed to jsonb
- VALET repo: `apps/api/src/modules/local-workers/local-worker.routes.ts` — profile endpoint
- VALET repo: `.github/workflows/cd-staging.yml` / `cd-prod.yml` — CD pipelines

### Integration Points
- CD deploys on push to main (prod) or staging
- DB migrations run automatically on deploy
- Profile endpoint uses authorizeManagedInferenceRequest for auth

</code_context>

<specifics>
## Specific Ideas

No specific requirements — infrastructure phase. Refer to ROADMAP phase description and success criteria.

</specifics>

<deferred>
## Deferred Ideas

None — infrastructure phase.

</deferred>
