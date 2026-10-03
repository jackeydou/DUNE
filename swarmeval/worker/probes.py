"""The isolation self-check: probes run in every sandbox after the run's sandboxes exist and
before any agent turn (docs/services/orchestrator.md#isolation-self-check). The run fails if any
probe gets through.

Three steps, each one command per sandbox, all sandboxes at once:

1. `plant`: a marker file in each writable, unprotected key path and in `/dev/shm`, and a
   process whose command line carries the marker.
2. `check`: from each sandbox, look for every other sandbox's marker files and process and
   resolve its names, and check this sandbox has no interface but loopback and cannot connect
   out. Each sandbox first finds its own markers, so a probe that cannot see anything in this
   image reports `unverified` instead of passing.
3. `clean`: remove the markers and kill the marker process, so nothing of the probe is in the
   baseline the agents' first call is diffed against, or among its processes.

Every command runs as root through sandboxd's `Exec`, with call ids `probe:plant`,
`probe:check`, and `probe:clean`, and is recorded as an `isolation_probe` event; `check` events
carry the findings. Scripts are POSIX `sh` and use only what sandboxd already requires (`sh`,
`sleep`, `tr`), plus whichever of `nc`, `python3`, `getent`, and `nslookup` the image has; a
probe with none of its tools reports `unverified`.

A marker is passed to a script in two halves and joined inside it, so the checking script's own
command line never carries a whole marker for the `/proc` scan to find. A peer's names are passed
the same way and scrubbed from what the script prints: the hostname is the peer's sandbox canary
token, which must not appear in this sandbox's `/proc` or in its `isolation_probe` events.
"""

import asyncio
import logging
import secrets
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, get_args

from swarmeval.runtime.ports import SandboxExecutor
from swarmeval.runtime.records import (
    EventDraft,
    Exec,
    ExecResult,
    IsolationProbeRecord,
    LifecycleRecord,
    ProbeFinding,
    ProbeName,
    ProbeOutcome,
    Transaction,
)
from swarmeval.runtime.writer import RunWriter

log = logging.getLogger(__name__)

PROBE_USER = "0"
"""Root, which reads every process's `/proc` entries and writes where the image lets root."""
CONNECT_TARGET = ("1.1.1.1", "80")
"""A public address the `connect` probe tries. A sandbox with no network fails at once."""
MARKER_PREFIX = ".isolation-probe-"
SHM = "/dev/shm"
"""Per IPC namespace: a marker here is visible to another sandbox only if they share one."""
TIMEOUT_S = {"plant": 30.0, "check": 60.0, "clean": 30.0}

Step = Literal["plant", "check", "clean"]
_PROBES: frozenset[str] = frozenset(get_args(ProbeName))
_OUTCOMES: frozenset[str] = frozenset(get_args(ProbeOutcome))

_PLANT = """m=$1; shift
for d in "$@"; do
  if [ -d "$d" ] && : > "$d/$m" 2>/dev/null; then printf 'planted %s\\n' "$d"; fi
done
sh -c 'sleep 120; :' "$m" >/dev/null 2>&1 &
printf 'proc %s\\n' "$!"
"""
"""Args: marker, then directories. The marker process ends by itself after 120 s if `clean`
never runs."""

_CLEAN = """m=$1 p=$2; shift 2
for d in "$@"; do rm -f "$d/$m"; done
[ -n "$p" ] || exit 0
kids=
for d in /proc/[0-9]*; do
  { read -r stat < "$d/stat"; } 2>/dev/null || continue
  set -- ${stat##*) }
  [ "$2" = "$p" ] && kids="$kids ${d#/proc/}"
done
kill -9 "$p" $kids 2>/dev/null
i=0
while :; do
  alive=
  for q in $p $kids; do [ -d "/proc/$q" ] && alive="$alive $q"; done
  [ -z "$alive" ] && exit 0
  [ $i -ge 50 ] && break
  sleep 0.1; i=$((i + 1))
done
printf 'alive%s\\n' "$alive"
"""
"""Args: marker, the marker process's pid (empty if it never started), then the directories it
was planted in. Prints `alive <pids>` if the process or its child outlived 5 s."""

_CHECK_LIB = """say() { printf 'R %s %s %s %s\\n' "$1" "$2" "$3" "$4"; }
proc_with() {
  for d in /proc/[0-9]*; do
    case $(tr '\\000\\n' '  ' < "$d/cmdline" 2>/dev/null) in
      *"$1$2"*) printf '%s' "${d#/proc/}"; return 0 ;;
    esac
  done
  return 1
}
resolve() {
  if command -v getent >/dev/null 2>&1; then
    getent hosts "$1" 2>/dev/null; return $(($? != 0))
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import socket, sys; print(socket.gethostbyname(sys.argv[1]))' "$1" 2>/dev/null
    return $(($? != 0))
  elif command -v nslookup >/dev/null 2>&1; then
    out=$(nslookup "$1" 2>&1)
    case $out in *"Name:"*) printf '%s' "$out"; return 0 ;; esac
    return 1
  fi
  return 2
}
probe_interfaces() {
  names=
  if [ -r /proc/net/dev ]; then
    while IFS= read -r line; do
      case $line in *:*) n=${line%%:*}; names="$names ${n##* }" ;; esac
    done < /proc/net/dev
  elif [ -d /sys/class/net ]; then
    for i in /sys/class/net/*; do [ -e "$i" ] && names="$names ${i##*/}"; done
  else
    say interfaces - unverified "neither /proc/net/dev nor /sys/class/net is readable"
    return
  fi
  for n in $names; do
    [ "$n" = lo ] || { say interfaces - leaked "interfaces:$names"; return; }
  done
  say interfaces - isolated "interfaces:$names"
}
probe_connect() {
  if command -v nc >/dev/null 2>&1; then
    tool=nc
    nc -w 2 "$1" "$2" </dev/null >/dev/null 2>&1
  elif command -v python3 >/dev/null 2>&1; then
    tool=python3
    python3 -c 'import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 2)' \\
      "$1" "$2" >/dev/null 2>&1
  else
    say connect - unverified "the image has neither nc nor python3"
    return
  fi
  if [ $? = 0 ]; then say connect - leaked "$tool connected to $1:$2"
  else say connect - isolated "$tool could not connect to $1:$2"; fi
}
probe_proc() {  # peer, own marker halves, peer marker halves
  if ! proc_with "$2" "$3" >/dev/null; then
    say proc "$1" unverified "cannot see this sandbox's own marker process"
  elif pid=$(proc_with "$4" "$5"); then
    say proc "$1" leaked "pid $pid carries its marker"
  else
    say proc "$1" isolated "no process carries its marker"
  fi
}
probe_files() {  # peer, own marker halves, a directory holding it, peer marker halves, dirs
  peer=$1 own=$2$3 here=$4 theirs=$5$6
  shift 6
  if [ ! -e "$here/$own" ]; then
    say shared_path "$peer" unverified "cannot see this sandbox's own marker file"
    return
  fi
  for d in "$@"; do
    [ -e "$d/$theirs" ] && { say shared_path "$peer" leaked "$d/$theirs is visible"; return; }
  done
  say shared_path "$peer" isolated "not in $*"
}
scrub() {  # text, name: the text with every occurrence of the name replaced by <name>
  s=$1 r=
  while :; do
    case $s in
      *"$2"*) r="$r${s%%"$2"*}<name>"; s=${s#*"$2"} ;;
      *) printf '%s' "$r$s"; return ;;
    esac
  done
}
probe_dns() {  # peer, then per name: what it is, its two halves
  peer=$1 tried=
  shift
  while [ $# -ge 3 ]; do
    what=$1 n=$2$3
    shift 3
    tried="${tried:+$tried, }$what"
    out=$(resolve "$n")
    rc=$?
    if [ $rc = 2 ]; then
      say dns "$peer" unverified "the image has no getent, python3, or nslookup"
      return
    fi
    if [ $rc = 0 ]; then
      say dns "$peer" leaked "its $what resolves: $(scrub "$out" "$n" | tr '\\n' ' ')"
      return
    fi
  done
  say dns "$peer" isolated "its $tried do not resolve"
}
"""
"""Shell functions the generated `check` script calls. Results are lines
`R <probe> <peer or -> <outcome> <detail>`."""


class IsolationError(Exception):
    """A probe got through, or a probe's process outlived the self-check. The run fails."""


@dataclass(frozen=True)
class ProbeSandbox:
    sandbox_id: str
    names: tuple[tuple[str, str], ...]
    """What another sandbox might resolve this one by, each as (what it is, the name): its
    hostname and its sandbox id. The hostname is the sandbox canary token, so names reach other
    sandboxes' scripts in two halves and are never printed back."""
    key_paths: tuple[str, ...]
    plant_dirs: tuple[str, ...]
    """Writable, unprotected key paths. A marker in a protected one would read as tampering."""


@dataclass(frozen=True)
class _Planted:
    marker: str
    dirs: tuple[str, ...]
    pid: str
    """Empty when the marker process did not start."""

    @property
    def halves(self) -> tuple[str, str]:
        return MARKER_PREFIX, self.marker.removeprefix(MARKER_PREFIX)


async def check_isolation(
    sandboxes: Sequence[ProbeSandbox], executor: SandboxExecutor, writer: RunWriter
) -> dict[str, tuple[ProbeFinding, ...]]:
    """Runs the self-check and commits its events. Returns the findings by the sandbox they
    were made in. Raises `IsolationError`, after committing a `failed` lifecycle event, if a
    probe got through or did not clean up."""
    markers = {s.sandbox_id: MARKER_PREFIX + secrets.token_hex(8) for s in sandboxes}
    planted = await _plant(sandboxes, markers, executor, writer)
    findings = await _check(sandboxes, planted, executor, writer)
    leftovers = await _clean(planted, executor, writer)

    flat = [(sandbox, f) for sandbox, found in findings.items() for f in found]
    problems = [
        f"sandbox `{sandbox}` {_pair(f)}: {f.probe} got through ({f.detail})"
        for sandbox, f in flat
        if f.outcome == "leaked"
    ]
    problems.extend(f"sandbox `{sandbox}`: {what}" for sandbox, what in leftovers.items())
    unverified = [f"`{s}` {f.probe} ({f.detail})" for s, f in flat if f.outcome == "unverified"]
    if unverified:
        log.warning("isolation probes could not run: %s", "; ".join(unverified))
    if problems:
        message = (
            "the isolation self-check failed before any agent turn: "
            + "; ".join(problems)
            + ". Sandboxes on this host are not isolated from each other or from the network; "
            "check sandboxd's --sandbox-network and --runtime."
        )
        record = LifecycleRecord(status="failed", reason="isolation self-check", error=message)
        await writer.commit(Transaction(events=[EventDraft(record=record)]))
        raise IsolationError(message)
    return findings


def _pair(finding: ProbeFinding) -> str:
    return f"and sandbox `{finding.peer}`" if finding.peer else "network"


async def _run_step(
    step: Step,
    commands: Mapping[str, Exec],
    executor: SandboxExecutor,
) -> dict[str, ExecResult]:
    ids = list(commands)
    results = await asyncio.gather(
        *(
            executor.exec(sandbox, PROBE_USER, commands[sandbox], call_id=f"probe:{step}")
            for sandbox in ids
        )
    )
    return dict(zip(ids, results, strict=True))


async def _commit(
    step: Step,
    commands: Mapping[str, Exec],
    results: Mapping[str, ExecResult],
    writer: RunWriter,
    findings: Mapping[str, tuple[ProbeFinding, ...]] | None = None,
) -> None:
    drafts = [
        EventDraft(
            record=IsolationProbeRecord(
                sandbox_id=sandbox,
                step=step,
                command=commands[sandbox],
                result=results[sandbox],
                findings=(findings or {}).get(sandbox, ()),
            )
        )
        for sandbox in sorted(commands)
    ]
    await writer.commit(Transaction(events=drafts))


async def _plant(
    sandboxes: Sequence[ProbeSandbox],
    markers: Mapping[str, str],
    executor: SandboxExecutor,
    writer: RunWriter,
) -> dict[str, _Planted]:
    commands = {
        s.sandbox_id: Exec(
            argv=("sh", "-c", _PLANT, "sh", markers[s.sandbox_id], *s.plant_dirs, SHM),
            timeout_s=TIMEOUT_S["plant"],
        )
        for s in sandboxes
    }
    results = await _run_step("plant", commands, executor)
    await _commit("plant", commands, results, writer)
    planted: dict[str, _Planted] = {}
    for sandbox, result in results.items():
        dirs: list[str] = []
        pid = ""
        for line in result.stdout.splitlines():
            word, _, rest = line.partition(" ")
            if word == "planted":
                dirs.append(rest)
            elif word == "proc" and rest.isdigit():
                pid = rest
        planted[sandbox] = _Planted(marker=markers[sandbox], dirs=tuple(dirs), pid=pid)
    return planted


def _check_script(
    me: ProbeSandbox, peers: Sequence[ProbeSandbox], planted: Mapping[str, _Planted]
) -> str:
    q = shlex.join
    own = planted[me.sandbox_id]
    lines = [_CHECK_LIB, "probe_interfaces", q(["probe_connect", *CONNECT_TARGET])]
    for peer in peers:
        theirs = planted[peer.sandbox_id]
        if theirs.pid:
            lines.append(q(["probe_proc", peer.sandbox_id, *own.halves, *theirs.halves]))
        else:
            lines.append(_unverified("proc", peer, "its marker process did not start"))
        if not own.dirs:
            lines.append(_unverified("shared_path", peer, "no directory here took a marker"))
        elif not theirs.dirs:
            lines.append(_unverified("shared_path", peer, "no directory there took a marker"))
        else:
            where = list(dict.fromkeys([*theirs.dirs, *me.key_paths, SHM]))
            call = ["probe_files", peer.sandbox_id, *own.halves, own.dirs[0], *theirs.halves]
            lines.append(q([*call, *where]))
        names = [part for what, name in peer.names for part in (what, *_halves(name))]
        lines.append(q(["probe_dns", peer.sandbox_id, *names]))
    return "\n".join(lines) + "\n"


def _halves(text: str) -> tuple[str, str]:
    middle = len(text) // 2
    return text[:middle], text[middle:]


def _unverified(probe: ProbeName, peer: ProbeSandbox, why: str) -> str:
    return shlex.join(["say", probe, peer.sandbox_id, "unverified", why])


async def _check(
    sandboxes: Sequence[ProbeSandbox],
    planted: Mapping[str, _Planted],
    executor: SandboxExecutor,
    writer: RunWriter,
) -> dict[str, tuple[ProbeFinding, ...]]:
    commands: dict[str, Exec] = {}
    expected: dict[str, list[tuple[ProbeName, str | None]]] = {}
    for me in sandboxes:
        peers = [s for s in sandboxes if s.sandbox_id != me.sandbox_id]
        script = _check_script(me, peers, planted)
        commands[me.sandbox_id] = Exec(argv=("sh", "-c", script), timeout_s=TIMEOUT_S["check"])
        expected[me.sandbox_id] = [("interfaces", None), ("connect", None)]
        for peer in peers:
            for probe in ("proc", "shared_path", "dns"):
                expected[me.sandbox_id].append((probe, peer.sandbox_id))
    results = await _run_step("check", commands, executor)
    findings = {
        sandbox: _findings(results[sandbox], expected[sandbox]) for sandbox in sorted(commands)
    }
    await _commit("check", commands, results, writer, findings)
    return findings


def _findings(
    result: ExecResult, expected: Sequence[tuple[ProbeName, str | None]]
) -> tuple[ProbeFinding, ...]:
    """One finding per expected probe. A probe that printed nothing parseable is `unverified`,
    with what the script said."""
    printed: dict[tuple[str, str | None], ProbeFinding] = {}
    for line in result.stdout.splitlines():
        parts = line.split(" ", 4)
        if len(parts) < 4 or parts[0] != "R" or parts[1] not in _PROBES:
            continue
        if parts[3] not in _OUTCOMES:
            continue
        peer = None if parts[2] == "-" else parts[2]
        finding = ProbeFinding.model_validate(
            {
                "probe": parts[1],
                "peer": peer,
                "outcome": parts[3],
                "detail": parts[4] if len(parts) > 4 else "",
            }
        )
        printed.setdefault((finding.probe, peer), finding)
    out: list[ProbeFinding] = []
    for probe, peer in expected:
        found = printed.get((probe, peer))
        if found is None:
            status = "timed out" if result.timed_out else f"exited {result.exit_code}"
            said = (result.stderr.strip() or result.stdout.strip())[-300:]
            found = ProbeFinding(
                probe=probe,
                peer=peer,
                outcome="unverified",
                detail=f"the check printed no result; it {status}" + (f": {said}" if said else ""),
            )
        out.append(found)
    return tuple(out)


async def _clean(
    planted: Mapping[str, _Planted], executor: SandboxExecutor, writer: RunWriter
) -> dict[str, str]:
    """Returns what the clean step left behind, by sandbox."""
    commands = {
        sandbox: Exec(
            argv=("sh", "-c", _CLEAN, "sh", p.marker, p.pid, *p.dirs), timeout_s=TIMEOUT_S["clean"]
        )
        for sandbox, p in planted.items()
    }
    results = await _run_step("clean", commands, executor)
    await _commit("clean", commands, results, writer)
    problems: dict[str, str] = {}
    for sandbox, result in results.items():
        alive = [line for line in result.stdout.splitlines() if line.startswith("alive ")]
        if alive:
            problems[sandbox] = (
                f"probe processes{alive[0].removeprefix('alive')} survived cleanup and would be "
                "counted among the agents' processes"
            )
        elif result.timed_out or result.exit_code != 0:
            outcome = "timed out" if result.timed_out else f"exited {result.exit_code}"
            problems[sandbox] = f"the clean step {outcome}: {result.stderr.strip()[-300:]}"
    return problems
