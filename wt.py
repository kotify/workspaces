#!/usr/bin/env python3
"""Create, run, and remove git worktrees that each have their own ports and stack.

Each worktree lives in `<repo>-wt/<name>` next to the repo's main checkout. A repo describes what
its worktrees need in an optional `.wt.toml`: a port block written to the worktree's `.envrc`, an
install command, and a process-compose stack. Without the file, wt manages plain worktrees. wt
records every worktree of every repo in one state file, so port blocks never collide and
parallel runs are safe. Commands act on the repo of the current directory; `-C DIR` selects
another.

Typical use:
  wt new feat/some-branch --json     create worktree feat-some-branch
  wt up feat-some-branch             start the stack's default processes, wait until they run
  wt exec feat-some-branch -- make test
  wt rm feat-some-branch             close its Rex session, stop the stack, remove the worktree

Don't create these worktrees with `git worktree add`, and don't edit their port variable. Each
running stack costs memory: start only the processes a task needs, and `wt down` idle worktrees.
Everything except `wt rex` works in any terminal. Commands that describe worktrees or servers
print JSON with `--json`. Run `wt config` for the `.wt.toml` reference and `wt COMMAND --help`
for details.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Generator
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
VERSION = (HERE / "VERSION").read_text().strip()
CONFIG_FILE = ".wt.toml"
# Bump only when older wt versions can't read the new state; unknown fields are dropped.
STATE_VERSION = 1
# Where agents look for user skills: Claude Code, then Codex and other agents.
AGENT_SKILL_DIRS = ("~/.claude/skills", "~/.agents/skills")
STUB_MARKER = "wt-stub"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# sockaddr_un.sun_path is 104 bytes on macOS (108 on Linux), including the trailing NUL.
MAX_SOCKET_PATH = 103
FAILED_STATES = ("Error", "Skipped", "Terminating")

CONFIG_HELP = f"""\
A repo describes its worktrees in {CONFIG_FILE} at its root. Every key is optional;
these are the defaults:

  [git]
  remote = "origin"     # fetched before `wt new`
  base = ""             # start point of new branches; "" = the remote's default branch
  fetch = true          # false: use local refs only

  [install]
  command = ""          # run in the new worktree's environment; "" = no install step

  [ports]               # absent: no port block and no .envrc rewrite
  var = "PROJECT_BASE_PORT"
  block = 100           # wt reserves and probes base .. base+block-1

  [stack]               # absent: no stack; `wt up` and `wt rex` have nothing to start
  start = "app.start -D"                       # wt appends the process names
  processes = []                               # started by every `wt up`
  socket = ".direnv/.pc/process-compose.sock"  # where the process-compose server listens

wt reads [git] from the main checkout and the rest from the worktree's own copy, falling back
to the main checkout's. With [ports], the main checkout's `.envrc` must exist and be ignored by
git: wt copies it into the worktree with `export VAR=<base>` and checks the effective value with
`direnv exec`. Without [ports], an ignored `.envrc` is copied as is. With [stack], `start` must
start process-compose detached with its API on `socket`, and the repo's ports must stay inside
the block. wt reads the main checkout's own port from a literal `export VAR=<number>` line,
to keep worktrees off it.

Port blocks come from the range in $WT_PORT_RANGE (default 8300-9999), shared by all repos,
aligned to the block size rounded up to 10. Size the block to the repo's highest port
offset plus one: smaller blocks leave room for more worktrees.
State lives in $XDG_STATE_HOME/wt (default ~/.local/state/wt)."""


class WtError(Exception):
    pass


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run(
    args: list[str], *, cwd: Path | None = None, check: bool = True, quiet: bool = False
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE if quiet else sys.stderr,
            stderr=subprocess.PIPE if quiet else None,
            check=False,
        )
    except FileNotFoundError:
        raise WtError(f"`{args[0]}` isn't installed or isn't on PATH") from None
    if check and result.returncode != 0:
        detail = f": {result.stderr.strip()}" if quiet and result.stderr else ""
        raise WtError(
            f"`{shlex.join(args)}` failed with exit code {result.returncode}{detail}"
        )
    return result


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def path_bytes(path: Path) -> int:
    return len(os.fsencode(path))


# Config.


@dataclass(frozen=True)
class GitConfig:
    remote: str = "origin"
    base: str = ""
    fetch: bool = True


@dataclass(frozen=True)
class PortsConfig:
    var: str = "PROJECT_BASE_PORT"
    block: int = 100


@dataclass(frozen=True)
class StackConfig:
    start: str = "app.start -D"
    processes: tuple[str, ...] = ()
    socket: str = ".direnv/.pc/process-compose.sock"


@dataclass(frozen=True)
class Config:
    git: GitConfig = field(default_factory=GitConfig)
    install: str = ""
    ports: PortsConfig | None = None
    stack: StackConfig | None = None


def _section(
    raw: dict[str, Any], name: str, keys: dict[str, type]
) -> dict[str, Any] | None:
    section = raw.get(name)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise WtError(f"{CONFIG_FILE}: [{name}] must be a table")
    values: dict[str, Any] = section  # pyright: ignore[reportUnknownVariableType]
    for key, value in values.items():
        expected = keys.get(key)
        if expected is None:
            raise WtError(f"{CONFIG_FILE}: unknown key {name}.{key}")
        if not isinstance(value, expected) or (
            expected is int and isinstance(value, bool)
        ):
            raise WtError(f"{CONFIG_FILE}: {name}.{key} must be {expected.__name__}")
    return values


def parse_config(text: str) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise WtError(f"{CONFIG_FILE}: {error}") from None
    unknown = set(raw) - {"git", "install", "ports", "stack"}
    if unknown:
        raise WtError(f"{CONFIG_FILE}: unknown section {min(unknown)}")
    git = _section(raw, "git", {"remote": str, "base": str, "fetch": bool}) or {}
    install = _section(raw, "install", {"command": str}) or {}
    ports = _section(raw, "ports", {"var": str, "block": int})
    stack = _section(raw, "stack", {"start": str, "processes": list, "socket": str})
    if ports is not None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ports.get("var", "X")):
            raise WtError(f"{CONFIG_FILE}: ports.var must be a variable name")
        if not 1 <= ports.get("block", 100) <= 1000:
            raise WtError(f"{CONFIG_FILE}: ports.block must be between 1 and 1000")
    stack_config = None
    if stack is not None:
        processes: list[Any] = stack.get("processes", [])
        if not all(isinstance(p, str) for p in processes):
            raise WtError(f"{CONFIG_FILE}: stack.processes must be a list of names")
        defaults = StackConfig()
        stack_config = StackConfig(
            start=stack.get("start", defaults.start),
            processes=tuple(processes),
            socket=stack.get("socket", defaults.socket),
        )
    return Config(
        git=GitConfig(**git),
        install=install.get("command", ""),
        ports=None if ports is None else PortsConfig(**ports),
        stack=stack_config,
    )


_ignored_configs: set[Path] = set()


def load_config(*checkouts: Path) -> Config:
    """The config of the first checkout that has a valid one, or the defaults.

    A broken config in a checkout other than the last falls back with a warning, so one
    branch's mistake, or a key from a newer wt, doesn't stop wt from managing it.
    """
    for i, checkout in enumerate(checkouts):
        try:
            text = (checkout / CONFIG_FILE).read_text()
        except FileNotFoundError:
            continue
        try:
            return parse_config(text)
        except WtError as error:
            if i == len(checkouts) - 1:
                raise
            if checkout not in _ignored_configs:
                _ignored_configs.add(checkout)
                log(f"wt: ignoring {checkout / CONFIG_FILE}: {error}")
    return Config()


# State: every worktree wt manages, across repos, with its port block and Rex session.


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "wt"


@dataclass
class Record:
    repo: str  # the main checkout's path
    name: str
    path: str
    branch: str
    status: str  # creating or ready
    created_at: str
    base_port: int | None = None
    port_block: int = 0
    start_commit: str | None = None
    created_branch: bool = False
    rex_session: str | None = None
    pid: int | None = None  # the `wt new` creating it

    @property
    def repo_name(self) -> str:
        return Path(self.repo).name

    @property
    def label(self) -> str:
        return f"{self.repo_name}:{self.name}"

    def config(self) -> Config:
        return load_config(Path(self.path), Path(self.repo))


@dataclass
class Repo:
    base_port: int | None = None  # the main checkout's own
    port_block: int = 0


@dataclass
class State:
    worktrees: dict[str, Record]  # by path
    repos: dict[str, Repo]  # by main checkout path
    # The file as read, so fields from another wt version survive a write.
    raw: dict[str, Any] = field(default_factory=dict[str, Any])

    def find(self, repo: Path, name: str) -> Record:
        for record in self.worktrees.values():
            if record.repo == str(repo) and record.name == name:
                return record
        raise WtError(f"no worktree {name!r} in {repo}; see `wt ls`")


@contextlib.contextmanager
def locked_state() -> Generator[State]:
    """The state, saved on exit. Holds an exclusive lock, so keep the block short."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "state.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state()
        yield state
        path = directory / "state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    **state.raw,
                    "version": STATE_VERSION,
                    "worktrees": merged(state.raw, "worktrees", state.worktrees),
                    "repos": merged(state.raw, "repos", state.repos),
                },
                indent=2,
            )
        )
        tmp.replace(path)


def read_state() -> State:
    try:
        raw: dict[str, Any] = json.loads((state_dir() / "state.json").read_text())
    except FileNotFoundError:
        raw = {}
    if raw.get("version", 1) > STATE_VERSION:
        raise WtError(
            f"{state_dir() / 'state.json'} was written by a newer wt; upgrade wt"
        )
    worktrees: dict[str, dict[str, Any]] = raw.get("worktrees", {})
    repos: dict[str, dict[str, Any]] = raw.get("repos", {})
    return State(
        worktrees={path: Record(**known(v, Record)) for path, v in worktrees.items()},
        repos={path: Repo(**known(v, Repo)) for path, v in repos.items()},
        raw=raw,
    )


def known(values: dict[str, Any], cls: type[Record | Repo]) -> dict[str, Any]:
    """values without the fields a newer or older wt wrote."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in values.items() if k in names}


def merged(
    raw: dict[str, Any], section: str, items: dict[str, Record] | dict[str, Repo]
) -> dict[str, dict[str, Any]]:
    """items as JSON, over the fields another wt version wrote for them."""
    old: dict[str, dict[str, Any]] = raw.get(section, {})
    return {key: {**old.get(key, {}), **asdict(item)} for key, item in items.items()}


@contextlib.contextmanager
def repo_lock(repo: Path) -> Generator[None]:
    """Serializes wt's git writes in one repo: concurrent fetches and worktree adds fail
    on git's own lock files instead of waiting. Don't nest it."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(str(repo).encode()).hexdigest()[:12]  # noqa: S324
    with (directory / f"repo-{digest}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


# Repos and ports.


def main_checkout(directory: Path) -> Path:
    common = run(
        [
            "git", "-C", str(directory), "rev-parse",
            "--path-format=absolute", "--git-common-dir",
        ],
        quiet=True,
    )  # fmt: skip
    git_dir = Path(common.stdout.strip())
    if git_dir.name != ".git":
        raise WtError(
            f"{git_dir} isn't a checkout's .git directory; wt supports repos whose main "
            "checkout holds .git, not bare repos, submodules, or separate git dirs"
        )
    return git_dir.parent


def worktree_root(repo: Path) -> Path:
    return repo.parent / f"{repo.name}-wt"


def port_line_re(var: str) -> re.Pattern[str]:
    return re.compile(
        rf"""^[ \t]*(?:export[ \t]+)?{re.escape(var)}=["']?(\d*)["']?[ \t]*(?:\#.*)?$""",
        re.MULTILINE,
    )


def parse_port(envrc: str, var: str) -> int | None:
    ports = [m[1] for m in port_line_re(var).finditer(envrc)]
    return int(ports[-1]) if ports and ports[-1] else None


def read_port(checkout: Path, var: str) -> int | None:
    try:
        return parse_port((checkout / ".envrc").read_text(), var)
    except OSError:
        return None


def set_port(envrc: str, var: str, base_port: int) -> str:
    """The .envrc with exactly one assignment of var, before everything else."""
    rest = port_line_re(var).sub("", envrc)
    return f"export {var}={base_port}\n{rest}"


def port_range() -> range:
    spec = os.environ.get("WT_PORT_RANGE", "8300-9999")
    match = re.fullmatch(r"(\d+)-(\d+)", spec)
    if not match or not 1024 <= int(match[1]) < int(match[2]) <= 65535:
        raise WtError(f"WT_PORT_RANGE must look like 8300-9999, not {spec!r}")
    return range(int(match[1]), int(match[2]) + 1)


def listening(port: int) -> bool:
    # Both loopbacks: on macOS, Node servers on `localhost` listen on ::1 only.
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family) as probe:
                probe.settimeout(0.2)
                if probe.connect_ex((host, port)) == 0:
                    return True
        except OSError:  # no IPv6
            continue
    return False


def block_free(block: range) -> bool:
    return not any(listening(port) for port in block)


def taken_blocks(state: State) -> list[range]:
    """Port ranges in use by wt worktrees and the main checkouts of every known repo."""
    owners: list[Record | Repo] = [*state.worktrees.values(), *state.repos.values()]
    return [
        range(owner.base_port, owner.base_port + owner.port_block)
        for owner in owners
        if owner.base_port is not None
    ]


def allocate_port_block(
    taken: list[range],
    size: int,
    *,
    pool: range,
    is_free: Callable[[range], bool] = block_free,
) -> int:
    # Blocks aligned to their size, rounded up to 10, first, so ports are easy to read;
    # any free block when none fits.
    step = -(-size // 10) * 10
    first = -(-pool.start // 10) * 10
    aligned = range(first, pool.stop - size + 1, step)
    unaligned = range(pool.start, pool.stop - size + 1)
    for base in [*aligned, *(b for b in unaligned if b not in aligned)]:
        block = range(base, base + size)
        overlaps = any(
            block.start < other.stop and other.start < block.stop for other in taken
        )
        if not overlaps and is_free(block):
            return base
    raise WtError("no free port block; remove a worktree with `wt rm`")


def worktree_name(branch: str, root: Path, socket_suffix: str | None) -> str:
    """The branch as a name, shortened with a hash when a socket path would be too long."""
    name = re.sub(r"[^a-z0-9-]+", "-", branch.lower()).strip("-") or "worktree"
    if socket_suffix is None:
        return name
    room = MAX_SOCKET_PATH - path_bytes(root / socket_suffix) - 1
    if room < 12:
        raise WtError(
            f"{root} is too deep for the stack's socket {socket_suffix}: socket paths "
            f"must stay under {MAX_SOCKET_PATH} bytes"
        )
    if len(name) > room:
        digest = hashlib.sha1(branch.encode()).hexdigest()[:6]  # noqa: S324
        name = f"{name[: room - 7].rstrip('-')}-{digest}"
    return name


def check_name(name: str, root: Path, socket_suffix: str | None) -> None:
    if not NAME_RE.match(name):
        raise WtError(f"invalid name {name!r}; use lowercase letters, digits, and -")
    if socket_suffix is None:
        return
    sock = root / name / socket_suffix
    if path_bytes(sock) > MAX_SOCKET_PATH:
        raise WtError(
            f"name {name!r} is too long: the socket path {sock} would exceed "
            f"{MAX_SOCKET_PATH} bytes; pass a shorter --name"
        )


def envrc_ignored(repo: Path) -> bool:
    is_tracked = run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", ".envrc"],
        check=False,
        quiet=True,
    )
    is_ignored = run(
        ["git", "-C", str(repo), "check-ignore", "-q", ".envrc"],
        check=False,
        quiet=True,
    )
    return is_tracked.returncode != 0 and is_ignored.returncode == 0


# Running commands in a worktree.


def exec_argv(path: str, command: list[str]) -> list[str]:
    """Run command in the worktree's direnv environment when it has an .envrc."""
    # Through sh: scripts without a shebang fail with "exec format error" under a direct exec.
    shell = ["sh", "-c", '"$@"', "sh", *command]
    if not (Path(path) / ".envrc").exists():
        return shell
    # DEVSHELL_NO_MOTD: numtide devshells print their command menu on every entry.
    return ["env", "DEVSHELL_NO_MOTD=1", "direnv", "exec", path, *shell]


# Stacks.


def socket_path(record: Record) -> Path | None:
    stack = record.config().stack
    return None if stack is None else Path(record.path) / stack.socket


def process_compose(record: Record, *args: str) -> subprocess.CompletedProcess[str]:
    sock = socket_path(record)
    if sock is None:
        raise WtError(f"{record.label} has no [stack] in {CONFIG_FILE}")
    return run(
        ["process-compose", "--unix-socket", str(sock), "--use-uds", *args],
        check=False,
        quiet=True,
    )


class StackUnreachable(WtError):
    pass


def processes(record: Record, *, strict: bool = False) -> list[dict[str, Any]] | None:
    """The stack's processes, or None without a stack or when no server listens.

    With strict, a server that listens but doesn't answer raises StackUnreachable instead.
    """
    sock = socket_path(record)
    if sock is None or not sock.exists():
        return None
    result = process_compose(record, "process", "list", "-o", "json")
    if result.returncode == 0:
        return json.loads(result.stdout or "[]")
    gone = re.search(r"connection refused|no such file", result.stderr, re.IGNORECASE)
    if strict and not gone:
        raise StackUnreachable(
            f"the stack at {sock} doesn't answer: {result.stderr.strip()}"
        )
    return None


def process_state(process: dict[str, Any]) -> str:
    status = process.get("status", "?")
    ready = process.get("is_ready")
    # is_ready is "-" both without a probe and before a probe's first check; older
    # process-compose versions don't report has_ready_probe.
    probed = process.get("has_ready_probe", ready not in ("-", "", None))
    if status == "Running" and probed and ready != "Ready":
        return "Starting"
    return status


def process_outcome(process: dict[str, Any] | None) -> str:
    """done, failed, or pending, for a process that `wt up` waits on."""
    if process is None:
        return "pending"
    state = process_state(process)
    # A running process keeps the exit code of its last run until it exits again.
    stopped = state not in ("Running", "Starting", "Launching", "Pending")
    if state in FAILED_STATES or (stopped and process.get("exit_code")):
        return "failed"
    # A one-shot process, such as a migration, is done once it exits cleanly.
    return "done" if state in ("Running", "Completed") else "pending"


def stop_stack(record: Record) -> None:
    if processes(record, strict=True) is None:
        return
    log(f"Stopping the stack in {record.path}")
    process_compose(record, "down")
    # Only a gone server counts as stopped; one that doesn't answer may still run.
    for _ in range(60):
        with contextlib.suppress(StackUnreachable):
            if processes(record, strict=True) is None:
                return
        time.sleep(1)
    raise WtError(f"the stack in {record.path} is still running after 60s")


def stack_config(record: Record) -> StackConfig:
    stack = record.config().stack
    if stack is None:
        raise WtError(
            f"{record.label} has no [stack] in {CONFIG_FILE}; see `wt config`"
        )
    return stack


# Rex.


def rex_session_ids() -> set[str] | None:
    """IDs of live Rex sessions, or None outside Rex."""
    if not shutil.which("rex"):
        return None
    result = run(
        ["rex", "--autostart=false", "--timeout", "3s", "ls", "--json"],
        check=False,
        quiet=True,
    )
    if result.returncode != 0:
        return None
    return {s["session_id"] for s in json.loads(result.stdout)["sessions"]}


def rex(*args: str, command: tuple[str, ...] = ()) -> dict[str, Any]:
    tail = ["--", *command] if command else []
    result = run(["rex", "--autostart=false", *args, "--json", *tail], quiet=True)
    return json.loads(result.stdout)


def close_rex_session(record: Record) -> None:
    if record.rex_session is None:
        return
    sessions = rex_session_ids()
    if sessions is None:
        log(
            f"wt: can't reach Rex to close {record.label}'s session; if it still runs, "
            f"close it with `rex kill {record.rex_session}`"
        )
    elif record.rex_session in sessions:
        log(f"Closing Rex session {record.label}")
        run(["rex", "--autostart=false", "kill", record.rex_session], quiet=True)


# Worktree commands.


def describe(record: Record, sessions: set[str] | None) -> dict[str, Any]:
    procs = processes(record)
    return {
        "name": record.name,
        "repo": record.repo,
        "path": record.path,
        "branch": record.branch,
        "status": record.status,
        "base_port": record.base_port,
        "start_commit": record.start_commit,
        "stack": (
            None
            if procs is None
            else {
                p["name"]: state
                for p in procs
                if (state := process_state(p)) != "Disabled"
            }
        ),
        "rex_session": (
            record.rex_session
            if sessions is not None and record.rex_session in sessions
            else None
        ),
    }


def print_worktrees(records: list[Record], *, as_json: bool, all_repos: bool) -> None:
    sessions = rex_session_ids()
    described = [describe(r, sessions) for r in records]
    if as_json:
        print(json.dumps(described, indent=2))
        return
    print(f"{'NAME':<32}  {'PORT':<5}  {'BRANCH':<36}  STACK")
    for wt in described:
        name = f"{Path(wt['repo']).name}:{wt['name']}" if all_repos else wt["name"]
        stack = wt["stack"]
        state = (
            wt["status"]
            if wt["status"] != "ready"
            else "-"
            if stack is None
            else ",".join(f"{n}:{s}" for n, s in sorted(stack.items()))
        )
        port = wt["base_port"] or "-"
        print(f"{name:<32}  {port:<5}  {wt['branch']:<36}  {state}")


def default_base(repo: Path, git: GitConfig) -> str:
    if git.base:
        return git.base
    head = run(
        [
            "git", "-C", str(repo), "symbolic-ref", "--short",
            f"refs/remotes/{git.remote}/HEAD",
        ],
        check=False,
        quiet=True,
    ).stdout.strip()  # fmt: skip
    if head:
        return head
    current = run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], quiet=True
    ).stdout.strip()
    log(f"{git.remote}/HEAD isn't set; starting new branches from {current}")
    return current


def cmd_new(args: argparse.Namespace) -> None:
    repo: Path = args.repo
    config = load_config(repo)
    root = worktree_root(repo)
    socket_suffix = config.stack.socket if config.stack else None
    name = args.name or worktree_name(args.branch, root, socket_suffix)
    check_name(name, root, socket_suffix)
    path = root / name
    if config.ports is not None:
        if not (repo / ".envrc").is_file():
            raise WtError(
                f"{CONFIG_FILE} has [ports], but {repo} has no .envrc to copy"
            )
        if not envrc_ignored(repo):
            raise WtError(
                f"{CONFIG_FILE} has [ports], so .envrc must be ignored by git: wt "
                "writes a different port into each worktree's copy"
            )

    if config.git.fetch:
        with repo_lock(repo):
            run(["git", "-C", str(repo), "fetch", "--quiet", config.git.remote])
    base = args.base or default_base(repo, config.git)
    main_port = read_port(repo, config.ports.var) if config.ports else None
    if config.ports and main_port is None:
        log(
            f"wt can't read a literal `export {config.ports.var}=<number>` in "
            f"{repo}/.envrc, so it can't keep worktrees off the main checkout's ports"
        )
    with locked_state() as state:
        state.repos[str(repo)] = Repo(
            base_port=main_port,
            port_block=config.ports.block if config.ports else 0,
        )
        if path.exists() or str(path) in state.worktrees:
            raise WtError(f"{path} already exists")
        record = Record(
            repo=str(repo),
            name=name,
            path=str(path),
            branch=args.branch,
            status="creating",
            created_at=now(),
            pid=os.getpid(),
        )
        if config.ports is not None:
            record.port_block = config.ports.block
            record.base_port = allocate_port_block(
                taken_blocks(state), config.ports.block, pool=port_range()
            )
        state.worktrees[record.path] = record

    try:
        create(
            record,
            base=base,
            remote=config.git.remote,
            ports=config.ports,
            install=args.install,
        )
    except BaseException:
        log(f"Rolling back {path}")
        rollback(record)
        raise
    if config.ports is None and config.stack is None and not config.install:
        log(
            f"No {CONFIG_FILE}: a plain worktree without ports or stack; see `wt config`"
        )
    print_worktrees([record], as_json=args.json, all_repos=False)


def create(
    record: Record,
    *,
    base: str,
    remote: str,
    ports: PortsConfig | None,
    install: bool,
) -> None:
    repo, path = Path(record.repo), Path(record.path)

    def has_ref(ref: str) -> bool:
        return (
            run(
                ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
                check=False,
                quiet=True,
            ).returncode
            == 0
        )

    if has_ref(f"refs/heads/{record.branch}"):
        add = [record.branch]
    elif has_ref(f"refs/remotes/{remote}/{record.branch}"):
        add = ["--track", "-b", record.branch, f"{remote}/{record.branch}"]
        record.created_branch = True
    else:
        add = ["--no-track", "-b", record.branch, base]
        record.created_branch = True
    path.parent.mkdir(parents=True, exist_ok=True)
    with repo_lock(repo):
        run(["git", "-C", str(repo), "worktree", "add", "--quiet", str(path), *add])
    record.start_commit = run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], quiet=True
    ).stdout.strip()
    # Saved now, so `wt rm` after a killed `wt new` deletes the branch it created.
    with locked_state() as state:
        state.worktrees[record.path] = record

    # [ports] comes from the main checkout, like the allocation; the rest from the branch.
    config = record.config()
    main_envrc, envrc = repo / ".envrc", path / ".envrc"
    if ports is not None and record.base_port is not None:
        if not envrc_ignored(path):
            raise WtError(
                f"{record.branch} tracks .envrc or doesn't ignore it; with [ports], wt "
                "writes a different port into each worktree's .envrc"
            )
        # A copy, not `source_env`: that would evaluate the main checkout's environment,
        # with its ports and data.
        text = set_port(main_envrc.read_text(), ports.var, record.base_port)
        envrc.write_text(text)
    elif main_envrc.is_file() and not envrc.exists():
        shutil.copyfile(main_envrc, envrc)
    if envrc.exists():
        run(["direnv", "allow", str(path)])
    if ports is not None:
        effective = run(
            exec_argv(str(path), ["printenv", ports.var]),
            cwd=path,
            check=False,
            quiet=True,
        ).stdout.strip()
        if effective != str(record.base_port):
            raise WtError(
                f"{envrc} sets {ports.var}={effective or '(unset)'}, "
                f"not {record.base_port}; check the main checkout's .envrc"
            )
    if install and config.install:
        log(f"Installing in {path}: {config.install}")
        run(exec_argv(str(path), shlex.split(config.install)), cwd=path)

    record.status = "ready"
    record.pid = None
    with locked_state() as state:
        state.worktrees[record.path] = record


def rollback(record: Record) -> None:
    repo, path = Path(record.repo), Path(record.path)
    with repo_lock(repo):
        if path.exists():
            run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(path)],
                check=False,
                quiet=True,
            )
            shutil.rmtree(path, ignore_errors=True)
        if record.created_branch:
            run(
                ["git", "-C", str(repo), "branch", "-D", record.branch],
                check=False,
                quiet=True,
            )
    with locked_state() as state:
        state.worktrees.pop(record.path, None)


def cmd_ls(args: argparse.Namespace) -> None:
    records = sorted(
        (
            r
            for r in read_state().worktrees.values()
            if args.all or r.repo == str(args.repo)
        ),
        key=lambda r: (r.repo, r.name),
    )
    print_worktrees(records, as_json=args.json, all_repos=args.all)


def cmd_info(args: argparse.Namespace) -> None:
    record = read_state().find(args.repo, args.name)
    print_worktrees([record], as_json=args.json, all_repos=False)


def ready_record(repo: Path, name: str) -> Record:
    record = read_state().find(repo, name)
    if record.status != "ready":
        raise WtError(
            f"{record.label} is still {record.status}; if its `wt new` stopped, `wt rm` it"
        )
    return record


def cmd_up(args: argparse.Namespace) -> None:
    record = ready_record(args.repo, args.name)
    stack = stack_config(record)
    defaults = () if args.only else stack.processes
    wanted = list(dict.fromkeys([*defaults, *args.processes]))
    if not wanted:
        raise WtError("name the processes to start, or set stack.processes")
    path = Path(record.path)
    if processes(record) is None:
        log(f"Starting {', '.join(wanted)} in {path}")
        run(exec_argv(str(path), [*shlex.split(stack.start), *wanted]), cwd=path)
    else:
        for process in wanted:
            result = process_compose(record, "process", "start", process)
            if result.returncode != 0 and "already running" not in result.stderr:
                raise WtError(f"can't start {process}: {result.stderr.strip()}")

    # Waits for process-compose's own state: readiness probes where the repo defines them,
    # otherwise only that the process runs.
    deadline = time.monotonic() + args.timeout
    while True:
        procs = {p["name"]: p for p in processes(record) or []}
        outcomes = {p: process_outcome(procs.get(p)) for p in wanted}
        failed = [p for p in wanted if outcomes[p] == "failed"]
        if failed:
            raise WtError(
                f"{', '.join(failed)} stopped; see `wt logs {record.name} {failed[0]}`"
            )
        pending = [p for p in wanted if outcomes[p] == "pending"]
        if not pending:
            break
        if time.monotonic() > deadline:
            raise WtError(
                f"not ready after {args.timeout}s: "
                + ", ".join(
                    f"{p} ({process_state(procs[p]) if p in procs else 'missing'})"
                    for p in pending
                )
                + f"; see `wt logs {record.name} <process>`"
            )
        time.sleep(1)
    print_worktrees([record], as_json=args.json, all_repos=False)


def cmd_down(args: argparse.Namespace) -> None:
    record = read_state().find(args.repo, args.name)
    stack_config(record)
    stop_stack(record)


def cmd_logs(args: argparse.Namespace) -> None:
    record = read_state().find(args.repo, args.name)
    stack_config(record)
    if processes(record) is None:
        raise WtError(f"the stack in {record.path} isn't running")
    result = process_compose(
        record, "process", "logs", args.process, "--tail", str(args.tail)
    )
    sys.stdout.write(result.stdout)
    if result.returncode != 0:
        raise WtError(result.stderr.strip())


def strip_dashes(command: list[str]) -> list[str]:
    return command[1:] if command[:1] == ["--"] else command


def cmd_exec(args: argparse.Namespace) -> None:
    record = ready_record(args.repo, args.name)
    command = strip_dashes(args.command)
    if not command:
        raise WtError("pass a command: wt exec NAME -- COMMAND")
    argv = exec_argv(record.path, command)
    os.chdir(record.path)
    try:
        os.execvp(argv[0], argv)  # noqa: S606
    except FileNotFoundError:
        raise WtError(f"`{argv[0]}` isn't installed or isn't on PATH") from None


def cmd_rex(args: argparse.Namespace) -> None:
    record = ready_record(args.repo, args.name)
    sessions = rex_session_ids()
    if sessions is None:
        raise WtError("`wt rex` needs a running Rex server and the rex CLI")
    agent = strip_dashes(args.agent)
    path = record.path
    # Panes run through exec_argv because Rex starts them in the login shell's environment,
    # without the worktree's.
    agent_command = tuple(exec_argv(path, agent))

    def split(
        session: str, anchor: str, side: str, ratio: int, label: str, *command: str
    ) -> str:
        created = rex(
            "split", "-s", session, "-b", anchor, f"--split={side}",
            "--ratio", str(ratio), "--label", label, "-c", path, "--focus=false",
            *(["--keep-open"] if label == "agent" else []),
            command=command,
        )  # fmt: skip
        return created["block_ids"][0]

    blocks: dict[str, str] = {}
    if record.rex_session is not None and record.rex_session in sessions:
        session_id = record.rex_session
        # Another agent goes below the last agent pane, or left of everything.
        listed = rex("block", "ls", "-s", session_id)["blocks"]
        agents = [b["block_id"] for b in listed if b.get("label") == "agent"]
        if agent:
            anchor = agents[-1] if agents else listed[0]["block_id"]
            side, ratio = ("below", 50) if agents else ("left", 65)
            blocks["agent"] = split(
                session_id, anchor, side, ratio, "agent", *agent_command
            )
    else:
        # One tab: the agent (or the shell) on the left, the stack's TUI and the shell
        # stacked on the right.
        created = rex("new", record.label, "-c", path, "--window", record.name)
        session_id: str = created["session_id"]
        with locked_state() as state:
            state.worktrees[record.path].rex_session = session_id
        shell = blocks["shell"] = created["initial_windows"][0]["block_ids"][0]
        if agent:
            blocks["agent"] = split(
                session_id, shell, "left", 65, "agent", *agent_command
            )
        sock = socket_path(record)
        if sock is not None and processes(record) is not None:
            # The pane starts in the login shell's environment, which may lack the
            # process-compose that wt itself uses; the socket makes the worktree's own
            # environment unnecessary.
            binary = shutil.which("process-compose") or "process-compose"
            attach = (binary, "attach", "--unix-socket", str(sock), "--use-uds")
            side, ratio = ("above", 60) if agent else ("right", 40)
            blocks["stack"] = split(session_id, shell, side, ratio, "stack", *attach)
    print(json.dumps({"session_id": session_id, "blocks": blocks}, indent=2))


def refuse_dirty(path: Path, *, force: bool) -> None:
    # --untracked-files=all: status.showUntrackedFiles=no would hide them, and git's own
    # check before `worktree remove` respects it too.
    dirty = run(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
        quiet=True,
    )
    if dirty.stdout.strip() and not force:
        raise WtError(
            f"{path} has uncommitted changes; commit them or pass --force:\n"
            f"{dirty.stdout.rstrip()}"
        )


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def cmd_rm(args: argparse.Namespace) -> None:
    record = read_state().find(args.repo, args.name)
    path = Path(record.path)
    creating = record.status == "creating" and record.pid is not None
    if creating and not args.force and alive(record.pid or 0):
        raise WtError(
            f"`wt new` (pid {record.pid}) is still creating {record.label}; wait for it. "
            "If no `wt new` runs anymore, its pid was reused: pass --force"
        )
    deleted_by_hand = not path.exists()
    if record.status == "ready" and path.exists():
        # Checked before and after stopping: a dirty worktree keeps its agents and stack,
        # and nothing can write after the second check.
        refuse_dirty(path, force=args.force)
        close_rex_session(record)
        stop_stack(record)
        refuse_dirty(path, force=args.force)
        remove = ["git", "-C", record.repo, "worktree", "remove", str(path)]
        with repo_lock(Path(record.repo)):
            run(remove + (["--force"] if args.force else []))
    elif path.exists():
        # A `wt new` that died: nothing in it is the user's yet.
        rollback(record)
        log(f"Removed the unfinished worktree {path}")
        return
    if deleted_by_hand:
        close_rex_session(record)
        # Git still counts the branch as checked out there until it prunes.
        with repo_lock(Path(record.repo)):
            run(
                ["git", "-C", record.repo, "worktree", "prune"], check=False, quiet=True
            )
    with locked_state() as state:
        state.worktrees.pop(record.path, None)
    log(f"Removed {path}; the branch {record.branch} stays")


def cmd_config(args: argparse.Namespace) -> None:
    if args.repo is None:
        print(CONFIG_HELP)
        return
    print(json.dumps(asdict(load_config(args.repo)), indent=2))


def shipped_skill(name: str) -> tuple[str, str]:
    """The frontmatter and guide of a skill wt ships."""
    try:
        text = (HERE / "skills" / name / "SKILL.md").read_text()
    except FileNotFoundError:
        raise WtError(
            f"no skill {name!r}; wt ships {', '.join(skill_names())}"
        ) from None
    match = re.match(r"---\n(.*?)\n---\n+", text, re.DOTALL)
    if match is None:
        raise WtError(f"skill {name!r} has no frontmatter")
    return match[1], text[match.end() :]


def skill_names() -> list[str]:
    return sorted(p.parent.name for p in (HERE / "skills").glob("*/SKILL.md"))


def skill_stub(name: str) -> str:
    """A SKILL.md that loads the guide from the wt on PATH, so it never goes stale."""
    frontmatter, _ = shipped_skill(name)
    return f"""\
---
{frontmatter}
metadata:
  {STUB_MARKER}: "1"
---
<!-- Installed by `wt skill install`. Remove it with `wt skill remove`. -->

Run `wt skill show {name}` and follow the guide it prints. The guide comes from the `wt`
on PATH, so it matches that version. If `wt` isn't on PATH, the current repo doesn't use
wt, or its dev shell isn't loaded; skip this skill.
"""


def is_stub(path: Path) -> bool:
    return path.is_file() and f"\n  {STUB_MARKER}:" in path.read_text()


def cmd_skill(args: argparse.Namespace) -> None:
    if args.action == "show":
        if args.name is None:
            raise WtError(f"name a skill: {', '.join(skill_names())}")
        sys.stdout.write(shipped_skill(args.name)[1])
        return
    names = [args.name] if args.name else skill_names()
    stubs = {name: skill_stub(name) for name in names}
    dirs = [Path(d).expanduser() for d in args.dir or AGENT_SKILL_DIRS]
    targets = [(d / name, name) for d in dirs for name in names]
    if args.action == "remove":
        for target, _ in targets:
            if is_stub(target / "SKILL.md"):
                (target / "SKILL.md").unlink()
                with contextlib.suppress(OSError):
                    target.rmdir()
                log(f"Removed {target}")
        return
    # Checked before writing anything, so a refusal leaves every agent as it was.
    if not args.force:
        for target, _ in targets:
            if (target.exists() or target.is_symlink()) and not is_stub(
                target / "SKILL.md"
            ):
                raise WtError(
                    f"{target} isn't a skill wt installed; pass --force to replace it"
                )
    for target, name in targets:
        if target.is_symlink() or target.is_file():
            target.unlink()
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(stubs[name])
        log(f"Installed {target}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wt",
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join((__doc__ or "").splitlines()[2:]),
    )
    parser.add_argument("--version", action="version", version=f"wt {VERSION}")
    parser.add_argument("-C", dest="directory", help="act on the repo of DIRECTORY")
    commands = parser.add_subparsers(dest="command", required=True)

    def add(
        name: str,
        handler: Callable[[argparse.Namespace], None],
        help: str,
        details: str = "",
        *,
        with_json: bool = False,
    ) -> argparse.ArgumentParser:
        command = commands.add_parser(
            name,
            help=help,
            description=f"{help[0].upper()}{help[1:]}.\n\n{details}".strip(),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        command.set_defaults(handler=handler)
        if with_json:
            command.add_argument("--json", action="store_true", help="print JSON")
        return command

    new = add(
        "new",
        cmd_new,
        "create a worktree for a branch",
        f"""\
Fetches the remote, then checks out the local branch, tracks REMOTE/BRANCH, or
creates BRANCH from --base (default: git.base, else the remote's default branch).
With [ports], copies the main checkout's .envrc with the next free port block and
checks the effective value. Then runs `direnv allow` and install.command.
Parallel runs are safe. On failure it removes what it created.
The name is --name, or the branch with `/` replaced by `-`, shortened with a hash
when the stack's socket path would exceed the macOS limit. See `wt config` for
{CONFIG_FILE}.

The worktree runs the branch's code: its .envrc setup, {CONFIG_FILE}, and install
scripts run now, and `wt up` and `wt exec` run the rest. Use wt only on branches
whose authors you'd let run code on this machine.""",
        with_json=True,
    )
    new.add_argument(
        "branch", help="existing branch, or a new one to create from --base"
    )
    new.add_argument("--name", help="worktree name (default: from the branch)")
    new.add_argument("--base", help="start point of a new branch")
    new.add_argument(
        "--no-install",
        dest="install",
        action="store_false",
        help="skip install.command",
    )

    describe_help = """\
JSON fields: name, repo, path, branch, status (creating or ready), base_port
(null without [ports]; the repo's ports are base_port + offsets inside the block),
start_commit (HEAD when the worktree was created; review work with
`git diff START`), stack (process name to state; null when stopped or without a
stack; disabled processes omitted), and rex_session (the live Rex session's ID,
or null)."""
    ls = add("ls", cmd_ls, "list worktrees", describe_help, with_json=True)
    ls.add_argument("--all", action="store_true", help="include every repo")
    info = add("info", cmd_info, "describe a worktree", describe_help, with_json=True)
    info.add_argument("name")

    up = add(
        "up",
        cmd_up,
        "start the stack and wait until it runs",
        """\
Runs stack.start with stack.processes and the named processes, or starts the
named processes in a running stack, then waits for process-compose's state and
fails as soon as one of them stops. Processes with a readiness probe are Running
once they answer; for the others, Running means only that they started.
Process names are those in `process-compose process list`.""",
        with_json=True,
    )
    up.add_argument("name")
    up.add_argument("processes", nargs="*", help="processes beyond stack.processes")
    up.add_argument(
        "--only", action="store_true", help="start only the named processes"
    )
    up.add_argument("--timeout", type=int, default=300, help="seconds to wait")

    add("down", cmd_down, "stop the stack").add_argument("name")

    logs = add("logs", cmd_logs, "print a process's recent logs")
    logs.add_argument("name")
    logs.add_argument("process")
    logs.add_argument("--tail", type=int, default=200)

    exec_ = add(
        "exec",
        cmd_exec,
        "run a command in a worktree",
        """\
Runs COMMAND in the worktree's directory and, when it has an .envrc, its direnv
environment, and exits with its status. Example: wt exec feat-x -- make test""",
    )
    exec_.add_argument("name")
    exec_.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND")

    rex_ = add(
        "rex",
        cmd_rex,
        "open the worktree in a Rex session",
        """\
Creates the Rex session REPO:NAME with one tab: a shell and, when the stack runs,
a pane with the process-compose TUI. With `-- COMMAND`, adds an agent pane on
the left that runs COMMAND like `wt exec`; it stays open after COMMAND exits, so
`rex wait` returns COMMAND's exit status. Use the shell pane for interactive
work. If the worktree's session exists, only another agent pane is added, below
the last one. Prints
{"session_id": ..., "blocks": {"shell": ..., "stack": ..., "agent": ...}}.
Example: wt rex feat-x -- claude -p --permission-mode acceptEdits "$(cat task.md)\"""",
    )
    rex_.add_argument("name")
    rex_.add_argument(
        "agent", nargs=argparse.REMAINDER, help="-- COMMAND to run in an agent pane"
    )

    rm = add(
        "rm",
        cmd_rm,
        "close the session, stop the stack, remove the worktree; keep the branch",
        """\
Refuses when the worktree has uncommitted or untracked changes unless --force,
before it touches anything. Then closes the worktree's Rex session, stops its
stack, checks again, and removes the worktree. Without --force, git's own checks
also apply. Removes a worktree whose `wt new` stopped, and refuses while it runs.""",
    )
    rm.add_argument("name")
    rm.add_argument(
        "--force",
        action="store_true",
        help="discard uncommitted changes; remove even if `wt new` seems to run",
    )

    add(
        "config",
        cmd_config,
        f"describe {CONFIG_FILE}, or print a repo's effective config",
        "Without -C, prints the reference. With -C DIR, prints that repo's config as JSON.",
    )
    skill = add(
        "skill",
        cmd_skill,
        "install the agent skills wt ships, or print one",
        f"""\
install writes a stub SKILL.md for each skill into {" and ".join(AGENT_SKILL_DIRS)},
or into each --dir, such as a repo's .agents/skills. The stub tells the agent to
run `wt skill show NAME`, so the guide always matches the wt on PATH. install
refuses to replace a skill it didn't write unless --force; remove deletes only
the stubs it wrote. show prints a skill's guide. Without NAME, install and remove
act on every skill wt ships: {", ".join(skill_names())}.""",
    )
    skill.add_argument("action", choices=["install", "remove", "show"])
    skill.add_argument("name", nargs="?", help="a skill wt ships")
    skill.add_argument("--dir", action="append", help="a skills directory (repeatable)")
    skill.add_argument(
        "--force", action="store_true", help="replace a skill wt didn't install"
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        repo_free = args.command == "skill" or (args.command == "ls" and args.all)
        if args.command == "config":
            repo_free = args.directory is None
        directory = Path(args.directory or ".").resolve()
        args.repo = None if repo_free else main_checkout(directory)
        args.handler(args)
    except WtError as error:
        sys.exit(f"wt: {error}")


if __name__ == "__main__":
    main()
