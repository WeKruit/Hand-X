-- Persistence for job-fill runs (Supabase / Postgres). One row per (batch, url) fill attempt.
-- Users will query these later, so keep the raw committed data + a screenshot reference.
create table if not exists job_fill_runs (
  id               uuid primary key default gen_random_uuid(),
  run_batch        text        not null,                 -- e.g. 'gen_sweep_newurls'
  row_num          int         not null,
  url              text        not null,
  host             text,
  ats_platform     text,                                 -- greenhouse / ashby / lever / custom
  profile_name     text,                                 -- test applicant used
  verdict          text        not null,                 -- PASS | FILLED | NO_LOAD | BLOCKED
  fields_filled    int         not null default 0,
  fields_escalated int         not null default 0,
  fields_total     int         not null default 0,
  field_fill_rate  numeric(5,2),                          -- filled / (filled+escalated) * 100
  cost_usd         numeric(10,5),
  latency_s        int,
  false_green      boolean     not null default false,
  committed_data   jsonb,                                 -- [{label,type,outcome,committed,value,trace}]
  escalations      jsonb,                                 -- denormalized escalate/skip fields for aggregation
  log_text         text,                                  -- run log tail for debugging reported issues                                 -- [{label, type, outcome, committed}] — the actual data filled
  screenshot_path  text,                                  -- local path or Supabase Storage URL
  created_at       timestamptz not null default now(),
  unique (run_batch, row_num)
);
create index if not exists job_fill_runs_batch_idx   on job_fill_runs (run_batch);
create index if not exists job_fill_runs_verdict_idx on job_fill_runs (verdict);
create index if not exists job_fill_runs_host_idx    on job_fill_runs (host);
