-- Suppression atomique depuis l'interface : visites choisies ou exécutions.

create or replace function public.ghost_clicker_delete_results(
  p_visits jsonb default '[]'::jsonb,
  p_run_ids jsonb default '[]'::jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  deleted_visits bigint := 0;
  deleted_runs bigint := 0;
begin
  if jsonb_typeof(p_visits) <> 'array'
     or jsonb_typeof(p_run_ids) <> 'array' then
    raise exception 'sélection de résultats invalide';
  end if;

  with requested as (
    select item.run_id, item.visit_number
    from jsonb_to_recordset(p_visits)
      as item(run_id text, visit_number bigint)
    where item.run_id is not null and item.visit_number > 0
  )
  delete from public.ghost_clicker_visits as visit
  using requested
  where visit.run_id = requested.run_id
    and visit.visit_number = requested.visit_number;
  get diagnostics deleted_visits = row_count;

  delete from public.ghost_clicker_reports as report
  where report.run_id in (
    select jsonb_array_elements_text(p_run_ids)
  );
  get diagnostics deleted_runs = row_count;

  return jsonb_build_object(
    'deleted_visits', deleted_visits,
    'deleted_runs', deleted_runs
  );
end;
$$;

revoke all on function public.ghost_clicker_delete_results(jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function public.ghost_clicker_delete_results(jsonb, jsonb)
  to service_role;

comment on function public.ghost_clicker_delete_results(jsonb, jsonb) is
  'Supprime de manière atomique des visites choisies et des exécutions complètes.';
