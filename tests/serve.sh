#!/bin/sh
set -eu
cd /opt/telegram888
export DATABASE_URL='dbname=telegram888_test user=telegram888 host=/var/run/postgresql'
export VAULT_KEY='Z3ViNURuaHI1bUZ4czF6Q19jb2RleF90ZXN0X2tleV8='
export SETUP_CODE='isolated-testing-only'
export PUBLIC_ORIGIN='https://104.168.91.152:8445'
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8091 --workers 1
