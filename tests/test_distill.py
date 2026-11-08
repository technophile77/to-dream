import json
import os
import random
import re
import tempfile
import time
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import new_seed

from to_dream import distill

# Fake secrets are assembled at runtime so no key-shaped literal exists in this file.
GH_TOKEN = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"
PASSWORD = "hunter2" + "hunter2"
SECRETS = (GH_TOKEN, PASSWORD)
BASE = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
ENTRY = re.compile(r"^- \[(\d\d:\d\d|--:--)\] L(\d+) (PROMPT|MEMORY-WRITE|ERROR|\*\*DENIED\*\*|\*\*CORRECTION\*\*|\*\*BLOCKED\*\*|\*\*APPROVED-AFTER-BLOCK\*\*|\*\*INTERRUPTED\*\*) (.*)$")
COUNTS = re.compile(r"^- counts: human_prompts=(\d+) corrections=(\d+) tool_errors=(\d+) denials=(\d+) memory_writes=(\d+) blocked=(\d+) approved_after_block=(\d+) interrupted=(\d+) rejected_at_prompt=(\d+)$", re.M)
DROPPED = re.compile(r"^- dropped for size: (.*)$", re.M)


def stamp(minutes: float) -> str:
    return (BASE + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(content, minutes=None, **extra):
    rec = {"type": "user", "message": {"role": "user", "content": content}, "cwd": "/work/proj",
           "sessionId": "s", "gitBranch": "main", **extra}
    if minutes is not None:
        rec["timestamp"] = stamp(minutes)
    return rec


def assistant(content, minutes=None):
    rec = {"type": "assistant", "message": {"role": "assistant", "content": content}, "cwd": "/work/proj"}
    if minutes is not None:
        rec["timestamp"] = stamp(minutes)
    return rec


def tool_use(tool_id, name, tool_input, minutes=None):
    return assistant([{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}], minutes)


def tool_result(tool_id, text, is_error=True, minutes=None, as_blocks=False, **extra):
    content = [{"type": "text", "text": text}] if as_blocks else text
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}
    return user([block], minutes, **extra)


def compact(rec) -> str:
    return json.dumps(rec, separators=(",", ":"))


def write_jsonl(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for rec in records:
            handle.write((rec if isinstance(rec, str) else json.dumps(rec)) + "\n")
    return path


def entries_of(text: str) -> list[tuple[str, int, str, str]]:
    return [(m[1], int(m[2]), m[3], m[4]) for m in map(ENTRY.match, text.splitlines()) if m]


def tags_of(text: str) -> list[str]:
    return [e[2] for e in entries_of(text)]


class DistillCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "projects"
        self.project = self.root / "slug"
        self.out = Path(tmp.name) / "out"
        self.project.mkdir(parents=True)

    def run_one(self, records, name="aaaaaaaa-1111.jsonl", **kwargs):
        path = write_jsonl(self.project / name, records)
        args = {"since": None, "until": None, "memory_root": self.root}
        args.update(kwargs)
        digest = distill.distill_transcript(path, self.out, **args)
        return digest, (digest.digest_path.read_text(encoding="utf-8") if digest else "")


class PureHelperTests(unittest.TestCase):
    def test_correction_phrases(self):
        for text in ["No, use the other file", "please don't do that", "Don’t", "stop", "that's wrong", "Not what I wanted",
                     "I said tabs", "I asked for X", "do it instead", "actually, hmm", "why did you rename it",
                     "revert that", "undo", "that's not right", "you forgot the tests", "again?"]:
            with self.subTest(text=text):
                self.assertTrue(distill.is_correction(text))

    def test_non_corrections(self):
        for text in ["please add a test", "nothing to see", "the notation is fine", "again, thanks", "declined options"]:
            with self.subTest(text=text):
                self.assertFalse(distill.is_correction(text))

    def test_strip_harness(self):
        raw = ("<system-reminder>secret stuff</system-reminder>do it<task-notification>x</task-notification>"
               "<ide_opened_file>f</ide_opened_file>\n[SYSTEM NOTIFICATION: hi\nmore]\n\nthen this")
        self.assertEqual(distill.strip_harness(raw), "do it\nthen this")
        self.assertEqual(distill.strip_harness("<command-name>/x</command-name><command-args>go on</command-args>"), "go on")

    def test_clip(self):
        self.assertEqual(distill.clip("a\n b", 10), "a b")
        self.assertEqual(distill.clip("x" * 12, 5), "xxxxx…[+7 chars]")
        self.assertEqual(distill.clip("x" * 5, 5, total=100), "xxxxx…[+95 chars]")

    def test_tool_summary(self):
        self.assertEqual(distill.tool_summary("Bash", {"command": "ls " + "x" * 300}), "ls " + "x" * 197 + "…[+103 chars]")
        self.assertEqual(distill.tool_summary("Read", {"file_path": "/a/b"}), "/a/b")
        self.assertEqual(distill.tool_summary("Grep", {"pattern": "foo", "path": "src", "a": 1, "b": 2}), "pattern=foo, path=src, a=1")
        self.assertEqual(distill.tool_summary("X", None), "")

    def test_parse_ts(self):
        self.assertEqual(distill.parse_ts("2026-11-01T09:00:00.000Z"), BASE)
        self.assertEqual(distill.parse_ts("2026-11-01T09:00:00"), BASE)
        self.assertIsNone(distill.parse_ts("garbage"))
        self.assertIsNone(distill.parse_ts(None))

    def test_trim_entries_drops_low_priority_first(self):
        entries = [distill.Entry(1, None, "PROMPT", "p" * 30), distill.Entry(2, None, "**CORRECTION**", "c" * 30),
                   distill.Entry(3, None, "ERROR", "e" * 30), distill.Entry(4, None, "PROMPT", "q" * 30)]
        one_line = len(entries[1].render()) + 1
        kept, dropped = distill.trim_entries(entries, one_line * 2 + 5)
        self.assertEqual([e.tag for e in kept], ["**CORRECTION**", "ERROR"])
        self.assertEqual(dropped, {"PROMPT": 2})


class DigestContentTests(DistillCase):
    def test_prompts_string_and_list_content_and_reminders(self):
        digest, text = self.run_one([
            {"type": "ai-title", "aiTitle": "Fix the parser"},
            user("first prompt as string", 0),
            assistant([{"type": "thinking", "thinking": "SECRET-THINKING"}, {"type": "text", "text": "ok"}], 1),
            user([{"type": "text", "text": "<system-reminder>REMINDER-BODY</system-reminder>"},
                  {"type": "text", "text": "second prompt as blocks"}], 2),
            user("<system-reminder>only a reminder</system-reminder>", 3),
            user("[SYSTEM NOTIFICATION: background task done]", 4),
            user("meta stuff", 5, isMeta=True),
            user("from a peer", 6, origin={"kind": "peer"}),
            user("typed by a human", 7, origin={"kind": "human"}),
        ])
        self.assertEqual(digest.human_prompts, 3)
        self.assertEqual(digest.title, "Fix the parser")
        self.assertEqual(digest.cwd, "/work/proj")
        self.assertEqual(digest.first_ts, stamp(0))
        self.assertEqual(digest.last_ts, stamp(7))
        self.assertEqual(digest.digest_path.name, "20261101-0900_aaaaaaaa.md")
        self.assertEqual([e[3] for e in entries_of(text)],
                         ["first prompt as string", "second prompt as blocks", "typed by a human"])
        self.assertNotIn("REMINDER-BODY", text)
        self.assertNotIn("SECRET-THINKING", text)
        self.assertIn("title: Fix the parser", text)
        self.assertEqual(digest.chars, len(text))

    def test_human_origin_wins_over_sdk_prompt_source(self):
        ide = "<ide_opened_file>The user opened the file /x/y.py in the IDE.</ide_opened_file>"
        sel = "<ide_selection>The user selected lines 1 to 3 from /x/y.py</ide_selection>"
        digest, text = self.run_one([
            user("plain extension prompt", 0, origin={"kind": "human"}, promptSource="sdk", isMeta=False),
            user([{"type": "text", "text": ide + sel}, {"type": "text", "text": "No, fix the real bug"}], 1,
                 origin={"kind": "human"}, promptSource="sdk"),
            user(ide + "\nstring form with ide prefix", 2, origin={"kind": "human"}),
            user(ide + sel, 3, origin={"kind": "human"}, promptSource="sdk"),
            user("sdk without origin", 4, promptSource="sdk"),
            user("meta without origin", 5, isMeta=True),
            user("plugin prompt", 6, origin={"kind": "plugin"}, promptSource="sdk"),
            user("task note", 7, origin={"kind": "task-notification"}),
            user("peer", 8, origin={"kind": "peer"}),
        ])
        self.assertEqual((digest.human_prompts, digest.corrections), (3, 1))
        self.assertEqual([e[3] for e in entries_of(text)][:1], ["plain extension prompt"])
        self.assertTrue(any(e[3].startswith("No, fix the real bug") for e in entries_of(text)))
        self.assertIn("string form with ide prefix", text)
        self.assertNotIn("opened the file", text)

    def test_long_prompt_is_truncated_with_count(self):
        _, text = self.run_one([user("word " * 400, 0)])
        self.assertIn("…[+", entries_of(text)[0][3])
        self.assertLess(len(entries_of(text)[0][3]), 650)

    def test_corrections_include_assistant_context(self):
        digest, text = self.run_one([
            assistant([{"type": "text", "text": "I renamed the module"}], 0),
            user("No, don't rename it", 1),
            user("I said keep the name", 2),
            user("why did you touch the tests", 3),
            user("looks good", 4),
        ])
        self.assertEqual((digest.human_prompts, digest.corrections), (4, 3))
        self.assertEqual(tags_of(text), ["**CORRECTION**"] * 3 + ["PROMPT"])
        self.assertIn('assistant had said: "I renamed the module"', text)

    def test_tool_errors_resolve_names_even_from_before_window(self):
        records = [
            tool_use("t1", "Bash", {"command": "pytest -x tests/"}, 0),
            tool_use("t2", "Edit", {"file_path": "/src/a.py", "old_string": "x"}, 1),
            tool_use("t3", "Grep", {"pattern": "foo"}, 20),
            tool_result("t1", "boom: exit 2", minutes=30),
            tool_result("t2", "no match", minutes=31, as_blocks=True),
            tool_result("t3", "fine", is_error=False, minutes=32),
            tool_result("nope", "orphan", minutes=33),
        ]
        digest, text = self.run_one(records, since=BASE + timedelta(minutes=25))
        self.assertEqual(digest.tool_errors, 3)
        bodies = [e[3] for e in entries_of(text)]
        self.assertEqual(bodies[0], "Bash `pytest -x tests/`: boom: exit 2")
        self.assertEqual(bodies[1], "Edit `/src/a.py`: no match")
        self.assertEqual(bodies[2], "unknown-tool ``: orphan")

    def test_denials_and_hook_blocks(self):
        digest, text = self.run_one([
            tool_use("t1", "Bash", {"command": "rm -rf x"}, 0),
            tool_result("t1", "The user doesn't want to proceed with this tool use.", minutes=1),
            tool_use("t2", "Write", {"file_path": "/a"}, 2),
            tool_result("t2", "PreToolUse hook error: blocked by policy", minutes=3),
            tool_use("t3", "Read", {"file_path": "/notes.txt"}, 4),
            tool_result("t3", "this file says access denied", is_error=False, minutes=5),
            tool_use("t4", "Bash", {"command": "x"}, 6),
            tool_result("t4", "plain failure", minutes=7),
        ])
        self.assertEqual((digest.denials, digest.tool_errors), (2, 1))
        # a no-kind "doesn't want to proceed" error is a user interrupt of an unprompted call
        self.assertEqual(tags_of(text), ["**INTERRUPTED**", "**DENIED**", "ERROR"])

    def test_repeated_failures_section(self):
        records = [tool_use("a", "Bash", {"command": "make"}, 0)]
        for i in range(3):
            records.append(tool_result("a", f"fail {i}", minutes=1 + i))
        records += [tool_use("b", "Bash", {"command": "once"}, 5), tool_result("b", "x", minutes=6)]
        _, text = self.run_one(records)
        section = text.split("## Repeated failures")[1]
        self.assertIn("- Bash `make` x3", section)
        self.assertNotIn("once", section)

    def test_memory_writes_only_inside_memory_dirs(self):
        inside = str(self.root / "other-slug" / "memory" / "feedback.md")
        outside = str(self.root / "other-slug" / "notes" / "feedback.md")
        digest, text = self.run_one([
            tool_use("w1", "Write", {"file_path": inside, "content": "x"}, 0),
            tool_use("w2", "Edit", {"file_path": outside}, 1),
            tool_use("w3", "Read", {"file_path": inside}, 2),
            tool_use("w4", "MultiEdit", {"file_path": inside.replace("\\", "/")}, 3),
        ])
        self.assertEqual(digest.memory_writes, 2)
        self.assertEqual([e[3] for e in entries_of(text)], ["Write feedback.md", "MultiEdit feedback.md"])

    def test_missing_timestamps_carry_forward(self):
        _, text = self.run_one([user("a", 5), user("b"), user("c", 9), user("d")])
        self.assertEqual([e[0] for e in entries_of(text)], ["09:05", "09:05", "09:09", "09:09"])

    def test_window_filters_by_timestamp(self):
        records = [user(f"prompt {i}", i * 10) for i in range(6)]
        digest, text = self.run_one(records, since=BASE + timedelta(minutes=10), until=BASE + timedelta(minutes=40))
        self.assertEqual([e[3] for e in entries_of(text)], [f"prompt {i}" for i in (1, 2, 3, 4)])
        self.assertEqual((digest.first_ts, digest.last_ts), (stamp(10), stamp(40)))

    def test_nothing_in_window_returns_none(self):
        digest, _ = self.run_one([user("x", 0)], since=BASE + timedelta(days=1))
        self.assertIsNone(digest)
        self.assertEqual(list(self.out.glob("*.md")) if self.out.exists() else [], [])

    def test_malformed_and_irrelevant_lines_are_skipped(self):
        digest, text = self.run_one([
            "{not json", "", '{"type":"api-request","message":"x"}',
            '{"type":"api-request-blob","big":"' + "z" * 1000 + '"}',
            json.dumps({"parentUuid": None, "type": "attachment", "x": 1}),
            user("kept", 0),
        ])
        self.assertEqual(digest.human_prompts, 1)
        self.assertEqual(entries_of(text)[0][1], 6)  # L-ref counts skipped lines too

    def test_spaced_json_is_still_read(self):
        digest, _ = self.run_one([json.dumps(user("spaced out", 0)), '{"type": "api-request", "n": 1}'])
        self.assertEqual(digest.human_prompts, 1)

    def test_max_chars_trims_low_priority_first(self):
        records = [user(f"plain prompt number {i} " + "w" * 300, i) for i in range(40)]
        records += [tool_use("t", "Bash", {"command": "make"}, 50), tool_result("t", "kaboom", minutes=51)]
        records.append(user("No, wrong approach", 52))
        digest, text = self.run_one(records, max_chars=2500)
        self.assertLessEqual(len(text), 2500)
        self.assertEqual(digest.chars, len(text))
        tags = tags_of(text)
        self.assertIn("ERROR", tags)
        self.assertIn("**CORRECTION**", tags)
        dropped = int(re.search(r"PROMPT=(\d+)", DROPPED.search(text).group(1)).group(1))
        self.assertEqual(dropped + tags.count("PROMPT"), 40)
        self.assertEqual(digest.human_prompts, 41)

    def test_tiny_max_chars_still_bounded(self):
        _, text = self.run_one([user("hello " * 100, 0)], max_chars=300)
        self.assertLessEqual(len(text), 300)

    def test_redaction_of_every_written_string(self):
        digest, text = self.run_one([
            {"type": "ai-title", "aiTitle": "debug " + GH_TOKEN},
            assistant([{"type": "text", "text": "using password=" + PASSWORD}], 0),
            user("No, token " + GH_TOKEN + " is wrong", 1),
            tool_use("t", "Bash", {"command": "curl -H 'Authorization: " + GH_TOKEN + "' x"}, 2),
            tool_result("t", "failed with " + GH_TOKEN + " and password: " + PASSWORD, minutes=3),
            tool_result("t", "again", minutes=4),
            tool_use("m", "Write", {"file_path": str(self.root / "s" / "memory" / (GH_TOKEN + ".md"))}, 5),
        ])
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertNotIn(GH_TOKEN, str(digest.title))
        self.assertIn("[REDACTED:github-token]", text)

    def test_unicode_and_odd_blocks_do_not_crash(self):
        digest, text = self.run_one([
            user("héllo ✓ 日本語", 0),
            user([{"type": "tool_result", "tool_use_id": 5, "content": None, "is_error": True}], 1),
            user([None, 3, "str", {"type": "text", "text": None}], 2),
            {"type": "assistant", "message": "not a dict"},
            tool_use("t", "Weird", "not-a-dict", 3),
        ])
        self.assertEqual(digest.human_prompts, 1)
        self.assertEqual(digest.tool_errors, 1)


class SubagentTests(DistillCase):
    def write_agent(self, records, meta=None):
        path = write_jsonl(self.project / "bbbbbbbb-2222" / "subagents" / "agent-abcdef123456.jsonl", records)
        if meta is not None:
            path.with_name("agent-abcdef123456.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return path

    def test_subagent_digest_has_meta_and_no_prompts(self):
        path = self.write_agent([
            user("the parent's instructions", 0),
            tool_use("t", "Bash", {"command": "ls"}, 1),
            tool_result("t", "oops", minutes=2),
            tool_result("t", "oops", minutes=3),
        ], {"agentType": "Explore", "description": "find things", "model": "sonnet", "finished": True})
        digest = distill.distill_transcript(path, self.out, None, None, self.root)
        text = digest.digest_path.read_text(encoding="utf-8")
        self.assertTrue(digest.is_subagent)
        self.assertEqual(digest.session_id, "bbbbbbbb-2222")
        self.assertEqual(digest.digest_path.name, "20261101-0900_bbbbbbbb_agent-abcdef12.md")
        self.assertEqual((digest.human_prompts, digest.corrections, digest.tool_errors), (0, 0, 2))
        self.assertIn("type=Explore", text)
        self.assertIn("description=find things", text)
        self.assertNotIn("parent's instructions", text)
        self.assertIn("- Bash `ls` x2", text)

    def test_subagent_without_failures_yields_none(self):
        path = self.write_agent([user("go", 0), assistant([{"type": "text", "text": "done"}], 1)])
        self.assertIsNone(distill.distill_transcript(path, self.out, None, None, self.root))

    def test_missing_or_corrupt_meta_is_tolerated(self):
        path = self.write_agent([tool_use("t", "Bash", {"command": "x"}, 0), tool_result("t", "e", minutes=1)])
        path.with_name("agent-abcdef123456.meta.json").write_text("{bad", encoding="utf-8")
        self.assertIsNotNone(distill.distill_transcript(path, self.out, None, None, self.root))


class ProjectTests(DistillCase):
    def make_project(self):
        write_jsonl(self.project / "old00000-aaaa.jsonl", [user("old one", 0), user("older", 1, cwd="/other")])
        write_jsonl(self.project / "new00000-bbbb.jsonl", [user("new one", 600)])
        write_jsonl(self.project / "mid00000-cccc.jsonl", [user("mid one", 300)])
        write_jsonl(self.project / "new00000-bbbb" / "subagents" / "agent-1234567890.jsonl",
                    [tool_use("t", "Bash", {"command": "x"}, 700), tool_result("t", "bad", minutes=701)])
        (self.project / "memory").mkdir()
        write_jsonl(self.project / "memory" / "ignored.jsonl", [user("memory dir", 0)])
        write_jsonl(self.project / "tool-results" / "ignored.jsonl", [user("tool results", 0)])

    def test_find_transcripts(self):
        self.make_project()
        names = [p.name for p in distill.find_transcripts(self.project)]
        self.assertEqual(sorted(names), sorted(["old00000-aaaa.jsonl", "new00000-bbbb.jsonl", "mid00000-cccc.jsonl", "agent-1234567890.jsonl"]))

    def test_distill_project_sorted_newest_first(self):
        self.make_project()
        digests = distill.distill_project(self.project, self.out)
        self.assertEqual([d.last_ts for d in digests], [stamp(701), stamp(600), stamp(300), stamp(1)])
        self.assertTrue(all(d.digest_path.exists() for d in digests))

    def test_max_sessions_keeps_most_recent_and_removes_others(self):
        self.make_project()
        digests = distill.distill_project(self.project, self.out, max_sessions=2)
        self.assertEqual([d.last_ts for d in digests], [stamp(701), stamp(600)])
        self.assertEqual(len(list(self.out.glob("*.md"))), 2)

    def test_since_until(self):
        self.make_project()
        digests = distill.distill_project(self.project, self.out, since=BASE + timedelta(minutes=200),
                                          until=BASE + timedelta(minutes=650))
        self.assertEqual(sorted(d.last_ts for d in digests), [stamp(300), stamp(600)])

    def test_index_and_project_cwd(self):
        self.make_project()
        digests = distill.distill_project(self.project, self.out)
        index = distill.write_index(digests, self.out)
        self.assertEqual(index.name, "INDEX.md")
        text = index.read_text(encoding="utf-8")
        rows = [line for line in text.splitlines() if line.startswith("| [")]
        self.assertEqual(len(rows), 4)
        self.assertLess(text.index(digests[0].digest_path.name), text.index(digests[-1].digest_path.name))
        self.assertIn("**Totals:** prompts=4, corrections=0, errors=1, denials=0, memory writes=0", text)
        self.assertEqual(distill.project_cwd(digests), "/work/proj")
        self.assertIsNone(distill.project_cwd([]))


class FormatDriftStatsTests(DistillCase):
    """DistillStats tells "quiet window" apart from "transcripts we can no longer read"."""

    def test_healthy_transcript_is_not_drift(self):
        write_jsonl(self.project / "aaaaaaaa-1111.jsonl", [user("hello", 0), assistant([{"type": "text", "text": "hi"}], 1)])
        stats = distill.DistillStats()
        digests = distill.distill_project(self.project, self.out, stats=stats)
        self.assertEqual(len(digests), 1)
        self.assertEqual((stats.files, stats.records), (1, 2))
        self.assertFalse(stats.format_changed)

    def test_unknown_record_shapes_count_as_drift(self):
        write_jsonl(self.project / "aaaaaaaa-1111.jsonl", [
            {"kind": "turn", "role": "user", "text": "hello"},
            {"kind": "turn", "role": "assistant", "text": "hi"},
            {"type": "user", "msg": "renamed field"},
        ])
        stats = distill.DistillStats()
        self.assertEqual(distill.distill_project(self.project, self.out, stats=stats), [])
        self.assertEqual((stats.files, stats.records), (1, 0))
        self.assertTrue(stats.format_changed)

    def test_unparseable_file_counts_as_drift(self):
        write_jsonl(self.project / "aaaaaaaa-1111.jsonl", ["{not json", "also not"])
        stats = distill.DistillStats()
        distill.distill_project(self.project, self.out, stats=stats)
        self.assertTrue(stats.format_changed)

    def test_records_outside_the_window_still_prove_the_format_parses(self):
        write_jsonl(self.project / "aaaaaaaa-1111.jsonl", [user("old", 0)])
        stats = distill.DistillStats()
        digests = distill.distill_project(self.project, self.out, since=BASE + timedelta(days=1), stats=stats)
        self.assertEqual(digests, [])
        self.assertFalse(stats.format_changed)

    def test_files_older_than_the_window_and_empty_files_are_not_considered(self):
        old = write_jsonl(self.project / "old00000-aaaa.jsonl", [{"kind": "other"}])
        past = time.time() - 30 * 86400
        os.utime(old, (past, past))
        (self.project / "empty000-bbbb.jsonl").write_bytes(b"")
        stats = distill.DistillStats()
        since = datetime.now(timezone.utc) - timedelta(days=1)
        self.assertEqual(distill.distill_project(self.project, self.out, since=since, stats=stats), [])
        self.assertEqual(stats.files, 0)
        self.assertFalse(stats.format_changed)

    def test_one_readable_file_among_unreadable_ones_is_not_drift(self):
        write_jsonl(self.project / "aaaaaaaa-1111.jsonl", [{"kind": "other"}])
        write_jsonl(self.project / "bbbbbbbb-2222.jsonl", [user("fine", 0)])
        stats = distill.DistillStats()
        distill.distill_project(self.project, self.out, stats=stats)
        self.assertEqual(stats.files, 2)
        self.assertFalse(stats.format_changed)


CHROME = "mcp__chrome-devtools__get_network_request"
INTERRUPT_TEXT = "The user doesn't want to proceed with this tool use. The tool use was rejected."
PERMISSION_HEADER = ("| tool | input | blocked | approved after block | interrupted | interrupted then corrected "
                     "| rejected at prompt | sessions | block reasons | approval modes | refs |")


def blocked_result(tool_id, reason="Credential Materialization", minutes=None):
    text = f"Permission for this action was denied by the Claude Code auto mode classifier. Reason: [{reason}]. Ask the user."
    return tool_result(tool_id, text, minutes=minutes, toolDenialKind="automode-blocked")


class PermissionEventTests(DistillCase):
    def test_classify_result(self):
        cases = [
            (("anything", True, "automode-blocked"), "blocked"),
            (("Permission for this action was denied by the Claude Code auto mode classifier. Reason: [X].", True), "blocked"),
            ((INTERRUPT_TEXT, True, "user-rejected"), "rejected"),
            ((INTERRUPT_TEXT, True), "interrupted"),
            (("session ended before this call's result was recorded", True, "interrupted"), "error"),
            (("rule matched", True, "permission-rule"), "denied"),
            (("classifier down", True, "automode-unavailable"), "denied"),
            (("PreToolUse hook error: blocked", True), "denied"),
            (("boom", True), "error"),
            (("fine", False), "ok"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(distill.classify_result(*args), expected)

    def test_classifier_reason(self):
        self.assertEqual(distill.classifier_reason("x Reason: [Credential Materialization]. y"), "Credential Materialization")
        self.assertEqual(distill.classifier_reason("no reason here"), "unspecified")

    def test_block_then_success_is_approved_with_mode(self):
        digest, text = self.run_one([
            user("start", 0, permissionMode="auto"),
            tool_use("t1", CHROME, {"reqid": 4}, 1),
            blocked_result("t1", minutes=2),
            user("manual permissions, do the read again", 3, permissionMode="acceptEdits"),
            tool_use("t2", CHROME, {"reqid": 4}, 4),
            tool_result("t2", "{...}", is_error=False, minutes=5),
        ])
        self.assertEqual((digest.blocked, digest.approved_after_block, digest.interrupted, digest.rejected_at_prompt), (1, 1, 0, 0))
        self.assertEqual(digest.denials, 1)
        permission_tags = [(e[1], e[2]) for e in entries_of(text) if e[2] in ("**BLOCKED**", "**APPROVED-AFTER-BLOCK**")]
        self.assertEqual(permission_tags, [(3, "**BLOCKED**"), (6, "**APPROVED-AFTER-BLOCK**")])
        self.assertIn(f"{CHROME} `reqid=4`: classifier [Credential Materialization]", text)
        self.assertIn("(mode: acceptEdits)", text)
        self.assertIn("counts: human_prompts=2 corrections=0 tool_errors=0 denials=1 memory_writes=0 blocked=1 "
                      "approved_after_block=1 interrupted=0 rejected_at_prompt=0", text)
        section = text.split("## Permission events")[1]
        self.assertIn(f"- {CHROME} `reqid=4`: blocked 1 ([Credential Materialization]); "
                      "approved after block 1 (mode: acceptEdits) - L3, L6", section)
        event = digest.permission_events[1]
        self.assertEqual((event.kind, event.permission_mode, event.line, event.session_id),
                         ("approved_after_block", "acceptEdits", 6, "aaaaaaaa-1111"))

    def test_approval_requires_same_tool_and_input_and_consumes_the_block(self):
        digest, _ = self.run_one([
            tool_use("t1", "Bash", {"command": "cat .env"}, 0), blocked_result("t1", minutes=1),
            tool_use("t2", "Bash", {"command": "ls"}, 2), tool_result("t2", "a", is_error=False, minutes=3),
            tool_use("t3", "Read", {"file_path": "x"}, 4), tool_result("t3", "a", is_error=False, minutes=5),
            tool_use("t4", "Bash", {"command": "cat .env"}, 6), tool_result("t4", "ok", is_error=False, minutes=7),
            tool_use("t5", "Bash", {"command": "cat .env"}, 8), tool_result("t5", "ok", is_error=False, minutes=9),
        ])
        self.assertEqual((digest.blocked, digest.approved_after_block), (1, 1))

    def test_unknown_mode_is_reported_as_unknown(self):
        digest, text = self.run_one([
            tool_use("t1", "Bash", {"command": "x"}, 0), blocked_result("t1", minutes=1),
            tool_use("t2", "Bash", {"command": "x"}, 2), tool_result("t2", "ok", is_error=False, minutes=3),
        ])
        self.assertIsNone(digest.permission_events[1].permission_mode)
        self.assertIn("(mode: unknown)", text)

    def test_block_never_retried(self):
        digest, text = self.run_one([
            tool_use("t1", "Bash", {"command": "curl x"}, 0), blocked_result("t1", "Exfiltration", 1),
            tool_use("t2", "Bash", {"command": "other"}, 2), tool_result("t2", "ok", is_error=False, minutes=3),
        ])
        self.assertEqual((digest.blocked, digest.approved_after_block), (1, 0))
        self.assertEqual(tags_of(text), ["**BLOCKED**"])
        self.assertIn("blocked 1 ([Exfiltration]) - L2", text.split("## Permission events")[1])

    def test_no_kind_rejection_is_an_interrupt(self):
        digest, text = self.run_one([
            tool_use("t1", "Bash", {"command": "sleep 99"}, 0),
            user([{"type": "tool_result", "tool_use_id": "t1", "content": INTERRUPT_TEXT, "is_error": True},
                  {"type": "text", "text": "[Request interrupted by user for tool use]"}], 1),
        ])
        self.assertEqual((digest.interrupted, digest.rejected_at_prompt, digest.denials, digest.human_prompts), (1, 0, 1, 0))
        self.assertEqual(tags_of(text), ["**INTERRUPTED**"])
        self.assertFalse(digest.permission_events[0].followed_by_correction)

    def test_marker_in_a_separate_record_does_not_double_count(self):
        digest, _ = self.run_one([
            tool_use("t1", "Bash", {"command": "sleep 99"}, 0), tool_result("t1", INTERRUPT_TEXT, minutes=1),
            user([{"type": "text", "text": "[Request interrupted by user for tool use]"}], 2),
        ])
        self.assertEqual((digest.interrupted, digest.human_prompts, digest.denials), (1, 0, 1))

    def test_call_without_result_then_bare_marker_is_interrupted(self):
        digest, text = self.run_one([
            tool_use("t1", "Bash", {"command": "make"}, 0),
            user([{"type": "text", "text": "[Request interrupted by user]"}], 1),
        ])
        self.assertEqual((digest.interrupted, digest.human_prompts, digest.denials), (1, 0, 1))
        self.assertEqual([e[3] for e in entries_of(text)], ["Bash `make`: interrupted by user before any result"])

    def test_bare_marker_without_open_call_is_ignored(self):
        digest, _ = self.run_one([
            tool_use("t1", "Bash", {"command": "make"}, 0), tool_result("t1", "ok", is_error=False, minutes=1),
            user([{"type": "text", "text": "[Request interrupted by user]"}], 2),
        ])
        self.assertEqual((digest.interrupted, digest.human_prompts), (0, 0))

    def test_user_rejected_is_rejected_at_prompt_not_interrupted(self):
        digest, text = self.run_one([
            tool_use("t1", "Bash", {"command": "rm x"}, 0),
            tool_result("t1", INTERRUPT_TEXT, minutes=1, toolDenialKind="user-rejected"),
        ])
        self.assertEqual((digest.rejected_at_prompt, digest.interrupted, digest.denials), (1, 0, 1))
        self.assertEqual(tags_of(text), ["**DENIED**"])
        self.assertIn("Bash `rm x` (user-rejected):", text)
        self.assertIn("rejected at prompt 1 - L2", text.split("## Permission events")[1])

    def test_other_denial_kinds_show_their_kind(self):
        digest, text = self.run_one([
            tool_use("t1", "Bash", {"command": "git push"}, 0),
            tool_result("t1", "Denied by a rule", minutes=1, toolDenialKind="permission-rule"),
            tool_use("t2", "Bash", {"command": "ls"}, 2),
            tool_result("t2", "classifier unavailable", minutes=3, toolDenialKind="automode-unavailable"),
        ])
        self.assertEqual((digest.denials, digest.tool_errors, digest.blocked, digest.interrupted, digest.rejected_at_prompt),
                         (2, 0, 0, 0, 0))
        self.assertEqual([e[3] for e in entries_of(text)],
                         ["Bash `git push` (permission-rule): Denied by a rule",
                          "Bash `ls` (automode-unavailable): classifier unavailable"])

    def test_session_ended_interruption_is_not_a_user_interrupt(self):
        digest, text = self.run_one([
            tool_use("t1", "Bash", {"command": "make"}, 0),
            tool_result("t1", "The session ended before this call's result was recorded", minutes=1,
                        toolDenialKind="interrupted"),
        ])
        self.assertEqual((digest.interrupted, digest.denials, digest.tool_errors), (0, 0, 1))
        self.assertEqual(tags_of(text), ["ERROR"])
        self.assertEqual(digest.permission_events, ())

    def test_interrupt_followed_by_correction(self):
        digest, text = self.run_one([
            tool_use("t1", "Bash", {"command": "deploy"}, 0), tool_result("t1", INTERRUPT_TEXT, minutes=1),
            user("No, don't deploy yet", 2),
            tool_use("t2", "Bash", {"command": "cleanup"}, 3), tool_result("t2", INTERRUPT_TEXT, minutes=4),
            user("ok carry on", 5),
            user("No, wrong", 6),  # a later correction is not "the next prompt"
        ])
        self.assertEqual([e.followed_by_correction for e in digest.permission_events], [True, False])
        section = text.split("## Permission events")[1]
        self.assertIn("Bash `deploy`: interrupted 1 (1 followed by a correction) - L2", section)
        self.assertIn("Bash `cleanup`: interrupted 1 (0 followed by a correction) - L5", section)

    def test_empty_section_says_none(self):
        _, text = self.run_one([user("hi", 0)])
        self.assertTrue(text.rstrip().endswith("## Permission events\n(none)"))

    def test_trimming_keeps_permission_tags(self):
        records = [user(f"plain prompt number {i} " + "w" * 300, i) for i in range(40)]
        records += [tool_use("a", "Bash", {"command": "x"}, 50), blocked_result("a", minutes=51),
                    tool_use("b", "Bash", {"command": "x"}, 52), tool_result("b", "ok", is_error=False, minutes=53),
                    tool_use("c", "Bash", {"command": "y"}, 54), tool_result("c", INTERRUPT_TEXT, minutes=55)]
        _, text = self.run_one(records, max_chars=3000)
        self.assertLessEqual(len(text), 3000)
        self.assertEqual(sorted(t for t in tags_of(text) if t != "PROMPT"),
                         ["**APPROVED-AFTER-BLOCK**", "**BLOCKED**", "**INTERRUPTED**"])
        self.assertIn("PROMPT=", DROPPED.search(text).group(1))

    def test_secrets_in_permission_data_are_redacted(self):
        digest, text = self.run_one([
            user("x", 0, permissionMode="mode-" + GH_TOKEN),
            tool_use("t1", "Bash", {"command": "curl -H 'Authorization: " + GH_TOKEN + "' h"}, 1),
            blocked_result("t1", "Leak " + GH_TOKEN, 2),
            tool_use("t2", "Bash", {"command": "curl -H 'Authorization: " + GH_TOKEN + "' h"}, 3),
            tool_result("t2", "ok", is_error=False, minutes=4),
        ])
        self.assertEqual(digest.approved_after_block, 1)
        index = distill.write_index([digest], self.out).read_text(encoding="utf-8")
        for secret in SECRETS:
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, index)
            for event in digest.permission_events:
                self.assertNotIn(secret, repr(event))

    def test_subagent_permission_events_are_digested(self):
        path = write_jsonl(self.project / "bbbbbbbb-2222" / "subagents" / "agent-abcdef123456.jsonl", [
            tool_use("t", "Bash", {"command": "cat .env"}, 0), blocked_result("t", minutes=1),
        ])
        digest = distill.distill_transcript(path, self.out, None, None, self.root)
        self.assertEqual((digest.is_subagent, digest.blocked), (True, 1))
        self.assertEqual(digest.permission_events[0].session_id, "bbbbbbbb-2222")

    def test_index_aggregates_across_sessions(self):
        write_jsonl(self.project / "aaaaaaaa-1111.jsonl", [
            tool_use("t1", CHROME, {"reqid": 4}, 0), blocked_result("t1", "Credential Materialization", 1),
            user("go on", 2, permissionMode="acceptEdits"),
            tool_use("t2", CHROME, {"reqid": 4}, 3), tool_result("t2", "ok", is_error=False, minutes=4),
            tool_use("t3", "Bash", {"command": "sleep 9"}, 5), tool_result("t3", INTERRUPT_TEXT, minutes=6),
            user("no, stop that", 7),
        ])
        write_jsonl(self.project / "bbbbbbbb-2222.jsonl", [
            tool_use("t1", CHROME, {"reqid": 4}, 100), blocked_result("t1", "Exfiltration", 101),
            tool_use("t2", "Bash", {"command": "sleep 9"}, 102), tool_result("t2", INTERRUPT_TEXT, minutes=103),
            tool_use("t3", "Bash", {"command": "sleep 9"}, 104), tool_result("t3", INTERRUPT_TEXT, minutes=105),
            tool_use("t4", "Bash", {"command": "rm y"}, 106),
            tool_result("t4", INTERRUPT_TEXT, minutes=107, toolDenialKind="user-rejected"),
        ])
        digests = distill.distill_project(self.project, self.out)
        rows = distill.aggregate_permission_events(digests)
        self.assertEqual([(r.tool, r.summary) for r in rows], [(CHROME, "reqid=4"), ("Bash", "sleep 9"), ("Bash", "rm y")])
        chrome, sleep, rm = rows
        self.assertEqual((chrome.blocked, chrome.approved_after_block, chrome.interrupted, chrome.sessions), (2, 1, 0, 2))
        self.assertEqual(chrome.reasons, ("Exfiltration", "Credential Materialization"))  # newest digest first
        self.assertEqual(chrome.modes, ("acceptEdits",))
        self.assertEqual((sleep.blocked, sleep.interrupted, sleep.interrupted_then_corrected, sleep.sessions), (0, 3, 1, 2))
        self.assertEqual((rm.rejected_at_prompt, rm.interrupted, rm.sessions), (1, 0, 1))
        self.assertEqual(len(chrome.refs), 3)
        text = distill.write_index(digests, self.out).read_text(encoding="utf-8")
        section = text.split("## Permission events (all sessions)")[1].splitlines()
        self.assertEqual(section[2], PERMISSION_HEADER)
        self.assertTrue(section[4].startswith(
            f"| {CHROME} | reqid=4 | 2 | 1 | 0 | 0 | 0 | 2 | Exfiltration, Credential Materialization | acceptEdits | "))
        self.assertTrue(section[5].startswith("| Bash | sleep 9 | 0 | 0 | 3 | 1 | 0 | 2 |  |  | "))
        self.assertIn("20261101-0900_aaaaaaaa.md L7", section[5])
        self.assertIn("blocked=2, approved after block=1, interrupted=3, rejected at prompt=1", text)

    def test_index_without_events_says_none(self):
        digest, _ = self.run_one([user("hi", 0)])
        text = distill.write_index([digest], self.out).read_text(encoding="utf-8")
        self.assertTrue(text.rstrip().endswith("## Permission events (all sessions)\n\n(none)"))
        self.assertEqual(distill.aggregate_permission_events([digest]), [])


# ---------------------------------------------------------------- stochastic

PLAIN = ["please add a unit test", "refactor the parser module", "looks good, ship it", "what does this function return",
         "use token {gh} for the call", "set password={pw} in the env"]
CORRECTING = ["No, that is the wrong file", "please don't touch the config", "I said use tabs", "stop adding logging",
              "revert that change", "why did you delete it", "that's not what I wanted"]
DENIAL_TEXTS = ["The user doesn't want to proceed with this tool use.", "hook error: blocked by guard"]
ERROR_TEXTS = ["Command failed with exit code 1", "ENOENT: no such file {gh}", "traceback password: {pw}"]


PERM_KEYS = [("mcp__x__y", {"q": 1}, "q=1"), ("mcp__x__y", {"q": 2}, "q=2"), ("mcp__z__w", {"q": 1}, "q=1")]
BLOCK_TEXT = "Permission for this action was denied by the Claude Code auto mode classifier. Reason: [Credential Materialization]. Do not retry."


class Expected:
    def __init__(self):
        self.prompts = self.corrections = self.errors = self.denials = self.memory = 0
        self.blocked = self.approved = self.interrupted = self.rejected = 0
        self.pending: Counter = Counter()
        self.blocked_by_key: Counter = Counter()
        self.approved_by_key: Counter = Counter()

    def counts(self) -> tuple[int, ...]:
        return (self.prompts, self.corrections, self.errors, self.denials, self.memory,
                self.blocked, self.approved, self.interrupted, self.rejected)


def gen_transcript(rng: random.Random, memory_dir: Path, count: int) -> tuple[list, Expected]:
    exp = Expected()
    records: list = []
    minute = 0.0
    for n in range(count):
        minute += rng.random() * 3
        ts = None if rng.random() < 0.15 else minute
        kind = rng.choice(["prompt", "prompt", "fail", "fail", "deny", "ok", "memory", "noise", "assistant", "garbage",
                           "block", "block", "approve", "approve", "reject"])
        if kind == "prompt":
            bad = rng.random() < 0.4
            text = rng.choice(CORRECTING if bad else PLAIN).format(gh=GH_TOKEN, pw=PASSWORD)
            content = text if rng.random() < 0.5 else [{"type": "text", "text": "<system-reminder>r</system-reminder>"}, {"type": "text", "text": text}]
            records.append(user(content, ts))
            exp.prompts += 1
            exp.corrections += bad
        elif kind in ("fail", "deny", "ok"):
            tid = f"t{n}"
            records.append(tool_use(tid, rng.choice(["Bash", "Edit", "Grep"]), {"command": "run " + GH_TOKEN, "file_path": "/f", "pattern": "p"}, ts))
            if kind == "fail":
                records.append(tool_result(tid, rng.choice(ERROR_TEXTS).format(gh=GH_TOKEN, pw=PASSWORD), minutes=None))
                exp.errors += 1
            elif kind == "deny":
                text = rng.choice(DENIAL_TEXTS)
                records.append(tool_result(tid, text, minutes=ts))
                exp.denials += 1
                exp.interrupted += text.startswith("The user doesn't")  # no toolDenialKind: user stopped it
            else:
                records.append(tool_result(tid, "all good", is_error=False))
        elif kind in ("block", "approve"):
            tool, tool_input, key = rng.choice(PERM_KEYS)
            tid = f"p{n}"
            records.append(tool_use(tid, tool, tool_input, ts))
            if kind == "block":
                records.append(tool_result(tid, BLOCK_TEXT, minutes=ts, toolDenialKind="automode-blocked"))
                exp.blocked += 1
                exp.denials += 1
                exp.pending[(tool, key)] += 1
                exp.blocked_by_key[(tool, key)] += 1
            else:
                records.append(tool_result(tid, "fine", is_error=False, minutes=ts, permissionMode=rng.choice(["auto", "plan"])))
                if exp.pending[(tool, key)] > 0:  # a success after an unconsumed block is a manual approval
                    exp.pending[(tool, key)] -= 1
                    exp.approved += 1
                    exp.approved_by_key[(tool, key)] += 1
        elif kind == "reject":
            tid = f"r{n}"
            records.append(tool_use(tid, "Bash", {"command": "rm x"}, ts))
            records.append(tool_result(tid, "The user doesn't want to proceed with this tool use.", minutes=ts,
                                       toolDenialKind="user-rejected"))
            exp.rejected += 1
            exp.denials += 1
        elif kind == "memory":
            inside = rng.random() < 0.5
            target = memory_dir / "x.md" if inside else memory_dir.parent / "elsewhere.md"
            records.append(tool_use(f"m{n}", "Write", {"file_path": str(target)}, ts))
            exp.memory += inside
        elif kind == "noise":
            records.append(rng.choice(['{"type":"api-request","n":1}', {"type": "attachment", "x": [1]}, {"type": "queue-operation"}]))
        elif kind == "assistant":
            records.append(assistant([{"type": "thinking", "thinking": GH_TOKEN}, {"type": "text", "text": "working on it"}], ts))
        else:
            records.append(rng.choice(["{broken", "", "[]", "42"]))
    return records, exp


def effective_timestamps(path: Path) -> dict[int, str | None]:
    """Independent re-derivation: timestamp of each line's record, carried forward when missing."""
    carried, result = None, {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("type") in ("user", "assistant"):
            carried = rec.get("timestamp") or carried
            result[lineno] = carried
    return result


class StochasticTests(DistillCase):
    TRIALS = 40

    def test_random_transcripts_hold_invariants(self):
        seed = new_seed()
        print(f"[test_random_transcripts_hold_invariants] seed={seed}")
        rng = random.Random(seed)
        memory_dir = self.root / "slug" / "memory"
        for trial in range(self.TRIALS):
            records, exp = gen_transcript(rng, memory_dir, rng.randint(1, 120))
            path = write_jsonl(self.project / f"{trial:08d}-rand.jsonl", records)
            for max_chars in (200_000, rng.randint(400, 4000)):
                ctx = f"seed={seed} trial={trial} max_chars={max_chars}"
                digest = distill.distill_transcript(path, self.out, None, None, self.root, max_chars)
                if digest is None:
                    self.assertFalse(effective_timestamps(path), ctx)
                    continue
                text = digest.digest_path.read_text(encoding="utf-8")
                self.assertLessEqual(len(text), max_chars, ctx)
                self.assertEqual(digest.chars, len(text), ctx)
                if "[truncated]" not in text:  # a tiny budget can cut into the header (its source path is OS-specific)
                    self.assertEqual(tuple(map(int, COUNTS.search(text).groups())), exp.counts(), ctx)
                self.assertEqual(
                    (digest.human_prompts, digest.corrections, digest.tool_errors, digest.denials, digest.memory_writes,
                     digest.blocked, digest.approved_after_block, digest.interrupted, digest.rejected_at_prompt),
                    exp.counts(), ctx)
                self.check_permission_events(digest, exp, ctx)
                for secret in SECRETS:
                    self.assertNotIn(secret, text, ctx)
                self.check_entries(path, text, digest, max_chars > 100_000, ctx)

    def check_permission_events(self, digest, exp, ctx):
        events = digest.permission_events
        kinds = Counter(e.kind for e in events)
        self.assertEqual((kinds["blocked"], kinds["approved_after_block"], kinds["interrupted"], kinds["rejected_at_prompt"]),
                         (exp.blocked, exp.approved, exp.interrupted, exp.rejected), ctx)
        for key in exp.blocked_by_key:
            tool, summary = key
            blocked = sum(e.kind == "blocked" and (e.tool, e.summary) == key for e in events)
            approved = sum(e.kind == "approved_after_block" and (e.tool, e.summary) == key for e in events)
            self.assertEqual((blocked, approved), (exp.blocked_by_key[key], exp.approved_by_key[key]), f"{ctx} {key}")
            self.assertLessEqual(approved, blocked, ctx)
        text = digest.digest_path.read_text(encoding="utf-8")
        if "[truncated]" not in text:
            section = text.split("## Permission events")[1]
            self.assertEqual(len([l for l in section.splitlines() if l.startswith("- ")]),
                             len({(e.tool, e.summary) for e in events}), ctx)

    def check_entries(self, path, text, digest, expect_all, ctx):
        ts_by_line = effective_timestamps(path)
        found = entries_of(text)
        for hm, line, tag, _ in found:
            self.assertIn(line, ts_by_line, ctx)
            self.assertEqual(ts_by_line[line][11:16] if ts_by_line[line] else "--:--", hm, f"{ctx} L{line}")
        tags = Counter(tag for _, _, tag, _ in found)
        dropped = {k: int(v) for k, v in re.findall(r"([A-Z*-]+)=(\d+)", (DROPPED.search(text) or [None, ""])[1])}
        shown = {"prompts": tags["PROMPT"] + tags["**CORRECTION**"], "corrections": tags["**CORRECTION**"],
                 "errors": tags["ERROR"], "memory": tags["MEMORY-WRITE"],
                 "denials": tags["**DENIED**"] + tags["**BLOCKED**"] + tags["**INTERRUPTED**"],
                 "blocked": tags["**BLOCKED**"], "approved": tags["**APPROVED-AFTER-BLOCK**"], "interrupted": tags["**INTERRUPTED**"]}
        lost = {"prompts": dropped.get("PROMPT", 0) + dropped.get("**CORRECTION**", 0), "corrections": dropped.get("**CORRECTION**", 0),
                "errors": dropped.get("ERROR", 0), "memory": dropped.get("MEMORY-WRITE", 0),
                "denials": dropped.get("**DENIED**", 0) + dropped.get("**BLOCKED**", 0) + dropped.get("**INTERRUPTED**", 0),
                "blocked": dropped.get("**BLOCKED**", 0), "approved": dropped.get("**APPROVED-AFTER-BLOCK**", 0),
                "interrupted": dropped.get("**INTERRUPTED**", 0)}
        total = {"prompts": digest.human_prompts, "corrections": digest.corrections, "errors": digest.tool_errors,
                 "denials": digest.denials, "memory": digest.memory_writes,
                 "blocked": digest.blocked, "approved": digest.approved_after_block, "interrupted": digest.interrupted}
        if "[truncated]" not in text:
            for key in total:
                self.assertEqual(shown[key] + lost[key], total[key], f"{ctx} {key}")
        if expect_all:
            self.assertEqual(lost, {k: 0 for k in lost}, ctx)



class PerfTests(DistillCase):
    TARGET_BYTES = 50 * 1024 * 1024

    @unittest.skipIf(os.environ.get("TO_DREAM_SKIP_PERF") == "1", "TO_DREAM_SKIP_PERF=1")
    def test_streams_large_file_quickly(self):
        rng = random.Random(1)
        blob = json.dumps({"type": "api-request-blob", "body": "z" * 20_000})
        path = self.project / "big00000-perf.jsonl"
        written, n = 0, 0
        with open(path, "w", encoding="utf-8") as handle:
            while written < self.TARGET_BYTES:
                n += 1
                batch = [blob, '{"type":"api-request","id":%d}' % n]
                batch.append(compact(user(f"prompt {n} " + "text " * rng.randint(1, 200), n * 0.01)))
                batch.append(compact(tool_use(f"t{n}", "Bash", {"command": f"cmd {n % 7}"}, n * 0.01)))
                batch.append(compact(tool_result(f"t{n}", "x" * rng.randint(10, 5000), is_error=n % 5 == 0, minutes=n * 0.01)))
                batch.append(compact(assistant([{"type": "text", "text": "y" * rng.randint(10, 3000)}], n * 0.01)))
                text = "\n".join(batch) + "\n"
                handle.write(text)
                written += len(text)
        started = time.perf_counter()
        digest = distill.distill_transcript(path, self.out, None, None, self.root)
        elapsed = time.perf_counter() - started
        mb = written / 1024 / 1024
        print(f"[perf] {mb:.0f} MB in {elapsed:.1f}s = {mb / elapsed:.0f} MB/s")
        self.assertLess(elapsed, 120)
        self.assertLessEqual(digest.chars, 20000)
        self.assertGreater(digest.tool_errors, 0)


if __name__ == "__main__":
    unittest.main()
