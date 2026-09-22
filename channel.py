"""独立通道域：内容进出通道的原因留痕与创作者可查询。

- 进入通道必须给出原因（人工策展 / 实验策略纳入 / 保护条款等）及来源；
- 退出通道同样强制原因；策略回滚时由应用层对该策略纳入的全部内容
  追加级联退出事件（cause=policy_rolled_back），不悄悄下架；
- 创作者查询只返回内容维度的进出时间线，任何查询路径都不含观看主体信息。
"""

from common import DomainError

# 通道进出的标准原因码。
ADMIT_REASONS = ("editorial_pick", "experiment_policy", "protection_clause")
EXIT_REASONS = ("editorial_remove", "policy_ended", "policy_rolled_back",
                "validity_expired", "risk_takedown")


class ChannelRegistry:
    def __init__(self):
        self.contents = {}          # content_id -> 注册信息
        self.channels = {}          # channel_id -> {content_id -> [events]}
        self.timeline = {}          # content_id -> [{channel_id, action, ...}]

    def apply(self, event):
        kind = event["kind"]
        p = event.get("payload", {})
        if kind == "content_registered":
            self.contents[p["content_id"]] = {
                "content_id": p["content_id"], "title": p.get("title"),
                "creator_id": p["creator_id"], "category": p["category"],
                "duration_seconds": p["duration_seconds"],
                "features": dict(p.get("features", {})),
            }
        elif kind == "channel_admitted":
            members = self.channels.setdefault(p["channel_id"], {})
            current = members.get(p["content_id"])
            if current is not None:
                raise DomainError("channel_conflict",
                                  f"内容 {p['content_id']} 已在通道 {p['channel_id']} 中")
            record = {"admitted_at": event["ts"], **p}
            members[p["content_id"]] = record
            self.timeline.setdefault(p["content_id"], []).append(
                {"action": "admitted", "at": event["ts"], **p})
        elif kind == "channel_exited":
            members = self.channels.setdefault(p["channel_id"], {})
            if p["content_id"] not in members:
                raise DomainError("channel_conflict",
                                  f"内容 {p['content_id']} 不在通道 {p['channel_id']} 中，无法退出")
            admission = members.pop(p["content_id"])
            self.timeline.setdefault(p["content_id"], []).append(
                {"action": "exited", "at": event["ts"],
                 "admitted_at": admission["admitted_at"], **p})

    def admitted_by_policy(self, policy_id):
        """找出仍在通道中、由指定策略纳入的内容（供回滚级联）。"""
        result = []
        for channel_id, members in self.channels.items():
            for content_id, record in members.items():
                if record.get("source", {}).get("policy_id") == policy_id:
                    result.append((channel_id, content_id, record))
        return result

    def creator_view(self, creator_id):
        """创作者视角：自己名下每条内容的通道状态与进出原因。"""
        out = []
        for content_id, info in self.contents.items():
            if info["creator_id"] != creator_id:
                continue
            timeline = self.timeline.get(content_id, [])
            current = [
                {"channel_id": cid, "admitted_at": rec["admitted_at"],
                 "reason": rec["reason"], "source": rec.get("source")}
                for cid, members in self.channels.items()
                for c, rec in members.items() if c == content_id
            ]
            out.append({
                "content_id": content_id, "title": info.get("title"),
                "category": info["category"], "duration_seconds": info["duration_seconds"],
                "in_channels": current,
                "timeline": timeline,
            })
        return out
