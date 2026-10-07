#!/usr/bin/env python3
"""
SQL for giving one client its own least-privilege Postgres login (finding F-01).

Before: every app-<client> container connected as the `postgres` SUPERUSER, so a
compromise of any one client exposed every client's database (and, via
COPY ... PROGRAM, the database server itself).

After: each client gets a role `coop_<slug>` that
  * is NOT a superuser and cannot create databases or roles,
  * owns only its own database `coop_<slug>` (the app runs DDL on boot, so it must
    own its schema),
  * is the only role (besides the superuser) allowed to CONNECT to that database,
  * cannot connect to any other client's database, nor to postgres/template1.

Usage (prints SQL; harden-db-roles.sh pipes it into psql):
    python3 db_roles.py stage1 <slug> <password>     # run against database `postgres`
    python3 db_roles.py stage2 <slug>                # run against database `coop_<slug>`
"""
import re
import sys

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
PASSWORD_RE = re.compile(r"^[A-Za-z0-9_\-]{20,128}$")   # token_urlsafe output; safe in SQL and URLs


def role_name(slug):
    return "coop_" + slug.replace("-", "_")


def db_name(slug):
    return "coop_" + slug


def stage1(slug, password):
    if not SLUG_RE.match(slug):
        raise ValueError("invalid client name")
    if not PASSWORD_RE.match(password):
        raise ValueError("password must be 20+ chars of letters, digits, _ or -")
    role, db = role_name(slug), db_name(slug)
    return f"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
    CREATE ROLE "{role}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{password}';
  ELSE
    ALTER ROLE "{role}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{password}';
  END IF;
END $$;

ALTER DATABASE "{db}" OWNER TO "{role}";

-- Nobody else may connect to this client's database (or any other client's) ...
DO $$
DECLARE d text;
BEGIN
  FOR d IN SELECT datname FROM pg_database WHERE datname LIKE 'coop\\_%' LOOP
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', d);
  END LOOP;
END $$;
-- ... and no client role needs the maintenance databases.
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;
"""


def stage2(slug):
    if not SLUG_RE.match(slug):
        raise ValueError("invalid client name")
    role = role_name(slug)
    return f"""
ALTER SCHEMA public OWNER TO "{role}";
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT format('%I.%I', schemaname, tablename) AS n FROM pg_tables WHERE schemaname = 'public' LOOP
    EXECUTE 'ALTER TABLE ' || r.n || ' OWNER TO "{role}"';
  END LOOP;
  FOR r IN SELECT format('%I.%I', sequence_schema, sequence_name) AS n
           FROM information_schema.sequences WHERE sequence_schema = 'public' LOOP
    EXECUTE 'ALTER SEQUENCE ' || r.n || ' OWNER TO "{role}"';
  END LOOP;
  FOR r IN SELECT format('%I.%I', schemaname, viewname) AS n FROM pg_views WHERE schemaname = 'public' LOOP
    EXECUTE 'ALTER VIEW ' || r.n || ' OWNER TO "{role}"';
  END LOOP;
END $$;
"""


if __name__ == "__main__":
    try:
        if sys.argv[1] == "stage1":
            print(stage1(sys.argv[2], sys.argv[3]))
        elif sys.argv[1] == "stage2":
            print(stage2(sys.argv[2]))
        else:
            raise IndexError
    except (IndexError, ValueError) as exc:
        sys.exit(f"{exc or __doc__}")
