#!/bin/bash
# One PostgreSQL cluster, three bounded-context databases.
set -euo pipefail
for db in veynod veynod_kernel veynod_runtime; do
    if ! psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
        -tAc "SELECT 1 FROM pg_database WHERE datname = '${db}'" | grep -q 1; then
        psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
            -c "CREATE DATABASE ${db}"
    fi
done
