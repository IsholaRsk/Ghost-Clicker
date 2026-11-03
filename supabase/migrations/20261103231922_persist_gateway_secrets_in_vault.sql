-- Conservation durable et chiffrée des identifiants de passerelles.
-- Les pseudos restent dans ghost_clicker_settings ; les secrets restent dans Vault.

create table if not exists public.ghost_clicker_gateway_secrets (
  gateway_id text primary key,
  secret_id uuid not null unique
    references vault.secrets(id) on delete cascade,
  updated_at timestamptz not null default now(),
  constraint ghost_clicker_gateway_secrets_id_format
    check (gateway_id ~ '^[A-Za-z0-9_-]{6,80}$')
);

alter table public.ghost_clicker_gateway_secrets enable row level security;
revoke all on table public.ghost_clicker_gateway_secrets
  from public, anon, authenticated;

create or replace function public.ghost_clicker_load_gateway_secrets()
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(
    jsonb_object_agg(mapping.gateway_id, decrypted.decrypted_secret),
    '{}'::jsonb
  )
  from public.ghost_clicker_gateway_secrets as mapping
  join vault.decrypted_secrets as decrypted
    on decrypted.id = mapping.secret_id;
$$;

create or replace function public.ghost_clicker_replace_gateway_secrets(
  p_secrets jsonb
)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  gateway_count bigint;
  gateway_record record;
  existing_secret_id uuid;
  created_secret_id uuid;
  obsolete_secret_id uuid;
begin
  if p_secrets is null or jsonb_typeof(p_secrets) <> 'object' then
    raise exception 'registre de passerelles invalide';
  end if;

  select count(*)
  into gateway_count
  from jsonb_each_text(p_secrets);

  if gateway_count > 20 then
    raise exception 'trop de passerelles';
  end if;

  for gateway_record in
    select key as gateway_id, value as gateway_secret
    from jsonb_each_text(p_secrets)
  loop
    if gateway_record.gateway_id !~ '^[A-Za-z0-9_-]{6,80}$'
       or char_length(gateway_record.gateway_secret) not between 1 and 4096 then
      raise exception 'passerelle invalide';
    end if;

    select mapping.secret_id
    into existing_secret_id
    from public.ghost_clicker_gateway_secrets as mapping
    where mapping.gateway_id = gateway_record.gateway_id;

    if existing_secret_id is null then
      select vault.create_secret(
        gateway_record.gateway_secret,
        'ghost_clicker_gateway_' || gateway_record.gateway_id,
        'Identifiants persistants de passerelle Ghost Clicker'
      )
      into created_secret_id;

      insert into public.ghost_clicker_gateway_secrets (
        gateway_id,
        secret_id,
        updated_at
      ) values (
        gateway_record.gateway_id,
        created_secret_id,
        now()
      );
    else
      perform vault.update_secret(
        existing_secret_id,
        gateway_record.gateway_secret
      );
      update public.ghost_clicker_gateway_secrets
      set updated_at = now()
      where gateway_id = gateway_record.gateway_id;
    end if;
  end loop;

  for obsolete_secret_id in
    select mapping.secret_id
    from public.ghost_clicker_gateway_secrets as mapping
    where not (p_secrets ? mapping.gateway_id)
  loop
    delete from vault.secrets
    where id = obsolete_secret_id;
  end loop;

  return gateway_count;
end;
$$;

revoke all on function public.ghost_clicker_load_gateway_secrets()
  from public, anon, authenticated;
revoke all on function public.ghost_clicker_replace_gateway_secrets(jsonb)
  from public, anon, authenticated;
grant execute on function public.ghost_clicker_load_gateway_secrets()
  to service_role;
grant execute on function public.ghost_clicker_replace_gateway_secrets(jsonb)
  to service_role;

comment on table public.ghost_clicker_gateway_secrets is
  'Associe les passerelles Ghost Clicker aux secrets chiffrés de Supabase Vault.';
comment on function public.ghost_clicker_load_gateway_secrets() is
  'Charge les identifiants de passerelles depuis Vault pour le serveur uniquement.';
comment on function public.ghost_clicker_replace_gateway_secrets(jsonb) is
  'Remplace atomiquement le registre durable des identifiants de passerelles.';
