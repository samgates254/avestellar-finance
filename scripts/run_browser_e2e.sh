#!/usr/bin/env bash
# Reproducible browser-E2E lifecycle for Avestellar Finance.
#
# Drives the REAL Next.js app + the REAL Django REST API against a disposable,
# explicitly-prefixed PostgreSQL database seeded with synthetic tenants only.
# Financial API calls are never mocked. The runner only ever starts/stops its OWN
# processes and only ever creates/drops a database carrying the
# "avestellar_live_http_" disposable prefix. It refuses to touch the ordinary
# development database or any pre-existing development server.
#
# Requires: bash, psql, a PostgreSQL server, the project .venv, and pnpm.
# Configure via environment (safe local defaults shown):
#   DATABASE_URL           postgresql://user:pass@host:port/<admin_db>  (DDL admin conn)
#   E2E_AV_DATABASE_NAME   disposable DB name  (default: avestellar_live_http_browser_e2e)
#   E2E_DJANGO_PORT        isolated Django port (default: 8200)
#   E2E_NEXT_PORT          isolated Next port   (default: 3100)
#   E2E_DROP_DB            1 to drop the disposable DB afterwards (default: 0)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND="$ROOT/frontend"
VENV_PY="$ROOT/.venv/bin/python"

# All transient files (launcher, pidfiles, logs) live under a project-local,
# git-ignored runtime dir. On some sandboxes `/tmp` is read-only, so we never
# depend on it: TMPDIR is forced here and every path is a fixed name under
# RUNTIME (no mktemp), so reruns overwrite cleanly and nothing leaks.
RUNTIME="$ROOT/.e2e-runtime"
mkdir -p "$RUNTIME"
export TMPDIR="$RUNTIME"
_LAUNCHER="$RUNTIME/launcher.py"
DJ_PIDFILE="$RUNTIME/django.pid"
NEXT_PIDFILE="$RUNTIME/next.pid"
RuntimeLog() { echo "$RUNTIME/$1"; }

DISPOSABLE="${E2E_AV_DATABASE_NAME:-avestellar_live_http_browser_e2e}"
DJANGO_PORT="${E2E_DJANGO_PORT:-8200}"
NEXT_PORT="${E2E_NEXT_PORT:-3100}"
DROP_DB="${E2E_DROP_DB:-0}"

fail() { echo "E2E ABORT: $*" >&2; exit 1; }

# ---- 1. Validate environment, target database, and safety invariants --------
[[ -x "$VENV_PY" ]] || fail "project venv python not found at $VENV_PY"
[[ -d "$FRONTEND" ]] || fail "frontend directory not found"
[[ -n "${DATABASE_URL:-}" ]] || fail "DATABASE_URL must be set (a PostgreSQL connection)"

# Refuse obviously unsafe fixed targets.
(( DJANGO_PORT == 8000 )) && fail "Django port 8000 is reserved for the pre-existing dev server; pick another"
(( DJANGO_PORT == NEXT_PORT )) && fail "Django and Next ports must differ"

case "$DISPOSABLE" in
  avestellar_live_http_*) : ;;
  *) fail "E2E_AV_DATABASE_NAME ('$DISPOSABLE') must start with the disposable prefix 'avestellar_live_http_'" ;;
esac

# Parse DATABASE_URL through django-environ (the same parser the app uses) so no
# credentials are hard-coded here.
eval "$("$VENV_PY" - "$DATABASE_URL" <<'PY'
import sys, shlex
import environ
cfg = environ.Env().db_url_config(sys.argv[1])
engine = cfg.get("ENGINE", "")
def emit(k, v):
    print(f"{k}={shlex.quote(str(v or ''))}")
if "postgresql" not in engine:
    sys.stderr.write("DATABASE_URL must point at a PostgreSQL database\n"); sys.exit(1)
emit("E2E_PG_HOST", cfg.get("HOST"))
emit("E2E_PG_PORT", cfg.get("PORT") or 5432)
emit("E2E_PG_USER", cfg.get("USER"))
emit("E2E_PG_PASS", cfg.get("PASSWORD"))
emit("E2E_PG_ADMIN_DB", cfg.get("NAME"))
PY
)" || fail "could not parse DATABASE_URL (expected PostgreSQL)"

[[ -n "${E2E_PG_ADMIN_DB:-}" ]] || fail "DATABASE_URL has no database name to use for DDL admin"
[[ "$E2E_PG_ADMIN_DB" != "$DISPOSABLE" ]] || fail "admin DB and disposable DB must differ"

# Pass the password via an exported variable rather than a `VAR=value command`
# inline prefix: some restricted/sandboxed exec contexts drop or redact inline
# assignment prefixes, which surfaces as a spurious "password authentication
# failed" even though the credential is correct.
psql_admin() {
  local pgpw="$E2E_PG_PASS"
  export PGPASSWORD="$pgpw"
  psql -h "$E2E_PG_HOST" -p "$E2E_PG_PORT" -U "$E2E_PG_USER" -d "$E2E_PG_ADMIN_DB" -v ON_ERROR_STOP=1 "$@"
  local rc=$?
  unset PGPASSWORD
  return $rc
}

# Guarded teardown of the disposable DB (used both for reset and optional drop).
validate_disposable_name() {
  [[ "$1" =~ ^avestellar_live_http_[a-zA-Z0-9_]+$ ]] || fail "refusing to operate on non-disposable DB name '$1'"
}

# ---- Preflight: refuse to start if our isolated ports are already in use ----
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&-; return 0; } || return 1; }
port_busy "$DJANGO_PORT" && fail "port $DJANGO_PORT already in use; free it or set E2E_DJANGO_PORT (the runner never kills a pre-existing server)"
port_busy "$NEXT_PORT"   && fail "port $NEXT_PORT already in use; free it or set E2E_NEXT_PORT"

DJPID=""
DJLAUNCHER=""
NXTJID=""
NEXTLAUNCHER=""
# Self-detaching session launcher. It starts the command in a brand-new session
# (its own process group), writes that leader PID to the pidfile, then waits for
# the runner to SIGTERM it. On teardown it walks /proc for every descendant and
# signals each one individually, so nested children that called setpgid (e.g.
# `pnpm exec next start` -> detached `next-server`) are still reaped. We avoid
# `cmd &` + `$!` for the *server* PID because bash job-control puts each `&`
# job in its own group and the launcher would then be a throwaway subshell PID,
# not the session leader we need to signal.
cat >"$_LAUNCHER" <<'PY'
import subprocess, os, signal, sys, time

def descendants(root):
    """Every PID that is a strict descendant of `root`, via /proc/*/stat ppid."""
    children = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        p = int(entry)
        try:
            with open(f"/proc/{entry}/stat") as f:
                # Format: pid (comm) state ppid ...  — comm may contain spaces/')',
                # so split on the last ')' rather than whitespace from the left.
                after = f.read().rsplit(")", 1)[1].split()
            ppid = int(after[1])
            children.setdefault(ppid, []).append(p)
        except (FileNotFoundError, ProcessLookupError, PermissionError,
                IndexError, ValueError):
            continue
    out, stack = [], [root]
    while stack:
        cur = stack.pop()
        for c in children.get(cur, []):
            out.append(c)
            stack.append(c)
    return out

def signal_tree(root, sig):
    for pid in descendants(root) + [root]:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

def main():
    pidfile, cmd = sys.argv[1], sys.argv[2:]
    p = subprocess.Popen(cmd, start_new_session=True)
    with open(pidfile, "w") as f:
        f.write(str(p.pid))

    def terminate(_sig, _frm):
        signal_tree(p.pid, signal.SIGTERM)
        deadline = time.time() + 3.0
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        if p.poll() is None:
            signal_tree(p.pid, signal.SIGKILL)
            try:
                p.wait(timeout=2)
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    p.wait()

if __name__ == "__main__":
    main()
PY
# stop_job takes the launcher PID (the python wrapper) and the child PID (the
# session leader). Signalling the launcher runs its /proc-walking handler; we
# then wait for it to exit (with SIGKILL escalation) and, as a last resort,
# hard-kill the child group directly in case the launcher died unexpectedly.
# Teardown is best-effort: a lingering zombie (parent-less child awaiting reap)
# is not a failure, and we never want `set -e` to abort the run *after* all
# tests have passed. Always return 0; surface the survivor with a warning.
stop_job() {
  local launcher="$1" child="$2" stat
  if [[ -n "$launcher" ]] && kill -0 "$launcher" 2>/dev/null; then
    kill -TERM "$launcher" 2>/dev/null || true
    for _ in $(seq 1 40); do
      kill -0 "$launcher" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$launcher" 2>/dev/null; then
      kill -KILL "$launcher" 2>/dev/null || true
    fi
    wait "$launcher" 2>/dev/null || true
  fi
  if [[ -n "$child" ]] && kill -0 "$child" 2>/dev/null; then
    stat="$(ps -p "$child" -o stat= 2>/dev/null | tr -d ' ' || true)"
    if [[ "$stat" != Z* ]]; then
      kill -KILL -- "-$child" 2>/dev/null || kill -KILL "$child" 2>/dev/null || true
      echo "WARN: E2E child $child (state=$stat) survived launcher teardown" >&2
    fi
  fi
  return 0
}
launch_session() { "$VENV_PY" "$_LAUNCHER" "$@" & }
cleanup() {
  stop_job "$NEXTLAUNCHER" "$NXTJID"
  stop_job "$DJLAUNCHER" "$DJPID"
}
trap cleanup EXIT

# ---- 2. Create/reset the disposable DB (prefix-guarded, never the dev DB) ---
validate_disposable_name "$DISPOSABLE"
echo "== resetting disposable database $DISPOSABLE =="
psql_admin -q -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DISPOSABLE';" \
            -c "DROP DATABASE IF EXISTS \"$DISPOSABLE\";" \
            -c "CREATE DATABASE \"$DISPOSABLE\" OWNER \"$E2E_PG_USER\";" >/dev/null

# ---- 3. Migrate + seed under the guarded overlay settings -------------------
export DJANGO_SETTINGS_MODULE="config.live_http_e2e_settings"
export E2E_AV_DATABASE_NAME="$DISPOSABLE"
cd "$ROOT"
# The overlay re-asserts the PostgreSQL + avestellar_live_http_* invariants.
echo "== applying migrations =="
"$VENV_PY" manage.py migrate --noinput >/dev/null
echo "== seeding synthetic fixture =="
"$VENV_PY" scripts/seed_browser_e2e.py

# ---- 4. Start Django with the guarded E2E settings --------------------------
DJ_LOG="$(RuntimeLog django.log)"
echo "== starting Django on 127.0.0.1:$DJANGO_PORT (isolated DB) =="
# Purge any stale pidfile from a prior run before we launch, so the `-s` poll
# below cannot succeed with an old (dead) PID and hand stop_job the wrong target.
rm -f "$DJ_PIDFILE"
launch_session "$DJ_PIDFILE" "$VENV_PY" manage.py runserver "127.0.0.1:$DJANGO_PORT" --noreload --skip-checks >"$DJ_LOG" 2>&1
DJLAUNCHER=$!
for _ in $(seq 1 25); do [[ -s "$DJ_PIDFILE" ]] && break; sleep 0.2; done
DJPID="$(cat "$DJ_PIDFILE" 2>/dev/null || true)"
[[ -n "$DJPID" ]] || fail "Django launcher produced no PID (see $DJ_LOG)"
for _ in $(seq 1 30); do
  curl -fs "http://127.0.0.1:$DJANGO_PORT/api/v1/health/" >/dev/null 2>&1 && break
  sleep 1
done
curl -fs "http://127.0.0.1:$DJANGO_PORT/api/v1/health/" >/dev/null 2>&1 || fail "Django health check failed (see $DJ_LOG)"

# Confirm the running server is on the disposable DB, not the dev DB.
ACTUAL_DB="$("$VENV_PY" - <<'PY'
import django, os
django.setup()
from django.db import connection
print(connection.settings_dict["NAME"])
PY
)" || fail "cannot confirm active DB"
[[ "$ACTUAL_DB" == "$DISPOSABLE" ]] || fail "active Django DB ('$ACTUAL_DB') is not the disposable DB ('$DISPOSABLE')"
echo "== confirmed Django is serving $ACTUAL_DB =="

# ---- 5. Build Next with the proxy explicitly pinned to the isolated Django --
cd "$FRONTEND"
export API_PROXY_TARGET="http://127.0.0.1:$DJANGO_PORT"
echo "== building Next (API_PROXY_TARGET=$API_PROXY_TARGET) =="
NEXT_BUILD_LOG="$(RuntimeLog next-build.log)"
pnpm exec next build >"$NEXT_BUILD_LOG" 2>&1 || fail "Next build failed (see $NEXT_BUILD_LOG)"
grep -q "127.0.0.1:$DJANGO_PORT" .next/routes-manifest.json || fail "built routes-manifest does NOT pin $API_PROXY_TARGET; refusing to run"

# ---- 6. Start Next and run Playwright against that origin -------------------
NEXT_LOG="$(RuntimeLog next.log)"
echo "== starting Next on 127.0.0.1:$NEXT_PORT =="
rm -f "$NEXT_PIDFILE"
launch_session "$NEXT_PIDFILE" pnpm exec next start -p "$NEXT_PORT" >"$NEXT_LOG" 2>&1
NEXTLAUNCHER=$!
for _ in $(seq 1 25); do [[ -s "$NEXT_PIDFILE" ]] && break; sleep 0.2; done
NXTJID="$(cat "$NEXT_PIDFILE" 2>/dev/null || true)"
[[ -n "$NXTJID" ]] || fail "Next launcher produced no PID (see $NEXT_LOG)"
for _ in $(seq 1 30); do
  curl -fs "http://127.0.0.1:$NEXT_PORT/" >/dev/null 2>&1 && break
  sleep 1
done
curl -fs "http://127.0.0.1:$NEXT_PORT/" >/dev/null 2>&1 || fail "Next did not become ready (see $NEXT_LOG)"

PW_TMP="$RUNTIME/playwright-tmp"
mkdir -p "$PW_TMP"
echo "== running Playwright against http://127.0.0.1:$NEXT_PORT =="
set +e
TMPDIR="$PW_TMP" E2E_BASE_URL="http://127.0.0.1:$NEXT_PORT" pnpm exec playwright test
PW=$?
set -e

# ---- 7. Collect results -----------------------------------------------------
echo "== Playwright exit=$PW; reports in frontend/test-results and frontend/playwright-report =="

# ---- 8. Stop ONLY the processes this runner started (and their descendants) --
stop_job "$NEXTLAUNCHER" "$NXTJID"
stop_job "$DJLAUNCHER" "$DJPID"
NEXTLAUNCHER=""; DJLAUNCHER=""; NXTJID=""; DJPID=""
trap - EXIT

# ---- 9. Optionally drop the disposable DB (prefix-validated) ---------------
if [[ "$DROP_DB" == "1" ]]; then
  validate_disposable_name "$DISPOSABLE"
  echo "== dropping disposable database $DISPOSABLE =="
  psql_admin -q -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DISPOSABLE';" \
              -c "DROP DATABASE IF EXISTS \"$DISPOSABLE\";" >/dev/null
fi

exit $PW
