import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_dsh


class RunnerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.record = root / "record"
        self.goal = root / "goal.txt"
        self.goal.write_text("Goal: deduplicate. Acceptance: preserve first spelling.", encoding="utf-8")
        self.context = root / "context.txt"
        self.context.write_text("Fact: one-shot iterable. Assumption: input is text.", encoding="utf-8")
        self.source = root / "names.py"
        self.source.write_text("INITIAL", encoding="utf-8")
        self.review_args = ["--workspace", str(root), "--source", self.source.name]

    def run_main(self, *args):
        argv = ["runner", "--goal", str(self.goal), "--run-dir", str(self.record), *args]
        with patch.object(sys, "argv", argv), patch.object(run_dsh, "check_plugin"), contextlib.redirect_stdout(io.StringIO()):
            return run_dsh.main()

    @staticmethod
    def review_reply(verdict="PASS", findings=None, questions=None):
        return json.dumps({"verdict": verdict, "findings": findings or [],
                           "questions": questions or []}), "session-example"

    def test_shared_context_and_review_followup(self):
        with patch.object(run_dsh, "ask_dsh", side_effect=[
            ("An independent approach.", "session-advice"),
            self.review_reply("FAIL", findings=["first spelling lost"]),
            self.review_reply(),
        ]) as call:
            self.assertEqual(self.run_main("--context", str(self.context), "--advice", "Compare."), 0)
            self.context.unlink()  # Round 1 must use the saved copy.
            self.assertEqual(self.run_main(*self.review_args), 2)
            first_brief = self.record / "review-1.brief.txt"
            original_brief = first_brief.read_bytes()
            self.context.write_text("Fact: one-shot iterable. Added proof: test X.", encoding="utf-8")
            self.source.write_text("REVISED", encoding="utf-8")
            decisions = self.source.with_name("decisions.txt")
            decisions.write_text("Fixed; confirmed by example X.", encoding="utf-8")
            self.assertEqual(self.run_main(*self.review_args, "--round", "2", "--context", str(self.context),
                                           "--decisions", str(decisions)), 0)
        for invocation in call.call_args_list:
            prompt = invocation.args[-1]
            self.assertIn("Acceptance: preserve first spelling", prompt)
            self.assertIn("Fact: one-shot iterable", prompt)
        final_prompt = call.call_args.args[-1]
        self.assertIn("REVISED", final_prompt)
        self.assertIn("first spelling lost", final_prompt)
        self.assertIn("confirmed by example X", final_prompt)
        self.assertIn("Added proof: test X", final_prompt)
        self.assertEqual(first_brief.read_bytes(), original_brief)
        self.assertNotIn(b"Added proof", original_brief)
        snapshot = (self.record / "review-2.source.txt").read_bytes()
        report = json.loads((self.record / "review-2.json").read_text(encoding="utf-8"))
        self.assertEqual(report["snapshot_sha256"], hashlib.sha256(snapshot).hexdigest())

    def test_changed_goal_requires_new_record_before_model_call(self):
        with patch.object(run_dsh, "ask_dsh", return_value=("Plan", "session")) as call:
            self.assertEqual(self.run_main("--advice", "What should we do?"), 0)
            self.goal.write_text("Different acceptance criteria.", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Shared goal changed"):
                self.run_main(*self.review_args)
            call.assert_called_once()

    def test_windows_utf8_bom_does_not_change_shared_goal(self):
        with patch.object(run_dsh, "ask_dsh", side_effect=[
            ("A plan", "session-advice"), self.review_reply(),
        ]) as call:
            self.assertEqual(self.run_main("--advice", "Compare."), 0)
            goal = self.goal.read_text(encoding="utf-8")
            self.goal.write_text(goal, encoding="utf-8-sig")
            self.assertEqual(self.run_main(*self.review_args), 0)
            self.assertNotIn("\ufeff", call.call_args.args[-1])

    def test_rejected_round_keeps_previous_context(self):
        with patch.object(run_dsh, "ask_dsh", return_value=self.review_reply()) as call:
            self.assertEqual(self.run_main(*self.review_args, "--context", str(self.context)), 0)
            prior = (self.record / "shared-context.txt").read_bytes()
            self.context.write_text("Context from an accidental duplicate.", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "already has a result"):
                self.run_main(*self.review_args, "--context", str(self.context))
            self.assertEqual((self.record / "shared-context.txt").read_bytes(), prior)
            call.assert_called_once()

    def test_second_round_cannot_switch_workspaces(self):
        other = self.source.parent / "other"
        other.mkdir()
        (other / self.source.name).write_text("UNRELATED", encoding="utf-8")
        with patch.object(run_dsh, "ask_dsh", return_value=self.review_reply()) as call:
            self.assertEqual(self.run_main(*self.review_args), 0)
            with self.assertRaisesRegex(RuntimeError, "Review workspace changed"):
                self.run_main("--workspace", str(other), "--source", self.source.name,
                              "--round", "2")
            call.assert_called_once()

    def test_only_completed_review_gets_a_canonical_source_snapshot(self):
        with patch.object(run_dsh, "ask_dsh", side_effect=[
            RuntimeError("DSH timed out"), self.review_reply(),
        ]):
            with self.assertRaisesRegex(RuntimeError, "DSH timed out"):
                self.run_main(*self.review_args)
            saved = self.record / "review-1.source.txt"
            self.assertFalse(saved.exists())
            self.assertTrue((self.record / "review-workspace.txt").is_file())
            with self.assertRaisesRegex(RuntimeError, "Round 1 has no completed review"):
                self.run_main(*self.review_args, "--round", "2")
            self.source.write_text("NEW ATTEMPT", encoding="utf-8")
            self.assertEqual(self.run_main(*self.review_args), 0)
            self.assertIn(b"NEW ATTEMPT", saved.read_bytes())

    def test_questions_prevent_automatic_acceptance(self):
        with patch.object(run_dsh, "ask_dsh", return_value=self.review_reply(
                questions=["Need the actual specification."],
        )), patch.object(run_dsh, "run_stage", return_value=(0, "")):
            self.assertEqual(self.run_main(*self.review_args, "--check", "python", "-B"), 2)

    def test_changed_artifact_is_not_accepted(self):
        def mutate(*args, **kwargs):
            self.source.write_text("CHANGED AFTER REVIEW", encoding="utf-8")
            return 0, ""
        with patch.object(run_dsh, "ask_dsh", return_value=self.review_reply()), \
                patch.object(run_dsh, "run_stage", side_effect=mutate):
            self.assertEqual(self.run_main(*self.review_args, "--check", "python", "-B"), 2)

    def test_unreadable_post_check_snapshot_is_reported_as_changed(self):
        for kind in ("missing", "invalid UTF-8"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source.py"
                source.write_text("ORIGINAL", encoding="utf-8")
                record = root / "record"
                argv = ["runner", "--goal", str(self.goal), "--run-dir", str(record),
                        "--workspace", str(root), "--source", source.name,
                        "--check", "python", "-B"]

                def acceptance(*args, **kwargs):
                    if kind == "missing":
                        source.rename(root / "renamed.py")
                    else:
                        source.write_bytes(b"\xff")
                    return 0, ""

                output = io.StringIO()
                with patch.object(sys, "argv", argv), patch.object(run_dsh, "check_plugin"), \
                        patch.object(run_dsh, "ask_dsh", return_value=self.review_reply()), \
                        patch.object(run_dsh, "run_stage", side_effect=acceptance), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(run_dsh.main(), 2)
                self.assertIn("SNAPSHOT_CHANGED", output.getvalue())


if __name__ == "__main__":
    unittest.main()
