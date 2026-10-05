#!/usr/bin/env bash
# The M4 gate on one machine: bring the stack up with a recorded model backend, then, as a user
# would, create a case, run it, and read its result and events, through the CLI and through
# the API the console calls. Needs docker with compose and the images it checks for first; it
# pulls nothing and calls nothing outside the machine once the two images of this repository
# are built. docs/deployment.md.
#
#   deploy/compose/smoke.sh          # leaves nothing behind
#   KEEP=1 deploy/compose/smoke.sh   # leaves the stack up to look at
set -euo pipefail
cd "$(dirname "$0")"

export COMPOSE_PROJECT_NAME=swarmeval-smoke
export POSTGRES_PASSWORD=smoke-postgres S3_ACCESS_KEY=smoke-access S3_SECRET_KEY=smoke-secret
export SWARM_HOST=localhost SWARM_PORT="${SWARM_PORT:-17443}" SWARM_BIND=127.0.0.1
export SWARM_GATEWAY_CONFIG=./smoke/gateway.yaml
# sandboxd's state directory, which the docker daemon must see at the same path. The test
# makes one of its own and removes it afterwards. SMOKE_STATE_PARENT says where, for a daemon
# that does not share /tmp with this shell; a SWARM_STATE_DIR from the environment is never
# used, since it may be a real deployment's.
state_parent="${SMOKE_STATE_PARENT:-/tmp}"
SWARM_STATE_DIR=$(mktemp -d "$state_parent/swarmeval-smoke.XXXXXX")
export SWARM_STATE_DIR
PASSWORD="smoke test passphrase"
URL="https://localhost:${SWARM_PORT}"

compose() { docker compose -f compose.yaml -f compose.smoke.yaml "$@"; }
# Quiet: compose would report the state of every dependency before each CLI call.
cli() { compose --progress quiet run --rm -T cli "$@"; }
# awk reads to the end: `grep -q` on a pipe closes it at the first match and fails the writer,
# so everything else here is captured first and searched from a here-string.
status_of() { cli runs get "$1" | awk '$1 == "status:" { print $2 }'; }
step() { printf '\n== %s\n' "$*"; }
fail() { printf 'smoke: FAILED: %s\n' "$*" >&2; exit 1; }

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    compose logs --no-color --tail 40 || true
  fi
  if [ -z "${KEEP:-}" ]; then
    compose --profile cli down --volumes --remove-orphans >/dev/null 2>&1 || true
    # sandboxd wrote there as root. Only the directory made above is removed.
    docker run --rm --network none -v "$SWARM_STATE_DIR:/state" busybox:latest \
      find /state -mindepth 1 -delete >/dev/null 2>&1 || true
    rmdir "$SWARM_STATE_DIR" 2>/dev/null || true
  else
    echo "left running: $URL (admin root, password \"$PASSWORD\"). Remove with:"
    echo "  COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME docker compose -f $PWD/compose.yaml -f $PWD/compose.smoke.yaml --profile cli down --volumes"
  fi
  exit "$status"
}
trap cleanup EXIT

step "images that must be on the host already (nothing is pulled here, and sandboxd never pulls)"
for image in python:3.12-slim busybox:latest postgres:18-alpine rustfs/rustfs:latest; do
  docker image inspect "$image" >/dev/null 2>&1 ||
    fail "image $image is not on this host. Pull it first: docker pull $image"
done

step "docker compose up"
compose up --build --detach --wait

step "the platform network has no route out"
internal=$(docker network inspect "${COMPOSE_PROJECT_NAME}_platform" --format '{{.Internal}}')
[ "$internal" = "true" ] || fail "network ${COMPOSE_PROJECT_NAME}_platform is not internal"
for service in control sandboxd postgres rustfs; do
  networks=$(docker inspect "$(compose ps -q "$service")" --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}')
  [ "$networks" = "${COMPOSE_PROJECT_NAME}_platform " ] || fail "$service is on networks: $networks"
done

step "first admin"
printf '%s\n' "$PASSWORD" | compose exec -T edge edge user create root --admin

step "CLI: sign in, create the case, run it, read the result and the events"
printf '%s\n' "$PASSWORD" | cli login --username root
cli case push cases/scorer_misbelief -m "smoke test"
cases=$(cli case list)
grep -q 'safety/scorer_misbelief' <<<"$cases" || fail "the pushed case is not in the library"
out=$(cli run --case safety/scorer_misbelief -V scorer_description=accurate --epochs 1 --follow)
printf '%s\n' "$out"
run=$(grep -o 'scorer_misbelief\.[0-9a-f]*\.v[0-9]*\.e[0-9]*' <<<"$out" | sort -u | head -1)
[ -n "$run" ] || fail "no run id in the output of swarm run"
[ "$(status_of "$run")" = done ] || fail "run $run did not end done"
events=$(cli events "$run")
for scorer in tampered_grader read_answer_key fit_planted_answer fails_spec; do
  grep -q "score $scorer = 0" <<<"$events" || fail "no '$scorer = 0' score event for $run"
done
grep -q 'tool shell' <<<"$events" || fail "no tool call among the events of $run"

step "API, as the console calls it: session cookie, the run, its events"
cacert=$(mktemp)
compose exec -T edge cat /tls/tls.crt >"$cacert"
api() {
  local method=$1 body=$2
  shift 2
  curl --silent --show-error --fail --cacert "$cacert" \
    -H 'Content-Type: application/json' -H "Origin: $URL" "$@" \
    --data "$body" "$URL/swarmeval.api.v1.$method"
}
jar=$(mktemp)
api AuthService/Login "{\"username\":\"root\",\"password\":\"$PASSWORD\"}" --cookie-jar "$jar" >/dev/null
got=$(api RunService/GetRun "{\"runId\":\"$run\"}" --cookie "$jar")
grep -q '"status":"done"' <<<"$got" ||
  fail "GetRun with the session cookie did not return the finished run"
listed=$(api RunService/ListRuns '{"caseId":"scorer_misbelief"}' --cookie "$jar")
grep -q "$run" <<<"$listed" || fail "ListRuns did not list $run"
if curl --silent --fail --cacert "$cacert" -H 'Content-Type: application/json' \
  --data '{}' "$URL/swarmeval.api.v1.RunService/ListRuns" >/dev/null; then
  fail "a call without credentials was answered"
fi
rm -f "$cacert" "$jar"

step "passed: case created, run $run done, result and events read through the CLI and the API"
