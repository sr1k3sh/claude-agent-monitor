---
name: monitor
description: Local live dashboard (http://127.0.0.1:8765) of the Claude Code sessions in the repos you watch — an animated orchestration graph per session, an optional stage pipeline per task (Research → Plan → Execute → Review → Fix → QA → your OK), live tool calls, tokens and estimated cost. Use when the user says /agent-monitor:monitor, asks to watch or monitor agent progress, wants to see a task's or thread's progress in a GUI, or when you start a multi-agent (plan / execute / review / QA) task and they want to follow it.
---

# Agent monitor

Usage: `/agent-monitor:monitor [start | track <title> | unwatch | stop]` — no argument means `start`.

Scope: only repos in the watch list (`~/.claude/agent-monitor/scopes.json`), each with its worktrees. A session shows while it is active (written in the last 2 min) or, once it has run subagents, until nothing in it has written for 30 minutes (`HIDE_AFTER_MIN`). It also disappears after `/clear` (the old session stops being written) and as soon as every stage of its tracked pipeline is `done`.

Read-only over Claude Code's transcripts in `~/.claude/projects/`; its own state is `~/.claude/agent-monitor/` (`scopes.json`, `pipelines/`). Stdlib Python, bound to `127.0.0.1` only.

## start

1. Watch this repo: `python3 ${CLAUDE_SKILL_DIR}/monitor.py scope add "$(pwd)"` (worktrees resolve to the main checkout; prints the watch list).
2. `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/api` — `200` means the server is already running (it picks up the new scope on its next poll); skip to 4.
3. Run `python3 ${CLAUDE_SKILL_DIR}/monitor.py` with the Bash tool's `run_in_background` (`--port N` if 8765 is taken by something else).
4. `open http://127.0.0.1:8765` and tell the user the URL. `?s=<first 8 chars of a session id>` opens that session's tab.

What the page shows:
- One tab per shown session. The graph is a timeline of your questions in that session (the last 6, plus `+N earlier`): each is `done` ✓ (with time and cost), `running` ▶, `waiting` ⏸ or `pending` ◌ (typed while Claude was busy). Subagents are listed under the question they started in. Sessions without a readable transcript fall back to the Orchestrator → agents graph.
- A tracked task (below) replaces its session's automatic graph with the task's stage lanes, including stages that have not started.
- **Waiting on you** (amber, paused ⏸ instead of the spinner): a permission or input prompt (the plugin's `Notification` hook writes `~/.claude/agent-monitor/waiting/<session>.json`) or an unanswered `AskUserQuestion` / `ExitPlanMode`. It clears as soon as the session writes again. The browser tab title shows `⏸ (n) waiting`.
- Cards for those sessions (`active` = written in the last 2 min) and their subagents: last 25 actions, tokens, estimated USD (`prices.json`, public API rates — an estimate, not the bill).

## track — a stage pipeline for this task

Optional; only for the session you run it in. Do it once when you begin orchestrating agents for a task, before spawning the first one.

1. Session id: the session UUID in your scratchpad path, or `python3 ${CLAUDE_SKILL_DIR}/monitor.py session` (newest session for the current directory; wrong if two sessions in the same directory are writing at once — then use the scratchpad one).
2. Write `~/.claude/agent-monitor/pipelines/<short-task-slug>.json`:

```json
{
  "session": "<session id>",
  "title": "PROJ-42 — offline sync for the mobile app",
  "lanes": [
    {"name": "Discovery", "stages": [
      {"label": "Research", "match": "Research"},
      {"label": "Plan", "match": "Plan"},
      {"label": "Your decisions", "manual": "waiting", "note": "design choices"}
    ]},
    {"name": "PR 1", "stages": [
      {"label": "Execute", "match": "Execute PR1"},
      {"label": "Review", "match": "Review PR1"},
      {"label": "Fix review", "match": "Fix PR1"},
      {"label": "QA", "match": "QA PR1"},
      {"label": "Your OK → commit/PR", "manual": "pending"}
    ]}
  ]
}
```

3. A stage lights up from this session's subagents whose Agent `description` contains its `match` (case-insensitive). **Start every Agent description with the stage's match text** (e.g. `description: "Review PR1 diff"`). An agent without a match still shows in the cards, but its stage stays `pending`.
4. Stages that are not agents (user decisions, approvals) use `"manual"`: `pending`, `waiting` (amber, the user must act), or `done`. Edit the file as they change — it is reread on every poll, no restart.
5. When the task is finished, set the last manual stage to `done`: the task leaves the page. Delete the file once you no longer need it.

## unwatch

`python3 ${CLAUDE_SKILL_DIR}/monitor.py scope remove "$(pwd)"` — this repo's sessions stop showing.

## stop

Stop the background task that runs `monitor.py` (or `kill $(lsof -ti tcp:8765)`).

## Files

- `monitor.py` — server, transcript parsing, cost, graphs (`serve`, `scope`, `session`).
- `index.html` — the page (SVG graph, animations; respects reduced motion). Reread per request.
- `prices.json` — USD per 1M tokens by model-id prefix. Update it when model prices change.
- `test_monitor.py` — `python3 -m unittest ${CLAUDE_SKILL_DIR}/test_monitor.py`.
