-- Выполни в Supabase → SQL Editor
create table if not exists settings (key text primary key, value jsonb not null default '{}'::jsonb);
create table if not exists channels (
  id bigserial primary key, chat_id bigint unique not null, title text, username text, link text,
  enabled boolean not null default true, created_at timestamptz not null default now());
create table if not exists users (
  tg_id bigint primary key, first_name text, username text, pending_url text,
  joined_at timestamptz not null default now(), last_seen timestamptz not null default now());
create table if not exists downloads (
  id bigserial primary key, tg_id bigint, platform text, kind text, ok boolean not null default true,
  created_at timestamptz not null default now());
create index if not exists downloads_created_idx on downloads (created_at);
create table if not exists updates (update_id bigint primary key, created_at timestamptz not null default now());

alter table settings enable row level security;
alter table channels enable row level security;
alter table users enable row level security;
alter table downloads enable row level security;
alter table updates enable row level security;
