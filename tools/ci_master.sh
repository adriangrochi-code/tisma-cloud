#!/usr/bin/env bash
# A Tisma Cloud master for the end-to-end tests of other repositories (the apps), from the built Linux package, with
# no one at the keyboard: SQLite, the master's own node computing with the package's tisma-engine, a test account and
# a library certified with a throwaway project key. For CI runners only; never on a real master.
#
#   tools/ci_master.sh start PACKAGE_DIR [PORT] [OUT_FILE]
#   tools/ci_master.sh stop  PACKAGE_DIR
#
# PACKAGE_DIR is the unpacked artifact «tisma-cloud-linux» of this repository's CI (the program tisma-cloud with
# tisma-engine/, tisma-profiles/ and tisma-relay/ beside it). The data of the master go to PACKAGE_DIR/../ci-master.
# PORT defaults to 18994. OUT_FILE (default: $GITHUB_OUTPUT when set, else stdout) gets, as KEY=VALUE lines:
#   url                  http://127.0.0.1:PORT (plain HTTP)
#   email, password      the test account, for POST /v1/auth/login
#   token                a session token of that account (Authorization: Bearer ...)
#   library_project_key  the throwaway project key, 64 hex: the apps check the library's certificate with it
# It returns once GET /v1/health answers and GET /v1/status counts the master's node as connected.
# Needs: bash, curl, openssl (Ed25519), python3 (only to read one JSON field).
set -euo pipefail

cmd=${1:?usage: ci_master.sh start|stop PACKAGE_DIR [PORT] [OUT_FILE]}
pkg=$(cd "${2:?the unpacked tisma-cloud-linux package}" && pwd)
exe="$pkg/tisma-cloud"
export TISMA_HOME="$(dirname "$pkg")/ci-master"
mkdir -p "$TISMA_HOME"
chmod +x "$exe" "$pkg"/tisma-engine/* "$pkg"/tisma-relay/* 2>/dev/null || true

if [ "$cmd" = stop ]; then
  "$exe" stop
  exit 0
fi
[ "$cmd" = start ] || { echo "usage: ci_master.sh start|stop PACKAGE_DIR [PORT] [OUT_FILE]" >&2; exit 2; }

port=${3:-18994}
out=${4:-${GITHUB_OUTPUT:-/dev/stdout}}
email=e2e@tisma.test
password=e2e-$(openssl rand -hex 8)

"$exe" role master > /dev/null
"$exe" config port "$port" > /dev/null
"$exe" admin plan-set e2e --hours 100 --concurrent 4 --classes all --public > /dev/null
# The password is read twice from stdin (no terminal here); a second run in the same folder sets it again.
printf '%s\n%s\n' "$password" "$password" | "$exe" admin user-add "$email" --plan e2e --password > /dev/null 2>&1 ||
  printf '%s\n%s\n' "$password" "$password" | "$exe" admin user-set "$email" --plan e2e --password > /dev/null 2>&1
"$exe" admin user-set "$email" --paid-until 2099-01-01 > /dev/null
token=$("$exe" admin user-token "$email" | grep -o 'tu_[A-Za-z0-9_-]*' | head -n 1)

# A throwaway project key (Ed25519, PKCS#8 PEM), only to certify this master's library key.
pem="$TISMA_HOME/e2e-project-key.pem"
[ -f "$pem" ] || openssl genpkey -algorithm ed25519 -out "$pem" 2> /dev/null
"$exe" admin library-certify --key "$pem" > /dev/null
project_key=$(openssl pkey -in "$pem" -pubout -outform DER | tail -c 32 | od -An -tx1 | tr -d ' \n')

"$exe" start > /dev/null
url="http://127.0.0.1:$port"
for _ in $(seq 1 60); do curl -sf "$url/v1/health" > /dev/null 2>&1 && break; sleep 1; done
curl -sf "$url/v1/health" > /dev/null || { "$exe" logs 80; exit 1; }
# The master's own node joins a few seconds after it; /v1/status is cached for 15 s.
online=0
for _ in $(seq 1 45); do
  online=$(curl -sf "$url/v1/status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["nodes_online"])' || echo 0)
  [ "$online" -ge 1 ] && break
  sleep 2
done
[ "$online" -ge 1 ] || { echo "the master's node did not connect" >&2; "$exe" logs 80; exit 1; }

{
  echo "url=$url"
  echo "email=$email"
  echo "password=$password"
  echo "token=$token"
  echo "library_project_key=$project_key"
} >> "$out"
