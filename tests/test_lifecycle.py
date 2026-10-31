"""End-to-end runs of the wt CLI against throwaway git repos, with real git and direnv."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

WT = Path(__file__).resolve().parent.parent / "wt.py"
CONFIG = """\
[ports]
var = "APP_PORT"
[install]
command = "sh -c 'echo $APP_PORT > installed'"
"""

pytestmark = pytest.mark.skipif(
    not shutil.which("direnv") or not shutil.which("git"), reason="needs git and direnv"
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "WT_PORT_RANGE": "20300-20999",
    }  # fmt: skip


def git(env: dict[str, str], cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def make_repo(
    tmp_path: Path,
    env: dict[str, str],
    *,
    config: str | None,
    envrc_ignored: bool = True,
) -> Path:
    remote = tmp_path / "remote.git"
    git(env, tmp_path, "init", "--quiet", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    git(env, tmp_path, "clone", "--quiet", str(remote), str(seed))
    ignores = ".envrc\ninstalled\n" if envrc_ignored else "installed\n"
    (seed / ".gitignore").write_text(ignores)
    if config is not None:
        (seed / ".wt.toml").write_text(config)
    if not envrc_ignored:
        (seed / ".envrc").write_text("export APP_PORT=8000\n")
    git(env, seed, "add", "-A")
    git(env, seed, "commit", "--quiet", "-m", "init")
    git(env, seed, "push", "--quiet", "origin", "main")
    repo = tmp_path / "app"
    git(env, tmp_path, "clone", "--quiet", str(remote), str(repo))
    if envrc_ignored:
        (repo / ".envrc").write_text("export A=1\nexport APP_PORT=8000\n")
    return repo


def wt(
    env: dict[str, str], repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(WT), "-C", str(repo), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        pytest.fail(f"wt {' '.join(args)} failed:\n{result.stderr}")
    return result


def state(env: dict[str, str]) -> dict[str, Any]:
    return json.loads((Path(env["XDG_STATE_HOME"]) / "wt" / "state.json").read_text())


def test_new_exec_rm(tmp_path: Path, env: dict[str, str]):
    repo = make_repo(tmp_path, env, config=CONFIG)

    [created] = json.loads(wt(env, repo, "new", "feat/x", "--json").stdout)

    path = Path(created["path"])
    assert path == tmp_path / "app-wt" / "feat-x"
    assert created["base_port"] == 20300
    assert created["start_commit"] == git(env, repo, "rev-parse", "origin/main")
    assert (path / ".envrc").read_text().startswith("export APP_PORT=20300\n")
    assert (path / "installed").read_text().strip() == "20300"

    ran = wt(env, repo, "exec", "feat-x", "--", "sh", "-c", 'pwd; echo "$APP_PORT"')
    assert ran.stdout.split() == [str(path), "20300"]

    (path / "scratch").write_text("work")
    refused = wt(env, repo, "rm", "feat-x", check=False)
    assert refused.returncode != 0
    assert "scratch" in refused.stderr
    assert path.exists()

    (path / "scratch").unlink()
    wt(env, repo, "rm", "feat-x")
    assert not path.exists()
    assert git(env, repo, "branch", "--list", "feat/x")
    assert state(env)["worktrees"] == {}


def test_parallel_worktrees_get_distinct_blocks(tmp_path: Path, env: dict[str, str]):
    repo = make_repo(tmp_path, env, config=CONFIG)
    runs = [
        subprocess.Popen(
            [sys.executable, str(WT), "-C", str(repo), "new", f"feat/{i}", "--json"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(4)
    ]

    ports = [json.loads(p.communicate()[0])[0]["base_port"] for p in runs]

    assert sorted(ports) == [20300, 20400, 20500, 20600]


def test_failed_install_rolls_back(tmp_path: Path, env: dict[str, str]):
    repo = make_repo(tmp_path, env, config=CONFIG.replace("echo", "exit 3; echo"))

    failed = wt(env, repo, "new", "feat/x", check=False)

    assert failed.returncode != 0
    assert "Rolling back" in failed.stderr
    assert not (tmp_path / "app-wt" / "feat-x").exists()
    assert not git(env, repo, "branch", "--list", "feat/x")
    assert state(env)["worktrees"] == {}


def test_without_config_makes_plain_worktrees(tmp_path: Path, env: dict[str, str]):
    repo = make_repo(tmp_path, env, config=None)

    [created] = json.loads(wt(env, repo, "new", "feat/x", "--json").stdout)

    assert created["base_port"] is None
    assert (Path(created["path"]) / ".envrc").read_text() == (
        repo / ".envrc"
    ).read_text()
    stackless = wt(env, repo, "up", "feat-x", check=False)
    assert "no [stack]" in stackless.stderr


def test_ports_need_an_ignored_envrc(tmp_path: Path, env: dict[str, str]):
    repo = make_repo(tmp_path, env, config=CONFIG, envrc_ignored=False)

    refused = wt(env, repo, "new", "feat/x", check=False)

    assert refused.returncode != 0
    assert ".envrc must be ignored" in refused.stderr
    assert not (tmp_path / "app-wt").exists()


def test_ports_refuse_a_branch_that_tracks_envrc(tmp_path: Path, env: dict[str, str]):
    repo = make_repo(tmp_path, env, config=CONFIG)
    git(env, repo, "switch", "--quiet", "-c", "tracked")
    (repo / ".gitignore").write_text("installed\n")
    git(env, repo, "add", "-A")
    git(env, repo, "add", "-f", ".envrc")
    git(env, repo, "commit", "--quiet", "-m", "track .envrc")
    git(env, repo, "switch", "--quiet", "main")
    # Leaving the branch that tracks it removed the main checkout's .envrc.
    (repo / ".envrc").write_text("export APP_PORT=8000\n")

    refused = wt(env, repo, "new", "tracked", check=False)

    assert refused.returncode != 0
    assert "tracks .envrc" in refused.stderr
    assert not (tmp_path / "app-wt" / "tracked").exists()
    assert state(env)["worktrees"] == {}
