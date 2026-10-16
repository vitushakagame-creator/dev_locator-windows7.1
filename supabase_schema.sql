-- DevSpace Supabase schema. Run the whole file in Supabase Dashboard > SQL Editor.
create extension if not exists pgcrypto;

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  username text not null check (char_length(username) between 2 and 24),
  bio text not null default '' check (char_length(bio) <= 240),
  created_at timestamptz not null default now()
);
create unique index if not exists profiles_username_lower_unique on public.profiles (lower(username));

create table if not exists public.projects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  title text not null check (char_length(title) between 1 and 120),
  language text not null default 'Другой',
  category text not null default 'Другое',
  description text not null default '' check (char_length(description) <= 5000),
  image_path text,
  symbol text not null default '</>',
  theme text not null default 'blue',
  created_at timestamptz not null default now()
);
create index if not exists projects_created_at_idx on public.projects (created_at desc);
create index if not exists projects_user_id_idx on public.projects (user_id);

create table if not exists public.project_files (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references public.projects(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  file_name text not null,
  storage_path text not null unique,
  size_bytes bigint not null default 0 check (size_bytes >= 0 and size_bytes <= 10485760),
  mime_type text not null default 'application/octet-stream',
  created_at timestamptz not null default now()
);
create index if not exists project_files_project_id_idx on public.project_files (project_id);

create table if not exists public.project_likes (
  project_id uuid not null references public.projects(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (project_id, user_id)
);

-- Ensure every Auth user gets a public profile. This also covers Google OAuth.
create or replace function public.devspace_create_profile()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  base_name text;
  candidate text;
  suffix integer := 0;
begin
  base_name := coalesce(
    nullif(trim(new.raw_user_meta_data ->> 'username'), ''),
    nullif(trim(new.raw_user_meta_data ->> 'full_name'), ''),
    nullif(trim(new.raw_user_meta_data ->> 'name'), ''),
    split_part(coalesce(new.email, 'member'), '@', 1)
  );
  base_name := left(regexp_replace(base_name, '[^[:alnum:]_ .-]', '', 'g'), 24);
  if char_length(trim(base_name)) < 2 then base_name := 'member'; end if;
  candidate := trim(base_name);
  while exists (select 1 from public.profiles p where lower(p.username) = lower(candidate) and p.id <> new.id) loop
    suffix := suffix + 1;
    candidate := left(trim(base_name), 24 - char_length(suffix::text) - 1) || '_' || suffix::text;
  end loop;
  insert into public.profiles (id, username, bio)
  values (new.id, candidate, '')
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists devspace_auth_user_created on auth.users;
create trigger devspace_auth_user_created
after insert on auth.users
for each row execute procedure public.devspace_create_profile();

alter table public.profiles enable row level security;
alter table public.projects enable row level security;
alter table public.project_files enable row level security;
alter table public.project_likes enable row level security;

drop policy if exists "Profiles are publicly readable" on public.profiles;
create policy "Profiles are publicly readable" on public.profiles for select using (true);
drop policy if exists "Users can update their own profile" on public.profiles;
create policy "Users can update their own profile" on public.profiles for update to authenticated using (auth.uid() = id) with check (auth.uid() = id);
drop policy if exists "Users can insert their own profile" on public.profiles;
create policy "Users can insert their own profile" on public.profiles for insert to authenticated with check (auth.uid() = id);

drop policy if exists "Projects are publicly readable" on public.projects;
create policy "Projects are publicly readable" on public.projects for select using (true);
drop policy if exists "Authenticated users can create own projects" on public.projects;
create policy "Authenticated users can create own projects" on public.projects for insert to authenticated with check (auth.uid() = user_id);
drop policy if exists "Owners can update projects" on public.projects;
create policy "Owners can update projects" on public.projects for update to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "Owners can delete projects" on public.projects;
create policy "Owners can delete projects" on public.projects for delete to authenticated using (auth.uid() = user_id);

drop policy if exists "Project file metadata is public" on public.project_files;
create policy "Project file metadata is public" on public.project_files for select using (true);
drop policy if exists "Owners can add project file metadata" on public.project_files;
create policy "Owners can add project file metadata" on public.project_files for insert to authenticated with check (
  auth.uid() = user_id and exists (select 1 from public.projects p where p.id = project_id and p.user_id = auth.uid())
);
drop policy if exists "Owners can delete project file metadata" on public.project_files;
create policy "Owners can delete project file metadata" on public.project_files for delete to authenticated using (auth.uid() = user_id);

drop policy if exists "Likes are publicly readable" on public.project_likes;
create policy "Likes are publicly readable" on public.project_likes for select using (true);
drop policy if exists "Users can like as themselves" on public.project_likes;
create policy "Users can like as themselves" on public.project_likes for insert to authenticated with check (auth.uid() = user_id);
drop policy if exists "Users can remove their own likes" on public.project_likes;
create policy "Users can remove their own likes" on public.project_likes for delete to authenticated using (auth.uid() = user_id);

-- Public bucket: uploaded project files are intentionally downloadable by site visitors.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('project-files', 'project-files', true, 10485760, null)
on conflict (id) do update set public = true, file_size_limit = 10485760;

drop policy if exists "DevSpace files are publicly downloadable" on storage.objects;
create policy "DevSpace files are publicly downloadable" on storage.objects for select using (bucket_id = 'project-files');
drop policy if exists "Users can upload to their own DevSpace folder" on storage.objects;
create policy "Users can upload to their own DevSpace folder" on storage.objects for insert to authenticated with check (
  bucket_id = 'project-files' and (storage.foldername(name))[1] = auth.uid()::text
);
drop policy if exists "Users can delete their own DevSpace files" on storage.objects;
create policy "Users can delete their own DevSpace files" on storage.objects for delete to authenticated using (
  bucket_id = 'project-files' and (storage.foldername(name))[1] = auth.uid()::text
);

-- Explicit API grants; RLS policies above still decide which rows are accessible.
grant usage on schema public to anon, authenticated;
grant select on public.profiles, public.projects, public.project_files, public.project_likes to anon, authenticated;
grant insert, update, delete on public.profiles, public.projects, public.project_files, public.project_likes to authenticated;
