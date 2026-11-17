"""Install and notify the optional DSH Desktop sidebar adapter."""

import argparse
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4


HERE = Path(__file__).resolve().parent
BEGIN = "# BEGIN codex-sidebar-sync"
END = "# END codex-sidebar-sync"


def atomic_write(path, data):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def install(home, workspace):
    registry = json.loads((home / "storages/workspace.json").read_text(encoding="utf-8-sig"))
    groups = registry.get("tables", {}).get("workspaces", {})
    workspace_id = next((key for key, row in groups.items() if Path(row["path"]).resolve() == workspace), None)
    if workspace_id is None:
        raise RuntimeError("请先在 DSH 桌面登记共用工作区。")
    profile = home / "profiles/desktop"
    patch = profile / "cordis.patch.yml"
    raw = patch.read_bytes() if patch.exists() else b""
    current = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in current else "\n"
    before, start, rest = current.partition(BEGIN + newline)
    if start:
        _, stop, after = rest.partition(END + newline)
        if not stop or BEGIN in after or "codex-sidebar-sync" in before + after:
            raise RuntimeError("找到不完整或重复的侧栏插件配置。")
    else:
        after = ""
        first = next((line.strip() for line in before.splitlines() if line.strip() and not line.lstrip().startswith("#")), "")
        if "codex-sidebar-sync" in before or (first and not first.startswith("- ")):
            raise RuntimeError("侧栏安装需要桌面 profile 使用常规补丁列表。")

    directory = home / "codex-sidebar"
    directory.mkdir(parents=True, exist_ok=True)
    adapter_source = (HERE / "sidebar_sync.mjs").read_bytes()
    browser_source = (HERE / "sidebar_client.js").read_text(encoding="utf-8").replace(
        '"__WORKSPACE_ID__"', json.dumps(workspace_id)
    ).encode("utf-8")
    revision = sha256(adapter_source + browser_source).hexdigest()[:12]
    bundle = directory / f"adapter-{revision}"
    bundle.mkdir(exist_ok=True)
    adapter = bundle / "index.mjs"
    manifest = {
        "name": "codex-dsh-sidebar-sync",
        "exports": {".": "./index.mjs", "./client": "./client.js"},
        "dsh": {"client": {"platform": "web", "immediately": True, "inject": [
            "@deepseek-ai/dsh-api-session-controller",
            "@deepseek-ai/dsh-api-workspace-controller",
        ]}},
    }
    for path, data in ((adapter, adapter_source), (bundle / "client.js", browser_source),
                       (bundle / "package.json", json.dumps(manifest).encode("utf-8"))):
        if not path.exists() or path.read_bytes() != data:
            atomic_write(path, data)
    block = (
        f"{BEGIN}\n- insert:\n"
        f"  - id: codex-sidebar-sync-{revision}\n"
        f"    name: {json.dumps(adapter.as_posix(), ensure_ascii=False)}\n"
        "    config:\n"
        f"      directory: {json.dumps(directory.as_posix(), ensure_ascii=False)}\n"
        f"      workspace: {json.dumps(str(workspace), ensure_ascii=False)}\n"
        f"{END}\n"
    )
    updated = (before if not before or before.endswith("\n") else before + newline) + block.replace("\n", newline) + after
    if updated != current:
        if patch.exists():
            backup = directory / f"desktop-patch-{datetime.now():%Y%m%d-%H%M%S-%f}.yml.bak"
            shutil.copy2(patch, backup)
        encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
        atomic_write(patch, updated.encode(encoding))
    atomic_write(directory / "workspace.json", json.dumps(str(workspace), ensure_ascii=False).encode("utf-8"))


def configured_workspace(home):
    setting = home / "codex-sidebar/workspace.json"
    if not setting.is_file():
        return None
    path = Path(json.loads(setting.read_text(encoding="utf-8"))).resolve()
    return path if path.is_dir() else None


def notify(home, workspace, session_id, *, timeout=6):
    """Return the host's title, False if queued, or None if unconfigured."""
    directory = home / "codex-sidebar"
    if configured_workspace(home) != workspace:
        return None
    token = str(uuid4())
    atomic_write(directory / f"{token}.publish.json", json.dumps({"sessionId": session_id}).encode("utf-8"))
    receipt = directory / f"{token}.ready.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if receipt.is_file():
            result = json.loads(receipt.read_text(encoding="utf-8"))
            if result.get("sessionId") != session_id or not result.get("workspaceId") or not result.get("title"):
                raise RuntimeError("DSH 侧栏回执与当前会话不匹配。")
            receipt.unlink()
            return result["title"]
        time.sleep(0.15)
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    dsh_home = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").resolve()
    install(dsh_home, args.workspace.resolve(strict=True))
    print("已配置 DSH 桌面侧栏同步。")
