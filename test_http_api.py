"""HTTP 接口集成测试：临时数据文件 + 真实端口，覆盖主要治理链路。"""

import json
import os
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import common
from caliber import day_key

DAY = 86400
HOUR = 3600
T0 = 1750000000
DAY0 = (T0 // DAY) * DAY


def http(method, url, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = Request(url, data=data, method=method,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.load(resp)
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".journal")
        cls.tmp.close()
        os.environ["CULTURE_POLICY_DB"] = cls.tmp.name
        # 在设置环境变量后导入 service，使其使用临时库
        import importlib
        import service
        cls.service = importlib.reload(service)
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        os.unlink(cls.tmp.name)
        os.environ.pop("CULTURE_POLICY_DB", None)

    def setUp(self):
        common.set_clock(fixed_ts=DAY0 + 3 * HOUR)

    def tearDown(self):
        common.set_clock()

    def test_health(self):
        status, body = http("GET", f"{self.base}/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "culture-policy-audit")

    def test_full_flow_over_http(self):
        b = self.base
        pm = {"id": "pm1", "role": "product_manager"}
        co = {"id": "co1", "role": "content_owner"}
        ro = {"id": "ro1", "role": "risk_owner"}
        ed = {"id": "ed1", "role": "content_editor"}

        # 登记长短两条内容
        for cid, dur in (("long1", 3 * HOUR), ("short1", 60)):
            status, _ = http("POST", f"{b}/content/register", {
                "actor": ed, "content_id": cid, "title": cid, "creator_id": "cr1",
                "category": "非遗" if cid == "long1" else "生活",
                "duration_seconds": dur,
                "features": {"finish_rate": 0.4 if cid == "long1" else 0.95,
                             "favorite": 0.8 if cid == "long1" else 0.2,
                             "revisit": 0.7 if cid == "long1" else 0.1,
                             "discussion_quality": 0.7, "diversity": 0.5}})
            self.assertEqual(status, 201)

        # 缺角色
        status, body = http("POST", f"{b}/policies/draft", {"policy_id": "x"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "bad_actor")

        # 产品草拟
        status, body = http("POST", f"{b}/policies/draft", {
            "actor": pm, "policy_id": "p1", "objectives": ["长内容曝光"],
            "weights": {"finish_rate": 0.4, "favorite": 0.2, "revisit": 0.2,
                        "discussion_quality": 0.15, "diversity": 0.05},
            "audience": {"name": "all"}, "start_ts": DAY0,
            "end_ts": DAY0 + 30 * DAY, "traffic_percent": 10})
        self.assertEqual(status, 201, body)

        # 未提交时批准 -> 409
        status, body = http("POST", f"{b}/policies/p1/approve",
                            {"actor": co, "role": "content_owner"})
        self.assertEqual(status, 409)

        http("POST", f"{b}/policies/p1/submit", {"actor": pm})
        status, _ = http("POST", f"{b}/policies/p1/approve",
                         {"actor": co, "role": "content_owner", "comment": "ok"})
        self.assertEqual(status, 200)
        status, _ = http("POST", f"{b}/policies/p1/approve",
                         {"actor": ro, "role": "risk_owner", "comment": "ok"})
        self.assertEqual(status, 200)

        status, _ = http("POST", f"{b}/experiments",
                         {"actor": pm, "experiment_id": "e1", "hypothesis": "h"})
        self.assertEqual(status, 201)
        status, seg = http("POST", f"{b}/experiments/e1/segments", {
            "actor": pm, "policy_id": "p1", "start_ts": DAY0,
            "end_ts": DAY0 + 10 * DAY, "traffic_percent": 10})
        self.assertEqual(status, 201, seg)

        # 分流
        status, rank = http("POST", f"{b}/rank", {
            "actor": ed, "subject_ref": "user-1", "content_ids": ["long1", "short1"],
            "user_attrs": {}, "experiment_id": "e1", "ts": DAY0 + 2 * HOUR})
        self.assertEqual(status, 200)
        self.assertIn(rank["variant"], ("treatment", "baseline", "off"))

        # 曝光
        items = [{"content_id": r["content_id"],
                  "category": "非遗" if r["content_id"] == "long1" else "生活",
                  "duration_seconds": 3 * HOUR if r["content_id"] == "long1" else 60}
                 for r in rank["ranked"]]
        status, expo = http("POST", f"{b}/exposures", {
            "actor": ed, "event_id": "ev1", "subject_ref": "user-1",
            "decision_seq": rank["decision_seq"], "items": items,
            "happened_at": DAY0 + 2 * HOUR + 60})
        self.assertEqual(status, 202)
        self.assertTrue(expo["accepted"])

        # 重复曝光 -> 202 accepted=false, reason=duplicate
        status, dup = http("POST", f"{b}/exposures", {
            "actor": ed, "event_id": "ev1", "subject_ref": "user-1",
            "decision_seq": rank["decision_seq"], "items": items,
            "happened_at": DAY0 + 2 * HOUR + 60})
        self.assertFalse(dup["accepted"])
        self.assertEqual(dup["reason"], "duplicate")

        # 回放
        status, rep = http("GET", f"{b}/experiments/e1/replay?day={day_key(DAY0 + 2*HOUR)}")
        self.assertEqual(status, 200)
        self.assertTrue(rep["all_reproducible"])
        self.assertEqual(rep["count"], 1)

        # 隔离清单与指标可读
        status, q = http("GET", f"{b}/quarantine")
        self.assertEqual(status, 200)
        self.assertTrue(any(e["reason"] == "duplicate" for e in q["events"]))
        status, report = http("GET", f"{b}/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(report["caliber"], "caliber-v1")

        # 未知路由 404
        status, _ = http("GET", f"{b}/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
