import argparse
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

from bridge_protocol import EVIDENCE_REQUEST, EVIDENCE_RESULTS, evidence_request_ids, tool_results, verify_results
from dsh_progress import RunTrace, has_local_shell_failure, write_progress
import sidebar


HERE = Path(__file__).resolve().parent
MODEL = "deepseek-v4.1-flash"
PROVIDER = "workbuddy"
DEFAULT_ROUTE = "cn-flash"
HY4 = "hy4-preview"
ROUTES = {
    DEFAULT_ROUTE: (PROVIDER, MODEL),
    "global-flash": ("workbuddy-global", MODEL),
    "cn-hy4": (PROVIDER, HY4),
    "global-hy4": ("workbuddy-global", HY4),
}
SESSION_ID = re.compile(r"session-[\da-fA-F]{8}(?:-[\da-fA-F]{4}){3}-[\da-fA-F]{12}")
MAX_REVIEWS = 2
DESKTOP_FALLBACK_EXIT = 3
CODEX_EVIDENCE_EXIT = 4
PROGRESS_INTERVAL = 15
EVENT_POLL_INTERVAL = 1
COMPLETED_EXIT_GRACE = 2
STOP_TIMEOUT = 5
CONVERSATION_RECORD = "dsh-conversation.json"
DSH_ROOT = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
COLLAB_ROOT = Path(os.environ.get("DSH_COLLAB_DIR") or Path.home() / "Documents/Codex/dsh-collaboration")
DESKTOP_ROOT = Path(os.environ.get("DSH_DESKTOP_DIR") or (
    Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    / "Programs/DeepSeek Harness"
))
DESKTOP_EXE = DESKTOP_ROOT / "DeepSeek Harness.exe"
DESKTOP_CLI = DESKTOP_ROOT / "resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
PLUGIN = Path(os.environ.get("DSH_WORKBUDDY_ENTRY") or (
    DSH_ROOT / "profiles/desktop/node_modules/dsh-connect-workbuddy/lib/index.js"
)).resolve()
PUBLIC_SEARCH = Path(os.environ.get("DSH_PUBLIC_SEARCH_ENTRY") or (
    DSH_ROOT / "profiles/desktop/node_modules/dsh-free-search/lib/index.js"
)).resolve()


@contextmanager
def task_lock(out):
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".dsh-run.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            def acquire():
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            acquire()
        except OSError as error:
            raise RuntimeError("该任务的 DSH 会话正在使用，请等当前轮回复后继续。") from error
        yield


def conversation(out):
    path = out / CONVERSATION_RECORD
    if not path.is_file():
        return {}
    state = json.loads(path.read_text(encoding="utf-8"))
    session_id = state.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id)):
        raise RuntimeError("协作记录里的 DSH 会话 ID 无效。")
    return state


def save_conversation(out, session_id, route=None, *, unavailable=None):
    state = conversation(out)
    if state.get("session_id") and state["session_id"] != session_id:
        raise RuntimeError("DSH 返回的会话与本任务绑定的会话不一致。")
    state["session_id"] = session_id
    if route is not None:
        state["usable_route"] = route
        if route in state.get("unavailable_routes", ()):
            state["unavailable_routes"].remove(route)
    if unavailable is not None:
        unavailable_routes = state.setdefault("unavailable_routes", [])
        if unavailable not in unavailable_routes:
            unavailable_routes.append(unavailable)
    pending = out / (CONVERSATION_RECORD + ".tmp")
    pending.write_text(json.dumps(state, indent=2), encoding="utf-8")
    pending.replace(out / CONVERSATION_RECORD)


def cli_session(events, workspace):
    ids = set()
    for row in events:
        if row.get("type") not in ("session", "final"):
            continue
        session_id = row.get("sessionId")
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            continue
        if row.get("cwd") and Path(row["cwd"]).resolve() != workspace:
            raise RuntimeError("DSH 返回的会话工作区与本任务不一致。")
        ids.add(session_id)
    if len(ids) > 1:
        raise RuntimeError("同一次命令行调用返回了多个 DSH 会话。")
    return next(iter(ids), None)


def task_session(out, workspace):
    out = out.resolve()
    state = conversation(out)
    if state.get("session_id"):
        return state["session_id"]
    # Bind an existing headless result when upgrading a task from the old runner.
    if not state:
        reports = sorted((*out.glob("review-*.json"), *out.glob("advice*.json")),
                         key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for path in reports:
            report = json.loads(path.read_text(encoding="utf-8"))
            session_id = report.get("session_id")
            if report.get("transport") == "headless" and isinstance(session_id, str) and SESSION_ID.fullmatch(session_id):
                save_conversation(out, session_id, report.get("route"))
                return session_id
    attempts = [path for path in out.glob("*.stdout.log")
                if path.name.startswith(("advice", "review-"))]
    known = {session_id for path in attempts
             if (session_id := cli_session(read_cli_events(path.read_text(encoding="utf-8", errors="replace")), workspace))}
    if len(known) == 1:
        session_id = known.pop()
        save_conversation(out, session_id)
        return session_id
    if state or attempts:
        raise RuntimeError("未能确定唯一的原 DSH 会话，已停止发送以避免再开一条。请核对本轮命令行日志。")
    return None


def stop_stage(process, diagnostics):
    """Stop the subprocess tree started for this stage, without touching either app."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
        try:
            result = subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                                    stdin=subprocess.DEVNULL, stdout=diagnostics, stderr=diagnostics,
                                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=STOP_TIMEOUT,
                                    check=False)
            if result.returncode and process.poll() is None:
                diagnostics.write(b"Process tree cleanup not confirmed; stopping the parent.\n")
        except (OSError, subprocess.TimeoutExpired) as error:
            diagnostics.write(f"Process tree cleanup: {error}\n".encode("utf-8"))
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("本轮进程退出未确认，请先检查日志和进程，再决定是否重试。") from error


def stage_status(out, label, status, has_prompt, trace=None):
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    links = [("输出", f"{label}.stdout.log"), ("诊断", f"{label}.stderr.log")]
    if has_prompt:
        links.insert(0, ("发给 DSH 的请求", f"{label}.prompt.txt"))
    details = trace.details(time.monotonic()) if trace else []
    index = " · ".join(f"[{title}]({file})" for title, file in links)
    summary = "".join(f"\n- {detail}" for detail in details)
    body = f"# {label}\n\n{status}\n{summary}\n最后记录时间 {stamp}\n\n{index} · [实时进度](progress.html)\n"
    try:
        (out / f"{label}.status.md").write_text(body, encoding="utf-8")
        write_progress(out, label, status, stamp, links, details, trace.recent if trace else ())
    except OSError as error:
        print(f"[{label}] 进度文件写入失败：{error}", file=sys.stderr, flush=True)


def run_stage(label, command, *, workspace, out, prompt=None, timeout=240, env=None,
              accept_completed_reply=False, on_events=None):
    stdout = out / f"{label}.stdout.log"
    stderr = out / f"{label}.stderr.log"
    # Preserve the inputs and diagnostics of each failed attempt as well.
    base_label = label
    counter = 1
    while stdout.exists() or stderr.exists():
        counter += 1
        label = f"{base_label}-attempt-{counter}"
        stdout = out / f"{label}.stdout.log"
        stderr = out / f"{label}.stderr.log"
    stdin_path = out / f"{label}.prompt.txt" if prompt is not None else Path(os.devnull)
    if prompt is not None:
        stdin_path.write_bytes(prompt.encode("utf-8"))
    with (stdin_path.open("rb") as request, stdout.open("wb") as response,
          stdout.open("rb") as observed, stderr.open("wb") as diagnostics):
        try:
            process = subprocess.Popen(command, cwd=workspace, stdin=request, stdout=response,
                                       stderr=diagnostics, env=env, start_new_session=os.name != "nt")
        except OSError:
            stage_status(out, label, "本轮进程启动失败。", prompt is not None)
            raise
        started = time.monotonic()
        deadline = started + timeout
        trace = RunTrace(observed, started) if accept_completed_reply else None

        def observe():
            if trace is None:
                return False
            events = trace.drain()
            if on_events is not None and events:
                on_events(events)
            return bool(events)

        def show(status, announce=False):
            stage_status(out, label, status, prompt is not None, trace)
            if announce:
                print(f"[{label}] {status}", flush=True)

        try:
            show(f"进程已启动，最多等待 {timeout:g} 秒。实时进度：{out / 'progress.html'}", announce=True)
            last_status = last_announce = started
            poll_interval = min(PROGRESS_INTERVAL, EVENT_POLL_INTERVAL) if trace else PROGRESS_INTERVAL
            while True:
                remaining = deadline - time.monotonic()
                try:
                    code = process.wait(timeout=max(0, min(poll_interval, remaining)))
                except subprocess.TimeoutExpired:
                    received_events = observe()
                    now = time.monotonic()
                    # DSH has already committed and flushed this result, but still
                    # give the process an opportunity to return its actual exit code.
                    if trace and trace.final_at is not None and (
                            now - trace.final_at >= COMPLETED_EXIT_GRACE or now >= deadline):
                        stop_stage(process, diagnostics)
                        show("已收到完整模型回复，命令行进程已收尾。", announce=True)
                        return 0, stdout.read_text(encoding="utf-8", errors="replace")
                    if now >= deadline:
                        raise subprocess.TimeoutExpired(command, timeout) from None
                    announce = now - last_announce >= PROGRESS_INTERVAL
                    if announce or (received_events and now - last_status >= EVENT_POLL_INTERVAL):
                        elapsed = int(now - started)
                        status = f"进程仍在运行，已等 {elapsed}/{timeout:g} 秒。"
                        show(status, announce=announce)
                        last_status = now
                        if announce:
                            last_announce = now
                else:
                    observe()
                    break
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            timed_out = isinstance(error, subprocess.TimeoutExpired)
            show(f"进程达到 {timeout:g} 秒等待上限，正在收尾。" if timed_out else "本轮被中断，正在收尾。")
            stop_stage(process, diagnostics)
            observe()
            if timed_out:
                error.stdout = stdout.read_bytes()
                error.stderr = stderr.read_bytes()
                show("进程超时已结束，正在检查已保存的结果。", announce=True)
            else:
                show("本轮进程已中断。")
            raise
        except (OSError, ValueError, RuntimeError):
            stop_stage(process, diagnostics)
            show("本轮进程异常退出，已保留工具轨迹。")
            raise
    status = f"进程已退出，退出码 {code}。结果以回复或验收结论为准。"
    if trace and trace.evidence_requests:
        status += " 已收到补证请求，等待 Codex 核实。"
    stage_status(out, label, status, prompt is not None, trace)
    print(f"[{label}] exit={code}", flush=True)
    if code and accept_completed_reply:
        if "already owned by an active write handle" in stderr.read_text(encoding="utf-8", errors="replace"):
            raise RuntimeError("这个 DSH 会话正被桌面或另一个进程打开写入，CLI 已停止重试。可在原会话继续，或释放占用后再用命令行。")
    return code, stdout.read_text(encoding="utf-8", errors="replace")


def dsh_command(*args):
    if os.name != "nt":
        raise RuntimeError("This launcher currently supports Windows.")
    if not DESKTOP_EXE.is_file() or not (DESKTOP_ROOT / "resources/app.asar").is_file():
        raise RuntimeError("DeepSeek Harness Desktop not found. See DSH_DESKTOP_DIR in README.")
    # Invoke the same Electron entry as dsh.cmd without putting paths through cmd.exe.
    return [str(DESKTOP_EXE), "--expose-internals", str(DESKTOP_CLI), *args]


def dsh_environment():
    return {**os.environ, "ELECTRON_RUN_AS_NODE": "1"}


def check_plugin():
    if not PLUGIN.is_file():
        raise RuntimeError("WorkBuddy plugin not found. See DSH_WORKBUDDY_ENTRY in README.")


def attach_dsh_workspace(out, workspace, session_id):
    live = sidebar.notify(DSH_ROOT, workspace, session_id)
    if live is not None:
        message = ("已更新正在运行的 DSH 侧栏" if live else "侧栏更新已排队，等待 DSH 桌面处理")
        title = f" → 「{live}」" if isinstance(live, str) else ""
        print(f"{message}「{workspace.name}」{title}。", flush=True)
        return
    state_path = DSH_ROOT / "storages/workspace.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    groups = state.get("tables", {}).get("workspaces", {})
    group = next((row for row in groups.values() if Path(row["path"]).resolve() == workspace), None)
    if group is None:
        raise RuntimeError("指定的目录尚未登记为 DSH 工作区。")
    if session_id in group.get("sessionIds", []):
        return
    launcher = HERE / "attach_workspace.mjs"
    if not launcher.is_file():
        raise RuntimeError("未找到随 Skill 安装的 DSH 工作区登记工具。")
    patch = [{"id": key, "disabled": True} for key in
             ("llm-deepseek", "llm-deepseek-account", "session-title-llm")]
    patch.append({"insert": [
        {"id": "workspace", "name": "@deepseek-ai/dsh-workspace"},
        {"id": "codex-attach-workspace", "name": launcher.as_posix(),
         "config": {"workspace": str(workspace), "sessionId": session_id}},
    ]})
    patch_file = out / "workspace-attach.json"
    patch_file.write_text(json.dumps(patch), encoding="utf-8")
    result = subprocess.run(dsh_command("headless", "--patch", str(patch_file), "--json", "-"),
                            cwd=workspace, env=dsh_environment(), stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=25, encoding="utf-8", errors="replace")
    (out / "workspace-attach.stdout.log").write_text(result.stdout, encoding="utf-8")
    (out / "workspace-attach.stderr.log").write_text(result.stderr, encoding="utf-8")
    updated = json.loads(state_path.read_text(encoding="utf-8"))
    if result.returncode or not any(
        Path(row["path"]).resolve() == workspace and session_id in row.get("sessionIds", [])
        for row in updated["tables"]["workspaces"].values()
    ):
        raise RuntimeError(f"未能将原会话登记到指定 DSH 工作区。详见 {out / 'workspace-attach.stderr.log'}")
    print(f"已将原会话登记到 DSH 工作区「{workspace.name}」。", flush=True)


def remember_cli_session(out, workspace, events, previous, *, attach=False):
    actual = cli_session(events, workspace)
    if actual is None:
        return previous
    save_conversation(out, actual)
    if attach:
        try:
            attach_dsh_workspace(out, workspace, actual)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            print(f"DSH 工作区登记待核对：{error}", file=sys.stderr, flush=True)
    return actual


def put_overlay(out, route=DEFAULT_ROUTE, inspect_roots=(), inspect_files=(), *, evidence_bridge=None, title=None,
                independent=False, effort=None):
    check_plugin()
    provider, model = ROUTES[route]
    region = "global" if provider == "workbuddy-global" else "cn"
    info = {
        "id": model, "name": "DeepSeek-V4.1-Flash",
        "contextWindow": 1000000, "maxTokens": 128000,
        "creditMultiplier": 0.11, "supportsImages": True,
        "reasoning": {
            "supportedEfforts": ["low", "high", "max"],
            "defaultEffort": "high", "canDisableThinking": True,
        },
    }
    if model == HY4:
        info.pop("creditMultiplier")
        info.update(name="Hy4 preview", contextWindow=200000, maxTokens=64000,
                    reasoning={"supportedEfforts": ["high"], "defaultEffort": "high",
                               "canDisableThinking": False})
    row = {
        "id": "dsh-connect-workbuddy",
        "name": PLUGIN.as_posix(),
        "config": {
            "regions": {
                ("cn" if region == "global" else "global"): {"enabled": False},
                region: {
                    "enabled": True,
                    "enabledModelIds": [model],
                    "lastCatalog": [info],
                    "imageToolEnabled": False,
                    "videoToolEnabled": False,
                    "pool": {"enabled": False, "autoCheckin": False},
                },
            },
        },
    }
    selection = {"provider": provider, "model": model}
    if effort is not None:
        selection["reasoningEffort"] = "high" if model == HY4 else effort
    overlay = [
        {"insert": [row]},
        {"id": "agent-default-model", "config": selection},
        *({"id": row_id, "disabled": True} for row_id in (
            "llm-deepseek", "llm-deepseek-account", "web-deepseek-api",
            "web-search-deepseek", "session-title-llm",
        )),
    ]
    if independent:
        search = PUBLIC_SEARCH.is_file()
        web = {"fetchProvider": "http"}
        if search:
            free_engines = ["bing", "ddg", "ddg-lite"]
            overlay.append({"insert": [{"id": "web-search-free", "name": PUBLIC_SEARCH.as_posix(), "config": {
                "provider": free_engines[0], "allowedEngines": free_engines, "fallbackOrder": free_engines,
                "fetchProviderTakeover": False,
            }}]})
            web["searchProvider"] = "ddg"
        overlay.extend((
            {"id": "web", "config": web},
            {"id": "tool-web", "disabled": False, "config": {"search": search, "fetch": True}},
        ))
    else:
        overlay.append({"id": "tool-web", "disabled": True})
    if title:
        overlay.append({"insert": [{
            "id": "codex-session-title",
            "name": (HERE / "session_title.mjs").as_posix(),
            "config": {"title": title},
        }]})
    if inspect_roots or inspect_files or evidence_bridge:
        plugin = HERE / "scoped_inspection.mjs"
        if not plugin.is_file():
            raise RuntimeError("未找到 DSH 只读取证工具。请核对 Skill 安装目录。")
        overlay.append({"insert": [{
            "id": "codex-scoped-inspection",
            "name": plugin.as_posix(),
            "config": {"roots": [
                {"path": str(path), "text": path in inspect_files}
                for path in dict.fromkeys((*inspect_roots, *inspect_files))
            ], "restrictTools": not independent},
        }]})
        if evidence_bridge:
            overlay[-1]["insert"][0]["config"]["evidenceBridge"] = evidence_bridge
    path = out / f"workbuddy-{route}.json"
    path.write_text(json.dumps(overlay, indent=2), encoding="utf-8")
    return path


class DesktopFallbackRequired(RuntimeError):
    pass


class CodexEvidencePending(RuntimeError):
    pass


class DSHResponseError(RuntimeError):
    """The selected WorkBuddy route could not supply a usable reply."""


def read_session_events(session_id):
    logs = list((DSH_ROOT / "sessions").glob(f"*/{session_id}/session.v4.jsonl.zstd"))
    if len(logs) != 1:
        raise RuntimeError("未找到唯一的 DSH 会话记录。请核对会话 ID、DSH_HOME 和会话保存状态。")
    try:
        import zstandard
    except ImportError as error:
        raise RuntimeError("导回桌面回复需要 zstandard：python -m pip install zstandard") from error
    try:
        with zstandard.open(logs[0], "rt", encoding="utf-8") as trace:
            return [json.loads(line) for line in trace]
    except (ValueError, zstandard.ZstdError) as error:
        raise RuntimeError("DSH 会话记录尚未写完或无法读取。") from error


def verified_session_reply(workspace, prompt, session_id, route=DEFAULT_ROUTE, *, inspect=False):
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise RuntimeError("请提供有效的 DSH 会话 ID。")
    events = read_session_events(session_id)
    if not events or any(not isinstance(event, dict) for event in events):
        raise RuntimeError("DSH 会话记录格式不受支持。")
    header = events[0]
    cwd = header.get("cwd")
    if (header.get("type") != "session" or header.get("version") != 4
            or header.get("id") != session_id or not isinstance(cwd, str)
            or not Path(cwd).is_absolute() or Path(cwd).resolve() != workspace.resolve()):
        raise RuntimeError("DSH 会话与接续工作区不匹配。")

    # Locate the matching turn in the conversation, not other stages or older attempts.
    def composer_text(text):
        return text.replace("\r\n", "\n").replace("\r", "\n").strip()

    latest = None
    turn = []
    for event in events[1:]:
        kind = event.get("type")
        if kind == "turn/start":
            turn = [event]
        elif turn:
            turn.append(event)
        if turn and kind == "user/message":
            row = event.get("data", {})
            content = row.get("content", [])
            if (row.get("source", {}).get("kind") == "user" and len(content) == 1
                    and content[0].get("type") == "text"
                    and composer_text(content[0].get("text", "")) == composer_text(prompt)):
                latest = turn
        if kind == "turn/end":
            turn = []
    if latest is None:
        raise RuntimeError("DSH 会话中的消息与原始请求不匹配。")

    def entries(kind):
        return [event["data"] for event in latest if event.get("type") == kind]

    users = [row for row in entries("user/message") if row.get("source", {}).get("kind") == "user"]
    if len(users) != 1:
        raise RuntimeError("原会话本轮又收到了其他消息，无法核对这条回复。")

    starts = entries("turn/start")
    ends = entries("turn/end")
    replies = entries("assistant/message")
    if (len(starts) != 1 or len(ends) != 1 or not replies or (not inspect and len(replies) != 1)
            or ends[0].get("reason", {}).get("kind") != "completed"
            or any(reply.get("turn") != starts[0].get("turn") for reply in replies)
            or ends[0].get("turn") != starts[0].get("turn")):
        raise RuntimeError("DSH 会话须有一次已经完成的回复。请等待写入或核对接续会话。")
    if inspect:
        expected = set()
        for row in entries("tool/call"):
            if row.get("name") != "codex_inspect":
                continue
            arguments = json.loads(row.get("arguments", "{}"))
            if arguments.get("action") in ("list", "measure", "read_text"):
                expected.add(row.get("callId"))
        if not any(row.get("message", {}).get("toolCallId") in expected
                   and row.get("message", {}).get("isError") is False
                   for row in entries("tool/result")):
            raise RuntimeError("DSH 会话没有完成的限定范围取证操作。")

    message = replies[-1].get("message", {})
    config = [row.get("header", {}).get("config", {}) for row in entries("request/header")]
    origin = message.get("source", {})
    provider, model = ROUTES[route]
    if (not config or origin.get("kind") != "model"
            or any((row.get("provider"), row.get("model")) != (provider, model)
                   for row in [*config, origin])):
        raise RuntimeError(f"DSH 会话模型与 {provider}/{model} 不匹配。")
    content = message.get("content", [])
    if any(part.get("type") not in {"reasoning", "text"} for part in content):
        raise RuntimeError("DSH 接续回复含工具调用或其他不支持的内容。")
    answer = "\n".join(part["text"] for part in content if part.get("type") == "text")
    if not answer.strip():
        raise RuntimeError("DSH 会话没有可导入的文本回复。")
    return answer


def completed_response(events):
    final = next((event for event in reversed(events) if event.get("type") == "final"), {})
    reason = final.get("reason")
    answer = final.get("text") or final.get("answer")
    session_id = final.get("sessionId")
    if (isinstance(reason, dict) and reason.get("kind") == "completed"
            and isinstance(answer, str) and answer.strip() and session_id):
        return answer, session_id
    return None


def read_cli_events(output):
    events = []
    for line in (output or "").splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # A timed-out process can leave a partial event at the end.
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def finished_inspection(events):
    calls = {row["callId"] for row in events
             if row.get("type") == "tool_call" and row.get("tool") == "codex_inspect"
             and row.get("input", {}).get("action") in ("list", "measure", "read_text")
             and row.get("callId")}
    return any(row.get("type") == "tool_result" and row.get("status") == "completed"
               and row.get("callId") in calls for row in events)


def verify_source_read(events, inspect_roots, inspect_files):
    roots = tuple(dict.fromkeys((*inspect_roots, *inspect_files)))
    permitted = {index for index, path in enumerate(roots) if path in inspect_files}
    calls = set()
    for row in events:
        args = row.get("input")
        if (row.get("type") == "tool_call" and row.get("tool") == "codex_inspect"
                and isinstance(args, dict) and args.get("action") == "read_text"
                and type(args.get("root")) is int and args["root"] in permitted and row.get("callId")):
            calls.add(row["callId"])
    for row in events:
        if row.get("type") != "tool_result" or row.get("callId") not in calls or row.get("status") != "completed":
            continue
        try:
            result = json.loads(row.get("result", ""))
        except (TypeError, ValueError):
            continue
        if isinstance(result, dict) and isinstance(result.get("text"), str) and result["text"].strip():
            return
    raise DSHResponseError("DSH 未成功读取本轮授权文件，报告不能作为代码审查。")


def verify_native_source(events, workspace, sources):
    eligible = set(sources)
    calls = {}
    for row in events:
        args = row.get("input")
        if (row.get("type") != "tool_call" or row.get("tool") != "read"
                or not isinstance(args, dict) or not isinstance(args.get("file_path"), str)):
            continue
        try:
            name = (workspace / args["file_path"]).resolve()
        except (ValueError, OSError, RuntimeError):
            continue
        if name in eligible and row.get("callId"):
            calls[row["callId"]] = name
    for row in events:
        result = row.get("result")
        expected = calls.get(row.get("callId"))
        if (row.get("type") != "tool_result" or expected is None or row.get("status") != "completed"
                or not isinstance(result, str) or "<type>file</type>" not in result):
            continue
        reported = re.match(r"<path>([^\r\n]+)</path>", result)
        if reported is None:
            continue
        try:
            returned_path = Path(reported[1])
            if not returned_path.is_absolute() or returned_path.resolve() != expected:
                continue
        except (ValueError, OSError, RuntimeError):
            continue
        _, separator, content = result.partition("<content>")
        if separator and content.split("</content>", 1)[0].strip():
            return
    raise DSHResponseError("DSH 未成功读取本轮审查的任何源码，回复原文仍在日志中。")


def bind_desktop_reply(out, tag, session_id, answer):
    path = out / f"{tag}.desktop-import.json"
    binding = {"session_id": session_id, "reply_sha256": sha256(answer.encode("utf-8")).hexdigest()}
    if not path.exists():
        fd, name = tempfile.mkstemp(dir=out)
        pending = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as staged:
                json.dump(binding, staged)
            # Publish a complete record without replacing a concurrent import.
            if os.name == "nt":
                pending.rename(path)
            else:
                os.link(pending, path)
        except FileExistsError:
            pass
        finally:
            pending.unlink(missing_ok=True)
    if json.loads(path.read_text(encoding="utf-8")) != binding:
        raise RuntimeError("本轮已绑定一个桌面会话及回复，请沿用首次导回的结果。")


def recover_cli_reply(out, prompt, session_id, route=DEFAULT_ROUTE, *, inspect=False):
    if not session_id:
        return None
    try:
        options = {"inspect": True} if inspect else {}
        answer = verified_session_reply(out, prompt, session_id, route, **options)
    except (OSError, RuntimeError):
        # No verified result yet. The same request can continue on the desktop.
        return None
    return answer, session_id


def route_stage(tag, route):
    return tag if route == DEFAULT_ROUTE else f"{tag}.{route}"


def desktop_record(out, tag, route):
    return out / f"{route_stage(tag, route)}.desktop.json"


def try_workbuddy_routes(out, routes, request, *, timeout, total_timeout):
    desktop = None
    response_error = None
    deadline = time.monotonic() + total_timeout
    for index, route in enumerate(routes):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("本轮已用完命令行总预算，保留原会话和本机记录。", flush=True)
            break
        try:
            result = request(route, min(timeout, remaining))
            session_id = conversation(out).get("session_id")
            if session_id:
                save_conversation(out, session_id, route)
            return result
        except DesktopFallbackRequired as error:
            desktop = desktop or error
            detail = "命令行未取得完整回复 " + str(error).removeprefix("DSH_DESKTOP_REQUIRED ")
        except DSHResponseError as error:
            response_error = error
            detail = str(error)
        if index + 1 < len(routes) and time.monotonic() < deadline:
            print(f"[{route}] {detail}；改试 {routes[index + 1]}。", flush=True)
    if desktop is not None:
        raise desktop
    if response_error is not None:
        raise response_error
    raise RuntimeError("本轮命令行总预算已耗尽。")


def scoped_paths(transport):
    return (*transport.get("inspect_roots", ()), *transport.get("inspect_files", ()))


def evidence_receipt(out, tag):
    return out / f"bridge-evidence-{tag}.json"


def save_evidence_requests(out, tag, prompt, route, events, session_id, *, answer=None):
    requests = tool_results(events, EVIDENCE_REQUEST)
    if not requests:
        return None
    evidence_request_ids(requests)
    receipt = evidence_receipt(out, tag)
    temp = receipt.with_suffix(".json.tmp")
    temp.write_text(json.dumps({
        "session_id": session_id,
        "route": route,
        "prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
        "requests": requests,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(receipt)
    if answer:
        (out / f"bridge-evidence-{tag}.md").write_text(answer.rstrip() + "\n", encoding="utf-8")
    return receipt


def capture_evidence_requests(out, tag, prompt, route, events, session_id, *, answer=None):
    if session_id is None:
        return
    receipt = save_evidence_requests(out, tag, prompt, route, events, session_id, answer=answer)
    if receipt is not None:
        save_conversation(out, session_id, route)
        raise CodexEvidencePending(f"DSH 等待 Codex 查证：{receipt}")




def recover_evidence_requests(out, tag, stage, prompt, labeled_prompt, route, workspace, session_id):
    attempts = [out / f"{stage}.stdout.log", *out.glob(f"{stage}-attempt-*.stdout.log")]
    attempts = sorted((path for path in attempts if path.is_file()),
                      key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for trace in attempts:
        saved_request = trace.with_name(trace.name.removesuffix(".stdout.log") + ".prompt.txt")
        if not saved_request.is_file() or saved_request.read_bytes() != labeled_prompt.encode("utf-8"):
            continue
        events = read_cli_events(trace.read_text(encoding="utf-8", errors="replace"))
        if cli_session(events, workspace) != session_id:
            continue
        completed = completed_response(events)
        answer = completed[0] if completed is not None and completed[1] == session_id else None
        capture_evidence_requests(out, tag, prompt, route, events, session_id, answer=answer)


def continue_with_evidence(out, tag, route, workspace, prompt, results_file, timeout,
                           *, code_task=False, inspect_roots=(), inspect_files=(), independent=False,
                           native_sources=(), project_write=False, effort=None):
    receipt = json.loads(evidence_receipt(out, tag).read_text(encoding="utf-8"))
    session_id = conversation(out).get("session_id")
    if not session_id or receipt.get("session_id") != session_id:
        raise RuntimeError("证据请求与本任务的 DSH 会话不一致。")
    if receipt.get("prompt_sha256") != sha256(prompt.encode("utf-8")).hexdigest():
        raise RuntimeError("原问题或材料已变化。请使用登记证据请求时的同一阶段与材料。")
    results = json.loads(results_file.read_text(encoding="utf-8-sig"))
    verify_results(evidence_request_ids(receipt["requests"]), results,
                   ("verified", "inconclusive", "not_checked"))
    overlay = put_overlay(out, route, inspect_roots, inspect_files,
                          evidence_bridge={"mode": "results", "results": results}, independent=independent,
                          effort=effort)
    stage = route_stage(f"bridge-evidence-{tag}-results", route)
    followup = (
        "继续原会话。先调用 codex_evidence_results，读出 Codex 核实后回传的证据。"
        "按编号逐条核对与原问题的关系，说明哪些结论因此改变、哪些还缺证据。"
        "以原问题所要求的格式给出最终中文判断。本轮用已取得的证据收敛。\n\n"
        f"原问题和材料：\n{prompt}\n"
    )
    if native_sources:
        followup += "\n复查时用原生 read 重新读取本轮相关源码。"
    if code_task:
        followup += "\n这轮仍须实际调用 codex_inspect 的 read_text 复核当前授权源码，再给结论。"
        if tag.startswith("review-"):
            followup += "审查答复直接从「## 结论」开始，工具执行情况放在「## 核查依据」内。"
    command = dsh_command("headless", "--patch", str(overlay), "--json",
                          "--permission-mode", "workspace-write" if project_write else "read-only",
                          "--approval-policy", "never",
                          "--session-id", session_id, "-")
    code, output = run_stage(stage, command, workspace=workspace, out=out, prompt=followup,
                             timeout=timeout, env=dsh_environment(), accept_completed_reply=True)
    events = read_cli_events(output)
    result = completed_response(events)
    if (code or result is None or result[1] != session_id or cli_session(events, workspace) != session_id
            or results not in tool_results(events, EVIDENCE_RESULTS)):
        raise RuntimeError(f"DSH 未在原会话完成证据复查。详见 {out / (stage + '.status.md')}")
    if code_task:
        verify_source_read(events, inspect_roots, inspect_files)
    if native_sources:
        verify_native_source(events, workspace, native_sources)
    remember_cli_session(out, workspace, events, session_id,
                         attach=not independent and (out / "dsh-workspace.txt").is_file())
    return result


def collaboration_overlay(out, route, transport):
    return put_overlay(out, route, transport.get("inspect_roots", ()), transport.get("inspect_files", ()),
                       evidence_bridge={"mode": "request"} if transport.get("ask_codex") else None,
                       title=transport.get("dsh_title"), independent=transport.get("independent", False),
                       effort=transport.get("effort"))


def ask_dsh(out, overlay, tag, prompt, *, timeout=240, desktop_answer=None,
            desktop_session_id=None, route=DEFAULT_ROUTE, inspect_roots=(), inspect_files=(),
            dsh_workspace=None, ask_codex=False, codex_results=None, dsh_title=None, code_task=False,
            independent=False, native_sources=(), project_write=False, effort=None):
    workspace = dsh_workspace or out
    attach = dsh_workspace is not None and not independent
    if codex_results is not None:
        return continue_with_evidence(out, tag, route, workspace, prompt, codex_results, timeout,
                                      code_task=code_task, inspect_roots=inspect_roots, inspect_files=inspect_files,
                                      independent=independent, native_sources=native_sources,
                                      project_write=project_write, effort=effort)
    if evidence_receipt(out, tag).is_file():
        raise CodexEvidencePending(f"DSH 等待 Codex 查证：{evidence_receipt(out, tag)}")
    stage = route_stage(tag, route)
    handoff = desktop_record(out, tag, route)
    provider, model = ROUTES[route]
    inspect = {"inspect": True} if not independent and (inspect_roots or inspect_files) else {}
    original_prompt = prompt
    # Distinguish identical tasks sent from different runs in the shared workspace.
    request_id = sha256(f"{out.resolve()}\n{stage}".encode("utf-8")).hexdigest()[:16]
    labeled_prompt = f"{prompt}\n\n协作记录编号：{request_id}\n"
    if handoff.is_file():
        record = json.loads(handoff.read_text(encoding="utf-8"))
        if (record.get("provider"), record.get("model")) != (provider, model):
            raise RuntimeError("接续记录的模型与当前 Skill 配置不匹配。")
        if record.get("request_id") == request_id:
            prompt = labeled_prompt
        elif "request_id" in record:
            raise RuntimeError("协作记录编号不匹配。请从原运行目录导回本轮回复。")
        prompt_hash = sha256(prompt.encode("utf-8")).hexdigest()
        if record["prompt_sha256"] != prompt_hash:
            raise RuntimeError("Task or snapshot changed since desktop handoff. Start a new review.")
        if sha256(Path(record["request"]).read_bytes()).hexdigest() != record["prompt_sha256"]:
            raise RuntimeError("The saved desktop request was edited. Start a new review.")
        if desktop_session_id is None:
            result = (None if ask_codex or native_sources else
                      recover_cli_reply(workspace, prompt, record.get("headless_session_id"), route, **inspect))
            if result is not None:
                return result
            raise DesktopFallbackRequired(f"DSH_DESKTOP_REQUIRED (already prepared): {handoff}")
        if native_sources:
            raise RuntimeError("源码复查需要本轮可验证的 CLI 读取记录。请接续原会话取证。")
        bound = record.get("session_id")
        if bound and desktop_session_id != bound:
            raise RuntimeError("请在本任务已绑定的 DSH 会话内继续。")
        if not bound and desktop_session_id == record.get("headless_session_id"):
            raise RuntimeError("请提供接续用的桌面会话 ID。")
        answer = verified_session_reply(Path(record["workspace"]), prompt, desktop_session_id, route,
                                        **inspect)
        if desktop_answer is not None and desktop_answer.read_text(encoding="utf-8-sig").strip() != answer.strip():
            raise RuntimeError("回复文件与 DSH 会话中的答复不匹配。")
        bind_desktop_reply(out, tag, desktop_session_id, answer)
        return answer, desktop_session_id
    if desktop_session_id is not None:
        raise RuntimeError(f"No timed-out CLI request for {tag}.")

    prompt = labeled_prompt
    session_id = task_session(out, workspace)
    if ask_codex and session_id:
        recover_evidence_requests(out, tag, stage, original_prompt, labeled_prompt, route, workspace, session_id)
    resume = ("--session-id", session_id) if session_id else ()
    command = dsh_command("headless", "--patch", str(overlay), "--json",
                          "--permission-mode", "workspace-write" if project_write else "read-only",
                          "--approval-policy", "never", *resume, "-")
    if not session_id:
        # Reserve creation before starting a process that may persist a session before printing its ID.
        save_conversation(out, None)
    detail = f"沿用会话 {session_id}" if session_id else "创建本任务唯一会话"
    if dsh_title and not session_id:
        detail += f"「{dsh_title}」"
    print(f"[{stage}] WorkBuddy 路线：{provider}/{model}，{detail}", flush=True)
    request_calls = set()
    request_events = []

    def checkpoint(batch):
        nonlocal session_id
        observed = cli_session(batch, workspace)
        if observed and observed != session_id:
            save_conversation(out, observed)
            session_id = observed
        if not ask_codex or not session_id:
            return
        changed = False
        for row in batch:
            call_id = row.get("callId")
            if not isinstance(call_id, str) or not call_id:
                continue
            if row.get("type") == "tool_call" and row.get("tool") == EVIDENCE_REQUEST and call_id:
                request_calls.add(call_id)
                request_events.append(row)
            elif row.get("type") == "tool_result" and call_id in request_calls and row.get("status") == "completed":
                request_events.append(row)
                changed = True
        if changed:
            save_evidence_requests(out, tag, original_prompt, route, request_events, session_id)

    try:
        code, events = run_stage(
            stage, command,
            workspace=workspace, out=out, prompt=prompt, env=dsh_environment(), timeout=timeout,
            accept_completed_reply=True, on_events=checkpoint,
        )
    except subprocess.TimeoutExpired as error:
        events = read_cli_events((error.stdout or b"").decode("utf-8", errors="replace"))
        session_id = remember_cli_session(out, workspace, events, session_id, attach=attach)
        result = completed_response(events)
        if ask_codex:
            answer = result[0] if result is not None and result[1] == session_id else None
            capture_evidence_requests(out, tag, original_prompt, route, events, session_id, answer=answer)
        if result is not None and result[1] == session_id:
            if code_task:
                verify_source_read(events, inspect_roots, inspect_files)
            if native_sources:
                verify_native_source(events, workspace, native_sources)
            if not inspect or finished_inspection(events):
                return result
        if has_local_shell_failure(events):
            raise RuntimeError("DSH 本机终端沙箱初始化失败。请按原授权改用只读取证或由 Codex 实测；换模型不会修好该工作区。") from error
        if session_id is None:
            raise RuntimeError("超时前未取得 DSH 会话 ID，已停止重试以避免另开会话。") from error
        last_phase = next((row.get("phase") for row in reversed(events)
                           if row.get("type") == "status"), None)
        result = None if ask_codex or native_sources else recover_cli_reply(workspace, prompt, session_id, route, **inspect)
        if result is not None:
            return result
        request = out / f"{stage}.desktop-request.txt"
        request.write_bytes(prompt.encode("utf-8"))
        record = {
            "workspace": str(workspace),
            "session_id": session_id,
            "request": str(request),
            "request_id": request_id,
            "provider": provider,
            "model": model,
            "headless_session_id": session_id,
            "timeout_seconds": error.timeout,
            "last_cli_phase": last_phase,
            "read_only_investigation": bool(inspect or ask_codex or native_sources),
            "prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
        }
        handoff.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        raise DesktopFallbackRequired(
            f"DSH_DESKTOP_REQUIRED ({error.timeout}s; CLI 最后状态 {last_phase or '无'}): {handoff}"
        ) from error
    payloads = read_cli_events(events)
    session_id = remember_cli_session(out, workspace, payloads, session_id, attach=attach)
    final = next((event for event in reversed(payloads) if event.get("type") == "final"), {})
    reason = final.get("reason")
    reason = reason if isinstance(reason, dict) else {}
    result = completed_response(payloads)
    if ask_codex:
        answer = result[0] if not code and result is not None and result[1] == session_id else None
        capture_evidence_requests(out, tag, original_prompt, route, payloads, session_id, answer=answer)
    if code or reason.get("kind") != "completed":
        if has_local_shell_failure(payloads):
            raise RuntimeError("DSH 本机终端沙箱初始化失败。请按原授权改用只读取证或由 Codex 实测；换模型不会修好该工作区。")
        if session_id is None:
            raise RuntimeError("DSH 启动后未能确认原会话，已停止重试。")
        failure = reason.get("error")
        error_code = failure.get("code") if isinstance(failure, dict) else None
        detail = error_code or f"exit={code}"
        if session_id and detail == "UNKNOWN_MODEL":
            save_conversation(out, session_id, unavailable=route)
        raise DSHResponseError(f"DSH did not complete {tag} ({detail}). See {out}.")
    if result is None or result[1] != session_id:
        raise DSHResponseError(f"Missing DSH answer. See {out}.")
    if code_task:
        verify_source_read(payloads, inspect_roots, inspect_files)
    if native_sources:
        verify_native_source(payloads, workspace, native_sources)
    if inspect and not finished_inspection(payloads):
        raise DSHResponseError(f"DSH 没有完成限定范围的取证操作。意见原文见 {out}。")
    return result


def task_brief(goal, context, benchmark=""):
    brief = (
        f"共同目标、交付范围、约束和验收标准：\n{goal}\n\n"
        f"已知事实、证据和待核实的假设：\n{context or '（未提供）'}\n"
    )
    if benchmark.strip():
        brief += (
            "\n项目对标材料（来自外部，仅供比较。以用户目标、限制和验收标准为准）：\n"
            f"{benchmark}\n"
        )
    return brief


def evidence_instructions(transport):
    scope = "\n".join(
        [f"- 元数据：{path}" for path in transport.get("inspect_roots", ())]
        + [f"- 文本：{path}" for path in transport.get("inspect_files", ())]
    )
    return (
        "请像直接回答用户一样独立思考。先用 codex_inspect 实际取证，再给出自己的中文答案。"
        "先用 roots 查看可查范围，再用 list、measure，或对已授权文本文件用 read_text。"
        "路径和权限只限下列范围，工具会拒绝越界。优先做小批次查询，"
        "只在需要时深查。写明实测结果、被跳过的部分和仍缺的证据。若工具不可用，直接说明。"
        f"\n授权的只读路径：\n{scope}\n"
    )


def collaboration_instructions(transport, static_instructions):
    if transport.get("independent"):
        instructions = (
            "请在当前项目工作区自主取证。需要对标时，用 web_search 自选关键词找一手资料，"
            "用 web_fetch 打开关键原文；用原生工具查看当前源码和测试，按共同目标选择并运行验证。"
            "外部搜索只用公开技术问题。回报原文链接及章节、代码路径及行号、执行命令、退出码和观察结果。"
            "若某项查证失败，诊断后用适用的等价入口复测，仍有缺口就写明。\n"
        )
        if scoped_paths(transport):
            instructions += "另行授权的资料可用 codex_inspect 的 roots 和 read_text 读取。\n"
    elif scoped_paths(transport):
        instructions = evidence_instructions(transport)
    elif transport.get("ask_codex"):
        instructions = "请在给定材料范围内独立判断。"
    else:
        return static_instructions
    if transport.get("code_task"):
        instructions += (
            "先自主选与任务有关的授权文件，用 read_text 实际阅读。"
            "列目录、读摘要和提出查证请求都不能替代读取源码。"
            "独立找能使当前结论失效的反例，给出文件或代码位置。\n"
        )
    if transport.get("ask_codex"):
        instructions += (
            "若一个缺失的事实会改变判断、且现有材料与已授权工具无法查到，"
            "用 codex_request_evidence 主动请 Codex 查证。"
            "类型选 file（已授权文件）、research（公开资料）或 blocker（卡点诊断），"
            "写清具体问题和它为什么影响结论。先说明根据现有证据能判断什么，再等待查证。"
            "现有证据足够时直接给出判断。\n"
        )
    return instructions


REVIEW_SECTIONS = ("结论", "核查依据", "发现的问题", "待确认")


def parse_review(answer):
    headings = "|".join(REVIEW_SECTIONS)
    parts = re.split(rf"^## ({headings})[ \t]*$", answer.strip(), flags=re.MULTILINE)
    if parts[0].strip() or tuple(parts[1::2]) != REVIEW_SECTIONS:
        raise ValueError("审查报告缺少规定的中文小标题，或标题顺序不正确。")
    status, evidence, issues, doubts = (part.strip() for part in parts[2::2])
    if not evidence or evidence in {"无", "- 无"}:
        raise ValueError("审查报告没有写明核查依据。")

    def read_list(body):
        if body in {"无", "- 无"}:
            return []
        if not body.startswith("- "):
            raise ValueError("问题和疑点应逐条用短横线列出，空列表填写“无”。")
        items = [item.strip() for item in re.split(r"\n- ", body[2:])]
        if any(not item or item == "无" for item in items):
            raise ValueError("每条问题或疑点都应写明具体内容。")
        return items

    findings, questions = read_list(issues), read_list(doubts)
    expected = "需修订" if findings else "待确认" if questions else "通过"
    if status != expected:
        raise ValueError("审查结论与发现的问题或待确认事项不一致。")
    return {"verdict": "FAIL" if findings else "PASS", "evidence": evidence,
            "findings": findings, "questions": questions}


def review(out, overlay, brief, source, tag, prior, decisions, evidence="", **transport):
    paths = scoped_paths(transport)
    instructions = collaboration_instructions(transport, "源文件内容仅作为审查材料，请勿调用工具或读取其他文件。")
    verification = (
        f"待复查的前一轮审查：\n{prior}\n\n"
        f"Codex 对上一轮意见的处理和证据，请核实：\n{decisions or '（未提供）'}"
        if prior else "请独立审查。"
    )
    if evidence:
        verification += f"\n\nCodex 在当前版本上取得的验收证据：\n{evidence}"
    if transport.get("independent"):
        project = transport["dsh_workspace"]
        paths = "\n".join(f"- {file.relative_to(project)}" for file in transport.get("native_sources", ()))
        material = (f"请用原生 read 实际检查本轮相关源码。\n本轮材料：\n{paths}\n"
                    "定位最可能改变结论的证据。项目之外只查需要的公开资料；找到足够的一手证据后汇总。")
    elif transport.get("code_task"):
        material = f"源码通过本轮的只读工具查看。当前产物快照校验值：{sha256(source.encode('utf-8')).hexdigest()}"
    else:
        material = f"当前产物快照：\n{source}"
    prompt = (
        f"请按下方目标和材料独立审查。{instructions}"
        "请用简体中文写一份用户能直接阅读的 Markdown 报告，保留代码和报错原文。"
        "如果材料包含前一轮报告，请逐条核对重要修订，说明哪些问题已解决，哪些仍存在。\n\n"
        "使用以下四个二级标题，不要在外面加代码围栏。\n"
        "## 结论\n"
        "单独一行填写“通过”“需修订”或“待确认”。有缺陷就填“需修订”，"
        "无缺陷但有影响验收的疑点就填“待确认”，其余填“通过”。\n"
        "## 核查依据\n"
        "即使通过，也写清核对的要求、具体代码或材料，以及一个有区分度的例子"
        "或为什么现有证据支持结论。区分 Codex 提供的验收证据和你阅读材料所得的判断。\n"
        "## 发现的问题\n"
        "每条以“- ”开头，写明位置、受影响的要求、实际影响和建议。"
        "没有问题时，本段只写一个“无”字，解释放在核查依据里。\n"
        "## 待确认\n"
        "每条以“- ”开头，只列会影响验收的缺失证据，说明还需要什么。"
        "没有疑点时，本段只写一个“无”字。\n\n"
        "Codex 会核实意见并作出最终决定。\n\n"
        f"{brief}\n{material}\n\n{verification}\n"
    )
    answer, session_id = ask_dsh(out, overlay, tag, prompt, **transport)
    route = transport.get("route", DEFAULT_ROUTE)
    provider, model = ROUTES[route]
    readable = out / f"{tag}.md"
    try:
        report = parse_review(answer.replace("\r\n", "\n"))
    except ValueError as error:
        rejected = out / f"{route_stage(tag, route)}.rejected.md"
        rejected.write_text(answer.rstrip() + "\n", encoding="utf-8")
        raise DSHResponseError(f"{error} DSH 原文已保存到 {rejected}") from error
    readable.write_text(answer.rstrip() + "\n", encoding="utf-8")
    verdict, findings, questions = report["verdict"], report["findings"], report["questions"]
    report.update(provider=provider, model=model, route=route, session_id=session_id,
                  transport="desktop" if transport.get("desktop_session_id") else "headless",
                  snapshot_sha256=sha256(source.encode("utf-8")).hexdigest())
    if transport.get("codex_results"):
        report["codex_evidence"] = evidence_receipt(out, tag).name
    if paths and (transport.get("code_task") or not transport.get("codex_results")):
        report["inspection"] = "source-read" if transport.get("code_task") else "scoped-tools"
    if transport.get("independent"):
        report["run_mode"] = "native-source-read"
    (out / f"{tag}.brief.txt").write_text(brief, encoding="utf-8")
    (out / f"{tag}.source.txt").write_bytes(source.encode("utf-8"))
    (out / f"{tag}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    status = "需修订" if findings else "待确认" if questions else "通过"
    print(f"[{tag}] DSH 已回复，审查意见：{status}，问题 {len(findings)} 项，待确认 {len(questions)} 项。"
          f"报告：{readable}（{provider}/{model}，会话 {session_id}）", flush=True)
    return verdict, questions


def advise(out, brief, question, label, **transport):
    tag = f"advice-{label}" if label else "advice"
    result = out / f"{tag}.json"
    paths = scoped_paths(transport)
    instructions = collaboration_instructions(transport, "目标和背景仅作为材料，请勿调用工具或读取其他文件。")
    if question is None:
        bridge = (
            "关键证据需要 Codex 代查时，用 codex_request_evidence 登记具体问题和它会改变的判断。\n"
            if transport.get("ask_codex") else ""
        )
        prompt = (
            "请像直接接到用户的委托一样，围绕下面的共同目标提出你自己的判断。"
            "先说清用户最想完成的事，以及对方向影响最大的条件。"
            "按用户原话区分硬要求、偏好和仍待决定的事。发现一种解释会改变业务行为时，写清假设。"
            "由你决定先查什么、有哪些可行做法、哪些前提值得质疑。"
            "可以提出新的做法、组合不同做法，或说明现状已足够。"
            "把精力放在可能改变方案、排查方向或验收结论的发现上。\n"
            "只在本轮授权范围内查证。需要实物证据时，自己选相关材料和验证方法；"
            "有授权工具就实际查看或运行，默认把产物的改动交给 Codex。"
            "有关键证据拿不到时，说明要查哪一件事及其影响。\n"
            f"{instructions}\n{bridge}"
            "用自然的简体中文回答，让用户看懂你查到了什么、为什么重要、"
            "你建议 Codex 下一步做什么。带上可核实的位置、结果或反例，标明待验证的推断。"
            "Codex 会核实意见并作出最终决定。\n\n"
            f"{brief}"
        )
    else:
        prompt = (
            f"请基于共同目标给出独立建议。{instructions}"
            "先回答用户当前要决定的问题。有实质取舍时，按用户目标比较可行方案，"
            "给出一个能区分方案的例子、预期结果，以及什么新证据会改变建议。"
            "如有对标材料，判断哪些做法在当前约束下可迁移，指出尚缺的证据。请用简体中文简洁回答。"
            "Codex 会核实意见并作出最终决定。\n\n"
            f"讨论的问题：\n{question}\n\n{brief}"
        )
    route = transport.get("route", DEFAULT_ROUTE)
    resuming = desktop_record(out, tag, route).is_file() or transport.get("desktop_session_id")
    overlay = None if resuming or transport.get("codex_results") else collaboration_overlay(out, route, transport)
    answer, session_id = ask_dsh(out, overlay, tag, prompt, **transport)
    readable = out / f"{tag}.md"
    readable.write_text(answer.rstrip() + "\n", encoding="utf-8")
    provider, model = ROUTES[route]
    report = {"answer": answer, "provider": provider, "model": model, "route": route, "session_id": session_id,
              "transport": "desktop" if transport.get("desktop_session_id") else "headless"}
    if transport.get("codex_results"):
        report["codex_evidence"] = evidence_receipt(out, tag).name
    if paths and (transport.get("code_task") or not transport.get("codex_results")):
        report["inspection"] = "source-read" if transport.get("code_task") else "scoped-tools"
    if transport.get("independent"):
        report["run_mode"] = "native-tools"
    result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"DSH 已回复，建议原文：{readable}（{provider}/{model}，会话 {session_id}）", flush=True)
    return 0


def snapshot(workspace, names):
    sections = []
    for name in names:
        path = (workspace / name).resolve()
        if Path(name).is_absolute() or not path.is_relative_to(workspace):
            raise RuntimeError("Source files must be relative paths inside the workspace.")
        sections.append(f"文件：{path.relative_to(workspace).as_posix()}\n" +
                        path.read_bytes().decode("utf-8"))
    return "\n\n".join(sections)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--doctor", action="store_true", help="Check the local launcher without a model call.")
    parser.add_argument("--goal", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--source", nargs="+")
    parser.add_argument("--check", nargs=argparse.REMAINDER,
                        help="Optional acceptance command. Put this option last.")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--dsh-workspace", type=Path,
                        help="可选，使用 DSH 中已有的绝对工作区目录，记录仍保存在 --run-dir。")
    parser.add_argument("--dsh-title", help="任务中文短标题，首次创建时写入 DSH 会话。")
    parser.add_argument("--sync-sidebar", action="store_true", help="只同步已绑定会话的侧栏，不调用模型。")
    parser.add_argument("--round", type=int, choices=range(1, MAX_REVIEWS + 1), default=1)
    discussion = parser.add_mutually_exclusive_group()
    discussion.add_argument("--explore", action="store_true", help="让 DSH 从用户目标出发独立发现问题和提出做法。")
    discussion.add_argument("--advice", help="Ask DSH a specific follow-up question.")
    parser.add_argument("--advice-label", help="Optional unique suffix for an advice record.")
    parser.add_argument("--context", type=Path, help="Shared UTF-8 facts and constraints.")
    benchmark_options = parser.add_mutually_exclusive_group()
    benchmark_options.add_argument("--benchmark", type=Path,
                                   help="共享的 UTF-8 项目对标摘录，省略时沿用本任务保存的材料。")
    benchmark_options.add_argument("--clear-benchmark", action="store_true",
                                   help="清空同一任务后续使用的对标摘录，保留已发送的记录。")
    parser.add_argument("--decisions", type=Path,
                        help="Codex's UTF-8 disposition of prior findings, for round 2.")
    parser.add_argument("--evidence", type=Path,
                        help="与本轮快照对应的 UTF-8 验收摘要，包含命令、结果和验证范围。")
    parser.add_argument("--inspect-root", action="append", type=Path, default=[],
                        help="授权 DSH 查看元数据的绝对路径，可重复。")
    parser.add_argument("--inspect-file", action="append", type=Path, default=[],
                        help="授权 DSH 读文本的单个绝对文件路径，可重复。")
    parser.add_argument("--ask-codex", action="store_true", help="允许 DSH 主动请 Codex 查证关键事实。")
    parser.add_argument("--code-task", action="store_true", help="实际代码协作。必须读取授权文件并可主动请求查证。")
    parser.add_argument("--independent", action="store_true",
                        help="在 --workspace 用 DSH 自己的公开搜索、网页阅读、文件和终端工具取证。")
    parser.add_argument("--project-write", action="store_true", help="原生工具模式下明确授权项目目录写入。")
    parser.add_argument("--effort", choices=("high", "max"), help="本次 WorkBuddy Flash 思考档位；Hy4 使用 high。")
    parser.add_argument("--codex-results", type=Path, help="沿用原命令，用此文件回传匹配的查证结果。")
    parser.add_argument("--timeout", type=int, default=240, help="每条命令行模型路线的等待上限（秒）。")
    parser.add_argument("--total-timeout", type=int,
                        help="本轮命令行总预算（秒）。默认 480 秒，显式的单路线等待更长时相应放宽。")
    parser.add_argument("--route", choices=("auto", *ROUTES), default="auto",
                        help="默认依序尝试 WorkBuddy 路线；显式指定时只用该路线。")
    parser.add_argument("--desktop-answer", type=Path, help="可选，用于与会话原文比对的本地回复文件。")
    parser.add_argument("--desktop-session-id", help="导回并核对所选 WorkBuddy 路线的桌面会话 ID。")
    args = parser.parse_args()
    if args.doctor:
        check_plugin()
        result = subprocess.run(dsh_command("--version"), cwd=HERE, env=dsh_environment(),
                                capture_output=True, timeout=20, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip()
                               or f"DSH launcher failed: exit={result.returncode}")
        version = result.stdout.decode("utf-8", errors="replace").strip()
        if not version:
            raise RuntimeError("DSH launcher returned no version.")
        print(f"DSH {version} | WorkBuddy plugin found | default route: {PROVIDER}/{MODEL}")
        print(f"Independent public search: {'plugin found' if PUBLIC_SEARCH.is_file() else 'plugin missing'}")
        return 0
    if args.sync_sidebar:
        if args.run_dir is None:
            parser.error("--sync-sidebar 需要原任务的 --run-dir。")
        out = args.run_dir.resolve()
        if not out.is_dir():
            raise RuntimeError("找不到原任务的运行目录。")
        with task_lock(out):
            session_id = conversation(out).get("session_id")
            workspace_file = out / "dsh-workspace.txt"
            if not session_id or not workspace_file.is_file():
                raise RuntimeError("原任务没有已绑定的 DSH 会话和工作区。")
            workspace = Path(workspace_file.read_text(encoding="utf-8"))
            if not workspace.is_absolute() or not workspace.is_dir():
                raise RuntimeError("原任务的 DSH 工作区不可用。")
            attach_dsh_workspace(out, workspace, session_id)
        return 0
    if args.goal is None or args.run_dir is None:
        parser.error("--goal and --run-dir are required for advice and review")
    if args.timeout < 1 or (args.total_timeout is not None and args.total_timeout < 1):
        parser.error("--timeout and --total-timeout must be positive")
    if args.dsh_workspace is not None and (not args.dsh_workspace.is_absolute() or not args.dsh_workspace.is_dir()):
        parser.error("--dsh-workspace 必须是已有的绝对工作区目录")
    if args.desktop_answer and not args.desktop_session_id:
        parser.error("--desktop-answer 需要同时提供 --desktop-session-id")
    if args.desktop_session_id and not SESSION_ID.fullmatch(args.desktop_session_id):
        parser.error("--desktop-session-id 需要有效的 DSH 会话 ID")
    if any(not path.is_absolute() or not path.exists() or path.is_symlink()
           for path in (*args.inspect_root, *args.inspect_file)):
        parser.error("只读取证路径必须是已存在的物理绝对路径")
    if any(not path.is_file() for path in args.inspect_file):
        parser.error("--inspect-file 只接受明确授权的单个文本文件")
    if args.desktop_session_id and (args.inspect_root or args.inspect_file or args.ask_codex or args.codex_results):
        parser.error("只读取证需要命令行的限定工具，不能导入没有同一工具限制的桌面会话。")
    roots = tuple(dict.fromkeys(path.resolve() for path in args.inspect_root))
    files = tuple(dict.fromkeys(path.resolve() for path in args.inspect_file))
    transport = dict(desktop_answer=args.desktop_answer,
                     desktop_session_id=args.desktop_session_id)
    if args.ask_codex or args.codex_results:
        transport["ask_codex"] = True
    if args.effort is not None:
        transport["effort"] = args.effort
    if args.codex_results:
        transport["codex_results"] = args.codex_results
    if roots:
        transport["inspect_roots"] = roots
    if files:
        transport["inspect_files"] = files
    if args.explore and args.benchmark:
        parser.error("--explore 首轮只发目标与事实。带候选做对照时使用 --advice。")
    if args.explore or args.advice is not None:
        if args.advice is not None and not args.advice.strip():
            parser.error("--advice cannot be blank")
        if args.source is not None or args.check is not None or args.decisions or args.evidence:
            parser.error("--explore and --advice accept --context, not --source, --check, --decisions or --evidence")
        if args.advice_label and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", args.advice_label):
            parser.error("--advice-label must be 1–40 lowercase letters, digits or hyphens")
    else:
        if args.advice_label:
            parser.error("--advice-label requires --explore or --advice")
        if args.decisions and args.round != 2:
            parser.error("--decisions requires review round 2")
        if not args.source:
            parser.error("review requires one or more --source paths")
        if args.check == []:
            parser.error("--check requires a command")
    goal = args.goal.resolve().read_text(encoding="utf-8-sig")
    if not goal.strip():
        parser.error("--goal file cannot be blank")
    out = args.run_dir.resolve()
    with task_lock(out):
        return run_task(args, parser, goal, out, transport)


def run_task(args, parser, goal, out, transport):
    code_record = out / ".dsh-code-task"
    code_task = args.code_task or code_record.is_file()
    independent_record = out / ".dsh-independent"
    independent = args.independent or independent_record.is_file()
    if independent:
        if code_task:
            parser.error("--code-task 选用限定文件的代码审查；--independent 使用项目工作区的原生工具。")
        transport.update(independent=True, project_write=args.project_write)
    elif args.project_write:
        parser.error("--project-write 需要 --independent。")
    if code_task:
        if args.desktop_session_id:
            parser.error("代码协作需要命令行的受限读取工具及取证轨迹。")
        transport.update(code_task=True, ask_codex=True)
    dsh_workspace_record = out / "dsh-workspace.txt"
    dsh_workspace = args.dsh_workspace.resolve() if args.dsh_workspace is not None else None
    if independent:
        project = args.workspace.resolve()
        if not project.is_dir() or (dsh_workspace is not None and dsh_workspace != project):
            parser.error("--independent 的 --workspace 必须是实际项目目录，并与 --dsh-workspace 一致。")
        dsh_workspace = project
    if dsh_workspace_record.is_file():
        recorded = Path(dsh_workspace_record.read_text(encoding="utf-8"))
        if not recorded.is_absolute() or (dsh_workspace is not None and dsh_workspace != recorded):
            raise RuntimeError("DSH 工作区与已绑定的任务工作区不一致。")
        dsh_workspace = recorded
    else:
        existing = bool(conversation(out) or any(out.glob("*.stdout.log")))
        if dsh_workspace is None and not existing:
            dsh_workspace = sidebar.configured_workspace(DSH_ROOT)
        if dsh_workspace is not None and dsh_workspace != out and existing:
            raise RuntimeError("已有 DSH 会话，不能更换它的工作区。")
        if dsh_workspace is not None:
            dsh_workspace_record.write_text(str(dsh_workspace), encoding="utf-8")
    if dsh_workspace is not None:
        transport["dsh_workspace"] = dsh_workspace
    if args.dsh_title or dsh_workspace is not None:
        heading = goal.strip().splitlines()[0].lstrip("# ").strip()
        title = " ".join((args.dsh_title or heading).split())[:40].strip()
        if title:
            transport["dsh_title"] = title
    previous_workspace = out / "review-workspace.txt"
    is_discussion = args.explore or args.advice is not None
    if is_discussion:
        tag = f"advice-{args.advice_label}" if args.advice_label else "advice"
        if (out / f"{tag}.json").exists():
            raise RuntimeError("Advice already exists. Use a different advice label.")
        if code_task and not transport.get("inspect_files"):
            parser.error("代码讨论至少需要一个 --inspect-file 授权文件。")
    else:
        workspace = args.workspace.resolve()
        tag = f"review-{args.round}"
        if (out / f"{tag}.json").exists():
            raise RuntimeError("That review already has a result. Use a new run directory.")
        if previous_workspace.exists():
            recorded = Path(previous_workspace.read_text(encoding="utf-8"))
            if not recorded.is_absolute() or recorded.resolve() != workspace:
                raise RuntimeError("Review workspace changed. Start a new run directory.")
        elif args.round == 2:
            raise RuntimeError("Missing round-1 workspace record. Start a new round 1.")
        prior = ""
        if args.round == 2:
            try:
                prior_report = json.loads((out / "review-1.json").read_text(encoding="utf-8"))
            except FileNotFoundError as error:
                raise RuntimeError("Round 1 has no completed review. Run round 1 again.") from error
            prior_md = out / "review-1.md"
            if prior_md.is_file():
                prior = prior_md.read_text(encoding="utf-8")
            else:
                # Older runs stored only the machine-readable fields.
                issues = prior_report["findings"]
                doubts = prior_report["questions"]
                status = "需修订" if issues else "待确认" if doubts else "通过"
                findings = "\n".join(f"- {item}" for item in issues) or "无"
                questions = "\n".join(f"- {item}" for item in doubts) or "无"
                prior = f"结论：{status}\n发现的问题：\n{findings}\n待确认：\n{questions}"
        decisions = args.decisions.read_text(encoding="utf-8-sig") if args.decisions else ""
        evidence = args.evidence.read_text(encoding="utf-8-sig") if args.evidence else ""
        if args.evidence and not evidence.strip():
            parser.error("--evidence 需要非空的验收摘要")
        source = snapshot(workspace, args.source)
        if code_task or independent:
            if args.round == 2 and not evidence.strip():
                parser.error("代码复查须用 --evidence 回传当前版本的实际验收结果。")
            source_files = tuple((workspace / name).resolve() for name in args.source)
            if code_task:
                transport["inspect_files"] = tuple(dict.fromkeys((*source_files, *transport.get("inspect_files", ()))))
            else:
                transport["native_sources"] = source_files

    goal_snapshot = out / "shared-goal.txt"
    if goal_snapshot.exists():
        if goal_snapshot.read_text(encoding="utf-8-sig") != goal:
            raise RuntimeError("Shared goal changed. Start a new run directory.")
    context_snapshot = out / "shared-context.txt"
    if args.context:
        context = args.context.read_text(encoding="utf-8-sig")
    else:
        context = context_snapshot.read_text(encoding="utf-8-sig") if context_snapshot.exists() else ""
    benchmark_snapshot = out / "shared-benchmark.md"
    if args.benchmark:
        benchmark = args.benchmark.read_text(encoding="utf-8-sig")
        if not benchmark.strip():
            parser.error("--benchmark 需要非空的对标摘录")
    elif args.clear_benchmark or args.explore:
        benchmark = ""
    else:
        benchmark = benchmark_snapshot.read_text(encoding="utf-8-sig") if benchmark_snapshot.exists() else ""
    if not goal_snapshot.exists():
        goal_snapshot.write_text(goal, encoding="utf-8")
    if args.route == "auto":
        state = conversation(out)
        preferred = state.get("usable_route")
        routes = tuple(sorted(ROUTES, key=lambda route: route != preferred))
    else:
        routes = (args.route,)
    pending = tuple(route for route in routes if desktop_record(out, tag, route).is_file())
    if args.desktop_session_id and args.route == "auto" and len(pending) != 1:
        parser.error("导回桌面回复请用 --route 指定交接记录对应的路线。")
    if args.codex_results:
        receipt = json.loads(evidence_receipt(out, tag).read_text(encoding="utf-8"))
        route = receipt.get("route") if args.route == "auto" else args.route
        if route not in ROUTES:
            raise RuntimeError("证据请求的 WorkBuddy 路线无效。")
        routes = (route,)
    elif pending:
        # An existing handoff is a recovery request, not a fresh model run.
        routes = pending
    else:
        if args.route == "auto":
            routes = tuple(route for route in routes if route not in state.get("unavailable_routes", ()))
            if not routes:
                raise DSHResponseError("本任务的 WorkBuddy 路线都报告模型不可用。")
        if args.context:
            context_snapshot.write_text(context, encoding="utf-8")
        if args.benchmark or args.clear_benchmark:
            benchmark_snapshot.write_text(benchmark, encoding="utf-8")
    brief = task_brief(goal, context, benchmark)
    if code_task and not code_record.exists():
        code_record.touch()
    if independent and not independent_record.exists():
        independent_record.touch()
    print(f"Run record: {out}", flush=True)
    budget = dict(timeout=args.timeout, total_timeout=args.total_timeout or max(480, args.timeout))
    if is_discussion:
        return try_workbuddy_routes(
            out, routes, lambda route, timeout: advise(out, brief, args.advice, args.advice_label,
                                                       route=route, timeout=timeout, **transport), **budget
        )
    if not previous_workspace.exists():
        previous_workspace.write_text(str(workspace), encoding="utf-8")
    def request_review(route, timeout):
        resuming = desktop_record(out, tag, route).is_file() or args.desktop_session_id
        overlay = None if resuming or transport.get("codex_results") else collaboration_overlay(out, route, transport)
        return review(out, overlay, brief, source, tag, prior, decisions, evidence,
                      route=route, timeout=timeout, **transport)

    verdict, questions = try_workbuddy_routes(out, routes, request_review, **budget)
    if args.check is not None:
        check_code, _ = run_stage(f"check-{args.round}", args.check,
                                  workspace=workspace, out=out)
        if check_code:
            print("CHECK_FAILED", flush=True)
            return 2
    try:
        current_source = snapshot(workspace, args.source)
    except (OSError, UnicodeError, RuntimeError) as error:
        print(f"SNAPSHOT_CHANGED: {error}", flush=True)
        return 2
    if current_source != source:
        print("SNAPSHOT_CHANGED", flush=True)
        return 2
    if verdict == "FAIL":
        print("REVIEW_HAS_FINDINGS", flush=True)
        return 2
    if questions:
        print("REVIEW_HAS_QUESTIONS", flush=True)
        return 2
    if args.check is None:
        print("REVIEW_PASSED", flush=True)
    else:
        print("ACCEPTED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DesktopFallbackRequired as error:
        print(error, file=sys.stderr)
        sys.exit(DESKTOP_FALLBACK_EXIT)
    except CodexEvidencePending as error:
        print(error, file=sys.stderr)
        sys.exit(CODEX_EVIDENCE_EXIT)
    except KeyboardInterrupt:
        print("本轮已中断。", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        sys.exit(str(error))
