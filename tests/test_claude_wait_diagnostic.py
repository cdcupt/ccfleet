#!/usr/bin/env python3
"""Synthetic privacy/behavior checks; no real conversation files are read.

Run with pytest; all conversation fixtures are synthetic.
"""
import ast
import datetime as dt
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "tools" / "claude_wait_diagnostic.py"
SOURCE = SCRIPT.read_text()
NAMESPACE = {"__name__": "diagnostic_test"}
exec(compile(SOURCE, str(SCRIPT), "exec"), NAMESPACE)
PRIVATE = "NEVER_EMIT_PRIVATE_PROMPT_TOKEN_abcdef"


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-diagnostic-test-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name) / "projects"
        self.sub = self.root / "private-project-name" / "session-123" / "subagents"
        self.sub.mkdir(parents=True)
        self.file = self.sub / "agent-test.jsonl"
        self.now = dt.datetime.now(dt.timezone.utc)

    def ts(self, delta):
        return (self.now + dt.timedelta(seconds=delta)).isoformat()

    def rows(self):
        return [
            {"type": "assistant", "timestamp": self.ts(-120), "message": {
                "id": "response-1", "model": "claude-test",
                "usage": {"input_tokens": 100, "cache_read_input_tokens": 300, "output_tokens": 10},
                "content": [{"type": "text", "text": PRIVATE},
                            {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": PRIVATE}}]}},
            {"type": "assistant", "timestamp": self.ts(-119), "message": {
                "id": "response-1", "model": "claude-test",
                "usage": {"input_tokens": 100, "cache_read_input_tokens": 300, "output_tokens": 15}, "content": []}},
            {"type": "user", "timestamp": self.ts(-118), "message": {
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "is_error": False, "content": PRIVATE}]}},
            {"type": "assistant", "timestamp": self.ts(-90), "message": {
                "id": "response-2", "model": "claude-test", "usage": {"input_tokens": 50, "output_tokens": 4},
                "content": [{"type": "tool_use", "id": "tool-2", "name": "Read", "input": {"file_path": PRIVATE}}]}}
        ]

    def write_rows(self, rows):
        self.file.write_text("\n".join(map(json.dumps, rows)) + "\n")

    def run_diagnostic(self, *args):
        result = subprocess.run([sys.executable, str(SCRIPT), "--projects-dir", str(self.root),
                                 "--no-processes", "--session", "session-123", *args],
                                capture_output=True, text=True)
        self.assertEqual(result.stderr, "")
        return result, json.loads(result.stdout)

    def test_syntax_privacy_deduplication_pair_duration_pending_and_read_only(self):
        ast.parse(SOURCE)
        self.write_rows(self.rows())
        before = self.file.read_bytes()
        result, report = self.run_diagnostic()
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(PRIVATE, result.stdout)
        self.assertNotIn("private-project-name", result.stdout)
        self.assertNotIn(str(self.root), result.stdout)
        item = report["files"][0]
        self.assertEqual(item["tail_usage_unique_response_count"], 2)
        self.assertEqual(item["tail_usage_totals_deduplicated"]["output_tokens"], 19)
        self.assertEqual(item["tail_usage_totals_deduplicated"]["input_tokens"], 150)
        self.assertEqual(item["tail_usage_field_sample_counts"]["cache_read_input_tokens"], 1)
        self.assertEqual(item["longest_observed_tool_pairs"][0]["observed_duration_seconds"], 2)
        self.assertEqual(item["pending_tool_candidates"][0]["tool"], "Read")
        self.assertGreaterEqual(item["pending_tool_candidates"][0]["age_seconds"], 89)
        self.assertEqual(item["tail_unique_tool_use_count"], 2)
        self.assertEqual(self.file.read_bytes(), before)

    def test_tail_truncation_marks_incomplete_pairs_and_omits_partial_content(self):
        huge = {"type": "user", "timestamp": self.ts(-300), "message": {"content": PRIVATE + "x" * 1100000}}
        self.write_rows([huge, *self.rows()[2:]])
        result, report = self.run_diagnostic("--tail-mib", "1")
        self.assertEqual(result.returncode, 0)
        item = report["files"][0]
        self.assertTrue(item["tail_truncated"])
        self.assertTrue(item["partial_first_line_discarded"])
        self.assertEqual(item["tail_tool_results_without_matching_start"], 1)
        self.assertNotIn(PRIVATE, result.stdout)

    def test_symlinks_are_not_selected(self):
        self.write_rows(self.rows())
        outside = pathlib.Path(self.temporary.name) / "outside.jsonl"
        outside.write_text(json.dumps(self.rows()[0]))
        (self.sub / "agent-link.jsonl").symlink_to(outside)
        result, report = self.run_diagnostic()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(report["files"]), 1)

    def test_invalid_session_and_help_produce_json(self):
        result, report = self.run_diagnostic("--session", "../escape")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["error"], "invalid_session_id")
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage", json.loads(result.stdout))

    def test_metadata_labels_cannot_reintroduce_absolute_paths(self):
        for path in ("/Users/person/private", "C:/Users/person/private"):
            self.assertEqual(NAMESPACE["label"](path), "unknown")

    def test_malformed_type_and_subtype_do_not_abort_report(self):
        self.write_rows([{"type": [], "timestamp": self.ts(-121)},
                         {"type": "system", "subtype": {}, "timestamp": self.ts(-121)},
                         *self.rows()])
        result, report = self.run_diagnostic()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["files"][0]["tail_unique_tool_use_count"], 2)

    def test_process_snapshot_uses_names_only_and_selects_descendants(self):
        output = ("900001 1 10:00 0.1 0.2 S /Users/person/.local/share/claude/versions/2.1.284\n"
                  "900002 900001 00:04 3 0.1 S /usr/bin/node\n"
                  "900003 1 00:01 0 0 S /usr/bin/unrelated\n")
        fake = subprocess.CompletedProcess([], 0, stdout=output)
        with mock.patch.object(NAMESPACE["subprocess"], "run", return_value=fake) as run:
            report = NAMESPACE["process_snapshot"]()
        command = run.call_args.args[0]
        self.assertEqual(command, ["ps", "-axo", "pid=,ppid=,etime=,pcpu=,pmem=,stat=,comm="])
        self.assertEqual({row["pid"] for row in report["processes"]}, {900001, 900002})
        self.assertNotIn("/Users/person", json.dumps(report))
        self.assertNotIn("/usr/bin", json.dumps(report))
        for row in report["processes"]:
            self.assertEqual(set(row), {"pid", "ppid", "elapsed", "cpu_percent", "memory_percent", "state", "process_name"})

    def test_oversized_or_nonfinite_usage_is_rejected(self):
        for value in (10**1000, float("inf"), float("nan"), True, -1):
            self.assertIsNone(NAMESPACE["number"](value))


if __name__ == "__main__":
    unittest.main(verbosity=2)
