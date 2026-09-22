"""反馈台账：去标识化曝光与反馈的入库、去重、撤回与隔离。

去污染分两层：
1) 入库隔离（结构层，立即判定）：重复 event_id、未来事件、格式非法、无法找到
   决策记录或与决策记录不一致——追加 feedback_quarantined 事件留痕，绝不进入统计；
2) 封板排除（时间层，在 metrics 计算时确定性判定）：晚于当日封板时刻到达的
   迟到/补传事件、撤回事件、找不到曝光的反馈——日志保留、指标排除。

乱序安全：反馈可能早于曝光到达。指标只在重放完整日志后计算，封板前到达的
乱序事件在曝光补齐后自然归因；封板后到达则永不改写已封板分区。
所有判定都是不可变事件的纯函数，任意时刻重放结果一致。

隐私：原始主体标识只在 HTTP 边界存在一瞬间，库内只存 HMAC 摘要；隔离事件
同样只记录摘要与事件 ID，不含原始标识。
"""

from caliber import LONG_WINDOW_SECONDS, LATE_GRACE_SECONDS
from common import digest

FEEDBACK_KINDS = {"playback", "favorite", "discussion", "revisit", "finish"}
# 允许的时钟前偏：客户端时间比服务器快不超过该值，否则判未来事件。
FUTURE_SKEW_SECONDS = 300


class FeedbackLedger:
    def __init__(self):
        self.exposures = {}        # exposure_id -> 记录
        self.feedbacks = []        # 反馈列表
        self.withdrawn = {}        # event_id -> 撤回信息
        self.quarantine = []       # 隔离清单
        self.event_ids = set()     # 幂等：所有见过的事件 ID
        self.decisions = {}        # decision_seq -> 决策记录（由 rank_decided 喂入）

    def apply(self, event):
        kind = event["kind"]
        p = event.get("payload", {})
        if kind == "rank_decided":
            self.decisions[event["seq"]] = event
        elif kind == "exposure_logged":
            self._index_exposure(event)
            self.event_ids.add(p["event_id"])
        elif kind == "feedback_received":
            self.feedbacks.append(event)
            self.event_ids.add(p["event_id"])
        elif kind == "feedback_withdrawn":
            self.withdrawn[p["ref_event_id"]] = {"at": event["ts"], "by_ref": p.get("ref_event_id")}
            self.event_ids.add("withdraw:" + p["ref_event_id"])
        elif kind == "feedback_quarantined":
            self.quarantine.append(event)
            self.event_ids.add(p["event_id"])

    def _index_exposure(self, event):
        p = event["payload"]
        self.exposures[p["exposure_id"]] = event

    # ---- 入库校验：返回隔离原因，None 表示可入库 ----

    def check_event(self, *, event_id, subject_digest, happened_at, received_at, kind,
                    payload, decision_seq=None):
        if not event_id or not isinstance(event_id, str):
            return "bad_event_id"
        if event_id in self.event_ids:
            return "duplicate"
        if happened_at > received_at + FUTURE_SKEW_SECONDS:
            return "future_event"
        if received_at - happened_at > LONG_WINDOW_SECONDS + LATE_GRACE_SECONDS + 86400:
            # 远超任何观察窗口，即使挂到未封板日也没有意义
            return "implausibly_late"
        if not subject_digest:
            return "missing_subject"
        if kind == "exposure":
            return self._check_exposure(payload, decision_seq, subject_digest)
        if kind not in FEEDBACK_KINDS:
            return "unknown_kind"
        return self._check_feedback_payload(kind, payload)

    def _check_exposure(self, payload, decision_seq, subject_digest):
        if decision_seq is None:
            return "missing_decision_ref"
        decision = self.decisions.get(decision_seq)
        if decision is None:
            return "decision_not_found"
        dp = decision["payload"]
        if dp["subject_digest"] != subject_digest:
            return "decision_subject_mismatch"
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return "bad_items"
        ranked_ids = [r["content_id"] for r in dp["ranked"]]
        for item in items:
            cid = str(item.get("content_id"))
            if cid not in ranked_ids:
                return "item_not_in_decision"
            if not isinstance(item.get("duration_seconds"), (int, float)) or item["duration_seconds"] <= 0:
                return "bad_duration"
            if not item.get("category"):
                return "bad_category"
        if payload.get("experiment_id") != dp["experiment_id"]:
            return "decision_experiment_mismatch"
        return None

    def _check_feedback_payload(self, kind, payload):
        if not payload.get("ref_exposure_id"):
            return "missing_exposure_ref"
        if not payload.get("content_id"):
            return "missing_content_ref"
        if kind in ("playback", "finish"):
            progress = payload.get("progress", 1.0 if kind == "finish" else None)
            if not isinstance(progress, (int, float)) or isinstance(progress, bool):
                return "bad_progress"
            if not 0 <= progress <= 1:
                return "bad_progress"
        if kind == "discussion":
            quality = payload.get("quality")
            if not isinstance(quality, (int, float)) or isinstance(quality, bool):
                return "bad_quality"
            if not 0 <= quality <= 1:
                return "bad_quality"
        return None

    def mark_seen(self, event_id):
        self.event_ids.add(event_id)

    def is_withdrawn(self, event_id):
        return event_id in self.withdrawn

    def exposure_withdrawn(self, exposure_id):
        exposure = self.exposures.get(exposure_id)
        if exposure is None:
            return False
        return exposure["payload"]["exposure_id"] in self.withdrawn


def deidentify(raw_subject, pepper=None):
    """边界去标识化：原始主体标识 → 不可逆摘要。调用方不得再持有原值。"""
    if pepper is None:
        return digest("subject", raw_subject)
    import hmac as _hmac
    import hashlib
    msg = f"subject|{raw_subject}".encode("utf-8")
    return _hmac.new(pepper.encode("utf-8"), msg, hashlib.sha256).hexdigest()
