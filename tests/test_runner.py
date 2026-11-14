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
            self.source.write_text("REVISED", encoding="utf-8")
            decisions = self.source.with_name("decisions.txt")
            decisions.write_text("Fixed; confirmed by example X.", encoding="utf-8")
            self.assertEqual(self.run_main(*self.review_args, "--round", "2",
                                           "--decisions", str(decisions)), 0)
        for invocation in call.call_args_list:
            prompt = invocation.args[-1]
            self.assertIn("Acceptance: preserve first spelling", prompt)
            self.assertIn("Fact: one-shot iterable", prompt)
        final_prompt = call.call_args.args[-1]
        self.assertIn("REVISED", final_prompt)
        self.assertIn("first spelling lost", final_prompt)
        self.assertIn("confirmed by example X", final_prompt)
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


if __name__ == "__main__":
    unittest.main()
