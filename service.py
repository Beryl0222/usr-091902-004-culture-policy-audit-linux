"""HTTP 入口：健康检查 + 策略治理接口。

- 数据文件由环境变量 CULTURE_POLICY_DB 指定（默认内存模式，重启即空）；
- 所有写接口都是 POST JSON，角色由请求体 actor={id,role} 携带（联调用）；
- 业务规则错误返回 409/422 与稳定错误码，不泄露堆栈。
"""

import argparse
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from app import Actor, Application
from common import AuthError, DomainError

SERVICE_ID = "culture-policy-audit"
SERVICE_NAME = "文化推荐策略治理"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class ApiState:
    def __init__(self, db_path=None):
        self.app = Application(db_path)
        self.lock = threading.RLock()


STATE = ApiState(os.environ.get("CULTURE_POLICY_DB") or None)


def _actor(body):
    raw = body.get("actor")
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("role"):
        raise DomainError("bad_actor", "请求必须包含 actor: {id, role}")
    return Actor(str(raw["id"]), str(raw["role"]))


def _policy_id_version(pid_text):
    """支持 p1 或 p1:v2 形式。"""
    if ":" in pid_text:
        pid, ver = pid_text.rsplit(":", 1)
        return pid, int(ver)
    return pid_text, None


class Handler(BaseHTTPRequestHandler):
    server_version = "CulturePolicyAudit/1.0"

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError("bad_json", f"请求体不是合法 JSON: {exc}")
        if not isinstance(body, dict):
            raise DomainError("bad_json", "请求体必须是 JSON 对象")
        return body

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/health":
                self._send(200, health_payload())
                return
            with STATE.lock:
                if path == "/policies":
                    self._send(200, {"policies": STATE.app.list_policies()})
                elif m := re.fullmatch(r"/policies/([^/]+)", path):
                    pid, ver = _policy_id_version(m.group(1))
                    self._send(200, STATE.app.policy_view(pid, ver))
                elif m := re.fullmatch(r"/experiments/([^/]+)", path):
                    self._send(200, STATE.app.experiment_view(m.group(1)))
                elif m := re.fullmatch(r"/experiments/([^/]+)/replay", path):
                    day = query.get("day", [None])[0]
                    if day is None:
                        raise DomainError("bad_query", "必须提供 day（Unix 日分区，ts//86400）")
                    self._send(200, STATE.app.reproduce_day(m.group(1), int(day)))
                elif path == "/metrics":
                    self._send(200, STATE.app.metrics_report())
                elif path == "/metrics/compare":
                    self._send(200, {"comparisons": STATE.app.compare_arms()})
                elif path == "/quarantine":
                    self._send(200, {"events": STATE.app.quarantine_list()})
                elif path == "/journal/verify":
                    self._send(200, STATE.app.verify_journal())
                elif m := re.fullmatch(r"/creators/([^/]+)/channels", path):
                    body = {"actor": {"id": m.group(1), "role": "creator"}}
                    actor = _actor(body)
                    self._send(200, {"contents": STATE.app.creator_view(actor, m.group(1))})
                else:
                    self._send(404, {"error": "not_found", "message": "unknown route"})
        except (DomainError, AuthError) as exc:
            self._send_error(exc)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            with STATE.lock:
                self._route_post(path, body)
        except (DomainError, AuthError) as exc:
            self._send_error(exc)

    def _route_post(self, path, body):
        app = STATE.app
        actor = _actor(body)

        def without_actor():
            return {k: v for k, v in body.items() if k != "actor"}

        if path == "/content/register":
            self._send(201, app.register_content(actor, **without_actor()))
        elif path == "/policies/draft":
            self._send(201, app.draft_policy(actor, **without_actor()))
        elif m := re.fullmatch(r"/policies/([^/]+)/revise", path):
            pid, _ = _policy_id_version(m.group(1))
            self._send(201, app.revise_policy(actor, pid, **without_actor()))
        elif m := re.fullmatch(r"/policies/([^/]+)/submit", path):
            pid, ver = _policy_id_version(m.group(1))
            self._send(200, app.submit_policy(actor, pid, ver))
        elif m := re.fullmatch(r"/policies/([^/]+)/approve", path):
            pid, ver = _policy_id_version(m.group(1))
            self._send(200, app.approve_policy(
                actor, pid, body["role"], body.get("comment", ""), ver))
        elif m := re.fullmatch(r"/policies/([^/]+)/reject", path):
            pid, ver = _policy_id_version(m.group(1))
            self._send(200, app.reject_policy(actor, pid, body["role"], body["reason"], ver))
        elif m := re.fullmatch(r"/policies/([^/]+)/rollback", path):
            pid, ver = _policy_id_version(m.group(1))
            self._send(200, app.rollback_policy(actor, pid, body["reason"], ver))
        elif m := re.fullmatch(r"/policies/([^/]+)/end", path):
            pid, ver = _policy_id_version(m.group(1))
            self._send(200, app.end_policy(actor, pid, ver))
        elif path == "/experiments":
            self._send(201, app.create_experiment(
                actor, body["experiment_id"], body["hypothesis"]))
        elif m := re.fullmatch(r"/experiments/([^/]+)/segments", path):
            kwargs = without_actor()
            self._send(201, app.open_segment(actor, m.group(1), kwargs.pop("policy_id"),
                                             kwargs.pop("version", None), **kwargs))
        elif m := re.fullmatch(r"/experiments/([^/]+)/segments/close", path):
            self._send(200, app.close_segment(actor, m.group(1), body["reason"],
                                              body.get("detail", "")))
        elif path == "/rank":
            self._send(200, app.rank(actor, **without_actor()))
        elif path == "/exposures":
            self._send(202, app.log_exposure(actor, **without_actor()))
        elif path == "/feedback":
            self._send(202, app.receive_feedback(actor, **without_actor()))
        elif path == "/feedback/withdraw":
            self._send(200, app.withdraw(actor, **without_actor()))
        elif path == "/privacy/profile":
            self._send(200, app.set_profile(actor, **without_actor()))
        elif path == "/privacy/reset":
            self._send(200, app.reset_interest(actor, **without_actor()))
        elif path == "/privacy/affinity":
            self._send(200, app.record_affinity(actor, **without_actor()))
        elif path == "/channels/admit":
            self._send(201, app.admit_channel(actor, **without_actor()))
        elif path == "/channels/exit":
            self._send(200, app.exit_channel(actor, **without_actor()))
        elif m := re.fullmatch(r"/creators/([^/]+)/channels", path):
            self._send(200, {"contents": app.creator_view(actor, m.group(1))})
        else:
            self._send(404, {"error": "not_found", "message": "unknown route"})

    def _send_error(self, exc):
        code = getattr(exc, "code", "error")
        status = 403 if isinstance(exc, AuthError) else 422
        if code in ("forbidden",):
            status = 403
        elif code in ("policy_not_found", "experiment_not_found", "content_not_found",
                      "event_not_found", "decision_not_found"):
            status = 404
        elif code in ("illegal_transition", "policy_exists", "experiment_exists",
                      "segment_overlap", "channel_conflict", "already_approved",
                      "already_withdrawn", "content_exists"):
            status = 409
        self._send(status, {"error": code, "message": exc.message})

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data", default=None, help="事件日志文件路径（默认内存）")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 领域模块冒烟：空状态可出指标、哈希链完好
        assert STATE.app.verify_journal()["intact"]
        print("基础检查通过")
        return
    if args.data:
        STATE.app = Application(args.data)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
