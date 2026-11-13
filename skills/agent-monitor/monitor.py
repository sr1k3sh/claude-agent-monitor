#!/usr/bin/env python3
"""Local dashboard of live Claude Code sessions and their subagents: http://127.0.0.1:8765

Only sessions started in a watched repo (or one of its worktrees) are read:
~/.claude/projects/<that dir>*/<session>.jsonl and <session>/subagents/agent-*.jsonl.
Watched repos are listed in $AGENT_MONITOR_HOME/scopes.json (`monitor.py scope add DIR`).
A session is shown while it is active (written in the last 2 min) or, once it has run subagents, while it or one of its agents wrote in the last HIDE_AFTER_MIN minutes
(default 30) and, when it has a task pipeline ($AGENT_MONITOR_HOME/pipelines/*.json), while that
pipeline has a stage not done. Stdlib only, read-only, binds to localhost. The page polls /api
every 2s and updates in place (keeps animations).

  monitor.py [serve] [--port 8765]          run the dashboard
  monitor.py scope add|remove|list [DIR]    watch / unwatch a repo (default DIR: the current directory)
  monitor.py session [--cwd DIR]            print the newest main session id for a project dir
  monitor.py notify                         Notification hook: mark the stdin session as waiting on the user
"""
import argparse, glob, html, json, os, re, subprocess, sys, time
from datetime import datetime
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


def tool_summary(name, inp):
    for k in ("description", "command", "file_path", "pattern", "query", "url", "prompt"):
        if isinstance(inp, dict) and inp.get(k):
            return f"{name}: {short(inp[k])}"
    return f"{name}: {short(inp)}"


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


_cache = {}  # path -> ((mtime, size), parsed) so 2s polling doesn't re-read unchanged multi-MB transcripts


def parse(path):
    key = (os.path.getmtime(path), os.path.getsize(path))
    if path in _cache and _cache[path][0] == key:
        return _cache[path][1]
    _cache[path] = (key, _parse(path))
    return _cache[path][1]


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
    return {
        "id": os.path.basename(p).removeprefix("agent-").removesuffix(".jsonl"),
        "kind": kind,
        "project": project_name(p.split("/")[-4 if kind == "sub" else -2]),
        "session": p.split("/")[-3][:8] if kind == "sub" else os.path.basename(p)[:8],
        "desc": meta.get("description", os.path.basename(p)),
        "type": meta.get("agentType", "?"),
        "status": status, "note": note, "tools": tools, "errors": errors, "tok": tok,
        # live rows: elapsed counts to now so the client clock ticks from the real start
        "elapsed": int(((time.time() if live else b.timestamp()) - a.timestamp())) if a and b else 0,
        "idle": int(time.time() - mtime),
        "start": a.astimezone().strftime("%m-%d %H:%M") if a else "",
        "events": events[-25:], "mtime": mtime,
        "t0": a.timestamp() if a else mtime, "t1": time.time() if live else (b.timestamp() if b else mtime),
        "last": next((e[2] for e in reversed(events) if e[1] == "tool"), ""),
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


def agents():
    """Rows for every session in scope that wrote within HIDE_S, plus all of those sessions' subagents."""
    now, out, last = time.time(), [], {}
    for d in scoped_dirs():
        for p in glob.glob(os.path.join(d, "*.jsonl")):
            last[os.path.basename(p)[:-6]] = (d, max(last.get(os.path.basename(p)[:-6], (d, 0))[1], os.path.getmtime(p)))
        for p in glob.glob(os.path.join(d, "*", "subagents", "agent-*.jsonl")):
            sid = p.split("/")[-3]
            last[sid] = (d, max(last.get(sid, (d, 0))[1], os.path.getmtime(p)))
    for sid, (d, t) in last.items():
        flag_t, flag_msg = wait_flag(sid)
        if now - max(t, flag_t) > HIDE_S:
            continue
        subs = []
        for p in glob.glob(os.path.join(d, sid, "subagents", "agent-*.jsonl")):
            meta = {}
            try:
                with open(p[: -len(".jsonl")] + ".meta.json") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                pass
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


def pipelines(rows):
    """Every task pipeline in $AGENT_MONITOR_HOME/pipelines/; a bad file is skipped, not fatal."""
    out = []
    for path in sorted(glob.glob(os.path.join(HOME, "pipelines", "*.json"))):
        try:
            with open(path) as f:
                out.append(pipeline(rows, json.load(f)))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def pipeline(rows, spec):
    """Stage graph for one task; a stage's state comes from that session's agents whose description contains `match`."""
    sid = spec.get("session", "")[:8]
    rows = [a for a in rows if not sid or a["session"] == sid]
    lanes = []
    for lane in spec.get("lanes", []):
        stages = []
        for s in lane["stages"]:
            hits = [a for a in rows if a["kind"] == "sub" and s.get("match") and s["match"].lower() in a["desc"].lower()]
            node = {"label": s["label"], "tools": sum(a["tools"] for a in hits), "elapsed": 0, "last": "",
                    "usd": round(sum(a["tok"]["usd"] for a in hits), 2)}
            if s.get("manual"):
                node.update(state=s["manual"], note=s.get("note", ""))
            elif any(a["status"] != "done" for a in hits):
                live = [a for a in hits if a["status"] != "done"]
                node.update(state="waiting" if any(a["status"] == "waiting" for a in live)
                            else "stalled" if all(a["status"] == "stalled?" for a in live) else "running",
                            ask=next((a["note"] for a in live if a["note"]), ""),
                            elapsed=max(a["elapsed"] for a in live),
                            last=next((e[2] for a in live for e in reversed(a["events"]) if e[1] == "tool"), ""))
            elif hits:
                node.update(state="done", elapsed=sum(a["elapsed"] for a in hits))
            else:
                node.update(state="pending")
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
    out = out[earlier:] + [{"label": q, "state": "pending", "tools": 0, "usd": 0, "elapsed": 0, "last": "",
                            "note": "queued", "agents": []} for q in m["queued"]]
    return out, earlier


def graphs(rows):
    """One orchestration graph per session that spawned subagents (session = parent dir of subagents/)."""
    mains = {a["session"]: a for a in rows if a["kind"] == "main"}
    by = {sid: [] for sid in mains}
    for a in rows:
        if a["kind"] == "sub":
            by.setdefault(a["session"], []).append(a)
    out = []
    for sid, subs in by.items():
        m = mains.get(sid)
        nodes = [{k: a[k] for k in ("id", "desc", "type", "status", "note", "tools", "elapsed", "t0", "t1", "last")}
                 | {"usd": round(a["tok"]["usd"], 2)} for a in sorted(subs, key=lambda a: a["t0"])]
        turns, earlier = turn_nodes(m, nodes) if m and m["turns"] else ([], 0)
        out.append({
            "session": sid, "project": (m or subs[0])["project"], "turns": turns, "earlier": earlier,
            "title": m["desc"] if m else f"session {sid}",
            "live": bool(m and m["status"] in ("active", "waiting")) or any(a["status"] in ("running", "waiting") for a in subs),
            "waiting": bool(m and m["status"] == "waiting") or any(a["status"] == "waiting" for a in subs),
            "note": m["note"] if m else "",
            "mtime": max([a["mtime"] for a in subs + ([m] if m else [])]),
            "usd": round(sum(a["tok"]["usd"] for a in subs), 2),
            "nodes": nodes,
        })
    return sorted(out, key=lambda g: (not g["live"], -g["mtime"]))


def state():
    rows = agents()
    pipes = pipelines(rows)
    finished = {p["session"] for p in pipes if all(n["state"] == "done" for l in p["lanes"] for n in l["stages"])}
    # a session without agents shows only while it is being written (a live chat), not while idle
    shown = {a["session"] for a in rows if a["kind"] == "sub" or a["status"] in ("active", "waiting")}
    rows = [a for a in rows if a["session"] in shown and a["session"] not in finished]
    live = {a["session"] for a in rows}
    pipes = [p for p in pipes if p["session"] in live]
    counts = {s: sum(a["status"] == s for a in rows) for s in ("waiting", "active", "running", "stalled?", "idle", "done")}
    counts["usd"] = round(sum(a["tok"]["usd"] for a in rows), 2)
    return {"pipelines": pipes, "graphs": graphs(rows), "cards": cards_by_session(rows), "counts": counts,
            "hide_after_min": HIDE_S / 60, "scopes": [os.path.basename(d) for d in scopes()]}


def latest_session(cwd):
    """Newest main-session id for the project at cwd (the one a running Claude Code session is writing)."""
    files = glob.glob(os.path.join(ROOT, project_dir(cwd), "*.jsonl"))
    return os.path.basename(max(files, key=os.path.getmtime))[:-6] if files else None


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api"):
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
