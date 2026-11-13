# agent-monitor

A Claude Code plugin: a live local dashboard (http://127.0.0.1:8765) of the Claude Code sessions running in the repos you watch.

- One tab per live session, with an animated orchestration graph: the main session fans out to its subagents, parallel agents share a column, and a moving dot shows where work is happening now.
- An optional stage pipeline per task (Research → Plan → Execute → Review → Fix → QA → your OK), so you also see the steps that haven't started yet.
- Live tool calls, errors, tokens and estimated cost for every session and agent.
- Only watched repos (plus their worktrees). Finished, idle or cleared sessions drop off on their own.

It only reads Claude Code's transcripts in `~/.claude/projects/` and keeps its own state in `~/.claude/agent-monitor/`. It uses only the Python standard library and listens on `127.0.0.1`.

## Install

```sh
claude plugin marketplace add sr1k3sh/claude-agent-monitor
claude plugin install agent-monitor@sr1k3sh-plugins
```

Update: `claude plugin marketplace update sr1k3sh-plugins && claude plugin update agent-monitor@sr1k3sh-plugins`, then `/reload-plugins`.

From a local clone: `claude plugin marketplace add /path/to/claude-agent-monitor`, then install the same way. Edits apply on the next session or after `/reload-plugins`.

## Use

In a session inside the repo you want to watch:

- `/agent-monitor:monitor`: adds this repo to the watch list, starts the dashboard if it isn't running, and opens it.
- `/agent-monitor:monitor track <title>`: adds a stage pipeline for this session's task.
- `/agent-monitor:monitor unwatch`: stops watching this repo.

Without Claude: `python3 skills/agent-monitor/monitor.py scope add <repo>`, then `python3 skills/agent-monitor/monitor.py`.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `HIDE_AFTER_MIN` | `30` | Minutes of no activity before a session with agents is hidden |
| `AGENT_MONITOR_HOME` | `~/.claude/agent-monitor` | Where `scopes.json` and `pipelines/` live |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Claude Code transcripts |

Costs are estimates at public API prices (`skills/agent-monitor/prices.json`), not your bill.

## Test

```sh
python3 -m unittest skills/agent-monitor/test_monitor.py
```
