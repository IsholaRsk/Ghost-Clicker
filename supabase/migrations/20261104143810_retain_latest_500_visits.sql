-- Limite durablement le CSV et l'historique détaillé aux 500 visites récentes.

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

  delete from public.ghost_clicker_visits as visit
  where (visit.run_id, visit.visit_number) in (
    select old_visit.run_id, old_visit.visit_number
    from public.ghost_clicker_visits as old_visit
    order by old_visit.created_at desc,
             old_visit.run_id desc,
             old_visit.visit_number desc
    offset 500
  );

  delete from public.ghost_clicker_reports as report
  where report.run_id <> p_run_id
    and report.payload ->> 'completed_visits' ~ '^[0-9]+$'
    and (report.payload ->> 'completed_visits')::bigint > 0
    and not exists (
      select 1
      from public.ghost_clicker_visits as visit
      where visit.run_id = report.run_id
    );
end;
$$;

-- Applique aussi la limite aux données déjà présentes lors de la migration.
delete from public.ghost_clicker_visits as visit
where (visit.run_id, visit.visit_number) in (
  select old_visit.run_id, old_visit.visit_number
  from public.ghost_clicker_visits as old_visit
  order by old_visit.created_at desc,
           old_visit.run_id desc,
           old_visit.visit_number desc
  offset 500
);

delete from public.ghost_clicker_reports as report
where report.payload ->> 'completed_visits' ~ '^[0-9]+$'
  and (report.payload ->> 'completed_visits')::bigint > 0
  and not exists (
    select 1
    from public.ghost_clicker_visits as visit
    where visit.run_id = report.run_id
  );

revoke all on function public.ghost_clicker_upsert_report(
  text, jsonb, bigint, jsonb
) from public, anon, authenticated;
grant execute on function public.ghost_clicker_upsert_report(
  text, jsonb, bigint, jsonb
) to service_role;

comment on table public.ghost_clicker_visits is
  'Les 500 visites les plus récentes, utilisées par la console et le CSV.';
comment on function public.ghost_clicker_upsert_report(
  text, jsonb, bigint, jsonb
) is
  'Enregistre une progression puis supprime les visites au-delà des 500 plus récentes.';
