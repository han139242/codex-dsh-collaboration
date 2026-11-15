import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_dsh


SESSION = "session-00000000-0000-0000-0000-000000000001"
OTHER_SESSION = "session-00000000-0000-0000-0000-000000000002"


def model_started(session_id=SESSION):
    return json.dumps({"type": "session", "sessionId": session_id}).encode("utf-8") + b"\n"


class RunnerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
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

    def test_run_stage_delivers_exact_prompt_and_records_wait_then_exit(self):
        self.record.mkdir()
        script = "import sys,time; time.sleep(.45); sys.stdout.buffer.write(sys.stdin.buffer.read())"
        output = io.StringIO()
        prompt = "中文\n  保留空格\n"
        with patch.object(run_dsh, "PROGRESS_INTERVAL", .1), contextlib.redirect_stdout(output):
            code, answer = run_dsh.run_stage("review", [sys.executable, "-c", script],
                                             workspace=self.record, out=self.record,
                                             prompt=prompt, timeout=5)
        self.assertEqual((code, answer), (0, prompt))
        self.assertIn("仍在运行", output.getvalue())
        status = (self.record / "review.status.md").read_text(encoding="utf-8")
        self.assertIn("进程已退出", status)
        self.assertIn("[发给 DSH 的请求](review.prompt.txt)", status)

    def test_timeout_stops_descendant_and_keeps_partial_output(self):
        self.record.mkdir()
        leaf = (
            "import pathlib,time; pathlib.Path('child-started').touch(); "
            "time.sleep(2.5); pathlib.Path('child-finished').touch()"
        )
        parent = (
            f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{leaf!r}]); "
            "print('partial',flush=True); time.sleep(8)"
        )
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(subprocess.TimeoutExpired) as error:
            run_dsh.run_stage("slow", [sys.executable, "-c", parent],
                              workspace=self.record, out=self.record, timeout=1)
        self.assertIn(b"partial", error.exception.stdout)
        self.assertTrue((self.record / "child-started").exists())
        time.sleep(2)
        self.assertFalse((self.record / "child-finished").exists())
        status = (self.record / "slow.status.md").read_text(encoding="utf-8")
        self.assertIn("进程超时已结束", status)

    def test_completed_reply_does_not_wait_for_a_lingering_process(self):
        self.record.mkdir()
        final = json.dumps({"type": "final", "reason": {"kind": "completed"},
                            "text": "完整答复", "sessionId": "session-example"})
        script = f"import time; print({final!r}, flush=True); time.sleep(10)"
        with patch.object(run_dsh, "PROGRESS_INTERVAL", .2), contextlib.redirect_stdout(io.StringIO()):
            code, output = run_dsh.run_stage("review", [sys.executable, "-c", script],
                                             workspace=self.record, out=self.record, timeout=4,
                                             accept_completed_reply=True)
        self.assertEqual(code, 0)
        self.assertEqual(run_dsh.completed_response(run_dsh.read_cli_events(output)),
                         ("完整答复", "session-example"))
        status = (self.record / "review.status.md").read_text(encoding="utf-8")
        self.assertIn("已收到完整模型回复", status)

    def test_desktop_write_handle_conflict_stops_route_retry(self):
        self.record.mkdir()
        script = "import sys; print('session is already owned by an active write handle', file=sys.stderr); sys.exit(1)"
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "已停止重试"):
            run_dsh.run_stage("review", [sys.executable, "-c", script], workspace=self.record,
                              out=self.record, timeout=5, accept_completed_reply=True)

    @staticmethod
    def review_reply(verdict="PASS", findings=None, questions=None):
        status = "需修订" if verdict == "FAIL" else "待确认" if questions else "通过"
        issues = "\n".join(f"- {item}" for item in findings or []) or "无"
        doubts = "\n".join(f"- {item}" for item in questions or []) or "无"
        return (f"## 结论\n{status}\n\n## 核查依据\n核对了首次输入拼写的保留规则。"
                f"\n\n## 发现的问题\n{issues}\n\n## 待确认\n{doubts}\n"), SESSION

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
        self.assertIn("核对了首次输入拼写", report["evidence"])
        self.assertIn("## 核查依据", (self.record / "review-2.md").read_text(encoding="utf-8"))
        self.assertIn("An independent approach.", (self.record / "advice.md").read_text(encoding="utf-8"))
        self.assertIn("## 核查依据", final_prompt)

    def test_bare_or_contradictory_review_does_not_count_as_completed(self):
        answer, _ = self.review_reply(findings=["一条实际缺陷。"])
        for invalid in ('{"verdict":"PASS","findings":[],"questions":[]}', answer):
            with self.subTest(invalid=invalid), \
                    patch.object(run_dsh, "ask_dsh", return_value=(invalid, "session-example")):
                with self.assertRaisesRegex(RuntimeError, "DSH 原文已保存"):
                    self.run_main(*self.review_args)
                self.assertEqual((self.record / "review-1.rejected.md").read_text(encoding="utf-8").strip(),
                                 invalid.strip())
                self.assertFalse((self.record / "review-1.json").exists())

    def test_review_preserves_markdown_details_in_a_finding(self):
        finding = "当前实现会漏掉规范等价写法。\n\n  ```python\n  name.casefold()\n  ```\n\n  需补规范化。"
        answer, _ = self.review_reply("FAIL", findings=[finding, "第二条问题。"])
        self.assertEqual(run_dsh.parse_review(answer)["findings"], [finding, "第二条问题。"])

    def test_round_two_accepts_legacy_report(self):
        self.record.mkdir()
        recorded_workspace = self.record / ".."
        (self.record / "review-workspace.txt").write_text(str(recorded_workspace), encoding="utf-8")
        (self.record / "review-1.json").write_text(json.dumps(
            {"verdict": "FAIL", "findings": ["旧报告里的问题"], "questions": []}
        ), encoding="utf-8")
        with patch.object(run_dsh, "ask_dsh", return_value=self.review_reply()) as model:
            self.assertEqual(self.run_main(*self.review_args, "--round", "2"), 0)
        prompt = model.call_args.args[-1]
        self.assertIn("- 旧报告里的问题", prompt)
        self.assertNotIn('"verdict":', prompt)

    def test_review_gets_current_evidence_separately_from_decisions(self):
        evidence = self.source.with_name("evidence.txt")
        evidence.write_text("python -B verify.py：3 项通过。", encoding="utf-8-sig")
        with patch.object(run_dsh, "ask_dsh", return_value=self.review_reply()) as model:
            self.assertEqual(self.run_main(*self.review_args, "--evidence", str(evidence)), 0)
        prompt = model.call_args.args[-1]
        self.assertIn("Codex 在当前版本上取得的验收证据：\npython -B verify.py：3 项通过。", prompt)
        self.assertIn("INITIAL", prompt)

    def test_benchmark_survives_background_update_and_can_be_withdrawn(self):
        benchmark = self.source.with_name("benchmark.md")
        excerpt = "官方来源：https://example.org/reference\n保留首次拼写。"
        benchmark.write_text(excerpt, encoding="utf-8-sig")
        with patch.object(run_dsh, "ask_dsh", return_value=self.review_reply()) as model:
            self.assertEqual(self.run_main("--advice", "比较", "--benchmark", str(benchmark)), 0)
            self.assertEqual(self.run_main(*self.review_args, "--context", str(self.context)), 0)
            self.assertEqual(self.run_main(*self.review_args, "--round", "2", "--clear-benchmark"), 0)
        advice, review, withdrawn = [call.args[-1] for call in model.call_args_list]
        self.assertIn(excerpt, advice)
        self.assertIn(excerpt, review)
        self.assertIn("Fact: one-shot iterable", review)
        self.assertNotIn(excerpt, withdrawn)
        self.assertIn("Acceptance: preserve first spelling", withdrawn)
        self.assertIn(excerpt, (self.record / "review-1.brief.txt").read_text(encoding="utf-8"))

    def test_whitespace_only_saved_benchmark_does_not_enter_prompt(self):
        self.record.mkdir()
        (self.record / "shared-benchmark.md").write_text(" \n\t", encoding="utf-8-sig")
        with patch.object(run_dsh, "ask_dsh", return_value=self.review_reply()) as model:
            self.assertEqual(self.run_main(*self.review_args), 0)
        self.assertNotIn("项目对标材料", model.call_args.args[-1])
        self.assertNotIn("\ufeff", model.call_args.args[-1])

    def test_replace_and_clear_benchmark_flags_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
            self.run_main("--advice", "比较", "--benchmark", "unused.md", "--clear-benchmark")
        self.assertEqual(error.exception.code, 2)
        self.assertIn("not allowed with argument", output.getvalue())
        self.assertFalse(self.record.exists())

    def test_pending_desktop_request_cannot_replace_or_withdraw_benchmark(self):
        benchmark = self.source.with_name("benchmark.md")
        benchmark.write_text("旧对标", encoding="utf-8")
        timeout = subprocess.TimeoutExpired("fake-dsh", 1, output=model_started())
        with patch.object(run_dsh, "dsh_command", return_value=["fake-dsh"]), \
                patch.object(run_dsh, "run_stage", side_effect=timeout) as stage:
            with self.assertRaises(run_dsh.DesktopFallbackRequired):
                self.run_main(*self.review_args, "--route", "cn-flash", "--benchmark", str(benchmark))
            benchmark.write_text("更新后的对标", encoding="utf-8")
            for flags in [("--benchmark", str(benchmark)), ("--clear-benchmark",)]:
                with self.subTest(flags=flags), self.assertRaisesRegex(RuntimeError, "Task or snapshot changed"):
                    self.run_main(*self.review_args, *flags)
            self.assertEqual((self.record / "shared-benchmark.md").read_text(encoding="utf-8"), "旧对标")
            stage.assert_called_once()

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

    def test_model_timeout_handoff_import_requires_unchanged_request(self):
        headless_id = "session-00000000-0000-0000-0000-000000000001"
        desktop_id = headless_id
        event = json.dumps({"type": "session", "sessionId": headless_id}).encode("utf-8")
        timeout = subprocess.TimeoutExpired("fake-dsh", 1, output=event)
        with patch.object(run_dsh, "dsh_command", return_value=["fake-dsh"]), \
                patch.object(run_dsh, "run_stage", side_effect=timeout) as stage:
            with self.assertRaisesRegex(run_dsh.DesktopFallbackRequired, "DSH_DESKTOP_REQUIRED"):
                self.run_main(*self.review_args, "--route", "cn-flash", "--timeout", "1")
        self.assertEqual(stage.call_args.kwargs["timeout"], 1)
        handoff = json.loads((self.record / "review-1.desktop.json").read_text(encoding="utf-8"))
        request = Path(handoff["request"])
        self.assertEqual(Path(handoff["workspace"]), self.record)
        self.assertFalse((self.record.parent / "shared-desktop").exists())
        self.assertEqual(handoff["session_id"], headless_id)
        self.assertEqual(request.parent, self.record)
        self.assertIn(handoff["request_id"], request.read_text(encoding="utf-8"))
        self.assertIn("INITIAL", request.read_text(encoding="utf-8"))
        self.assertEqual(handoff["headless_session_id"], headless_id)
        self.assertFalse((self.record / "review-1.json").exists())

        reply = self.review_reply(questions=["请核对输入。"])[0]
        flags = ("--desktop-session-id", desktop_id)
        with patch.object(run_dsh, "run_stage") as stage, \
                patch.object(run_dsh, "recover_cli_reply", return_value=None), \
                patch.object(run_dsh, "verified_session_reply", return_value=reply) as extract:
            with self.assertRaisesRegex(run_dsh.DesktopFallbackRequired, "already prepared"):
                self.run_main(*self.review_args)
            with self.assertRaisesRegex(RuntimeError, "已绑定的 DSH 会话"):
                self.run_main(*self.review_args, "--desktop-session-id", OTHER_SESSION)
            self.source.write_text("UPDATED", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Task or snapshot changed"):
                self.run_main(*self.review_args, *flags)
            self.source.write_text("INITIAL", encoding="utf-8")
            exact_request = request.read_bytes()
            request.write_bytes(exact_request + b"\nEdited")
            with self.assertRaisesRegex(RuntimeError, "saved desktop request was edited"):
                self.run_main(*self.review_args, *flags)
            request.write_bytes(exact_request)
            answer = self.record / "desktop-reply.txt"
            answer.write_text("不相关的回复", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "回复文件与 DSH 会话"):
                self.run_main(*self.review_args, *flags, "--desktop-answer", str(answer))
            self.assertEqual(self.run_main(*self.review_args, *flags), 2)
            extract.assert_called_with(Path(handoff["workspace"]), exact_request.decode("utf-8"),
                                       desktop_id, run_dsh.DEFAULT_ROUTE)
            stage.assert_not_called()
        report = json.loads((self.record / "review-1.json").read_text(encoding="utf-8"))
        self.assertEqual(report["transport"], "desktop")
        self.assertEqual(report["session_id"], desktop_id)
        self.assertEqual(report["questions"], ["请核对输入。"])

    def test_identical_tasks_keep_distinct_conversations_and_request_ids(self):
        other = self.record.parent / "other"
        self.record.mkdir()
        other.mkdir()
        timeouts = [subprocess.TimeoutExpired("fake-dsh", 1, output=model_started(session))
                    for session in (SESSION, OTHER_SESSION)]
        with patch.object(run_dsh, "dsh_command", return_value=["fake-dsh"]), \
                patch.object(run_dsh, "run_stage", side_effect=timeouts):
            for folder in [self.record, other]:
                with self.assertRaises(run_dsh.DesktopFallbackRequired):
                    run_dsh.ask_dsh(folder, None, "review-1", "同一个问题")
        first, second = [json.loads((folder / "review-1.desktop.json").read_text(encoding="utf-8"))
                         for folder in [self.record, other]]
        self.assertNotEqual(first["workspace"], second["workspace"])
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertNotEqual(first["prompt_sha256"], second["prompt_sha256"])

    def test_workbuddy_routes_select_only_the_requested_model_and_region(self):
        self.record.mkdir()
        with patch.object(run_dsh, "check_plugin"):
            for route, (provider, model) in run_dsh.ROUTES.items():
                with self.subTest(route=route):
                    patch_file = run_dsh.put_overlay(self.record, route)
                    bundle, agent, *disabled = json.loads(patch_file.read_text(encoding="utf-8"))
                    regions = bundle["insert"][0]["config"]["regions"]
                    enabled, = [name for name, config in regions.items() if config["enabled"]]
                    self.assertEqual(enabled, "global" if provider == "workbuddy-global" else "cn")
                    self.assertEqual(regions[enabled]["enabledModelIds"], [model])
                    self.assertEqual(agent["config"], {"provider": provider, "model": model})
                    self.assertIn({"id": "llm-deepseek", "disabled": True}, disabled)
                    self.assertIn({"id": "llm-deepseek-account", "disabled": True}, disabled)

    def test_scoped_review_requires_tool_evidence_and_saves_real_route(self):
        answer, session_id = self.review_reply()
        final = {"type": "final", "reason": {"kind": "completed"},
                 "text": answer, "sessionId": session_id}
        call = {"type": "tool_call", "tool": "codex_inspect", "callId": "scope-1",
                "input": {"action": "read_text"}}
        result = {"type": "tool_result", "callId": "scope-1", "status": "completed"}
        completed = "\n".join(map(json.dumps, (call, result, final)))
        flags = (*self.review_args, "--inspect-file", str(self.source), "--route", "cn-flash")
        with patch.object(run_dsh, "dsh_command", return_value=["fake-dsh"]), \
                patch.object(run_dsh, "run_stage", side_effect=[(0, json.dumps(final)), (0, completed)]) as stage:
            with self.assertRaisesRegex(run_dsh.DSHResponseError, "没有完成限定范围"):
                self.run_main(*flags)
            self.assertFalse((self.record / "review-1.json").exists())
            self.assertEqual(self.run_main(*flags), 0)
        self.assertIn("先用 codex_inspect 实际取证", stage.call_args.kwargs["prompt"])
        overlay = json.loads((self.record / "workbuddy-cn-flash.json").read_text(encoding="utf-8"))
        self.assertEqual(overlay[-1]["insert"][0]["config"]["roots"],
                         [{"path": str(self.source), "text": True}])
        report = json.loads((self.record / "review-1.json").read_text(encoding="utf-8"))
        self.assertEqual(report["inspection"], "scoped-tools")

    def test_auto_retry_keeps_unusable_domestic_reply_and_labels_final_review(self):
        with patch.object(run_dsh, "ask_dsh", side_effect=[
            ("这条回复没有审查内容。", "session-cn"), self.review_reply(),
        ]) as model:
            self.assertEqual(self.run_main(*self.review_args), 0)
        self.assertEqual(model.call_args_list[0].args[-1], model.call_args_list[1].args[-1])
        self.assertIn("没有审查内容", (self.record / "review-1.rejected.md").read_text(encoding="utf-8"))
        report = json.loads((self.record / "review-1.json").read_text(encoding="utf-8"))
        self.assertEqual((report["provider"], report["model"], report["route"]),
                         ("workbuddy-global", run_dsh.MODEL, "global-flash"))

    def test_pending_evidence_pauses_review_until_same_session_reads_actual_results(self):
        answer, session_id = self.review_reply()
        request_id = "00000000-0000-0000-0000-000000000023"
        request = {"request_id": request_id, "kind": "file", "question": "默认值是什么？", "why": "影响验收"}
        results = [{"request_id": request_id, "status": "verified", "evidence": "配置所用默认值是 safe。"}]

        def completion(text, tool=None, payload=None):
            events = []
            if tool:
                events += [{"type": "tool_call", "tool": tool, "callId": "evidence"},
                           {"type": "tool_result", "callId": "evidence", "status": "completed",
                            "result": json.dumps(payload)}]
            events.append({"type": "final", "reason": {"kind": "completed"}, "text": text, "sessionId": session_id})
            return 0, "\n".join(map(json.dumps, events))

        flags = (*self.review_args, "--ask-codex")
        results_path = self.source.parent / "evidence.json"
        with patch.object(run_dsh, "dsh_command", side_effect=lambda *args: list(args)), \
                patch.object(run_dsh, "run_stage", side_effect=[
                    completion("需要查证", run_dsh.EVIDENCE_REQUEST, request),
                    completion(answer),
                    completion(answer, run_dsh.EVIDENCE_RESULTS, results),
                ]) as stage:
            with self.assertRaises(run_dsh.CodexEvidencePending):
                self.run_main(*flags)
            with self.assertRaises(run_dsh.CodexEvidencePending):
                self.run_main(*self.review_args)
            receipt = run_dsh.evidence_receipt(self.record, "review-1")
            self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["requests"], [request])
            results_path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "每条原请求"):
                self.run_main(*flags, "--codex-results", str(results_path))
            stage.assert_called_once()
            results_path.write_text(json.dumps(results), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "未在原会话完成证据复查"):
                self.run_main(*flags, "--codex-results", str(results_path))
            self.assertFalse((self.record / "review-1.json").exists())
            self.assertEqual(self.run_main(*flags, "--codex-results", str(results_path)), 0)
        self.assertIn("主动请 Codex 查证", stage.call_args_list[0].kwargs["prompt"])
        for invocation in stage.call_args_list[1:]:
            command = invocation.args[1]
            self.assertEqual(command[command.index("--session-id") + 1], session_id)
        report = json.loads((self.record / "review-1.json").read_text(encoding="utf-8"))
        self.assertEqual(report["codex_evidence"], receipt.name)

    def test_auto_cli_timeout_falls_back_in_order_before_returning_a_real_finding(self):
        timeout_output = model_started() + b'{"type":"status","phase":"step_start"}\n'
        timeout = subprocess.TimeoutExpired("fake-dsh", 1, output=timeout_output)
        answer, session_id = self.review_reply("FAIL", findings=["发现一条真实缺陷。"])
        final = json.dumps({"type": "final", "reason": {"kind": "completed"},
                            "text": answer, "sessionId": session_id})
        with patch.object(run_dsh, "dsh_command", side_effect=lambda *args: list(args)), \
                patch.object(run_dsh, "run_stage", side_effect=[timeout, timeout, (0, final)]) as call:
            self.assertEqual(self.run_main(*self.review_args, "--timeout", "1"), 2)
        labels = [stage.args[0] for stage in call.call_args_list]
        self.assertEqual(labels, ["review-1", "review-1.global-flash", "review-1.cn-hy4"])
        commands = [stage.args[1] for stage in call.call_args_list]
        self.assertNotIn("--session-id", commands[0])
        for command in commands[1:]:
            self.assertEqual(command[command.index("--session-id") + 1], SESSION)
        report = json.loads((self.record / "review-1.json").read_text(encoding="utf-8"))
        self.assertEqual((report["route"], report["transport"], report["verdict"]),
                         ("cn-hy4", "headless", "FAIL"))
        handoff = json.loads((self.record / "review-1.desktop.json").read_text(encoding="utf-8"))
        self.assertEqual(handoff["last_cli_phase"], "step_start")
        self.assertEqual(handoff["timeout_seconds"], 1)

    def test_auto_waits_for_all_cli_routes_before_requesting_desktop(self):
        timeout = subprocess.TimeoutExpired("fake-dsh", 1, output=model_started())
        with patch.object(run_dsh, "dsh_command", return_value=["fake-dsh"]), \
                patch.object(run_dsh, "run_stage", side_effect=timeout) as call:
            with self.assertRaisesRegex(run_dsh.DesktopFallbackRequired, r"review-1\.desktop\.json"):
                self.run_main(*self.review_args, "--timeout", "1")
            self.assertEqual(call.call_count, 4)
            with self.assertRaisesRegex(run_dsh.DesktopFallbackRequired, "already prepared"):
                self.run_main(*self.review_args, "--timeout", "1")
            self.assertEqual(call.call_count, 4)

    def test_unknown_startup_identity_does_not_launch_another_conversation(self):
        with patch.object(run_dsh, "dsh_command", return_value=["fake-dsh"]), \
                patch.object(run_dsh, "run_stage", side_effect=subprocess.TimeoutExpired("fake-dsh", 1)) as call:
            with self.assertRaisesRegex(RuntimeError, "未取得 DSH 会话 ID"):
                self.run_main(*self.review_args, "--timeout", "1")
            with self.assertRaisesRegex(RuntimeError, "未能确定唯一"):
                self.run_main(*self.review_args, "--timeout", "1")
            call.assert_called_once()

    def test_advice_and_review_share_a_session_and_reuse_the_working_route(self):
        reply, session_id = self.review_reply()
        completed = lambda text: json.dumps({"type": "final", "sessionId": session_id,
                                              "text": text, "reason": {"kind": "completed"}})
        error = json.dumps({"type": "final", "sessionId": session_id,
                            "reason": {"kind": "error", "error": {"code": "MODEL_BUSY"}}})
        with patch.object(run_dsh, "dsh_command", side_effect=lambda *args: list(args)), \
                patch.object(run_dsh, "run_stage", side_effect=[
                    (1, error), (0, completed("独立建议")), (0, completed(reply)),
                ]) as call:
            self.assertEqual(self.run_main("--advice", "提出测试"), 0)
            self.assertEqual(self.run_main(*self.review_args), 0)
        self.assertEqual([row.args[0] for row in call.call_args_list],
                         ["advice", "advice.global-flash", "review-1.global-flash"])
        for invocation in call.call_args_list[1:]:
            command = invocation.args[1]
            self.assertEqual(command[command.index("--session-id") + 1], session_id)

    def test_model_reported_unknown_is_skipped_on_future_automatic_calls(self):
        def error(code):
            return 1, json.dumps({"type": "final", "sessionId": SESSION,
                                  "reason": {"kind": "error", "error": {"code": code}}})

        reply, session_id = self.review_reply()
        success = (0, json.dumps({"type": "final", "sessionId": session_id,
                                  "text": reply, "reason": {"kind": "completed"}}))
        with patch.object(run_dsh, "dsh_command", side_effect=lambda *args: list(args)), \
                patch.object(run_dsh, "run_stage", side_effect=[
                    error("UNKNOWN_MODEL"), error("MODEL_BUSY"), error("MODEL_BUSY"), success,
                ]) as call:
            with self.assertRaisesRegex(run_dsh.DSHResponseError, "UNKNOWN_MODEL"):
                self.run_main("--advice", "独立思考", "--route", "cn-hy4")
            self.assertEqual(self.run_main(*self.review_args), 0)
        self.assertEqual([row.args[0] for row in call.call_args_list],
                         ["advice.cn-hy4", "review-1", "review-1.global-flash", "review-1.global-hy4"])

    def test_rejecting_a_concurrent_turn_releases_the_task_for_followup(self):
        with run_dsh.task_lock(self.record):
            with self.assertRaisesRegex(RuntimeError, "会话正在使用"):
                self.run_main("--advice", "同时发送")
        with run_dsh.task_lock(self.record):
            self.assertTrue(self.record.is_dir())

    def test_existing_dsh_workspace_keeps_one_conversation_and_separate_logs(self):
        group = self.record.parent / "DSH group"
        group.mkdir()
        output = "\n".join((json.dumps({"type": "session", "sessionId": SESSION, "cwd": str(group)}),
                             json.dumps({"type": "final", "sessionId": SESSION,
                                         "text": "独立建议", "reason": {"kind": "completed"}})))
        with patch.object(run_dsh, "attach_dsh_workspace") as attach, \
                patch.object(run_dsh, "dsh_command", side_effect=lambda *args: list(args)), \
                patch.object(run_dsh, "run_stage", return_value=(0, output)) as call:
            self.assertEqual(self.run_main("--advice", "第一个问题", "--dsh-workspace", str(group)), 0)
            self.assertEqual(self.run_main("--advice", "接着讨论", "--advice-label", "next"), 0)
            with self.assertRaisesRegex(RuntimeError, "DSH 工作区与"):
                self.run_main("--advice", "更换目录", "--advice-label", "other",
                              "--dsh-workspace", str(self.source.parent))
        for turn in call.call_args_list:
            self.assertEqual(turn.kwargs["workspace"], group)
            self.assertEqual(turn.kwargs["out"], self.record)
        self.assertNotIn("--session-id", call.call_args_list[0].args[1])
        self.assertIn(SESSION, call.call_args_list[1].args[1])
        self.assertEqual(attach.call_count, 2)
        attach.assert_called_with(self.record, group, SESSION)

    def test_workspace_membership_is_registered_without_creating_a_model_turn(self):
        group = self.record.parent / "DSH group"
        group.mkdir()
        self.record.mkdir()
        storages = self.record.parent / "storages"
        storages.mkdir()
        path = storages / "workspace.json"
        records = {"tables": {"workspaces": {"demo": {"path": str(group), "sessionIds": []}}}}
        path.write_text(json.dumps(records), encoding="utf-8")

        def simulated_registry(command, **options):
            self.assertIs(options["stdin"], subprocess.DEVNULL)
            self.assertEqual(options["cwd"], group)
            patch_file = Path(command[command.index("--patch") + 1])
            config = json.loads(patch_file.read_text(encoding="utf-8"))[-1]["insert"][-1]["config"]
            self.assertEqual(config, {"workspace": str(group), "sessionId": SESSION})
            records["tables"]["workspaces"]["demo"]["sessionIds"] = [SESSION]
            path.write_text(json.dumps(records), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="attached", stderr="")

        with patch.object(run_dsh, "DSH_ROOT", self.record.parent), \
                patch.object(run_dsh, "dsh_command", side_effect=lambda *args: list(args)), \
                patch.object(run_dsh.subprocess, "run", side_effect=simulated_registry) as attach:
            run_dsh.attach_dsh_workspace(self.record, group, SESSION)
            run_dsh.attach_dsh_workspace(self.record, group, SESSION)
        attach.assert_called_once()

    def test_desktop_reply_requires_matching_workspace_prompt_and_model(self):
        session_id = "session-00000000-0000-0000-0000-000000000002"
        route = {"provider": run_dsh.PROVIDER, "model": run_dsh.MODEL}
        prompt = "保留代码缩进。\r\n\r\n    return item\r\n"
        reply = self.review_reply()[0]
        header = {"type": "session", "version": 4, "id": session_id, "cwd": str(self.record)}
        user = {"source": {"kind": "user"}, "content": [
            {"type": "text", "text": prompt.replace("\r\n", "\n").strip()},
        ]}
        config = {"header": {"config": route.copy()}}
        message = {"source": {"kind": "model", **route}, "content": [
            {"type": "reasoning", "text": "private reasoning"},
            {"type": "text", "text": reply},
        ]}
        end = {"turn": 1, "reason": {"kind": "completed"}}
        trace = [header, *({"type": kind, "data": row} for kind, row in [
            ("turn/start", {"turn": 1}),
            ("user/message", user),
            ("user/message", {"source": {"kind": "runtime-context"}}),
            ("request/header", config),
            ("assistant/message", {"turn": 1, "message": message}),
            ("turn/end", end),
        ])]
        with patch.object(run_dsh, "read_session_events", return_value=trace):
            self.assertEqual(run_dsh.verified_session_reply(self.record, prompt, session_id), reply)
            prior = [
                {"type": "turn/start", "data": {"turn": 0}},
                {"type": "user/message", "data": {"source": {"kind": "user"}, "content": [
                    {"type": "text", "text": "上一个问题"},
                ]}},
                {"type": "request/header", "data": {"header": {"config": {
                    "provider": "workbuddy-global", "model": run_dsh.MODEL,
                }}}},
                {"type": "turn/end", "data": {"turn": 0, "reason": {"kind": "interrupted"}}},
            ]
            trace[1:1] = prior
            self.assertEqual(run_dsh.verified_session_reply(self.record, prompt, session_id), reply)
            global_provider, hy4 = run_dsh.ROUTES["global-hy4"]
            international = {"provider": global_provider, "model": hy4}
            with patch.dict(config["header"]["config"], international), \
                    patch.dict(message["source"], international):
                self.assertEqual(run_dsh.verified_session_reply(
                    self.record, prompt, session_id, "global-hy4"
                ), reply)
                with self.assertRaisesRegex(RuntimeError, "模型与"):
                    run_dsh.verified_session_reply(self.record, prompt, session_id)
            for row, field, changed, error in [
                (header, "cwd", str(self.source.parent), "工作区不匹配"),
                (user["content"][0], "text", prompt.replace("    return", "  return"), "原始请求不匹配"),
                (config["header"]["config"], "provider", "workbuddy-global", "模型与"),
                (message["source"], "model", "other-model", "模型与"),
                (end["reason"], "kind", "cancelled", "已经完成"),
            ]:
                with self.subTest(field=field, changed=changed), patch.dict(row, {field: changed}):
                    with self.assertRaisesRegex(RuntimeError, error):
                        run_dsh.verified_session_reply(self.record, prompt, session_id)

    def test_timeout_with_completed_cli_response_does_not_prepare_handoff(self):
        reply, session_id = self.review_reply()
        final = json.dumps({"type": "final", "sessionId": session_id, "text": reply,
                            "reason": {"kind": "completed"}}).encode("utf-8")
        timeout = subprocess.TimeoutExpired("fake-dsh", 1, output=final + b'\n{"partial":')
        with patch.object(run_dsh, "dsh_command", return_value=["fake-dsh"]), \
                patch.object(run_dsh, "run_stage", side_effect=timeout):
            self.assertEqual(self.run_main(*self.review_args, "--timeout", "1"), 0)
        self.assertFalse((self.record / "review-1.desktop.json").exists())

    def test_timeout_recovers_verified_saved_cli_reply_without_desktop(self):
        reply = self.review_reply()[0]
        session_id = "session-00000000-0000-0000-0000-000000000001"
        event = json.dumps({"type": "session", "sessionId": session_id}).encode("utf-8")
        timeout = subprocess.TimeoutExpired("fake-dsh", 1, output=event)
        with patch.object(run_dsh, "dsh_command", return_value=["fake-dsh"]), \
                patch.object(run_dsh, "run_stage", side_effect=timeout), \
                patch.object(run_dsh, "verified_session_reply", return_value=reply) as recover:
            self.assertEqual(self.run_main(*self.review_args, "--timeout", "1"), 0)
        self.assertEqual(recover.call_args.args[0], self.record)
        self.assertEqual(recover.call_args.args[2], session_id)
        self.assertFalse((self.record / "review-1.desktop.json").exists())

    def test_desktop_import_keeps_first_verified_session_and_reply(self):
        self.record.mkdir()
        tag = "review-1"
        run_dsh.bind_desktop_reply(self.record, tag, "session-a", "一致的回答")
        run_dsh.bind_desktop_reply(self.record, tag, "session-a", "一致的回答")
        for session, answer in [("session-b", "一致的回答"), ("session-a", "变更的回答")]:
            with self.subTest(session=session, answer=answer), self.assertRaisesRegex(RuntimeError, "已绑定"):
                run_dsh.bind_desktop_reply(self.record, tag, session, answer)
        self.assertEqual(len(list(self.record.iterdir())), 1)

    def test_parallel_desktop_imports_publish_only_one_binding(self):
        self.record.mkdir()

        def attempt(session):
            try:
                run_dsh.bind_desktop_reply(self.record, "review-1", session, "审查结论")
            except RuntimeError as error:
                self.assertIn("已绑定", str(error))
                return None
            return session

        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted = [session for session in pool.map(attempt, ["session-a", "session-b"]) if session]
        self.assertEqual(len(accepted), 1)
        binding = json.loads((self.record / "review-1.desktop-import.json").read_text(encoding="utf-8"))
        self.assertEqual(binding["session_id"], accepted[0])

    def test_collaboration_prompts_request_chinese(self):
        with patch.object(run_dsh, "ask_dsh", side_effect=[self.review_reply(), ("方案", "session")]) as call:
            self.assertEqual(self.run_main(*self.review_args), 0)
            self.assertEqual(self.run_main("--advice", "比较方案"), 0)
        for invocation in call.call_args_list:
            prompt = invocation.args[-1]
            self.assertIn("简体中文", prompt)
            self.assertIn("共同目标", prompt)

    def test_local_check_timeout_does_not_prepare_desktop_handoff(self):
        with patch.object(run_dsh, "ask_dsh", return_value=self.review_reply()), \
                patch.object(run_dsh, "run_stage", side_effect=subprocess.TimeoutExpired("check", 240)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_main(*self.review_args, "--check", "python", "-B")
        self.assertFalse((self.record / "review-1.desktop.json").exists())

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
