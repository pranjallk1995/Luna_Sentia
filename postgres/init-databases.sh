#!/bin/sh
# Give every Luna app, and Sentia's sign-on service (auth), its own login role and
# database on the shared server.
# Idempotent: existing roles get their password re-synced, existing databases are kept.
# Every database gets the pgvector extension.
# LUNA<APP>_DB_PASSWORD must match POSTGRES_PASSWORD in that app's .env.
set -eu

for app in auth visia vita lumia fiora lexia; do
    name="luna$app"
    password=$(printenv "$(echo "LUNA${app}_DB_PASSWORD" | tr '[:lower:]' '[:upper:]')" || true)
    if [ -z "$password" ]; then
        echo "Skipping $name: no password set"
        continue
    fi
    psql -v ON_ERROR_STOP=1 -v name="$name" -v password="$password" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'name')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'name') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'name', :'password') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'name', :'name')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'name') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'name') \gexec
SQL
    # pgvector is not a trusted extension, so the superuser installs it for the app.
    psql -v ON_ERROR_STOP=1 -d "$name" -c "CREATE EXTENSION IF NOT EXISTS vector"
    echo "Ready: $name"
done
