import json
from uuid import UUID


EVIDENCE_REQUEST = "codex_request_evidence"
EVIDENCE_RESULTS = "codex_evidence_results"
EVIDENCE_KINDS = ("file", "research", "blocker")


def tool_results(events, name):
    calls = {event["callId"] for event in events
             if event.get("type") == "tool_call" and event.get("tool") == name and event.get("callId")}
    return [json.loads(event["result"]) for event in events
            if event.get("type") == "tool_result" and event.get("callId") in calls
            and event.get("status") == "completed"]


def verify_requests(requests, fields):
    if not isinstance(requests, list) or not 1 <= len(requests) <= 3:
        raise RuntimeError("需要一至三项 DSH 工具请求。")
    seen = set()
    for item in requests:
        if not isinstance(item, dict) or set(item) != {"request_id", *fields}:
            raise RuntimeError("工具请求格式不完整。")
        if any(not isinstance(v, str) or not v.strip() or len(v) > 1200 for v in item.values()):
            raise RuntimeError("工具请求包含无效字段。")
        try:
            canonical_id = str(UUID(item["request_id"]))
        except ValueError as error:
            raise RuntimeError("工具请求编号无效。") from error
        if canonical_id != item["request_id"] or canonical_id in seen:
            raise RuntimeError("工具请求编号无效或重复。")
        seen.add(canonical_id)
    return seen


def verify_results(ids, results, statuses):
    if not isinstance(results, list) or len(results) != len(ids):
        raise RuntimeError("每条原请求都需要一条结果或未查到的原因。")
    remaining = ids.copy()
    for item in results:
        if (not isinstance(item, dict) or set(item) != {"request_id", "status", "evidence"}
                or not isinstance(item["request_id"], str) or item["request_id"] not in remaining
                or item["status"] not in statuses
                or not isinstance(item["evidence"], str) or not item["evidence"].strip()
                or len(item["evidence"]) > 4000):
            raise RuntimeError("结果编号或证据无效。")
        remaining.remove(item["request_id"])


def evidence_request_ids(requests):
    ids = verify_requests(requests, ("kind", "question", "why"))
    if any(item["kind"] not in EVIDENCE_KINDS for item in requests):
        raise RuntimeError("证据请求类型无效。")
    return ids
