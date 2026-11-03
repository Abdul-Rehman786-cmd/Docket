-- Initial schema. Review before executing against a remote project.
create extension if not exists pgcrypto;

create table if not exists public.messages (
  id uuid primary key default gen_random_uuid(),
  author_name text,
  body text not null,
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create table if not exists public.commitments (
  id uuid primary key default gen_random_uuid(),
  message_id uuid references public.messages(id) on delete set null,
  description text not null,
  owner_name text,
  due_at timestamptz,
  approval_status text not null default 'pending'
    check (approval_status in ('pending', 'approved', 'rejected')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.decisions (
  id uuid primary key default gen_random_uuid(),
  message_id uuid references public.messages(id) on delete set null,
  summary text not null,
  approval_status text not null default 'pending'
    check (approval_status in ('pending', 'approved', 'rejected')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_commitments_due_at on public.commitments(due_at);
create index if not exists idx_commitments_approval_status on public.commitments(approval_status);
create index if not exists idx_decisions_approval_status on public.decisions(approval_status);

alter table public.messages enable row level security;
alter table public.commitments enable row level security;
alter table public.decisions enable row level security;
-- No anonymous policies. Authorization design must precede mobile/private access.
