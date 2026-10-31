from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import wt

ROOT = Path("/home/dev/src/app-wt")
WT = Path(wt.__file__)
SOCKET = ".direnv/.pc/process-compose.sock"


@pytest.mark.parametrize(
    ("envrc", "port"),
    [
        ("export PROJECT_BASE_PORT=8200\nuse flake .\n", 8200),
        ('export PROJECT_BASE_PORT="8200"\n', 8200),
        ("PROJECT_BASE_PORT='8200'  # main\n", 8200),
        ("export PROJECT_BASE_PORT=8200\nexport PROJECT_BASE_PORT=8400\n", 8400),
        ("export OTHER_BASE_PORT=8200\n", None),
        ("use flake .\n", None),
    ],
)
def test_parse_port(envrc: str, port: int | None):
    assert wt.parse_port(envrc, "PROJECT_BASE_PORT") == port


def test_set_port_replaces_every_assignment():
    envrc = 'export A=1\nexport BASE="8200"\nuse flake .\nBASE=1\n'

    updated = wt.set_port(envrc, "BASE", 8300)

    assert updated.startswith("export BASE=8300\n")
    assert updated.count("BASE=") == 1
    assert wt.parse_port(updated, "BASE") == 8300
    assert "export A=1\n" in updated
    assert "use flake .\n" in updated


def test_parse_config_defaults():
    config = wt.parse_config("")

    assert config == wt.Config()
    assert config.ports is None
    assert config.stack is None
    assert config.git.remote == "origin"


def test_parse_config_full():
    config = wt.parse_config(
        """
        [git]
        base = "origin/develop"
        fetch = false
        [install]
        command = "make install"
        [ports]
        var = "BASE_PORT"
        block = 20
        [stack]
        processes = ["db", "cache"]
        """
    )

    assert config.git == wt.GitConfig(
        remote="origin", base="origin/develop", fetch=False
    )
    assert config.install == "make install"
    assert config.ports == wt.PortsConfig(var="BASE_PORT", block=20)
    assert config.stack is not None
    assert config.stack.processes == ("db", "cache")
    assert config.stack.start == "app.start -D"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[stak]\n", "unknown section stak"),
        ("[ports]\nbase = 1\n", "unknown key ports.base"),
        ("[ports]\nblock = '100'\n", "ports.block must be int"),
        ("[ports]\nblock = true\n", "ports.block must be int"),
        ("[ports]\nblock = 0\n", "between 1 and 1000"),
        ("[ports]\nvar = 'not a var'\n", "variable name"),
        ("[stack]\nprocesses = [1]\n", "list of names"),
        ("[git\n", ".wt.toml"),
    ],
)
def test_parse_config_rejects_mistakes(text: str, error: str):
    with pytest.raises(wt.WtError, match=error):
        wt.parse_config(text)


def test_worktree_name_keeps_short_branches():
    assert wt.worktree_name("feat/Some_branch", ROOT, SOCKET) == "feat-some-branch"


def test_worktree_name_shortens_to_fit_the_socket_path():
    branch = "feat/a-branch-name-that-goes-on-and-on-well-past-the-socket-limit"

    name = wt.worktree_name(branch, ROOT, SOCKET)

    wt.check_name(name, ROOT, SOCKET)
    assert wt.path_bytes(ROOT / name / SOCKET) <= wt.MAX_SOCKET_PATH
    assert name != wt.worktree_name(branch + "-2", ROOT, SOCKET)


def test_check_name_counts_bytes():
    root = Path("/home/dev/src/" + "é" * 20 + "-wt")
    # Fits when the 20 two-byte characters count once, not when they count twice.
    name = "x" * (wt.MAX_SOCKET_PATH - len(str(root / SOCKET)) - 1)

    with pytest.raises(wt.WtError, match="too long"):
        wt.check_name(name, root, SOCKET)


def test_allocate_port_block_skips_taken_overlapping_and_busy_blocks():
    taken = [range(8200, 8300), range(8300, 8400), range(8450, 8460)]

    port = wt.allocate_port_block(
        taken, 100, pool=range(8300, 10000), is_free=lambda block: 8550 not in block
    )

    # 8300 is taken, 8400 overlaps 8450, and 8500 has a busy port.
    assert port == 8600


def test_allocate_port_block_probes_the_whole_block():
    probed: list[range] = []

    def is_free(block: range) -> bool:
        probed.append(block)
        return True

    wt.allocate_port_block([], 100, pool=range(8300, 10000), is_free=is_free)

    assert probed == [range(8300, 8400)]


def test_allocate_port_block_aligns_small_blocks_to_their_size():
    taken = [range(8300, 8330), range(8330, 8340)]

    port = wt.allocate_port_block(taken, 30, pool=range(8300, 10000), is_free=bool)

    assert port == 8360


def test_allocate_port_block_falls_back_to_unaligned_blocks():
    assert wt.allocate_port_block([], 100, pool=range(8301, 8401), is_free=bool) == 8301


def test_allocate_port_block_fails_when_full():
    with pytest.raises(wt.WtError, match="no free port block"):
        wt.allocate_port_block(
            [range(8300, 8500)], 100, pool=range(8300, 8500), is_free=bool
        )


def reserve(state_home: str, index: int) -> int:
    os.environ["XDG_STATE_HOME"] = state_home
    with wt.locked_state() as state:
        port = wt.allocate_port_block(
            wt.taken_blocks(state), 100, pool=range(8300, 10000), is_free=bool
        )
        path = f"/tmp/wt-test/{index}"
        state.worktrees[path] = wt.Record(
            repo="/tmp/repo",
            name=str(index),
            path=path,
            branch=str(index),
            status="creating",
            created_at=wt.now(),
            base_port=port,
            port_block=100,
        )
    return port


def test_concurrent_allocations_get_distinct_ports(tmp_path: Path):
    with multiprocessing.get_context("spawn").Pool(8) as pool:
        ports = pool.starmap(reserve, [(str(tmp_path), i) for i in range(16)])

    assert len(set(ports)) == 16


def test_find_is_scoped_to_the_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    for repo in ("/src/api", "/src/web"):
        with wt.locked_state() as state:
            record = wt.Record(
                repo=repo,
                name="feat-x",
                path=f"{repo}-wt/feat-x",
                branch="feat/x",
                status="ready",
                created_at=wt.now(),
            )
            state.worktrees[record.path] = record

    found = wt.read_state().find(Path("/src/web"), "feat-x")

    assert found.path == "/src/web-wt/feat-x"
    assert found.label == "web:feat-x"


def save(record: wt.Record) -> None:
    with wt.locked_state() as state:
        state.worktrees[record.path] = record


def ignore(*args: object, **kwargs: object) -> None:
    return None


def record_at(tmp_path: Path, **fields: Any) -> wt.Record:
    values: dict[str, Any] = {
        "repo": str(tmp_path / "app"),
        "name": "feat-x",
        "path": str(tmp_path / "app-wt" / "feat-x"),
        "branch": "feat/x",
        "status": "ready",
        "created_at": wt.now(),
    }
    return wt.Record(**{**values, **fields})


def rm(tmp_path: Path, *, force: bool = False) -> None:
    wt.cmd_rm(argparse.Namespace(repo=tmp_path / "app", name="feat-x", force=force))


def test_rm_refuses_untracked_files_that_git_status_hides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    record = record_at(tmp_path)
    subprocess.run(["git", "init", "--quiet", record.path], check=True)
    subprocess.run(
        ["git", "-C", record.path, "config", "status.showUntrackedFiles", "no"],
        check=True,
    )
    (Path(record.path) / "scratch").write_text("work")
    save(record)
    monkeypatch.setattr(wt, "close_rex_session", ignore)
    monkeypatch.setattr(wt, "stop_stack", ignore)

    with pytest.raises(wt.WtError, match="scratch"):
        rm(tmp_path)


def test_rm_refuses_while_wt_new_is_still_creating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    record = record_at(tmp_path, status="creating", pid=os.getpid())
    Path(record.path).mkdir(parents=True)
    save(record)

    with pytest.raises(wt.WtError, match="still creating"):
        rm(tmp_path)
    assert Path(record.path).exists()

    # The pid may belong to another process by now; --force trusts the user.
    rm(tmp_path, force=True)
    assert not Path(record.path).exists()


def test_rm_closes_the_rex_session_of_a_worktree_deleted_by_hand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    save(record_at(tmp_path, rex_session="session:1"))
    closed: list[str | None] = []

    def close(record: wt.Record) -> None:
        closed.append(record.rex_session)

    monkeypatch.setattr(wt, "close_rex_session", close)

    rm(tmp_path)

    assert closed == ["session:1"]


def test_stop_stack_fails_when_the_server_stops_answering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    answers: list[list[dict[str, Any]] | None] = [[{"name": "postgres"}]]

    def processes(record: wt.Record, *, strict: bool = False):
        if answers:
            return answers.pop()
        if strict:
            raise wt.StackUnreachable("EOF")
        return None

    monkeypatch.setattr(wt, "processes", processes)
    monkeypatch.setattr(wt, "process_compose", ignore)
    monkeypatch.setattr(wt.time, "sleep", ignore)

    with pytest.raises(wt.WtError, match="still running"):
        wt.stop_stack(record_at(tmp_path))


def test_listening_sees_servers_on_ipv6_localhost():
    with socket.socket(socket.AF_INET6) as server:
        server.bind(("::1", 0))
        server.listen()

        assert wt.listening(server.getsockname()[1])


def test_rm_refuses_a_dirty_worktree_before_stopping_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "app-wt" / "feat-x"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    (path / "scratch").write_text("work")
    save(
        wt.Record(
            repo=str(tmp_path / "app"),
            name="feat-x",
            path=str(path),
            branch="feat/x",
            status="ready",
            created_at=wt.now(),
        )
    )
    stopped: list[str] = []

    def stop(what: str) -> Callable[[wt.Record], None]:
        return lambda record: stopped.append(what)

    monkeypatch.setattr(wt, "close_rex_session", stop("rex"))
    monkeypatch.setattr(wt, "stop_stack", stop("stack"))

    with pytest.raises(wt.WtError, match="scratch"):
        wt.cmd_rm(argparse.Namespace(repo=tmp_path / "app", name="feat-x", force=False))

    assert stopped == []


def test_state_keeps_unknown_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / "state.json").write_text(
        json.dumps(
            {
                "worktrees": {
                    "/src/api-wt/x": {
                        "repo": "/src/api",
                        "name": "x",
                        "path": "/src/api-wt/x",
                        "branch": "x",
                        "status": "ready",
                        "created_at": "2026-10-31T00:00:00+00:00",
                        "retired_field": 1,
                    }
                },
                "repos": {"/src/api": {"base_port": 8200, "retired_field": 1}},
            }
        )
    )

    state = wt.read_state()
    with wt.locked_state():
        pass

    assert state.worktrees["/src/api-wt/x"].name == "x"
    assert state.repos["/src/api"].base_port == 8200
    # A newer wt's fields survive a write by an older one.
    saved = json.loads((tmp_path / "wt" / "state.json").read_text())
    assert saved["worktrees"]["/src/api-wt/x"]["retired_field"] == 1
    assert saved["repos"]["/src/api"]["retired_field"] == 1


def test_read_state_refuses_a_newer_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / "state.json").write_text(
        json.dumps({"version": wt.STATE_VERSION + 1})
    )

    with pytest.raises(wt.WtError, match="newer wt"):
        wt.read_state()


def skill(*args: str, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WT), "skill", *args],
        env={**os.environ, "HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
    )


def test_skill_install_writes_stubs_that_show_the_shipped_guide(tmp_path: Path):
    installed = skill("install", home=tmp_path)

    assert installed.returncode == 0, installed.stderr
    for agent in (".claude", ".agents"):
        stub = (tmp_path / agent / "skills" / "rex-agents" / "SKILL.md").read_text()
        assert "name: rex-agents" in stub
        assert "Run `wt skill show rex-agents`" in stub
    guide = skill("show", "rex-agents", home=tmp_path).stdout
    assert guide.startswith("`wt` owns the worktree lifecycle")


def test_skill_install_and_remove_leave_other_skills_alone(tmp_path: Path):
    own = tmp_path / ".claude" / "skills" / "rex-agents" / "SKILL.md"
    own.parent.mkdir(parents=True)
    own.write_text("my own skill")

    refused = skill("install", home=tmp_path)
    skill("remove", home=tmp_path)

    assert refused.returncode != 0
    assert "--force" in refused.stderr
    assert own.read_text() == "my own skill"
    assert not (tmp_path / ".agents" / "skills" / "rex-agents").exists()


def test_config_falls_back_to_the_main_checkout_when_the_branch_copy_is_broken(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo, path = tmp_path / "app", tmp_path / "app-wt" / "feat-x"
    for checkout, text in ((repo, '[install]\ncommand = "main"\n'), (path, "[nope]\n")):
        checkout.mkdir(parents=True)
        (checkout / ".wt.toml").write_text(text)
    record = wt.Record(
        repo=str(repo),
        name="feat-x",
        path=str(path),
        branch="feat/x",
        status="ready",
        created_at=wt.now(),
    )

    assert record.config().install == "main"
    assert record.config().install == "main"
    assert capsys.readouterr().err.count("ignoring") == 1


@pytest.mark.parametrize(
    ("process", "outcome"),
    [
        ({"status": "Running", "is_ready": "Ready"}, "done"),
        ({"status": "Running", "is_ready": "Not Ready"}, "pending"),
        ({"status": "Running", "is_ready": "-", "has_ready_probe": True}, "pending"),
        ({"status": "Running", "is_ready": "-", "has_ready_probe": False}, "done"),
        ({"status": "Pending"}, "pending"),
        ({"status": "Completed", "exit_code": 0}, "done"),
        ({"status": "Completed", "exit_code": 1}, "failed"),
        ({"status": "Running", "exit_code": 1, "restarts": 1}, "done"),
        ({"status": "Error"}, "failed"),
        (None, "pending"),
    ],
)
def test_process_outcome(process: dict[str, Any] | None, outcome: str):
    assert wt.process_outcome(process) == outcome


def test_up_failure_points_at_the_dependency_that_failed():
    procs = {
        "postgres": {"name": "postgres", "status": "Skipped"},
        "postgres-init": {
            "name": "postgres-init",
            "status": "Completed",
            "exit_code": 1,
        },
        "redis": {"name": "redis", "status": "Running", "is_ready": "Ready"},
    }

    message = wt.up_failure("sentry", ["postgres"], procs)

    assert message == (
        "postgres stopped; postgres-init failed: see `wt logs sentry postgres-init`"
    )
