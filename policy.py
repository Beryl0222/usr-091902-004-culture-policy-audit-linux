"""策略域：目标/权重/人群/有效期的生命周期与双负责人批准门。

状态机（每个版本独立）：
    draft --submit--> pending --approve(content)--> pending
                            --approve(risk)--> approved
    pending --reject--> draft（批准记录清空，驳回事件保留在日志）
    approved --activate--> active（必须已绑定实验分段且在有效期内）
    active --rollback--> rolled_back（紧急回滚，原因强制；级联由应用层编排）
    active/approved --end--> ended
    active 到期后在决策点惰性失效（不产生事件，决策日志记录 expired）

铁律：
- approved/active 的权重等字段冻结，任何修改只能派生新版本，重新走完整审批；
- 单一策略版本最大流量 10%，小流量是制度不是建议；
- 两名批准人必须是不同自然人（content 与 risk 两个角色各一人）。
"""

from common import SIGNALS, DomainError, iso, now_ts, to_ts

MAX_TRAFFIC_PERCENT = 10
MAX_VALIDITY_DAYS = 90
WEIGHT_EPSILON = 1e-6
ROLE_CONTENT = "content_owner"
ROLE_RISK = "risk_owner"
REQUIRED_APPROVALS = (ROLE_CONTENT, ROLE_RISK)

STATE_MACHINE = {
    "draft": {"submit"},
    "pending": {"approve", "reject"},
    "approved": {"activate"},
    "active": {"rollback", "end"},
    "rolled_back": set(),
    "ended": set(),
    "rejected": set(),
}


def validate_weights(weights):
    if not isinstance(weights, dict) or not weights:
        raise DomainError("bad_weights", "权重必须是非空的信号→数值映射")
    total = 0.0
    for signal, value in weights.items():
        if signal not in SIGNALS:
            raise DomainError("bad_weights", f"未知信号 {signal}，允许: {', '.join(SIGNALS)}")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise DomainError("bad_weights", f"信号 {signal} 的权重必须是数值")
        if value < 0 or value > 1:
            raise DomainError("bad_weights", f"信号 {signal} 的权重必须在 [0,1]")
        total += float(value)
    if abs(total - 1.0) > WEIGHT_EPSILON:
        raise DomainError("bad_weights", f"权重之和必须为 1，当前为 {total:.6f}")
    return {k: float(v) for k, v in weights.items()}


def validate_audience(audience):
    if not isinstance(audience, dict) or not audience:
        raise DomainError("bad_audience", "适用人群必须是非空结构化对象")
    name = audience.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DomainError("bad_audience", "适用人群必须包含 name")
    return audience


def validate_validity(start_ts, end_ts):
    start_ts = to_ts(start_ts)
    end_ts = to_ts(end_ts)
    if end_ts <= start_ts:
        raise DomainError("bad_validity", "有效期结束时间必须晚于开始时间")
    if end_ts - start_ts > MAX_VALIDITY_DAYS * 86400:
        raise DomainError("bad_validity", f"有效期不得超过 {MAX_VALIDITY_DAYS} 天")
    return start_ts, end_ts


def validate_objectives(objectives):
    if not isinstance(objectives, list) or not objectives:
        raise DomainError("bad_objectives", "目标必须是非空列表")
    if not all(isinstance(o, str) and o.strip() for o in objectives):
        raise DomainError("bad_objectives", "每条目标必须是非空字符串")
    return [o.strip() for o in objectives]


class Policy:
    """事件溯源聚合。只通过 apply 消费事件，不直接改字段。"""

    def __init__(self, policy_id):
        self.id = policy_id
        self.version = 1
        self.status = None
        self.spec = None
        self.approvals = {}
        self.activated_at = None
        self.ended_at = None
        self.rollback = None
        self.rejections = []
        self.created_at = None

    def assert_transition(self, action):
        allowed = STATE_MACHINE.get(self.status, set())
        if action not in allowed:
            raise DomainError(
                "illegal_transition",
                f"策略 {self.id} 当前状态 {self.status} 不允许 {action}",
            )

    def apply(self, event):
        kind = event["kind"]
        p = event["payload"]
        if kind == "policy_drafted":
            self.status = "draft"
            self.version = p["version"]
            self.spec = p["spec"]
            self.created_at = event["ts"]
        elif kind == "policy_submitted":
            self.status = "pending"
        elif kind == "policy_approved":
            self.status = "pending"  # 双批准集齐前保持 pending
            self.approvals[p["role"]] = {"by": p["by"], "at": event["ts"], "comment": p.get("comment")}
            if all(r in self.approvals for r in REQUIRED_APPROVALS):
                self.status = "approved"
        elif kind == "policy_rejected":
            self.status = "draft"
            self.rejections.append({"by": p["by"], "role": p["role"], "at": event["ts"],
                                    "reason": p["reason"]})
            self.approvals = {}
        elif kind == "policy_activated":
            self.status = "active"
            self.activated_at = event["ts"]
        elif kind == "policy_rolled_back":
            self.status = "rolled_back"
            self.rollback = {"by": p["by"], "role": p["role"], "at": event["ts"],
                             "reason": p["reason"], "cause": p.get("cause", "manual")}
        elif kind == "policy_ended":
            self.status = "ended"
            self.ended_at = event["ts"]
        # 其他域事件忽略

    def valid_at(self, ts):
        if self.status != "active":
            return False
        return self.spec["start_ts"] <= ts < self.spec["end_ts"]

    def snapshot(self):
        """决策日志固化用的不可变口径快照。"""
        return {
            "policy_id": self.id,
            "version": self.version,
            "status": self.status,
            "objectives": list(self.spec["objectives"]),
            "weights": dict(self.spec["weights"]),
            "audience": dict(self.spec["audience"]),
            "traffic_percent": self.spec["traffic_percent"],
            "start_ts": self.spec["start_ts"],
            "end_ts": self.spec["end_ts"],
            "start_iso": iso(self.spec["start_ts"]),
            "end_iso": iso(self.spec["end_ts"]),
            "approvals": {r: dict(a) for r, a in self.approvals.items()},
            "activated_at": self.activated_at,
            "rollback": self.rollback,
        }


def build_spec(*, objectives, weights, audience, start_ts, end_ts, traffic_percent,
               rationale=""):
    """统一构造并校验策略规格。"""
    weights = validate_weights(weights)
    audience = validate_audience(audience)
    start_ts, end_ts = validate_validity(start_ts, end_ts)
    objectives = validate_objectives(objectives)
    if not isinstance(traffic_percent, int) or isinstance(traffic_percent, bool):
        raise DomainError("bad_traffic", "流量百分比必须是整数")
    if not 1 <= traffic_percent <= MAX_TRAFFIC_PERCENT:
        raise DomainError("bad_traffic",
                          f"小流量分发只能在 1..{MAX_TRAFFIC_PERCENT}% 之间")
    if end_ts <= now_ts():
        raise DomainError("bad_validity", "提交时有效期结束时间必须晚于当前时间")
    return {
        "objectives": objectives,
        "weights": weights,
        "audience": audience,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "traffic_percent": traffic_percent,
        "rationale": rationale,
    }
