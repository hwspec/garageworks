# Using gwscript's MCP server with Claude Code

`gwscript --mcp` serves the same stage pipeline (`prereq`, `rtlgen`, `cocotb`,
`fpgatiming`, `aved`, `buildhw`, `programhw`, `testhw`) as an
[MCP](https://modelcontextprotocol.io/) server over stdio: tools like
`status`, `dry_run`, `run`, `job`, `log`, `jobs`, `cancel`, `results`,
`clean` and `sudoers` map directly onto the CLI, sharing one lock and one set
of state files with it. This lets an agent (Claude Code, Claude Desktop,
anything else that speaks MCP) drive a GarageWorks project without shelling
out to `gwscript` itself.

A common setup is **two MCP servers pointed at two different places**: one
for local simulation (`cocotb` and friends), one for a remote machine that
has an actual FPGA board attached. This doc walks through both.

## Prerequisites

- `pip install "garageworks[mcp]"` (or `pip install mcp` alongside an
  existing `garageworks` install) in whichever Python environment `gwscript`
  runs from — local and remote can use different environments.
- A GarageWorks project directory with a working `.gwconfig` (see
  [garageworks.md](garageworks.md)). `gwscript --mcp` loads `.gwconfig` the
  same way the CLI does, so the project must already be set up (or scaffold
  it first with `gwscript --setup ...`).
- The `claude` CLI (Claude Code). Everything below uses `claude mcp add`.

## MCP servers must be spawned by the client

A stdio MCP server is just a subprocess whose stdin/stdout carry JSON-RPC
messages. Claude Code has to be the one that launches that subprocess — it
cannot attach to a `gwscript --mcp` process someone already started by hand
in another terminal. If you've manually run `gwscript --mcp` somewhere,
stop it; `claude mcp add` will start its own copy with the exact command you
give it.

## Local server

Register a server for a local project directory:

```sh
claude mcp add gws-local -- /path/to/env/bin/gwscript -C /path/to/project --mcp
```

- Use the full path to the `gwscript`/`gws` entry point inside whichever
  Python environment (venv, conda env, ...) has `garageworks` installed —
  don't rely on `PATH`, since the command Claude Code spawns doesn't go
  through your shell's rc files.
- `-C /path/to/project` pins the project root, so the server always operates
  on that project regardless of Claude Code's own working directory.

Newly-registered MCP servers don't appear mid-session: restart Claude Code
(`/exit`, then `claude --continue`) so it reconnects and loads the new
server's tools. After that, `claude mcp list` should show the server as
`✔ Connected`, and tools show up as `mcp__gws-local__status`,
`mcp__gws-local__run`, etc.

## Remote server over SSH (including two hops)

If the FPGA board (or just a faster build machine) lives on a remote host,
point a second server at it over SSH instead of installing a second copy of
Claude Code there. Since `claude mcp add` passes its command straight to
`exec` (no local shell involved), you can chain SSH hops directly:

One hop:

```sh
claude mcp add gws-remote -- ssh -T remote-host /path/to/env/bin/gwscript -C /path/to/project --mcp
```

Two hops (e.g. a bastion/jump host in front of the actual target):

```sh
claude mcp add gws-remote -- ssh -T jumphost 'ssh -T target-host /path/to/env/bin/gwscript -C /path/to/project --mcp'
```

This works because each `ssh` layer re-quotes and forwards its remaining
argv to the next hop's shell; nesting two levels of `ssh -T ... 'ssh -T ...'`
is enough even without a local shell in the loop. For more than two hops,
nest further the same way, or use an `ssh -J` ProxyJump host alias defined
in `~/.ssh/config` and skip the manual chaining entirely.

Requirements for this to work non-interactively:

- **Key-based auth at every hop** — no password or interactive MFA prompt,
  since nothing is attached to a terminal to answer one.
- **Host keys already trusted** at every hop (`ssh` has connected manually
  at least once, or the key is in `known_hosts`) — a first-time
  `"Are you sure you want to continue connecting?"` prompt will hang the
  server forever.
- **`-T`** (no pseudo-terminal) on every `ssh` in the chain — a pty adds
  terminal control sequences and local echo to the stream, which corrupts
  the JSON-RPC framing MCP needs on stdout.
- **No extra stdout output from login** — login banners, MOTDs, or
  `.bashrc`/`.profile` scripts that `echo` something will also land on the
  same stdout MCP reads from and corrupt the stream. Keep remote login
  shells for this account quiet, or invoke an explicit non-login shell/path
  instead of relying on `ssh`'s default remote shell.

### conda/venv activation on the remote end

Giving the full path to the `gwscript` binary inside an environment
(`/path/to/env/bin/gwscript`) runs *that interpreter*, but stages that shell
out to other tools (`make`, test helper scripts, toolchains) still resolve
those by name through whatever `PATH` the SSH session inherits — which, for
a non-interactive SSH command, is usually the login shell's default `PATH`,
**not** the activated environment's. If a stage's subprocess picks up a
different (often older, or entirely missing) copy of a tool from a
system/base environment instead of the one in your target environment, that
is almost always this `PATH` ordering issue, not a broken install.

Two ways to fix it:

```sh
# 1. Prepend the environment's bin/ to PATH explicitly in the remote command
claude mcp add gws-remote -- ssh -T host 'PATH=/path/to/env/bin:$PATH /path/to/env/bin/gwscript -C /path/to/project --mcp'

# 2. Or activate the environment properly before running gwscript
claude mcp add gws-remote -- ssh -T host 'source /path/to/conda/etc/profile.d/conda.sh && conda activate myenv && gwscript -C /path/to/project --mcp'
```

Prefer whichever matches how you'd normally activate the environment by
hand — the point is just that `PATH` needs to list the target environment's
`bin/` first for the whole lifetime of the `gwscript` process, not just for
launching it.

## Routing conventions between multiple servers

With two servers registered, it helps to pick a simple convention so you
(and the agent) don't have to think about which tool prefix to use each
time — for example: "stages that only need simulation (`cocotb`, ...) run
through the local server; stages that touch the board (`testhw`, ...) run
through the remote server." State the convention once in the conversation
(or in a project `CLAUDE.md`) and Claude Code will route accordingly for
the rest of the session.

## Verifying the setup

```sh
claude mcp list          # should show every registered server as "✔ Connected"
```

Inside a Claude Code session (after the restart mentioned above), ask it to
call `status` on each server, or run `gws-local`/`gws-remote`'s `dry_run`
tool to confirm each one is pointed at the project you expect before running
anything that touches hardware.
