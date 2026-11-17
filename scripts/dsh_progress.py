from collections import deque
from html import escape
import json
import re
import time


TOOL_LABELS = {
    "codex_inspect": "只读查证", "codex_request_evidence": "请 Codex 补证",
    "codex_evidence_results": "读取 Codex 补证", "codex_request_test": "请 Codex 跑测试",
    "codex_test_results": "读取测试结果", "read": "读文件", "grep": "搜索项目内容",
    "glob": "查找文件", "web_search": "搜索公开资料", "web_fetch": "打开原文",
    "pwsh": "运行 PowerShell", "bash": "运行 Shell", "subagent": "委托子任务",
}
SHELL_TOOLS = {"bash", "pwsh", "shell", "terminal"}


def local_shell_blocker(event):
    if event.get("status") == "completed":
        return False
    result = event.get("result")
    return isinstance(result, str) and (
        ("sandbox-local:" in result and ("Win32 5" in result or "SANDBOX_UNAVAILABLE" in result))
        or "STATUS_DLL_INIT_FAILED" in result
        or ("sandbox" in result.lower() and "3221225794" in result)
    )


def has_local_shell_failure(events):
    calls = {row.get("callId") for row in events
             if row.get("type") == "tool_call" and row.get("tool") in SHELL_TOOLS and row.get("callId")}
    return any(row.get("type") == "tool_result" and row.get("callId") in calls and local_shell_blocker(row)
               for row in events)


class RunTrace:
    def __init__(self, stream, started):
        self.stream = stream
        self.started = started
        self.first_activity = None
        self.last_activity = None
        self.session_seen = False
        self.calls = {}
        self.done = 0
        self.failed = 0
        self.blocked = 0
        self.evidence_requests = 0
        self.recent = deque(maxlen=5)
        self.final_at = None

    def drain(self):
        events = []
        now = time.monotonic()
        while line := self.stream.readline():
            if not line.endswith(b"\n"):
                self.stream.seek(-len(line), 1)
                break
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict):
                continue
            events.append(row)
            kind = row.get("type")
            if kind == "session":
                self.session_seen = True
            if kind in {"thinking", "text", "tool_call", "tool_result"}:
                if self.first_activity is None:
                    self.first_activity = now
                self.last_activity = now
            if kind == "tool_call" and isinstance(row.get("callId"), str):
                tool = row.get("tool")
                self.calls[row["callId"]] = tool if isinstance(tool, str) else ""
            elif kind == "tool_result" and isinstance(row.get("callId"), str):
                tool = self.calls.pop(row["callId"], None)
                if tool is None:
                    continue
                ok = row.get("status") == "completed"
                blocked = tool in SHELL_TOOLS and local_shell_blocker(row)
                self.done += int(ok)
                self.failed += int(not ok)
                self.blocked += int(blocked)
                self.evidence_requests += int(ok and tool == "codex_request_evidence")
                label = TOOL_LABELS.get(tool, tool if re.fullmatch(r"[A-Za-z0-9_-]{1,48}", tool) else "工具")
                result = "沙箱启动失败" if blocked else ("已返回" if ok else "失败")
                self.recent.append(f"{label} · {result}")
            elif kind == "final" and isinstance(row.get("reason"), dict):
                answer = row.get("text") or row.get("answer")
                if (row["reason"].get("kind") == "completed" and isinstance(answer, str)
                        and answer.strip() and row.get("sessionId")):
                    self.final_at = now
        return events

    def details(self, now):
        if not self.session_seen and self.first_activity is None:
            return ["正在等待 DSH 启动并建立会话。"]
        details = [f"本次尝试已用 {now - self.started:.1f} 秒。"]
        if self.first_activity is None:
            details.append("已连接会话，尚无可见的回复或工具动作。")
        else:
            details.append(f"启动后约 {self.first_activity - self.started:.1f} 秒出现首个可见动作，"
                           f"距最近可见动作 {int(now - self.last_activity)} 秒。")
        if self.calls or self.done or self.failed:
            details.append(f"工具已返回 {self.done} 项，失败 {self.failed} 项，正在等待 {len(self.calls)} 项。")
        if self.blocked:
            details.append(f"本机终端沙箱启动失败 {self.blocked} 次。")
        if self.evidence_requests:
            details.append(f"DSH 已提交 {self.evidence_requests} 项 Codex 查证请求。")
        return details


def write_progress(out, label, status, stamp, links, details, recent):
    out_file = out / "progress.html"
    temp = out / ".progress.html.tmp"
    facts = "".join(f"<li>{escape(item)}</li>" for item in details)
    timeline = "".join(f"<li>{escape(item)}</li>" for item in reversed(recent))
    actions = " · ".join(f'<a href="{escape(file, quote=True)}">{escape(name)}</a>' for name, file in links)
    temp.write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="refresh" content="5"><title>Codex + DSH 协作进度</title>'
        '<style>body{margin:0;background:#f4f5f8;color:#20232a;font:16px/1.65 system-ui,sans-serif}'
        'main{max-width:650px;margin:8vh auto;padding:24px 28px;background:white;border:1px solid #dfE2e8;'
        'border-radius:12px}h1{font-size:24px}h2{font-size:16px}small{color:#4c566a}'
        'a{color:#184d9a}code{overflow-wrap:anywhere}</style>'
        f'<main><h1>Codex + DSH 协作进度</h1><p>{escape(status)}</p>'
        f'<small><code>{escape(label)}</code> · {escape(stamp)}</small>'
        f'<ul>{facts}</ul>' + (f'<h2>最近的工具结果</h2><ul>{timeline}</ul>' if recent else "")
        + f'<p>{actions}</p></main></html>', encoding="utf-8")
    temp.replace(out_file)
