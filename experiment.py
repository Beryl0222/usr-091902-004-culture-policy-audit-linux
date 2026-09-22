"""实验域：分流实验、不可变分段、确定性分桶与决策固化。

分段铁律：
- 分段一旦开启，其权重快照、流量、口径版本全部冻结；
- 实验中途调权 = 关闭当前分段 + 用新版本策略（重新审批通过后）开启新分段，
  新分段拥有独立种子与时间区间，指标按分段分别累计——禁止并段伪装连续实验；
- 策略回滚/结束立即关闭在跑分段；对照组继续保留以完成观察。

可复现铁律：
- 每次分流写一条 rank_decided 事件，固化当日采用的策略版本、权重、分桶与排序；
- 分桶是 (分段种子, 去标识化主体) 的纯函数，任何时候重算结果必须与记录一致。
"""

import hashlib
import json

from caliber import BASELINE_POLICY_ID, BASELINE_WEIGHTS, day_key
from common import DomainError, iso, stable_bucket

BUCKETS = 100  # 流量百分比粒度


class Segment:
    def __init__(self, segment_id):
        self.id = segment_id
        self.status = None
        self.spec = None
        self.closed = None

    def apply(self, event):
        kind = event["kind"]
        p = event["payload"]
        if kind == "segment_opened" and p["segment_id"] == self.id:
            self.status = "live"
            self.spec = p
        elif kind == "segment_closed" and p["segment_id"] == self.id:
            self.status = "closed"
            self.closed = {"reason": p["reason"], "at": event["ts"], "by": p.get("by")}

    def live_at(self, ts):
        return self.status == "live" and self.spec["start_ts"] <= ts

    @property
    def freeze(self):
        return self.spec


class Experiment:
    def __init__(self, experiment_id):
        self.id = experiment_id
        self.hypothesis = None
        self.created_at = None
        self.segments = {}
        self.segment_order = []
        self.decisions = []

    def apply(self, event):
        kind = event["kind"]
        p = event["payload"]
        if kind == "experiment_created" and p["experiment_id"] == self.id:
            self.hypothesis = p["hypothesis"]
            self.created_at = event["ts"]
        elif kind in ("segment_opened", "segment_closed") and p.get("experiment_id") == self.id:
            seg = self.segments.setdefault(p["segment_id"], Segment(p["segment_id"]))
            seg.apply(event)
            if kind == "segment_opened" and p["segment_id"] not in self.segment_order:
                self.segment_order.append(p["segment_id"])
        elif kind == "rank_decided" and p.get("experiment_id") == self.id:
            self.decisions.append(event)

    def live_segment_at(self, ts):
        """返回 ts 时刻生效的分段（同刻只允许一个在跑）。"""
        live = [self.segments[sid] for sid in self.segment_order
                if self.segments[sid].live_at(ts)]
        if len(live) > 1:
            raise DomainError("segment_overlap", f"实验 {self.id} 在 {iso(ts)} 存在多个在跑分段")
        return live[0] if live else None

    def decisions_on_day(self, day):
        return [e for e in self.decisions if day_key(e["ts"]) == day]


def audience_matches(audience, user_attrs):
    """结构简单的人群匹配：name=all 命中全体；否则 attrs 必须被用户属性包含。"""
    if audience.get("name") == "all":
        return True
    required = audience.get("attrs", {})
    user_attrs = user_attrs or {}
    return all(user_attrs.get(k) == v for k, v in required.items())


def assign_variant(segment_spec, subject_digest):
    """纯函数分桶。返回 (bucket, enrolled, variant)。

    bucket 0..99：< traffic_percent 入组；入组后再做一次独立哈希二选一，
    treatment / baseline 各半，避免按桶奇偶造成系统性偏差。
    """
    seed = segment_spec["seed"]
    bucket = stable_bucket(seed, subject_digest, BUCKETS)
    enrolled = bucket < segment_spec["traffic_percent"]
    if not enrolled:
        return bucket, False, "off"
    side = stable_bucket(seed + ":side", subject_digest, 2)
    return bucket, True, ("treatment" if side == 1 else "baseline")


def score_content(catalog, weights, affinities=None, personalization_alpha=0.0):
    """对目录打分排序。确定性：分数相同时按 content_id 升序。

    可复现要求：信号按名称排序后求和（浮点加法不结合，字典顺序在 JSON
    往返后可能变化），且排序键先取整到 9 位小数，使跨进程重算逐字节一致。
    """
    affinities = affinities or {}
    ordered_weights = sorted(weights.items())
    ranked = []
    for item in catalog:
        features = item.get("features", {})
        score = 0.0
        for signal, w in ordered_weights:
            score += w * float(features.get(signal, 0.0))
        if personalization_alpha and affinities:
            score += personalization_alpha * float(affinities.get(item.get("category"), 0.0))
        ranked.append((content_id_of(item), round(score, 9)))
    ranked.sort(key=lambda x: (-x[1], x[0]))
    return ranked


def content_id_of(item):
    return str(item["content_id"])


def catalog_fingerprint(catalog):
    basis = json.dumps(
        sorted(
            (content_id_of(i), i.get("category"), i.get("duration_seconds"),
             i.get("channel"))
            for i in catalog
        ),
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def make_decision(*, experiment, subject_digest, ts, catalog, user_attrs, policy_snapshot,
                  affinities, profile_active):
    """计算一次分流结果（纯读，事件由应用层写入）。"""
    segment = experiment.live_segment_at(ts)
    result = {
        "experiment_id": experiment.id,
        "ts": ts,
        "day": day_key(ts),
        "subject_digest": subject_digest,
        "catalog_hash": catalog_fingerprint(catalog),
        "personalized": bool(profile_active and affinities),
    }
    if segment is None:
        result.update({
            "strategy": "none", "variant": "off", "enrolled": False,
            "reason": "no_live_segment",
            "weights_used": dict(BASELINE_WEIGHTS),
            "policy_id": BASELINE_POLICY_ID,
        })
        ranked = score_content(catalog, BASELINE_WEIGHTS)
        result["ranked"] = [{"content_id": cid, "score": round(s, 9)} for cid, s in ranked]
        return result

    spec = segment.freeze
    result["segment_id"] = spec["segment_id"]
    result["caliber"] = spec["caliber_version"]
    bucket, enrolled, variant = assign_variant(spec, subject_digest)
    result.update({"bucket": bucket, "enrolled": enrolled, "variant": variant})

    active = policy_snapshot is not None and policy_snapshot["status"] == "active"
    in_audience = audience_matches(spec["audience"], user_attrs)
    within_window = spec["start_ts"] <= ts < spec["end_ts"]

    if not enrolled:
        result.update({"strategy": "baseline", "reason": "not_enrolled",
                       "weights_used": dict(BASELINE_WEIGHTS),
                       "policy_id": BASELINE_POLICY_ID})
    elif variant == "baseline":
        result.update({"strategy": "baseline", "reason": "control_arm",
                       "weights_used": dict(BASELINE_WEIGHTS),
                       "policy_id": BASELINE_POLICY_ID})
    elif not active:
        result.update({"strategy": "baseline", "reason": "policy_not_active",
                       "weights_used": dict(BASELINE_WEIGHTS),
                       "policy_id": BASELINE_POLICY_ID,
                       "policy_id_intended": spec["policy_id"],
                       "policy_version_intended": spec["policy_version"]})
    elif not in_audience:
        result.update({"strategy": "baseline", "reason": "audience_mismatch",
                       "weights_used": dict(BASELINE_WEIGHTS),
                       "policy_id": BASELINE_POLICY_ID})
    elif not within_window:
        result.update({"strategy": "baseline", "reason": "segment_out_of_window",
                       "weights_used": dict(BASELINE_WEIGHTS),
                       "policy_id": BASELINE_POLICY_ID})
    else:
        alpha = 0.15 if result["personalized"] else 0.0
        result.update({
            "strategy": "treatment",
            "reason": "treatment_arm",
            "weights_used": dict(spec["weights"]),
            "policy_id": spec["policy_id"],
            "policy_version": spec["policy_version"],
            "personalization_alpha": alpha,
        })
    ranked = score_content(catalog, result["weights_used"],
                           affinities=affinities if result["strategy"] == "treatment" else None,
                           personalization_alpha=result.get("personalization_alpha", 0.0))
    result["ranked"] = [{"content_id": cid, "score": round(s, 9)} for cid, s in ranked]
    return result

