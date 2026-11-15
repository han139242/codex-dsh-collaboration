import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bridge_tests


REQUEST_ID = "00000000-0000-0000-0000-000000000001"
REQUEST = {"request_id": REQUEST_ID, "title": "边界", "input": "输入", "expected": "预期"}
RESULT = {"request_id": REQUEST_ID, "status": "passed", "evidence": "实际结果"}


class BridgeTest(unittest.TestCase):
    def test_only_completed_results_for_the_requested_tool_are_accepted(self):
        events = [
            {"type": "tool_call", "tool": bridge_tests.REQUEST_TOOL, "callId": "a"},
            {"type": "tool_call", "tool": bridge_tests.RESULTS_TOOL, "callId": "b"},
            {"type": "tool_result", "callId": "a", "status": "failed"},
            {"type": "tool_result", "callId": "b", "status": "completed", "result": "[]"},
            {"type": "tool_result", "callId": "a", "status": "completed", "result": json.dumps(REQUEST)},
        ]
        self.assertEqual(bridge_tests.tool_results(events, bridge_tests.REQUEST_TOOL), [REQUEST])

    def test_every_result_must_correspond_to_a_distinct_request(self):
        other = {**REQUEST, "request_id": "00000000-0000-0000-0000-000000000002"}
        bridge_tests.verify_results([REQUEST], [RESULT])
        bridge_tests.verify_results([REQUEST], [{**RESULT, "status": "not_run"}])
        for requests, results in (([REQUEST], []), ([other], [RESULT]), ([REQUEST, other], [RESULT, RESULT])):
            with self.subTest(requests=requests, results=results), self.assertRaises(RuntimeError):
                bridge_tests.verify_results(requests, results)

    def test_malformed_request_receipts_are_rejected(self):
        for requests in (None, [], [REQUEST, REQUEST], [{**REQUEST, "request_id": "wrong"}]):
            with self.subTest(requests=requests), self.assertRaises(RuntimeError):
                bridge_tests.verify_requests(requests)
