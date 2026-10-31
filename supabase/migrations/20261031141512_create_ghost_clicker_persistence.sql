-- Persistance serveur de Ghost Clicker.
-- Les identifiants Rayobyte restent volontairement hors de Supabase.

create table if not exists public.ghost_clicker_settings (
  id text primary key default 'default',
  payload jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now(),
  constraint ghost_clicker_settings_singleton check (id = 'default'),
  constraint ghost_clicker_settings_payload_object
    check (jsonb_typeof(payload) = 'object')
);

create table if not exists public.ghost_clicker_reports (
  run_id text primary key,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint ghost_clicker_reports_run_id_length
    check (char_length(run_id) between 1 and 200),
  constraint ghost_clicker_reports_payload_object
    check (jsonb_typeof(payload) = 'object')
);

create table if not exists public.ghost_clicker_visits (
  run_id text not null
    references public.ghost_clicker_reports(run_id) on delete cascade,
  visit_number bigint not null,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (run_id, visit_number),
  constraint ghost_clicker_visits_number_positive check (visit_number > 0),
  constraint ghost_clicker_visits_payload_object
    check (jsonb_typeof(payload) = 'object')
);

create table if not exists public.ghost_clicker_events (
  id bigint generated always as identity primary key,
  event_type text not null,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint ghost_clicker_events_type_length
    check (char_length(event_type) between 1 and 100),
  constraint ghost_clicker_events_payload_object
    check (jsonb_typeof(payload) = 'object')
);

create index if not exists ghost_clicker_reports_updated_at_idx
  on public.ghost_clicker_reports (updated_at desc);
create index if not exists ghost_clicker_visits_created_at_idx
  on public.ghost_clicker_visits (created_at desc);
create index if not exists ghost_clicker_events_created_at_idx
  on public.ghost_clicker_events (created_at desc);

alter table public.ghost_clicker_settings enable row level security;
alter table public.ghost_clicker_reports enable row level security;
alter table public.ghost_clicker_visits enable row level security;
alter table public.ghost_clicker_events enable row level security;

revoke all on table public.ghost_clicker_settings from anon, authenticated;
revoke all on table public.ghost_clicker_reports from anon, authenticated;
revoke all on table public.ghost_clicker_visits from anon, authenticated;
revoke all on table public.ghost_clicker_events from anon, authenticated;
grant select, insert, update, delete
  on table public.ghost_clicker_settings to service_role;
grant select, insert, update, delete
  on table public.ghost_clicker_reports to service_role;
grant select, insert, update, delete
  on table public.ghost_clicker_visits to service_role;
grant select, insert, update, delete
  on table public.ghost_clicker_events to service_role;
grant usage, select
  on sequence public.ghost_clicker_events_id_seq to service_role;

create or replace function public.ghost_clicker_upsert_report(
  p_run_id text,
  p_report jsonb,
  p_visit_number bigint default null,
  p_visit jsonb default null
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_run_id is null or char_length(p_run_id) not between 1 and 200 then
    raise exception 'run_id invalide';
  end if;
  if p_report is null or jsonb_typeof(p_report) <> 'object' then
    raise exception 'rapport invalide';
  end if;

  insert into public.ghost_clicker_reports (run_id, payload)
  values (p_run_id, p_report)
  on conflict (run_id) do update
    set payload = excluded.payload,
        updated_at = now();

  if p_visit_number is not null then
    if p_visit_number < 1
       or p_visit is null
       or jsonb_typeof(p_visit) <> 'object' then
      raise exception 'visite invalide';
    end if;
    insert into public.ghost_clicker_visits (run_id, visit_number, payload)
    values (p_run_id, p_visit_number, p_visit)
    on conflict (run_id, visit_number) do update
      set payload = excluded.payload,
          updated_at = now();
  end if;
end;
$$;

create or replace function public.ghost_clicker_clear_reports()
returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare
  deleted_count bigint;
begin
  with deleted as (
    delete from public.ghost_clicker_reports returning 1
  )
  select count(*) into deleted_count from deleted;
  return deleted_count;
end;
$$;

revoke all on function public.ghost_clicker_upsert_report(
  text, jsonb, bigint, jsonb
) from public, anon, authenticated;
revoke all on function public.ghost_clicker_clear_reports()
  from public, anon, authenticated;
grant execute on function public.ghost_clicker_upsert_report(
  text, jsonb, bigint, jsonb
) to service_role;
grant execute on function public.ghost_clicker_clear_reports()
  to service_role;

comment on table public.ghost_clicker_settings is
  'Configuration persistante non sensible de Ghost Clicker.';
comment on table public.ghost_clicker_reports is
  'Résumé persistant de chaque exécution.';
comment on table public.ghost_clicker_visits is
  'Historique détaillé et non tronqué des visites.';
comment on table public.ghost_clicker_events is
  'Journal des actions administratives sans secret.';

insert into public.ghost_clicker_settings (id, payload)
values (
  'default',
  '{
    "targets": [],
    "schedule_enabled": false,
    "interval_minutes": 60,
    "execution_mode": "fixed",
    "repetition_count": 1,
    "repetition_min": 1,
    "repetition_max": 10
  }'::jsonb
)
on conflict (id) do nothing;
