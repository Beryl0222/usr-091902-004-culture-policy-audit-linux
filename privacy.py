"""隐私域：画像开关、兴趣重置墓碑与偏好生效规则。

保证：
- 关闭画像后，任何时刻的分流都不得使用类别亲和（decision 中 personalized=false
  且权重外加成分为 0）；重新开启也不恢复旧偏好，只能从重置点之后重新积累；
- 兴趣重置写墓碑（追加、永不删除），重置点之前的亲和更新对之后的决策全部失效，
  历史偏好无法"换个马甲"继续影响推荐；
- 本域只按去标识化摘要存取，不提供按摘要枚举/反查观看记录的能力；
  任何角色都不能从系统输出反推出个人观看记录（指标层另有 k 匿名）。
"""

from common import DomainError


class PrivacyRegistry:
    def __init__(self):
        self._state = {}   # subject_digest -> {"opt": [(ts, bool)], "resets": [ts],
                           #                    "affinities": {cat: {"v", "ts"}}}

    def _bag(self, digest):
        return self._state.setdefault(digest, {"opt": [], "resets": [], "affinities": {}})

    def apply(self, event):
        kind = event["kind"]
        p = event.get("payload", {})
        if kind == "profile_opt_changed":
            bag = self._bag(p["subject_digest"])
            bag["opt"].append((event["ts"], bool(p["enabled"])))
        elif kind == "interest_reset":
            bag = self._bag(p["subject_digest"])
            bag["resets"].append(event["ts"])
            bag["affinities"] = {}  # 重置点之前的积累逻辑上作废
        elif kind == "affinity_updated":
            bag = self._bag(p["subject_digest"])
            # 重放乱序事件时，重置墓碑之后到达的"重置前的旧更新"必须丢弃
            latest_reset = bag["resets"][-1] if bag["resets"] else None
            if latest_reset is not None and event["ts"] <= latest_reset:
                return
            enabled = self._enabled_at(bag, event["ts"])
            if not enabled:
                return  # 画像关闭期间产生的亲和更新不落库
            bag["affinities"][p["category"]] = {"v": float(p["value"]), "ts": event["ts"]}

    @staticmethod
    def _enabled_at(bag, ts):
        enabled = True  # 默认开启，但必须显式 opt-in 才有数据；未记录时不产生个性化
        if not bag["opt"]:
            return False
        for change_ts, value in bag["opt"]:
            if change_ts <= ts:
                enabled = value
            else:
                break
        return enabled

    def effective(self, digest, ts):
        """返回 ts 时刻的隐私生效状态与可用亲和（纯读）。"""
        bag = self._state.get(digest)
        if bag is None:
            return {"profile_enabled": False, "latest_reset_at": None, "affinities": {}}
        enabled = self._enabled_at(bag, ts)
        reset_cutoff = max((r for r in bag["resets"] if r <= ts), default=None)
        affinities = {}
        if enabled:
            for cat, rec in bag["affinities"].items():
                if rec["ts"] <= ts and (reset_cutoff is None or rec["ts"] > reset_cutoff):
                    affinities[cat] = rec["v"]
        return {"profile_enabled": enabled, "latest_reset_at": reset_cutoff,
                "affinities": affinities}

    def assert_not_tagged_with_stale_prefs(self, digest, ts, used_affinities):
        """决策自检：若使用了重置点前/关闭期的偏好，直接拒绝该决策。"""
        eff = self.effective(digest, ts)
        if used_affinities and not eff["profile_enabled"]:
            raise DomainError("privacy_violation", "画像已关闭，不得使用历史偏好")
        stale = set(used_affinities or {}) - set(eff["affinities"])
        if stale:
            raise DomainError("privacy_violation",
                              f"以下类别偏好已被重置失效，不得继续使用: {sorted(stale)}")
        return eff
