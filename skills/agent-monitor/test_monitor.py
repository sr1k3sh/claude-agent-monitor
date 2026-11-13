"""Cost maths, usage de-duplication, and per-session graphs/pipelines over fake transcripts."""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


def load(root, home):
    os.environ.update(CLAUDE_PROJECTS_DIR=root, AGENT_MONITOR_HOME=home, AGENT_MONITOR_SCOPE="/proj")
    spec = importlib.util.spec_from_file_location(
        "agent_monitor_under_test", Path(__file__).with_name("monitor.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def assistant(mid, ts, usage, tool=None):
    content = [{"type": "tool_use", "name": "Bash", "input": {"description": tool}}] if tool else []
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": mid, "model": "claude-opus-5-5", "usage": usage, "content": content}}


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


MTOK = {"input_tokens": 1_000_000, "output_tokens": 0}


class AgentMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root, self.home = base / "projects", base / "home"
        sess = self.root / "-proj" / "abcdef12-0000-0000-0000-000000000000"
        write_jsonl(self.root / "-proj" / "abcdef12-0000-0000-0000-000000000000.jsonl", [
            {"type": "user", "timestamp": "2026-11-13T00:00:00Z", "message": {"content": "add offline sync"}},
        ])
        # the same message id twice = one streamed response split over two records
        write_jsonl(sess / "subagents" / "agent-a1.jsonl", [
            assistant("m1", "2026-11-13T00:00:01Z", MTOK, "plan it"),
            assistant("m1", "2026-11-13T00:00:02Z", MTOK),
        ])
        (sess / "subagents" / "agent-a1.meta.json").write_text(
            json.dumps({"description": "Plan the swap", "agentType": "Plan", "finished": True}))
        write_jsonl(sess / "subagents" / "agent-a2.jsonl", [
            assistant("m2", "2026-11-13T00:05:00Z", MTOK, "write code"),
        ])
        (sess / "subagents" / "agent-a2.meta.json").write_text(
            json.dumps({"description": "Execute Android", "agentType": "general-purpose"}))
        (self.home / "pipelines").mkdir(parents=True)
        (self.home / "pipelines" / "task.json").write_text(json.dumps({
            "session": "abcdef12", "title": "Task",
            "lanes": [{"name": "L", "stages": [
                {"label": "Plan", "match": "Plan the"}, {"label": "Execute", "match": "Execute Android"},
                {"label": "QA", "match": "QA Android"}, {"label": "OK", "manual": "waiting"}]}]}))
        (self.home / "pipelines" / "broken.json").write_text("{not json")
        # another repo's session, and a worktree of the scope repo
        write_jsonl(self.root / "-other" / "99999999-0000-0000-0000-000000000000.jsonl", [
            {"type": "user", "timestamp": "2026-11-13T00:00:00Z", "message": {"content": "unrelated"}}])
        write_jsonl(self.root / "-proj-worktrees-x" / "77777777-0000-0000-0000-000000000000.jsonl", [
            {"type": "user", "timestamp": "2026-11-13T00:00:00Z", "message": {"content": "worktree work"}}])
        self.m = load(str(self.root), str(self.home))

    def tearDown(self):
        self.tmp.cleanup()

    def test_cost_uses_cache_multipliers_and_prefix_match(self):
        prices = self.m.load_prices()
        u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
             "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000}}
        self.assertAlmostEqual(self.m.cost_of("claude-opus-5-5", u, prices), 4 + 20 + 0.2 + 5 + 8)
        self.assertEqual(self.m.cost_of("claude-haiku-4-5-20251001", {"output_tokens": 1_000_000}, prices), 5.0)
        self.assertEqual(self.m.cost_of("claude-opus-5-5", {**MTOK, "speed": "fast"}, prices), 8.0)
        self.assertIsNone(self.m.cost_of("other-model", u, prices))

    def test_repeated_message_id_is_counted_once(self):
        tok = self.m.parse(str(self.root / "-proj" / "abcdef12-0000-0000-0000-000000000000" / "subagents" / "agent-a1.jsonl"))[5]
        self.assertEqual(tok["input"], 1_000_000)
        self.assertAlmostEqual(tok["usd"], 4.0)

    def test_graph_groups_subagents_under_their_session(self):
        s = self.m.state()
        g = next(g for g in s["graphs"] if g["session"] == "abcdef12")
        self.assertEqual(g["title"], "add offline sync")
        self.assertEqual([n["desc"] for n in g["nodes"]], ["Plan the swap", "Execute Android"])
        self.assertTrue(g["live"])  # a2 has no finished flag and was just written

    def test_pipeline_states_and_bad_file_skipped(self):
        [p] = self.m.state()["pipelines"]
        self.assertEqual([n["state"] for n in p["lanes"][0]["stages"]], ["done", "running", "pending", "waiting"])
        self.assertAlmostEqual(p["lanes"][0]["stages"][0]["usd"], 4.0)

    def test_only_scope_repo_and_its_worktrees(self):
        titles = {a["desc"] for a in self.m.agents() if a["kind"] == "main"}
        self.assertEqual(titles, {"add offline sync", "worktree work"})

    def test_session_without_agents_shows_only_while_active(self):
        s = self.m.state()
        self.assertIn("worktree work", s["cards"])  # just written: a live chat
        self.assertIn("77777777", [g["session"] for g in s["graphs"]])
        old = time.time() - 5 * 60
        f = self.root / "-proj-worktrees-x" / "77777777-0000-0000-0000-000000000000.jsonl"
        os.utime(f, (old, old))
        self.assertNotIn("worktree work", self.m.state()["cards"])

    def test_idle_session_is_hidden(self):
        old = time.time() - 31 * 60
        for p in self.root.rglob("*abcdef12*"):
            for f in [p, *p.rglob("*")] if p.is_dir() else [p]:
                os.utime(f, (old, old))
        self.assertNotIn("abcdef12", [g["session"] for g in self.m.state()["graphs"]])
        self.assertEqual(self.m.state()["pipelines"], [])

    def test_finished_pipeline_hides_its_session(self):
        spec = json.loads((self.home / "pipelines" / "task.json").read_text())
        spec["lanes"][0]["stages"] = [{"label": "Plan", "match": "Plan the"}]
        (self.home / "pipelines" / "task.json").write_text(json.dumps(spec))
        s = self.m.state()
        self.assertNotIn("abcdef12", [g["session"] for g in s["graphs"]])
        self.assertEqual(s["pipelines"], [])
        self.assertNotIn("add offline sync", s["cards"])

    def test_scope_file_add_remove(self):
        del os.environ["AGENT_MONITOR_SCOPE"]
        try:
            self.assertEqual(self.m.scopes(), [])
            self.assertEqual(self.m.edit_scopes("add", "/proj"), ["/proj"])
            self.assertEqual(self.m.edit_scopes("add", "/other"), ["/other", "/proj"])
            self.assertIn("worktree work", self.m.state()["cards"])
            self.assertEqual(self.m.edit_scopes("remove", "/proj"), ["/other"])
            self.assertNotIn("worktree work", self.m.state()["cards"])
        finally:
            os.environ["AGENT_MONITOR_SCOPE"] = "/proj"

    def test_permission_prompt_pauses_blocked_agent_until_it_writes(self):
        sid = "abcdef12-0000-0000-0000-000000000000"
        a2 = self.root / "-proj" / sid / "subagents" / "agent-a2.jsonl"
        old = time.time() - 30
        os.utime(a2, (old, old))
        self.m.notify({"session_id": sid, "message": "Claude needs your permission to use Bash"})
        self.m.notify({"session_id": "../evil", "message": "x"})  # not a file name: ignored
        self.assertEqual([p.name for p in (self.home / "waiting").iterdir()], [sid + ".json"])
        s = self.m.state()
        g = next(g for g in s["graphs"] if g["session"] == "abcdef12")
        self.assertTrue(g["waiting"])
        self.assertEqual({n["desc"]: n["status"] for n in g["nodes"]}["Execute Android"], "waiting")
        self.assertEqual(s["counts"]["waiting"], 1)
        self.assertEqual(s["pipelines"][0]["lanes"][0]["stages"][1]["state"], "waiting")
        later = time.time() + 5  # the agent writes again once you answer
        os.utime(a2, (later, later))
        self.assertFalse(next(g for g in self.m.state()["graphs"] if g["session"] == "abcdef12")["waiting"])

    def test_unanswered_ask_question_marks_main_session_waiting(self):
        f = self.root / "-proj-worktrees-x" / "77777777-0000-0000-0000-000000000000.jsonl"
        ask = {"type": "assistant", "timestamp": "2026-11-13T00:00:01Z", "message": {"id": "q", "content": [
            {"type": "tool_use", "id": "t1", "name": "AskUserQuestion", "input": {}}]}}
        write_jsonl(f, [ask])
        old = time.time() - 5 * 60  # idle past ACTIVE_S, still shown because it is waiting on you
        os.utime(f, (old, old))
        g = next(g for g in self.m.state()["graphs"] if g["session"] == "77777777")
        self.assertTrue(g["waiting"])
        write_jsonl(f, [ask, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}}])
        os.utime(f, (old, old))
        self.assertNotIn("77777777", [g["session"] for g in self.m.state()["graphs"]])

    def test_each_question_is_a_node_with_its_state(self):
        f = self.root / "-proj-worktrees-x" / "77777777-0000-0000-0000-000000000000.jsonl"
        user = lambda ts, c, **k: {"type": "user", "timestamp": ts, "message": {"content": c}, **k}
        bot = lambda ts, stop, tool=None: {"type": "assistant", "timestamp": ts, "message": {
            "id": ts, "model": "claude-opus-5-5", "usage": MTOK, "stop_reason": stop,
            "content": [{"type": "tool_use", "id": ts, "name": "Bash", "input": {"description": tool}}] if tool else []}}
        write_jsonl(f, [
            user("2026-11-13T00:00:00Z", "first question"),
            bot("2026-11-13T00:00:05Z", "end_turn"),
            user("2026-11-13T00:01:00Z", "<command-message>x</command-message><command-name>/review</command-name><command-args>42</command-args>"),
            bot("2026-11-13T00:01:05Z", "end_turn"),
            user("2026-11-13T00:01:30Z", "<task-notification>agent done</task-notification>"),  # same question continues
            bot("2026-11-13T00:01:40Z", "tool_use", "follow up"),
            user("2026-11-13T00:02:00Z", "stopped one"),
            user("2026-11-13T00:02:10Z", [{"type": "text", "text": "[Request interrupted by user]"}]),
            user("2026-11-13T00:03:00Z", "current one"),
            bot("2026-11-13T00:03:05Z", "tool_use", "working"),
            {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-11-13T00:03:10Z", "content": "next one"},
            {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-11-13T00:03:11Z", "content": "<task-notification>x"},
            {"type": "queue-operation", "operation": "remove", "timestamp": "2026-11-13T00:03:12Z", "content": "<task-notification>x"},
        ])
        g = next(g for g in self.m.state()["graphs"] if g["session"] == "77777777")
        self.assertEqual([(t["label"], t["state"]) for t in g["turns"]], [
            ("first question", "done"), ("/review 42", "done"), ("stopped one", "done"),
            ("current one", "running"), ("next one", "pending")])
        self.assertEqual(g["turns"][1]["tools"], 1)  # the follow-up after the notification counted on /review
        self.assertAlmostEqual(g["turns"][0]["usd"], 4.0)
        self.assertEqual(g["turns"][3]["last"], "Bash: working")

    def test_subagents_hang_under_the_question_they_started_in(self):
        g = next(g for g in self.m.state()["graphs"] if g["session"] == "abcdef12")
        [t] = g["turns"]
        self.assertEqual([a["desc"] for a in t["agents"]], ["Plan the swap", "Execute Android"])
        self.assertEqual(t["state"], "running")

    def test_latest_session_for_project_dir(self):
        self.assertEqual(self.m.project_dir("/a/b.c"), "-a-b-c")
        self.assertEqual(self.m.latest_session("/proj"), "abcdef12-0000-0000-0000-000000000000")
        self.assertIsNone(self.m.latest_session("/nowhere"))


if __name__ == "__main__":
    unittest.main()
