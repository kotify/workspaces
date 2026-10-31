# workspaces

`wt` creates git worktrees that each have their own ports and their own [process-compose](https://github.com/F1bonacc1/process-compose) stack, so several branches of several repos can run side by side: one agent or one person per branch, each with its own database and dev servers, none of them colliding.

```sh
wt new feat/login --json   # worktree ../myapp-wt/feat-login, with a free port block
wt up feat-login           # start its stack and wait until it runs
wt exec feat-login -- make test
wt rm feat-login           # stop everything and remove the worktree; the branch stays
```

`wt` records every worktree of every repo in one state file under a lock, so port blocks never collide, even when several `wt new` run at once.

## Install

With Nix:

```sh
nix profile install github:kotify/workspaces
```

or as a flake input of your project's dev shell:

```nix
inputs.workspaces.url = "github:kotify/workspaces";
# ...
packages = [ inputs.workspaces.packages.${system}.default ];
```

Without Nix, `wt.py` and `VERSION` are all it needs: Python 3.11 or later, git, and, depending on the features you use, direnv and process-compose. It runs on macOS and Linux.

## How a repo opts in

Worktrees live next to the main checkout, in `<repo>-wt/<name>`. Without configuration, `wt` manages plain worktrees. A repo describes what its worktrees need in a tracked `.wt.toml`:

```toml
[install]
command = "make install"        # run in each new worktree

[ports]
var = "PROJECT_BASE_PORT"       # wt writes `export PROJECT_BASE_PORT=<base>` into the worktree's .envrc
block = 100                     # the repo's ports must stay within base .. base+99

[stack]
start = "app.start -D"          # starts process-compose detached; wt appends process names
processes = ["postgres", "redis"]
socket = ".direnv/.pc/process-compose.sock"
```

`wt config` prints the full reference. The contract behind it:

- **Ports.** With `[ports]`, the main checkout has an `.envrc` that git ignores, and the repo derives every port from the variable. `wt` copies the `.envrc` into each worktree with a free base port from a range shared by all repos (8300-9999, or `$WT_PORT_RANGE`), then checks the effective value with `direnv exec`.
- **Stack.** With `[stack]`, `start` runs process-compose detached with its API on `socket`, relative to the worktree. Keep data (databases, caches) inside the worktree, so each worktree's stack is isolated.
- **Main port.** With `[ports]`, the main checkout's `.envrc` sets the variable with a literal `export VAR=<number>` line, so `wt` can keep worktrees off the main checkout's ports.
- **Environment.** Commands run in the worktree's directory and, when it has an `.envrc`, in its direnv environment.
- **Layout.** The main checkout holds `.git`. Bare repos, submodules, and separate git directories aren't supported.

A Nix flake built with [process-compose-flake](https://github.com/Platonic-Systems/process-compose-flake) and [services-flake](https://github.com/juspay/services-flake) fits the contract with little work: derive ports from the variable, keep service data under the project directory, and put the socket under `.direnv/`.

## Trust

A worktree runs its branch's code. `wt new` runs `direnv allow` and the install command, so the branch's `.envrc` setup (for example its `flake.nix`), its `.wt.toml`, and its package scripts run right away, and `wt up` and `wt exec` run whatever the branch contains. This is what `git checkout` and direnv already do in your main checkout: nix-direnv re-evaluates a changed `flake.nix` without asking. `wt` has no trust step of its own, because one that runs once would miss the code that later pulls bring in.

Use `wt` only on branches whose authors you'd let run code on your machine. To review anyone else's branch, read it with `git fetch` and `git diff` in the main checkout, which runs nothing. Run code from strangers, such as fork pull requests, only in an isolated environment without your credentials.

## Commands

| Command | What it does |
| --- | --- |
| `wt new BRANCH` | Create a worktree: check out or create the branch, assign ports, install. Rolls back on failure. |
| `wt ls [--all]`, `wt info NAME` | Describe worktrees: path, branch, ports, start commit, stack state, Rex session. |
| `wt up NAME [PROCESS...]` | Start the stack's default processes and the named ones (`--only`: just the named), and wait until they run; fail fast when one stops. |
| `wt down NAME`, `wt logs NAME PROCESS` | Stop the stack; read a process's logs. |
| `wt exec NAME -- COMMAND` | Run a command in the worktree's directory and environment. |
| `wt rm NAME [--force]` | If the worktree is clean, close its Rex session, stop its stack, and remove it. |
| `wt skill install`, `wt skill show NAME` | Install the agent skills `wt` ships; print a skill's guide. |
| `wt rex NAME [-- COMMAND]` | Open the worktree in a [Rex](https://www.superlogical.com/rex) session, optionally with an agent. |

`-C DIR` before the command acts on another repo. `--json` prints machine-readable output. `wt COMMAND --help` has the details.

## Agents

`wt` is built to be driven by coding agents. Each command's help is written for them, and it ships agent skills:

- `rex-agents`: run sub-agents in parallel, each in its own worktree and Rex session, from one orchestrating agent.

`wt skill install` installs them for Claude Code (`~/.claude/skills`) and for Codex and other agents (`~/.agents/skills`); `--dir` picks another directory, such as a repo's `.agents/skills`. It writes a short stub that runs `wt skill show NAME`, so the guide always matches the `wt` on `PATH`, including the version a repo pins. `wt skill remove` removes the stubs.

## Development

```sh
nix develop
pytest
ruff check . && ruff format --check . && pyright
nix flake check   # everything CI runs
```

## License

MIT
