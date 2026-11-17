"""Exchange DSH test requests and Codex evidence in an existing conversation."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from bridge_protocol import tool_results, verify_requests as check_requests, verify_results as check_results
import run_dsh as dsh


REQUEST_RECEIPT = "bridge-test-requests.json"
REQUEST_TOOL = "codex_request_test"
RESULTS_TOOL = "codex_test_results"


def verify_requests(requests):
    return check_requests(requests, ("title", "input", "expected"))


def verify_results(requests, results):
    check_results(verify_requests(requests), results, ("passed", "failed", "not_run"))


def bridge_turn(out, workspace, session_id, route, roots, mode, results, timeout):
    overlay_path = dsh.put_overlay(out, route, inspect_files=roots)
    overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    config = {"mode": mode}
    if results is not None:
        config["results"] = results
    if roots:
        overlay[-1]["insert"][0]["config"]["testBridge"] = config
    else:
        overlay.append({"insert": [{
            "id": "codex-scoped-inspection", "name": (dsh.HERE / "scoped_inspection.mjs").as_posix(),
            "config": {"testBridge": config},
        }]})
    stage = f"bridge-{mode}"
    path = out / f"{stage}.overlay.json"
    path.write_text(json.dumps(overlay, ensure_ascii=False), encoding="utf-8")
    if mode == "request":
        prompt = (
            "沿用本会话的用户目标。像直接回答用户一样独立提出最值得由 Codex 实测的一个或两个测试，"
            "优先选当前证据尚不能直接回答的边界。具体写明输入和预期结果。"
            "用 codex_request_test 分别提交。工具只登记请求；Codex 核实后负责执行。"
            "可用 codex_inspect 查授权的项目文件。最后用中文解释用例能区分什么。"
        )
    else:
        prompt = (
            "继续本会话，调用 codex_test_results 读取 Codex 对你此前测试请求的实际结果。"
            "逐条核实结果是否回答了你的问题、覆盖了什么边界，还有没有影响验收的缺口。"
            "用简体中文给出独立结论，不用提出新的测试请求。"
        )
    command = dsh.dsh_command("headless", "--patch", str(path), "--json", "--permission-mode",
                              "read-only", "--approval-policy", "never", "--session-id", session_id, "-")
    try:
        code, output = dsh.run_stage(stage, command, workspace=workspace, out=out, prompt=prompt,
                                     timeout=timeout, env=dsh.dsh_environment(), accept_completed_reply=True)
    except subprocess.TimeoutExpired as error:
        code = 0
        output = (error.stdout or b"").decode("utf-8", errors="replace")
    events = dsh.read_cli_events(output)
    final = dsh.completed_response(events)
    if (code or final is None or final[1] != session_id
            or dsh.cli_session(events, workspace) != session_id):
        raise RuntimeError("原会话的工具交换未完成，原始轨迹已保留。")
    if mode == "request":
        proposals = tool_results(events, REQUEST_TOOL)
        verify_requests(proposals)
        (out / REQUEST_RECEIPT).write_text(
            json.dumps({"session_id": session_id, "requests": proposals}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    elif results not in tool_results(events, RESULTS_TOOL):
        raise RuntimeError("未确认 DSH 已通过工具读回完整实测结果。")
    (out / f"{stage}.md").write_text(final[0].strip() + "\n", encoding="utf-8")
    dsh.remember_cli_session(out, workspace, events, session_id,
                             attach=(out / "dsh-workspace.txt").is_file())
    dsh.save_conversation(out, session_id, route)
    print(f"工具交换已完成：{out / (stage + '.md')}（原会话 {session_id}）", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--inspect-file", type=Path, action="append", default=[])
    parser.add_argument("--results", type=Path, help="Codex 实测记录。省略时请 DSH 提交测试请求。")
    parser.add_argument("--route", choices=tuple(dsh.ROUTES))
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    out = args.run_dir.resolve()
    if not (out / dsh.CONVERSATION_RECORD).is_file():
        parser.error("工具桥只接续已有的 DSH 会话。")
    if args.timeout < 1 or any(not p.is_absolute() or not p.is_file() or p.is_symlink()
                               for p in args.inspect_file):
        parser.error("需要正数超时和已授权的物理绝对文件路径。")
    with dsh.task_lock(out):
        state = dsh.conversation(out)
        session_id = state.get("session_id")
        if not session_id:
            parser.error("任务尚未绑定 DSH 会话。")
        workspace_file = out / "dsh-workspace.txt"
        workspace = Path(workspace_file.read_text(encoding="utf-8")).resolve() if workspace_file.exists() else out
        requests_path = out / REQUEST_RECEIPT
        results = None
        mode = "request"
        if args.results is not None:
            saved = json.loads(requests_path.read_text(encoding="utf-8"))
            if saved.get("session_id") != session_id:
                raise RuntimeError("测试请求与当前 DSH 会话不一致。")
            results = json.loads(args.results.read_text(encoding="utf-8-sig"))
            verify_results(saved["requests"], results)
            mode = "results"
        if (out / f"bridge-{mode}.md").exists():
            raise RuntimeError("本次工具交换已经完成，请使用保存的结果。")
        route = args.route or state.get("usable_route") or dsh.DEFAULT_ROUTE
        bridge_turn(out, workspace, session_id, route, tuple(p.resolve() for p in args.inspect_file),
                    mode, results, args.timeout)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        sys.exit(str(error))
