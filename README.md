<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/logo-dark.png">
    <img src="docs/images/logo-light.png" alt="AgentMesh logo" width="140">
  </picture>
</p>

<h1 align="center">AgentMesh</h1>

<p align="center">
  A local coordination layer for several AI coding agents working on one codebase.
</p>

<p align="center">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB">
  <img alt="Platform: Windows" src="https://img.shields.io/badge/platform-Windows-0078D4">
  <img alt="Version 0.5.6" src="https://img.shields.io/badge/version-0.5.6-lightgrey">
  <img alt="96 tests" src="https://img.shields.io/badge/tests-96-brightgreen">
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/demo-dark.gif">
    <img src="docs/images/demo-light.gif" alt="A leader assigns two tasks, two workers build them in their own Git worktrees, the agents review each other's work, and the branches merge into main." width="800">
  </picture>
</p>

<p align="center"><sub>A leader plans, workers build in separate Git worktrees, a different agent reviews each commit, and the branches merge.</sub></p>

You run a leader, a few workers, a reviewer and an integrator as separate agent sessions (Claude Code, Codex, Gemini, anything that can run a shell command). They don't talk to each other directly. They all talk to one CLI, which keeps the state in a local SQLite database and makes sure the rules hold:

- **Each worker gets its own Git worktree and branch.** The CLI creates it when the worker claims a task. Workers never touch the base branch.
- **Nothing merges without review.** A worker submits a commit, a different agent reviews that exact SHA, and only then does the integrator merge.
- **Tasks carry acceptance criteria and dependencies.** The leader imports a plan once. The CLI holds dependent tasks back until their prerequisites are merged.
- **Work survives crashes.** Claims are leases that `next` renews. If an agent disappears, its lease expires and the task can be recovered.
- **Every answer says what to do next.** Each response is one JSON line with a `next_action`, so an agent that has never seen the project can follow it.

Everything runs on your machine. There is no server and no network access.

## Status

Version 0.5.6. It works and has 96 tests, but it is young and I built it around my own workflow, which is Windows. The leader appointment popup uses a Windows dialog, and the command examples assume Windows paths. Treat it as a working prototype, not a polished product.

## Install

Python 3.12 or newer and Git are required.

```
git clone https://github.com/mann13072/agentmesh-local.git
cd agentmesh-local
python -m venv .venv
.venv\Scripts\pip install .
.venv\Scripts\agentmesh --version
.venv\Scripts\agentmesh doctor
```

On a fresh install `agentmesh doctor` will report the rulebook as unsealed and the host skills as missing. That is expected: run `rulebook-seal` and add the rulebook to your agents' skills when you set up a project.

## How it works

Every call is one JSON object in, one JSON line out:

```
agentmesh --root <project-root> <command> --input <file | ->
agentmesh help worker
agentmesh help leader
agentmesh help <command>
```

The typical flow:

1. **Leader:** `leader-join`, then `project-git`, then `plan-import` with the whole task graph.
2. **Worker:** `worker-join`, then `task-claim` with `"start": true`. The CLI creates the worktree. The worker commits there, calls `checkpoint` along the way and `task-submit` when done.
3. **Reviewer:** `review` with an outcome, evidence and the `reviewed_sha`. A worker can never review its own work.
4. **Integrator:** `task-integrate` with `"merge": true`, then verifies on the base branch.
5. **Everyone:** call `next` on a timer. It returns new messages and the next action, and it renews the agent's leases.

Run `agentmesh help` for the full command list.

## The rulebook

`vault/04-Skills/agent-mesh/SKILL.md` is the one file an agent has to read. It holds the rules the CLI can't enforce by itself: who may ask the human what, what counts as scope, and what each role must never do. It is written as an agent skill, so you can drop it into a Claude Code skills folder or paste it into any agent's instructions.

## Tests

```
.venv\Scripts\pip install pytest
.venv\Scripts\python -m pytest -q
```

Some tests create Git worktrees, so run them from a short path. Windows rejects very long worktree paths.

## License

MIT. See [LICENSE](LICENSE).
