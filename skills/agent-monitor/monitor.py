#!/usr/bin/env python3
"""Local dashboard of live Claude Code sessions and their subagents: http://127.0.0.1:8765

Only sessions started in a watched repo (or one of its worktrees) are read:
~/.claude/projects/<that dir>*/<session>.jsonl and <session>/subagents/**/agent-*.jsonl (workflow agents sit in subagents/workflows/<run>/).
Watched repos are listed in $AGENT_MONITOR_HOME/scopes.json (`monitor.py scope add DIR`).
A session is shown while it is active (written in the last 2 min) or, once it has run subagents, while it or one of its agents wrote in the last HIDE_AFTER_MIN minutes
(default 30) and, when it has a task pipeline ($AGENT_MONITOR_HOME/pipelines/*.json), while that
pipeline has a stage not done. Stdlib only, read-only, binds to localhost. The page polls /api
every 2s and updates in place (keeps animations).

  monitor.py [serve] [--port 8765]          run the dashboard (/api, /api/agent?s=SESSION&id=AGENT|main)
  monitor.py scope add|remove|list [DIR]    watch / unwatch a repo (default DIR: the current directory)
  monitor.py session [--cwd DIR]            print the newest main session id for a project dir
  monitor.py notify                         Notification hook: mark the stdin session as waiting on the user
"""
import argparse, glob, html, json, os, re, statistics, subprocess, sys, time
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))
HOME = os.environ.get("AGENT_MONITOR_HOME", os.path.expanduser("~/.claude/agent-monitor"))
HERE = os.path.dirname(os.path.abspath(__file__))
HIDE_S = float(os.environ.get("HIDE_AFTER_MIN", "30")) * 60


def main_checkout(path):
    """The repo's main checkout, also when path is inside one of its worktrees; path itself outside git."""
    try:
        common = subprocess.run(["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, check=True).stdout.strip()
        return os.path.dirname(common)
    except (OSError, subprocess.CalledProcessError):
        return os.path.abspath(path)


SCOPES_FILE = os.path.join(HOME, "scopes.json")


def scopes():
    """Watched repos; AGENT_MONITOR_SCOPE (os.pathsep-separated) overrides the file."""
    if os.environ.get("AGENT_MONITOR_SCOPE"):
        return os.environ["AGENT_MONITOR_SCOPE"].split(os.pathsep)
    try:
        with open(SCOPES_FILE) as f:
            return [d for d in json.load(f) if isinstance(d, str)]
    except (OSError, ValueError):
        return []


def edit_scopes(op, path):
    cur, d = scopes(), main_checkout(path)
    new = sorted(set(cur) | {d}) if op == "add" else [x for x in cur if x != d]
    os.makedirs(HOME, exist_ok=True)
    with open(SCOPES_FILE, "w") as f:
        json.dump(new, f, indent=2)
    return new
STALE_S = 600  # running agent with no writes for 10 min is shown as "stalled?"
HIST_MAX, HIST_TTL, MIN_RUNS = 400, 60, 3  # estimates: newest 400 agent transcripts, rescanned each minute, 3+ runs
ASK_TOOLS = {"AskUserQuestion", "ExitPlanMode"}  # a tool_use of these without its result = waiting on the user
WAIT_DIR = os.path.join(HOME, "waiting")


def notify(data):
    """Notification hook (permission / input prompt): one small file per session, its mtime = when it asked."""
    sid = str(data.get("session_id", ""))
    if not re.fullmatch(r"[\w-]+", sid):  # used as a file name
        return
    os.makedirs(WAIT_DIR, exist_ok=True)
    with open(os.path.join(WAIT_DIR, sid + ".json"), "w") as f:
        json.dump({"message": short(data.get("message") or "needs your input", 120)}, f)


def wait_flag(sid):
    """(mtime, message) of the session's last prompt to the user; a file written after it means it was answered.
    shortcut: flag files are never deleted (a few bytes per session), prune if that ever matters."""
    p = os.path.join(WAIT_DIR, sid + ".json")
    try:
        with open(p) as f:
            return os.path.getmtime(p), json.load(f).get("message", "")
    except (OSError, ValueError, AttributeError):
        return 0, ""


def short(v, n=140):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    return s[:n] + ("…" if len(s) > n else "")


def tool_target(inp):
    """(key, value) of the input field that best says what a tool call does."""
    for k in ("description", "command", "file_path", "notebook_path", "pattern", "query", "url", "prompt", "message"):
        if isinstance(inp, dict) and inp.get(k):
            return k, inp[k]
    return None, inp


def tool_summary(name, inp):
    """One line in human words: 'Bash: Check git status', 'Read LocationService.kt'."""
    k, v = tool_target(inp)
    if name == "SubagentHandback":
        return f"Reported: {short(v)}"
    if k in ("file_path", "notebook_path"):
        return f"{name} {os.path.basename(str(v))}"
    return f"{name}: {short(v)}"


def load_prices():
    try:
        with open(os.path.join(HERE, "prices.json")) as f:
            return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    except (OSError, ValueError):
        return {}


def cost_of(model, u, prices):
    """USD for one API response's usage; None when the model has no price entry."""
    key = max((k for k in prices if (model or "").startswith(k)), key=len, default=None)
    if not key:
        return None
    p, cc = prices[key], u.get("cache_creation") or {}
    if cc:
        w5, w1 = cc.get("ephemeral_5m_input_tokens", 0), cc.get("ephemeral_1h_input_tokens", 0)
    else:
        w5, w1 = u.get("cache_creation_input_tokens", 0), 0
    usd = (u.get("input_tokens", 0) * p["input"] + u.get("output_tokens", 0) * p["output"]
           + u.get("cache_read_input_tokens", 0) * p["cache_read"]
           + w5 * p["input"] * 1.25 + w1 * p["input"] * 2) / 1e6
    return usd * (2 if u.get("speed") == "fast" else 1)


def prompt_label(r):
    """Label of a user record that starts a new question, else None (tool results, notifications, meta)."""
    text = user_text(r).strip()
    if r.get("isMeta") or not text or text.startswith("[Request interrupted"):
        return None
    cmd = re.search(r"<command-name>(.*?)</command-name>", text)
    if cmd:  # a slash command you typed
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        return short(f"{cmd.group(1)} {args.group(1) if args else ''}".strip(), 90)
    return None if text.startswith("<") else short(text, 90)


def user_text(r):
    c = (r.get("message") or {}).get("content")
    return c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text")


_cache = {}  # (path, parser) -> ((mtime, size), parsed) so 2s polling doesn't re-read unchanged multi-MB transcripts


def parse(path, fn=None):
    fn = fn or _parse
    key = (os.path.getmtime(path), os.path.getsize(path))
    if (path, fn) in _cache and _cache[path, fn][0] == key:
        return _cache[path, fn][1]
    _cache[path, fn] = (key, fn(path))
    return _cache[path, fn][1]


def clip(s, n):
    """Truncate long text but keep its line breaks (prompts and reports read better that way)."""
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s[:n] + ("…" if len(s) > n else "")


MAX_TIMELINE, MAX_TEXT = 200, 4000


def _detail(path):
    """Prompt, tool-call timeline (last MAX_TIMELINE, with ok/error) and final report of one transcript."""
    prompt, handback, last_text, items, by_id = "", "", "", [], {}
    with open(path, errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            content, ts = (r.get("message") or {}).get("content"), r.get("timestamp")
            if r.get("type") == "user" and not prompt and not r.get("isMeta"):
                prompt = clip(user_text(r).strip(), MAX_TEXT)
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                t = c.get("type")
                if t == "tool_result" and c.get("tool_use_id") in by_id:
                    by_id.pop(c["tool_use_id"])["ok"] = not c.get("is_error")
                elif r.get("type") != "assistant":
                    continue
                elif t == "tool_use":
                    inp = c.get("input")
                    if c.get("name") == "SubagentHandback" and isinstance(inp, dict):
                        handback = clip(inp.get("message", ""), MAX_TEXT)  # a subagent's final report
                    items.append({"t": ts, "kind": "tool", "name": c.get("name"),
                                  "what": short(tool_target(inp)[1], 300), "ok": None})
                    by_id[c.get("id")] = items[-1]
                elif t == "text" and c.get("text", "").strip():
                    items.append({"t": ts, "kind": "text", "what": clip(c["text"].strip(), 1000)})
                    last_text = clip(c["text"].strip(), MAX_TEXT)
    return {"prompt": prompt, "result": handback or last_text, "timeline": items[-MAX_TIMELINE:], "total": len(items)}


def _parse(path):
    events, tools, first, last, errors, asking = [], 0, None, None, 0, {}
    title = {}  # Claude Code's session title records: ai-title (auto), custom-title (/rename)
    turns, queue = [], []  # one dict per question you asked; queue = prompts typed while Claude was busy

    def start_turn(label, ts):
        if turns and not turns[-1]["end"]:
            turns[-1]["end"] = ts
        turns.append({"label": label, "t0": ts, "end": None, "usd": 0.0, "tools": 0, "last": ""})
    prices, seen = load_prices(), set()
    tok = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "usd": 0.0, "unpriced": set()}
    with open(path, errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = r.get("timestamp")
            if ts:
                first = first or ts
                last = ts
            msg = r.get("message") or {}
            kind = r.get("type")
            if kind in ("ai-title", "custom-title"):
                title[kind] = r.get("aiTitle") or r.get("customTitle") or title.get(kind)
            elif kind == "queue-operation":
                op, qc = r.get("operation"), r.get("content")
                label = prompt_label({"message": {"content": qc}}) if isinstance(qc, str) else None
                if op == "enqueue":
                    queue.append(label)
                elif op == "dequeue" and queue:
                    queue.pop(0)
                elif op == "remove":
                    hit = label if label in queue else (queue[0] if queue else None)
                    if queue:
                        queue.remove(hit)
                    if label and r.get("reason") == "absorbed_mid_turn":  # your follow-up, folded into the running turn
                        start_turn(label, ts)
            elif kind == "user":
                label, text = prompt_label(r), user_text(r)
                if label:
                    start_turn(label, ts)
                elif turns and ("[Request interrupted" in text or "<local-command-stdout>" in text):
                    turns[-1]["end"] = ts  # Esc, or a local command like /clear that has no model turn
                elif turns and text.lstrip().startswith("<task-notification>"):
                    turns[-1]["end"] = None  # a background agent reported back: the same question continues
            elif kind == "system" and r.get("subtype") == "turn_duration" and turns:
                turns[-1]["end"] = ts
            elif kind == "assistant" and turns:
                turns[-1]["end"] = ts if msg.get("stop_reason") == "end_turn" else None
            u, mid = msg.get("usage"), msg.get("id")
            if r.get("type") == "assistant" and u and mid not in seen:  # streamed blocks repeat the same usage
                seen.add(mid)
                tok["input"] += u.get("input_tokens", 0)
                tok["output"] += u.get("output_tokens", 0)
                tok["cache_read"] += u.get("cache_read_input_tokens", 0)
                tok["cache_write"] += u.get("cache_creation_input_tokens", 0)
                c = cost_of(msg.get("model"), u, prices)
                if c is None:
                    tok["unpriced"].add(msg.get("model"))
                else:
                    tok["usd"] += c
                    if turns:
                        turns[-1]["usd"] += c
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                t = c.get("type")
                if t == "tool_result":
                    asking.pop(c.get("tool_use_id"), None)
                if r.get("type") == "assistant" and t == "tool_use":
                    tools += 1
                    if turns:
                        turns[-1]["tools"] += 1
                        turns[-1]["last"] = tool_summary(c.get("name"), c.get("input"))
                    if c.get("name") in ASK_TOOLS:
                        asking[c.get("id")] = c.get("name")
                    events.append((ts, "tool", tool_summary(c.get("name"), c.get("input"))))
                elif r.get("type") == "assistant" and t == "text" and c.get("text", "").strip():
                    events.append((ts, "text", short(c["text"], 300)))
                elif t == "tool_result" and c.get("is_error"):
                    errors += 1
                    events.append((ts, "error", short(c.get("content"), 200)))
    tok["unpriced"] = sorted(m for m in tok["unpriced"] if m and m != "<synthetic>")
    return events, tools, errors, first, last, tok, bool(asking), turns, [q for q in queue if q], title.get("custom-title") or title.get("ai-title")


# shortcut: common built-in commands that say nothing about the task; extend if another shows up as a title
HOUSEKEEPING = {"/clear", "/compact", "/resume", "/model", "/login", "/config", "/plugin", "/status", "/cost", "/context"}


def first_prompt(path):
    """First real user text of a main session, used as its title."""
    with open(path, errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            label = prompt_label(r) if r.get("type") == "user" else None
            if label and label.split()[0] not in HOUSEKEEPING:
                return label
    return ""


def project_dir(cwd):
    """Claude Code names a project's transcript dir after its path, non-alphanumerics -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))


def project_name(d):
    return d.removeprefix(project_dir(os.path.expanduser("~"))).strip("-") or d


def iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


ACTIVE_S = 120  # main session written in the last 2 min counts as active
_titles = {}


def row(p, kind, meta, status, note=""):
    events, tools, errors, first, last, tok, _, turns, queued, _ = parse(p)
    a, b = iso(first), iso(last)
    mtime = os.path.getmtime(p)
    live = status in ("running", "active", "waiting")
    act = next((iso(e[0]).timestamp() for e in reversed(events) if e[0]), mtime)  # last tool call / text / error
    return {
        "id": os.path.basename(p).removeprefix("agent-").removesuffix(".jsonl"),
        "kind": kind,
        "project": project_name(rel(p)[0]),
        "session": rel(p)[1][:8] if kind == "sub" else os.path.basename(p)[:8],
        "desc": meta.get("description", os.path.basename(p)),
        "type": meta.get("agentType", "?"),
        "status": status, "note": note, "tools": tools, "errors": errors, "tok": tok,
        # live rows: elapsed counts to now so the client clock ticks from the real start
        "elapsed": int(((time.time() if live else b.timestamp()) - a.timestamp())) if a and b else 0,
        "idle": int(time.time() - mtime),
        "start": a.astimezone().strftime("%m-%d %H:%M") if a else "",
        "events": events[-25:], "mtime": mtime, "act": act,
        "t0": a.timestamp() if a else mtime, "t1": time.time() if live else (b.timestamp() if b else mtime),
        "last": next((e[2] for e in reversed(events) if e[1] in ("tool", "text")), ""),  # what it is doing now
        "path": p,  # server side only: graphs() and cards() pick the fields they send
        "turns": turns if kind == "main" else [], "queued": queued if kind == "main" else [],
    }


def scoped_dirs():
    """Transcript dirs of each watched repo and its worktrees (<repo>, <repo>/.claude/worktrees/*, <repo>.worktrees/*)."""
    out = set()
    for scope in scopes():
        prefix = project_dir(scope)
        out |= {d for d in glob.glob(os.path.join(ROOT, glob.escape(prefix) + "*"))
                if os.path.basename(d) == prefix or os.path.basename(d)[len(prefix)] == "-"}
    return sorted(out)


def rel(p):
    """[project dir, session id, ...] of a transcript path under ROOT."""
    return os.path.relpath(p, ROOT).split(os.sep)


def sub_glob(d, sid="*"):
    """Subagent transcripts of a session, including workflow agents (subagents/workflows/<run>/agent-*.jsonl)."""
    return glob.glob(os.path.join(d, sid, "subagents", "**", "agent-*.jsonl"), recursive=True)


def read_meta(p):
    try:
        with open(p[: -len(".jsonl")] + ".meta.json") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return {}
    journal = os.path.join(os.path.dirname(p), "journal.jsonl")
    if not meta.get("finished") and os.path.exists(journal):  # workflow agents: done once the run journal has their result
        aid = os.path.basename(p)[len("agent-"):-len(".jsonl")]
        try:
            with open(journal) as f:
                meta["finished"] = any(r.get("type") == "result" and r.get("agentId") == aid
                                       for r in map(json.loads, f))
        except (OSError, ValueError):
            pass
    return meta


def agents():
    """Rows for every session in scope that wrote within HIDE_S, plus all of those sessions' subagents."""
    now, out, last = time.time(), [], {}
    for d in scoped_dirs():
        for p in glob.glob(os.path.join(d, "*.jsonl")):
            last[os.path.basename(p)[:-6]] = (d, max(last.get(os.path.basename(p)[:-6], (d, 0))[1], os.path.getmtime(p)))
        for p in sub_glob(d):
            sid = rel(p)[1]
            last[sid] = (d, max(last.get(sid, (d, 0))[1], os.path.getmtime(p)))
    for sid, (d, t) in last.items():
        flag_t, flag_msg = wait_flag(sid)
        if now - max(t, flag_t) > HIDE_S:
            continue
        subs = []
        for p in sub_glob(d, sid):
            meta = read_meta(p)
            mtime = os.path.getmtime(p)
            # a prompt newer than this agent's last write: it is the one blocked on the user (or paused behind it)
            status = ("done" if meta.get("finished") else "waiting" if flag_t > mtime
                      else "stalled?" if now - mtime > STALE_S else "running")
            subs.append(row(p, "sub", meta, status, flag_msg if status == "waiting" else ""))
        main = os.path.join(d, sid + ".jsonl")
        if os.path.exists(main):
            if main not in _titles:
                _titles[main] = first_prompt(main) or sid
            mtime, note = os.path.getmtime(main), ""
            if parse(main)[6]:
                note = "asked you a question"
            elif flag_t > t and not any(a["status"] == "waiting" for a in subs):  # nothing in the session wrote since
                note = flag_msg
            status = "waiting" if note else "active" if now - mtime < ACTIVE_S else "idle"
            title = parse(main)[9] or _titles[main]  # the session title Claude Code shows, else your first message
            out.append(row(main, "main", {"description": title, "agentType": "main session"}, status, note))
        out += subs
    order = {"waiting": 0, "active": 1, "running": 2, "stalled?": 3, "idle": 4, "done": 5}
    return sorted(out, key=lambda x: (order[x["status"]], -x["mtime"]))


def fmt(s):
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def specs():
    """Every task pipeline file in $AGENT_MONITOR_HOME/pipelines/; a bad file is skipped, not fatal."""
    out = []
    for path in sorted(glob.glob(os.path.join(HOME, "pipelines", "*.json"))):
        try:
            with open(path) as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


def pipelines(rows, hist=None):
    out = []
    for spec in specs():
        try:
            out.append(pipeline(rows, spec, hist or {}))
        except (KeyError, TypeError, AttributeError):
            continue
    return out


def role(desc, typ, stage=None):
    """What an agent's run is compared with: its pipeline stage, else its subagent type, else its task's first word."""
    if stage:
        return f"{stage} stage"
    if typ and typ != "?":
        return typ
    return (desc.split() or ["?"])[0].lower()


def guess(hist, r):
    """Median run time (s) of finished agents with the same role; None under MIN_RUNS runs."""
    d = hist.get(r, [])
    return {"role": r, "est": int(statistics.median(d)) if len(d) >= MIN_RUNS else None, "runs": len(d)}


def _span(path):
    first = last = None
    with open(path, errors="replace") as f:
        for line in f:
            m = re.search(r'"timestamp":\s*"([^"]+)"', line)
            if m:
                first, last = first or m.group(1), m.group(1)
    return (iso(last) - iso(first)).total_seconds() if first else 0


_hist = {"t": 0, "v": {}}


def history():
    """role -> run times (s) of finished subagents in the watched repos (newest HIST_MAX transcripts, any age)."""
    if time.time() - _hist["t"] < HIST_TTL:
        return _hist["v"]
    match = {}  # session -> [(match, stage label)], as pipeline() matches them
    for spec in specs():
        for lane in spec.get("lanes", []) if isinstance(spec, dict) else []:
            for s in lane.get("stages", []):
                if s.get("match") and s.get("label"):
                    match.setdefault(str(spec.get("session", ""))[:8], []).append((s["match"].lower(), s["label"]))
    files = [p for d in scoped_dirs() for p in sub_glob(d)]
    out = {}
    for p in sorted(files, key=os.path.getmtime, reverse=True)[:HIST_MAX]:
        meta = read_meta(p)
        dur = meta.get("finished") and parse(p, _span)  # a finished transcript never changes: parsed once
        if dur and dur > 0:
            desc = str(meta.get("description", ""))
            stage = next((lbl for m, lbl in match.get(rel(p)[1][:8], []) if m in desc.lower()), None)
            out.setdefault(role(desc, meta.get("agentType"), stage), []).append(dur)
    _hist.update(t=time.time(), v=out)
    return out


def pipeline(rows, spec, hist={}):
    """Stage graph for one task; a stage's state comes from that session's agents whose description contains `match`."""
    sid = spec.get("session", "")[:8]
    rows = [a for a in rows if not sid or a["session"] == sid]
    lanes = []
    for lane in spec.get("lanes", []):
        stages = []
        for s in lane["stages"]:
            hits = [a for a in rows if a["kind"] == "sub" and s.get("match") and s["match"].lower() in a["desc"].lower()]
            node = {"label": s["label"], "id": hits[-1]["id"] if hits else "", "ids": [a["id"] for a in hits], "tools": sum(a["tools"] for a in hits), "elapsed": 0, "last": "",
                    "usd": round(sum(a["tok"]["usd"] for a in hits), 2)}
            if s.get("manual"):
                node.update(state=s["manual"], note=s.get("note", ""), manual=True)
            elif any(a["status"] != "done" for a in hits):
                live = [a for a in hits if a["status"] != "done"]
                node.update(state="waiting" if any(a["status"] == "waiting" for a in live)
                            else "stalled" if all(a["status"] == "stalled?" for a in live) else "running",
                            ask=next((a["note"] for a in live if a["note"]), ""),
                            elapsed=max(a["elapsed"] for a in live), act=max(a["act"] for a in live),
                            last=next((e[2] for a in live for e in reversed(a["events"]) if e[1] == "tool"), ""))
            elif hits:
                node.update(state="done", elapsed=sum(a["elapsed"] for a in hits))
            else:
                node.update(state="pending")
            if not s.get("manual"):
                node.update(guess(hist, role("", "", s["label"])))
            stages.append(node)
        lanes.append({"name": lane["name"], "stages": stages})
    return {"title": spec.get("title", "Pipeline"), "session": sid, "lanes": lanes}


def ktok(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def usage_line(t):
    unpriced = f" · no price for {', '.join(t['unpriced'])}" if t["unpriced"] else ""
    return (f'<span class="cost">${t["usd"]:.2f}</span> · in {ktok(t["input"])} · out {ktok(t["output"])} · '
            f'cache read {ktok(t["cache_read"])} · cache write {ktok(t["cache_write"])}{unpriced}')


def card(a):
    ev = "".join(
        f'<li class="{k}"><span>{iso(ts).astimezone().strftime("%H:%M:%S") if ts else ""}</span> {html.escape(t)}</li>'
        for ts, k, t in reversed(a["events"])
    )
    return f"""
<details class="card {a['status'].rstrip('?')}" data-id="{a['id']}" data-status="{a['status']}">
<summary><b class="st">{a['status']}</b> <b>{html.escape(a['desc'])}</b>{f' <span class="ask">⏸ {html.escape(a["note"])}</span>' if a["note"] else ""}
<small>{a['type']} · {a['project']} · s:{a['session']} · start {a['start']} · ran {fmt(a['elapsed'])} ·
{a['tools']} tools · {a['errors']} errors · last write {fmt(a['idle'])} ago</small>
<small>{usage_line(a['tok'])}</small></summary>
<ol>{ev or '<li>no activity yet</li>'}</ol></details>"""


def cards(rows):
    out = []
    for kind, title in (("main", "Claude sessions"), ("sub", "Subagents")):
        group = [a for a in rows if a["kind"] == kind]
        if group:
            usd = sum(a["tok"]["usd"] for a in group)
            out.append(f'<h3 class="group">{title} <small>{len(group)} · ${usd:.2f}</small></h3>' + "".join(map(card, group)))
    return "".join(out)


def cards_by_session(rows):
    """Cards per session, so the page lists only the selected tab's session and its agents."""
    return {sid: cards([a for a in rows if a["session"] == sid]) for sid in {a["session"] for a in rows}}


MAX_TURNS = 6


def turn_nodes(m, subs):
    """The session as a timeline of your questions; each subagent hangs under the question it started in."""
    now, out = time.time(), []
    for i, t in enumerate(m["turns"]):
        t0 = iso(t["t0"]).timestamp()
        end = iso(t["end"]).timestamp() if t["end"] else None
        last_turn = i == len(m["turns"]) - 1
        if not last_turn or end:
            state = "done"
        elif m["status"] == "waiting":
            state = "waiting"
        else:  # open turn: working, unless nothing wrote for STALE_S (an interrupt we could not see)
            state = "running" if m["idle"] < STALE_S else "done"
        out.append({"label": t["label"], "state": state, "t0": t0, "tools": t["tools"], "last": t["last"],
                    "usd": round(t["usd"], 2), "note": m["note"] if state == "waiting" else "",
                    "elapsed": int((end or (now if state != "done" else m["mtime"])) - t0), "agents": []})
    for a in subs:
        host = next((t for t in reversed(out) if t["t0"] <= a["t0"] + 1), out[0] if out else None)
        if host is not None:
            host["agents"].append(a)
    earlier = max(0, len(out) - MAX_TURNS)
    return out[earlier:] + [{"label": q, "state": "pending", "tools": 0, "usd": 0, "elapsed": 0, "last": "",
                             "note": "queued", "agents": []} for q in m["queued"]], earlier, out


LANE_ORDER = {"running": 0, "waiting": 1, "done": 2, "pending": 3}


def stage(a):
    """A board stage for one agent node."""
    return {"label": a["desc"], "state": a["status"].rstrip("?"), "ask": a["note"]} | {
        k: a.get(k) for k in ("id", "desc", "type", "last", "elapsed", "usd", "tools", "errors", "act", "role", "est", "runs")}


def lane_eta(stages, seq):
    """Seconds left from the medians: pipeline stages run one after another (sum), a question's agents side by
    side (max). None when a step still to do has no estimate."""
    rest = [s for s in stages if s["state"] != "done" and not s.get("manual")]
    if not rest or any(s.get("est") is None for s in rest):
        return None
    left = [max(0, s["est"] - (s.get("elapsed") or 0)) for s in rest]
    return sum(left) if seq else max(left)


def lanes(nodes, turns, pipe=None):
    """Board rows for one session: the pipeline's lanes, else one per question that started agents; agents that
    nothing claims go in 'Other'. Ordered running, waiting, most recently finished, then not started."""
    by_id, rows, claimed = {a["id"]: a for a in nodes}, [], set()
    if pipe:
        for lane in pipe["lanes"]:
            ids = {i for s in lane["stages"] for i in s["ids"] if i in by_id}
            stages = [s | {k: by_id[s["id"]][k] for k in ("desc", "type", "errors")} if s["id"] in by_id else s
                      for s in lane["stages"]]
            rows.append((lane["name"], stages, [by_id[i] for i in ids], "", True))
            claimed |= ids
    else:
        for t in turns:
            if t["agents"]:
                rows.append((t["label"], [stage(a) for a in t["agents"]], t["agents"], t["state"], False))
                claimed |= {a["id"] for a in t["agents"]}
    rest = [a for a in nodes if a["id"] not in claimed]
    if rest:
        rows.append(("Other", [stage(a) for a in rest], rest, "", False))
    out = []
    for name, stages, ags, turn_state, seq in rows:
        sts = {s["state"] for s in stages} | {turn_state}  # a question still being worked on keeps its lane live
        state = ("waiting" if "waiting" in sts else "running" if sts & {"running", "stalled"}
                 else "pending" if sts <= {"pending", ""} else "done")
        end = max((a["t1"] for a in ags), default=0)
        out.append({"name": name, "state": state, "stages": stages, "usd": round(sum(a["usd"] for a in ags), 2),
                    "elapsed": int(end - min(a["t0"] for a in ags)) if ags else 0, "end": end,
                    "done": sum(s["state"] == "done" for s in stages), "total": len(stages), "eta": lane_eta(stages, seq)})
    return sorted(out, key=lambda l: (LANE_ORDER[l["state"]], -l["end"] if l["state"] == "done" else 0))


def graphs(rows, pipes=(), hist=None):
    """One orchestration graph per session that spawned subagents (session = parent dir of subagents/)."""
    mains = {a["session"]: a for a in rows if a["kind"] == "main"}
    by = {sid: [] for sid in mains}
    for a in rows:
        if a["kind"] == "sub":
            by.setdefault(a["session"], []).append(a)
    out = []
    for sid, subs in by.items():
        m, pipe = mains.get(sid), next((p for p in pipes if p["session"] == sid), None)
        stage_of = {}  # agent id -> its pipeline stage's label (the first stage that matches it)
        for s in [s for l in (pipe or {}).get("lanes", []) for s in l["stages"]]:
            for i in s["ids"]:
                stage_of.setdefault(i, s["label"])
        nodes = [{k: a[k] for k in ("id", "desc", "type", "status", "note", "tools", "errors", "elapsed", "t0", "t1", "last", "act")}
                 | {"usd": round(a["tok"]["usd"], 2)} | guess(hist or {}, role(a["desc"], a["type"], stage_of.get(a["id"])))
                 for a in sorted(subs, key=lambda a: a["t0"])]
        turns, earlier, every = turn_nodes(m, nodes) if m and m["turns"] else ([], 0, [])
        out.append({
            "session": sid, "project": (m or subs[0])["project"], "turns": turns, "earlier": earlier,
            "title": m["desc"] if m else f"session {sid}",
            "live": bool(m and m["status"] in ("active", "waiting")) or any(a["status"] in ("running", "waiting") for a in subs),
            "waiting": bool(m and m["status"] == "waiting") or any(a["status"] == "waiting" for a in subs),
            "note": m["note"] if m else "",
            "mtime": max([a["mtime"] for a in subs + ([m] if m else [])]),
            "usd": round(sum(a["tok"]["usd"] for a in subs), 2),
            "nodes": nodes,
            "lanes": lanes(nodes, every, pipe),
        })
    return sorted(out, key=lambda g: (not g["live"], -g["mtime"]))


def state():
    rows, hist = agents(), history()
    pipes = pipelines(rows, hist)
    finished = {p["session"] for p in pipes if all(n["state"] == "done" for l in p["lanes"] for n in l["stages"])}
    # a session without agents shows only while it is being written (a live chat), not while idle
    shown = {a["session"] for a in rows if a["kind"] == "sub" or a["status"] in ("active", "waiting")}
    rows = [a for a in rows if a["session"] in shown and a["session"] not in finished]
    live = {a["session"] for a in rows}
    pipes = [p for p in pipes if p["session"] in live]
    counts = {s: sum(a["status"] == s for a in rows) for s in ("waiting", "active", "running", "stalled?", "idle", "done")}
    counts["usd"] = round(sum(a["tok"]["usd"] for a in rows), 2)
    return {"pipelines": pipes, "graphs": graphs(rows, pipes, hist), "cards": cards_by_session(rows), "counts": counts,
            "hide_after_min": HIDE_S / 60, "scopes": [os.path.basename(d) for d in scopes()]}


def agent_detail(sid, aid):
    """Detail of one shown session ('main') or subagent; None unless it is a transcript the dashboard lists."""
    a = next((a for a in agents() if a["session"] == sid and (a["id"] == aid or aid == "main" and a["kind"] == "main")), None)
    if not a:
        return None
    keep = ("id", "kind", "desc", "type", "status", "note", "tools", "errors", "tok", "elapsed", "start", "t0", "session")
    return {k: a[k] for k in keep} | parse(a["path"], _detail)


def latest_session(cwd):
    """Newest main-session id for the project at cwd (the one a running Claude Code session is writing)."""
    files = glob.glob(os.path.join(ROOT, project_dir(cwd), "*.jsonl"))
    return os.path.basename(max(files, key=os.path.getmtime))[:-6] if files else None


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/agent":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            d = agent_detail(q.get("s", ""), q.get("id", ""))
            if d is None:
                return self.send_error(404)
            body, ctype = json.dumps(d).encode(), "application/json"
        elif self.path.startswith("/api"):
            body, ctype = json.dumps(state()).encode(), "application/json"
        else:
            with open(os.path.join(HERE, "index.html"), "rb") as f:  # read per request: page edits need no restart
                body, ctype = f.read(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", nargs="?", default="serve", choices=["serve", "session", "scope", "notify"])
    ap.add_argument("op", nargs="?", choices=["add", "remove", "list"], default="list")
    ap.add_argument("dir", nargs="?", default=os.getcwd())
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args()
    if args.cmd == "scope":
        print("\n".join(scopes() if args.op == "list" else edit_scopes(args.op, args.dir)) or "(none)")
        sys.exit(0)
    if args.cmd == "notify":  # never fail the hook: a bad payload just isn't recorded
        try:
            notify(json.load(sys.stdin))
        except (OSError, ValueError, AttributeError):
            pass
        sys.exit(0)
    if args.cmd == "session":
        sid = latest_session(args.cwd)
        print(sid or "")
        sys.exit(0 if sid else 1)
    print(f"Agent monitor on http://127.0.0.1:{args.port}  (scopes: {SCOPES_FILE}, pipelines: {os.path.join(HOME, 'pipelines')})")
    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()
