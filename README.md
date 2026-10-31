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

Without Nix, `wt.py` and `VERSION` are all it needs: Python 3.11 or later, git, and, depending on the features you use, direnv, process-compose, lsof, and ps. It runs on macOS and Linux.

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

## Commands

| Command | What it does |
| --- | --- |
| `wt new BRANCH` | Create a worktree: check out or create the branch, assign ports, install. Rolls back on failure. |
| `wt ls [--all]`, `wt info NAME` | Describe worktrees: path, branch, ports, start commit, stack state, Rex session. |
| `wt up NAME [PROCESS...]` | Start the stack's default processes and the named ones (`--only`: just the named), and wait until they run; fail fast when one stops. |
| `wt down NAME`, `wt logs NAME PROCESS` | Stop the stack; read a process's logs. |
| `wt exec NAME -- COMMAND` | Run a command in the worktree's directory and environment. |
| `wt rm NAME [--force]` | Close the Rex session, stop the stack, then remove the worktree if it's clean. |
| `wt ps`, `wt prune` | List every process-compose server on the machine; stop the ones left behind. |
| `wt rex NAME [-- COMMAND]` | Open the worktree in a [Rex](https://www.superlogical.com/rex) session, optionally with an agent. |

`-C DIR` before the command acts on another repo. `--json` prints machine-readable output. `wt COMMAND --help` has the details.

## Agents

`wt` is built to be driven by coding agents. Each command's help is written for them, and `wt skills` prints the directory of the agent skills it ships:

- `rex-agents`: run sub-agents in parallel, each in its own worktree and Rex session, from one orchestrating agent.

Install a skill by linking it into your agent's skills directory, for example `ln -s "$(wt skills)/rex-agents" ~/.claude/skills/rex-agents`.

## Development

```sh
nix develop
pytest
ruff check . && ruff format --check . && pyright
nix flake check   # everything CI runs
```

## License

MIT
