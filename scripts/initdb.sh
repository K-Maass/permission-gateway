#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
  --set "app_password=$PAOS_DB_PASSWORD" <<'SQL'
CREATE ROLE paos LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEROLE NOCREATEDB;
CREATE DATABASE paos OWNER paos;
REVOKE CONNECT ON DATABASE paos FROM PUBLIC;
SQL
