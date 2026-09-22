"""应用门面：命令编排、角色鉴权、状态折叠与级联动作。

所有写操作都走这里：鉴权 → 领域校验 → 追加事件。聚合根只通过事件重放构建，
进程重启后从 Journal 原样恢复。
"""

import caliber as cal
from channel import ADMIT_REASONS, EXIT_REASONS, ChannelRegistry
from common import DomainError, iso, now_ts, to_ts
from experiment import (Experiment, assign_variant, catalog_fingerprint,
                        make_decision, score_content)
from feedback import FeedbackLedger, deidentify
from journal import Journal
from metrics import MetricsEngine
from policy import REQUIRED_APPROVALS, Policy, build_spec
from privacy import PrivacyRegistry

ROLE_PRODUCT = "product_manager"
ROLE_CONTENT = "content_owner"
ROLE_RISK = "risk_owner"
ROLE_EDITOR = "content_editor"
ROLE_CREATOR = "creator"
ROLE_USER = "user"

CLOSE_REASONS = ("weight_change", "policy_rollback", "experiment_end", "validity_expired")


def derive_exposure_id(event_id):
    """曝光 ID 由客户端事件 ID 确定性派生：乱序反馈可先引用、曝光后补。"""
    import hashlib
    return "expo_" + hashlib.sha1(str(event_id).encode("utf-8")).hexdigest()[:16]


class Actor:
    def __init__(self, actor_id, role):
        self.id = actor_id
        self.role = role

    def require(self, *roles):
        if self.role not in roles:
            raise DomainError("forbidden",
                              f"该操作需要角色 {'/'.join(roles)}，当前为 {self.role}")


class Application:
    def __init__(self, journal_path=None, pepper=None):
        self.journal = Journal(journal_path)
        self.pepper = pepper
        self.policies = {}        # (policy_id, version) -> Policy
        self.policy_series = {}   # policy_id -> latest version
        self.experiments = {}
        self.channels = ChannelRegistry()
        self.privacy = PrivacyRegistry()
        self.ledger = FeedbackLedger()
        self._seq_counters = {}
        for agg in (self._dispatch,):
            self.journal.subscribe(agg)
        self.journal.replay(self._dispatch)

    # ---------- 事件分发 ----------

    def _dispatch(self, event):
        kind = event["kind"]
        p = event.get("payload", {})
        if kind.startswith("policy_"):
            key = (p["policy_id"], p.get("version", 1))
            policy = self.policies.get(key)
            if policy is None:
                policy = Policy(p["policy_id"])
                self.policies[key] = policy
            policy.apply(event)
            if kind == "policy_drafted":
                self.policy_series[p["policy_id"]] = max(
                    p["version"], self.policy_series.get(p["policy_id"], 0))
        elif kind in ("experiment_created", "segment_opened", "segment_closed", "rank_decided"):
            exp = self.experiments.setdefault(p["experiment_id"], Experiment(p["experiment_id"]))
            exp.apply(event)
        elif kind.startswith(("content_registered", "channel_")):
            self.channels.apply(event)
        elif kind in ("profile_opt_changed", "interest_reset", "affinity_updated"):
            self.privacy.apply(event)
        self.ledger.apply(event)

    def _policy(self, policy_id, version=None):
        version = version or self.policy_series.get(policy_id)
        policy = self.policies.get((policy_id, version))
        if policy is None:
            raise DomainError("policy_not_found", f"策略 {policy_id} v{version} 不存在")
        return policy

    def _append(self, kind, payload, actor, ts=None):
        return self.journal.append(kind, payload, actor=actor.id if actor else None, ts=ts)

    # ---------- 内容注册 ----------

    def register_content(self, actor, *, content_id, title, creator_id, category,
                         duration_seconds, features, channel=None):
        actor.require(ROLE_CREATOR, ROLE_CONTENT, ROLE_EDITOR)
        if actor.role == ROLE_CREATOR and creator_id != actor.id:
            raise DomainError("forbidden", "创作者只能登记自己名下的内容")
        if content_id in self.channels.contents:
            raise DomainError("content_exists", f"内容 {content_id} 已登记")
        duration_seconds = int(duration_seconds)
        if duration_seconds <= 0:
            raise DomainError("bad_content", "时长必须为正")
        feats = {}
        for signal, value in (features or {}).items():
            feats[signal] = float(value)
            if not 0 <= feats[signal] <= 1:
                raise DomainError("bad_content", f"信号 {signal} 取值须在 [0,1]")
        payload = {"content_id": str(content_id), "title": title, "creator_id": creator_id,
                   "category": category, "duration_seconds": duration_seconds,
                   "features": feats}
        self._append("content_registered", payload, actor)
        if channel:
            self.admit_channel(actor, channel_id=channel, content_id=str(content_id),
                               reason="editorial_pick",
                               note="登记时随附的人工策展通道")
        return payload

    def _catalog_of(self, content_ids):
        items = []
        for cid in content_ids:
            info = self.channels.contents.get(str(cid))
            if info is None:
                raise DomainError("content_not_found", f"内容 {cid} 未登记")
            items.append({"content_id": info["content_id"], "category": info["category"],
                          "duration_seconds": info["duration_seconds"],
                          "features": dict(info.get("features", {}))})
        return items

    # ---------- 策略生命周期 ----------

    def draft_policy(self, actor, *, policy_id, objectives, weights, audience,
                     start_ts, end_ts, traffic_percent, rationale=""):
        actor.require(ROLE_PRODUCT)
        if policy_id in self.policy_series:
            raise DomainError("policy_exists", "该策略系列已存在，请使用 revise 派生新版本")
        spec = build_spec(objectives=objectives, weights=weights, audience=audience,
                          start_ts=start_ts, end_ts=end_ts,
                          traffic_percent=traffic_percent, rationale=rationale)
        self._append("policy_drafted",
                     {"policy_id": policy_id, "version": 1, "spec": spec}, actor)
        return self.policy_view(policy_id, 1)

    def revise_policy(self, actor, policy_id, **changes):
        """调权/改人群/延期：派生新版本，原版本冻结，新版本必须重新走完整审批。"""
        actor.require(ROLE_PRODUCT)
        current = self._policy(policy_id)
        if current.status == "draft" and not current.rejections:
            raise DomainError("not_submitted",
                              "草稿尚未提交：无需派生版本，直接提交即可；"
                              "提交后任何修改都必须走新版本")
        for exp in self.experiments.values():
            for seg in exp.segments.values():
                if seg.status == "live" and seg.spec["policy_id"] == policy_id \
                        and seg.spec["policy_version"] == current.version:
                    raise DomainError("segment_live",
                                      "该版本仍有在跑分段：调权前必须先关闭分段，"
                                      "再提交新版本并另开分段")
        new_version = current.version + 1
        spec = dict(current.spec)
        spec.update({k: v for k, v in changes.items() if v is not None})
        spec = build_spec(**spec)
        self._append("policy_drafted",
                     {"policy_id": policy_id, "version": new_version, "spec": spec,
                      "revised_from": current.version}, actor)
        return self.policy_view(policy_id, new_version)

    def submit_policy(self, actor, policy_id, version=None):
        actor.require(ROLE_PRODUCT)
        policy = self._policy(policy_id, version)
        policy.assert_transition("submit")
        self._append("policy_submitted",
                     {"policy_id": policy_id, "version": policy.version}, actor)
        return self.policy_view(policy_id, policy.version)

    def approve_policy(self, actor, policy_id, role, comment="", version=None):
        actor.require(ROLE_CONTENT, ROLE_RISK)
        if role not in REQUIRED_APPROVALS:
            raise DomainError("bad_role", f"批准角色必须是 {'/'.join(REQUIRED_APPROVALS)}")
        policy = self._policy(policy_id, version)
        others = [a["by"] for r, a in policy.approvals.items() if r != role]
        if actor.id in others:
            raise DomainError("self_approval", "内容与风险批准必须是不同的人")
        if actor.role != role:
            raise DomainError("forbidden", f"登录角色 {actor.role} 不能以 {role} 身份批准")
        policy.assert_transition("approve")
        if role in policy.approvals:
            raise DomainError("already_approved", f"{role} 已批准，不能重复批准")
        self._append("policy_approved",
                     {"policy_id": policy_id, "version": policy.version,
                      "role": role, "by": actor.id, "comment": comment}, actor)
        return self.policy_view(policy_id, policy.version)

    def reject_policy(self, actor, policy_id, role, reason, version=None):
        actor.require(ROLE_CONTENT, ROLE_RISK)
        if actor.role != role:
            raise DomainError("forbidden", f"登录角色 {actor.role} 不能以 {role} 身份驳回")
        policy = self._policy(policy_id, version)
        policy.assert_transition("reject")
        self._append("policy_rejected",
                     {"policy_id": policy_id, "version": policy.version,
                      "role": role, "by": actor.id, "reason": reason}, actor)
        return self.policy_view(policy_id, policy.version)

    def rollback_policy(self, actor, policy_id, reason, version=None):
        """紧急回滚：风险或内容负责人均可执行；级联关闭分段、退出通道。"""
        actor.require(ROLE_RISK, ROLE_CONTENT)
        policy = self._policy(policy_id, version)
        policy.assert_transition("rollback")
        if not reason or not reason.strip():
            raise DomainError("rollback_without_reason", "紧急回滚必须填写原因")
        self._append("policy_rolled_back",
                     {"policy_id": policy_id, "version": policy.version,
                      "by": actor.id, "role": actor.role, "reason": reason,
                      "cause": "manual"}, actor, ts=now_ts())
        self._cascade_rollback(actor, policy_id, policy.version, reason)
        return self.policy_view(policy_id, policy.version)

    def _cascade_rollback(self, actor, policy_id, version, reason):
        for exp_id, exp in self.experiments.items():
            for seg_id in list(exp.segment_order):
                seg = exp.segments[seg_id]
                if seg.status != "live":
                    continue
                if seg.spec["policy_id"] == policy_id and seg.spec["policy_version"] == version:
                    self.close_segment(actor, exp_id, reason="policy_rollback",
                                       detail=reason)
        # 通道退出：仅当该策略系列已没有任何在跑分段时，其纳入的内容才级联退出。
        if not self._series_has_live_segment(policy_id):
            for channel_id, content_id, record in self.channels.admitted_by_policy(policy_id):
                self._append("channel_exited",
                             {"channel_id": channel_id, "content_id": content_id,
                              "reason": "policy_rolled_back",
                              "by": actor.id, "detail": reason,
                              "source": record.get("source")}, actor)

    def _series_has_live_segment(self, policy_id):
        for exp in self.experiments.values():
            for seg in exp.segments.values():
                if seg.status == "live" and seg.spec["policy_id"] == policy_id:
                    return True
        return False

    def end_policy(self, actor, policy_id, version=None):
        actor.require(ROLE_PRODUCT, ROLE_CONTENT, ROLE_RISK)
        policy = self._policy(policy_id, version)
        policy.assert_transition("end")
        self._append("policy_ended",
                     {"policy_id": policy_id, "version": policy.version,
                      "by": actor.id, "role": actor.role}, actor)
        self._cascade_finish(actor, policy_id, policy.version, "policy_ended",
                             "policy_ended")
        return self.policy_view(policy_id, policy.version)

    def _cascade_finish(self, actor, policy_id, version, seg_reason, channel_reason):
        for exp_id, exp in self.experiments.items():
            for seg_id in list(exp.segment_order):
                seg = exp.segments[seg_id]
                if seg.status == "live" and seg.spec["policy_id"] == policy_id \
                        and seg.spec["policy_version"] == version:
                    self.close_segment(actor, exp_id, reason=seg_reason, detail=channel_reason)
        if not self._series_has_live_segment(policy_id):
            for channel_id, content_id, record in self.channels.admitted_by_policy(policy_id):
                self._append("channel_exited",
                             {"channel_id": channel_id, "content_id": content_id,
                              "reason": channel_reason, "by": actor.id,
                              "source": record.get("source")}, actor)

    def policy_view(self, policy_id, version=None):
        policy = self._policy(policy_id, version)
        view = policy.snapshot()
        view["start_iso"] = iso(policy.spec["start_ts"])
        view["end_iso"] = iso(policy.spec["end_ts"])
        return view

    def list_policies(self):
        return [self.policy_view(pid, ver) for (pid, ver) in sorted(self.policies)]

    # ---------- 实验与分段 ----------

    def create_experiment(self, actor, experiment_id, hypothesis):
        actor.require(ROLE_PRODUCT)
        if experiment_id in self.experiments:
            raise DomainError("experiment_exists", f"实验 {experiment_id} 已存在")
        self._append("experiment_created",
                     {"experiment_id": experiment_id, "hypothesis": hypothesis,
                      "caliber_version": cal.CALIBER_VERSION}, actor)
        return {"experiment_id": experiment_id, "hypothesis": hypothesis,
                "caliber_version": cal.CALIBER_VERSION}

    def _segment_index(self, exp):
        return len(exp.segment_order) + 1

    def open_segment(self, actor, experiment_id, policy_id, version=None, *,
                     start_ts=None, end_ts=None, traffic_percent=None):
        """开启不可变分段。策略必须已双批准；流量不得超过策略批准的上限。"""
        actor.require(ROLE_PRODUCT)
        exp = self.experiments.get(experiment_id)
        if exp is None:
            raise DomainError("experiment_not_found", experiment_id)
        policy = self._policy(policy_id, version)
        if policy.status not in ("approved", "active"):
            raise DomainError("policy_not_approved",
                              f"策略状态 {policy.status}，须经内容与风险双负责人批准")
        live = exp.live_segment_at(now_ts())
        if live is not None:
            raise DomainError("segment_overlap",
                              f"已有在跑分段 {live.id}，调权请先关闭它再另开新分段")
        start_ts = to_ts(start_ts) if start_ts is not None else now_ts()
        end_ts = to_ts(end_ts) if end_ts is not None else policy.spec["end_ts"]
        if not policy.spec["start_ts"] <= start_ts < end_ts <= policy.spec["end_ts"]:
            raise DomainError("segment_window", "分段时间窗必须落在策略有效期之内")
        traffic = int(traffic_percent if traffic_percent is not None
                      else policy.spec["traffic_percent"])
        if not 1 <= traffic <= policy.spec["traffic_percent"]:
            raise DomainError("bad_traffic",
                              f"分段流量不得超过批准上限 {policy.spec['traffic_percent']}%")
        segment_id = f"{experiment_id}-seg{self._segment_index(exp)}"
        import hashlib
        seed = hashlib.sha256(
            f"rollout|{experiment_id}|{segment_id}".encode()).hexdigest()[:32]
        if policy.status == "approved":
            self._append("policy_activated",
                         {"policy_id": policy_id, "version": policy.version,
                          "by": actor.id, "experiment_id": experiment_id,
                          "segment_id": segment_id}, actor, ts=start_ts)
        spec = {"experiment_id": experiment_id, "segment_id": segment_id,
                "policy_id": policy_id, "policy_version": policy.version,
                "weights": dict(policy.spec["weights"]),
                "audience": dict(policy.spec["audience"]),
                "objectives": list(policy.spec["objectives"]),
                "traffic_percent": traffic, "start_ts": start_ts, "end_ts": end_ts,
                "seed": seed, "caliber_version": cal.CALIBER_VERSION}
        self._append("segment_opened", spec, actor, ts=start_ts)
        return {"segment_id": segment_id, **{k: v for k, v in spec.items()
                                             if k != "segment_id"}}

    def close_segment(self, actor, experiment_id, reason, detail=""):
        actor.require(ROLE_PRODUCT, ROLE_RISK, ROLE_CONTENT)
        if reason not in CLOSE_REASONS:
            raise DomainError("bad_reason", f"分段关闭原因必须是 {CLOSE_REASONS} 之一")
        exp = self.experiments.get(experiment_id)
        if exp is None:
            raise DomainError("experiment_not_found", experiment_id)
        seg = exp.live_segment_at(now_ts())
        if seg is None:
            # 回滚级联时以注入时间判定；退化为找最后一个 live 段
            live = [s for s in exp.segments.values() if s.status == "live"]
            if not live:
                raise DomainError("no_live_segment", "实验当前没有在跑分段")
            seg = live[-1]
        self._append("segment_closed",
                     {"experiment_id": experiment_id, "segment_id": seg.id,
                      "reason": reason, "by": actor.id, "detail": detail}, actor)
        return {"segment_id": seg.id, "status": "closed", "reason": reason}

    def experiment_view(self, experiment_id):
        exp = self.experiments.get(experiment_id)
        if exp is None:
            raise DomainError("experiment_not_found", experiment_id)
        return {
            "experiment_id": exp.id, "hypothesis": exp.hypothesis,
            "segments": [
                {"segment_id": sid, "status": seg.status,
                 "policy_id": (seg.spec or {}).get("policy_id"),
                 "policy_version": (seg.spec or {}).get("policy_version"),
                 "weights": (seg.spec or {}).get("weights"),
                 "traffic_percent": (seg.spec or {}).get("traffic_percent"),
                 "start_ts": (seg.spec or {}).get("start_ts"),
                 "end_ts": (seg.spec or {}).get("end_ts"),
                 "caliber_version": (seg.spec or {}).get("caliber_version"),
                 "seed": (seg.spec or {}).get("seed"),
                 "closed": seg.closed}
                for sid, seg in exp.segments.items()
            ],
            "decision_count": len(exp.decisions),
        }

    # ---------- 分流与决策固化 ----------

    def rank(self, actor, *, subject_ref, content_ids, user_attrs=None,
             experiment_id=None, ts=None):
        actor.require(ROLE_PRODUCT, ROLE_CONTENT, ROLE_RISK, ROLE_EDITOR)
        ts = to_ts(ts) if ts is not None else now_ts()
        subject_digest = deidentify(subject_ref, self.pepper)
        catalog = self._catalog_of(content_ids)
        eff = self.privacy.effective(subject_digest, ts)
        affinities = eff["affinities"]
        profile_active = eff["profile_enabled"]

        if experiment_id is None:
            exp = Experiment("organic")
            policy_snapshot = None
        else:
            exp = self.experiments.get(experiment_id)
            if exp is None:
                raise DomainError("experiment_not_found", experiment_id)
            seg = exp.live_segment_at(ts)
            policy_snapshot = None
            if seg is not None:
                policy_snapshot = self._policy(
                    seg.spec["policy_id"], seg.spec["policy_version"]).snapshot()
        decision = make_decision(
            experiment=exp, subject_digest=subject_digest, ts=ts, catalog=catalog,
            user_attrs=user_attrs or {}, policy_snapshot=policy_snapshot,
            affinities=affinities, profile_active=profile_active)
        decision["catalog_snapshot"] = catalog
        if profile_active and affinities and decision["strategy"] == "treatment":
            decision["affinity_used"] = dict(affinities)
        event = self._append("rank_decided", decision, actor, ts=ts)
        return {"decision_seq": event["seq"], **{k: v for k, v in decision.items()
                                                 if k not in ("catalog_snapshot",
                                                              "affinity_used",
                                                              "subject_digest")},
                "subject_digest_prefix": subject_digest[:cal.DIGEST_PREFIX_LEN]}

    def reproduce_day(self, experiment_id, day):
        """复现某日每次分流采用的策略。

        - 哈希链完整性在 Journal 加载/校验时保证；
        - 用冻结的分段种子+主体摘要重算分桶，必须与固化记录逐字节一致；
        - 用固化权重+目录快照重算排序分数，必须一致；
        - 个性化输入若事后被重置，标记失效，但不影响策略版本/权重的复现结论。
        """
        if experiment_id == "organic":
            exp = Experiment("organic")
            for e in self.ledger.decisions.values():
                if e["payload"]["experiment_id"] == "organic":
                    exp.apply(e)
        else:
            exp = self.experiments.get(experiment_id)
            if exp is None:
                raise DomainError("experiment_not_found", experiment_id)
        verdicts = []
        for event in exp.decisions_on_day(int(day)):
            p = event["payload"]
            checks = {"decision_seq": event["seq"], "ts": p["ts"], "iso": iso(p["ts"]),
                      "strategy": p["strategy"], "variant": p.get("variant"),
                      "policy_id": p.get("policy_id"),
                      "policy_version": p.get("policy_version"),
                      "weights_used": p.get("weights_used")}
            problems = []
            if p.get("segment_id"):
                seg = exp.segments[p["segment_id"]]
                spec = seg.spec
                bucket, enrolled, variant = assign_variant(spec, p["subject_digest"])
                if (bucket, enrolled, variant) != (p["bucket"], p["enrolled"], p["variant"]):
                    problems.append("bucket_mismatch")
                alpha = p.get("personalization_alpha", 0.0)
                aff = p.get("affinity_used") if p.get("personalized") else None
                reranked = score_content(p["catalog_snapshot"], p["weights_used"],
                                         affinities=aff, personalization_alpha=alpha)
                expect = [{"content_id": cid, "score": round(s, 9)} for cid, s in reranked]
                if expect != p["ranked"]:
                    problems.append("ranking_mismatch")
                if catalog_fingerprint(p["catalog_snapshot"]) != p["catalog_hash"]:
                    problems.append("catalog_hash_mismatch")
                if dict(spec["weights"]) != dict(p["weights_used"]) and p["strategy"] == "treatment":
                    problems.append("weights_drift")
                if p.get("personalized"):
                    eff = self.privacy.effective(p["subject_digest"], p["ts"])
                    checks["personalization_now_invalid"] = not eff["profile_enabled"]
            else:
                reranked = score_content(p["catalog_snapshot"], p["weights_used"])
                expect = [{"content_id": cid, "score": round(s, 9)} for cid, s in reranked]
                if expect != p["ranked"]:
                    problems.append("ranking_mismatch")
            checks["reproducible"] = not problems
            checks["problems"] = problems
            verdicts.append(checks)
        return {"experiment_id": experiment_id, "day": int(day),
                "caliber": cal.CALIBER_VERSION, "decisions": verdicts,
                "all_reproducible": all(v["reproducible"] for v in verdicts),
                "count": len(verdicts)}

    # ---------- 曝光与反馈 ----------

    def log_exposure(self, actor, *, event_id, subject_ref, decision_seq, items,
                     happened_at=None, exposure_id=None):
        actor.require(ROLE_PRODUCT, ROLE_CONTENT, ROLE_RISK, ROLE_EDITOR)
        ts = to_ts(happened_at) if happened_at is not None else now_ts()
        subject_digest = deidentify(subject_ref, self.pepper)
        decision = self.ledger.decisions.get(int(decision_seq))
        if decision is None:
            raise DomainError("decision_not_found", f"决策序号 {decision_seq} 不存在")
        payload = {"event_id": event_id, "subject_digest": subject_digest,
                   "decision_seq": int(decision_seq),
                   "experiment_id": decision["payload"]["experiment_id"],
                   "items": items}
        reason = self.ledger.check_event(
            event_id=event_id, subject_digest=subject_digest, happened_at=ts,
            received_at=now_ts(), kind="exposure", payload=payload,
            decision_seq=int(decision_seq))
        if reason is None and ts < decision["payload"]["ts"]:
            reason = "exposure_before_decision"
        if reason:
            self._quarantine("exposure", event_id, subject_digest, reason, payload)
            return {"accepted": False, "reason": reason}
        # 曝光 ID 由事件 ID 确定性派生，迟到/重放保持稳定；乱序反馈可预先引用。
        derived = derive_exposure_id(event_id)
        if exposure_id is not None and exposure_id != derived:
            reason = "exposure_id_mismatch"
            self._quarantine("exposure", event_id, subject_digest, reason, payload)
            return {"accepted": False, "reason": reason}
        exposure_id = derived
        payload["exposure_id"] = exposure_id
        self.ledger.mark_seen(event_id)
        event = self._append("exposure_logged", payload, actor, ts=ts)
        return {"accepted": True, "exposure_id": exposure_id, "event_seq": event["seq"]}

    def receive_feedback(self, actor, *, event_id, subject_ref, ref_exposure_id,
                         content_id, kind, happened_at, payload=None):
        actor.require(ROLE_PRODUCT, ROLE_CONTENT, ROLE_RISK, ROLE_EDITOR, ROLE_USER)
        ts = to_ts(happened_at)
        received_at = now_ts()
        subject_digest = deidentify(subject_ref, self.pepper)
        body = {"event_id": event_id, "subject_digest": subject_digest,
                "ref_exposure_id": ref_exposure_id, "content_id": str(content_id),
                "kind": kind, **(payload or {})}
        reason = self.ledger.check_event(
            event_id=event_id, subject_digest=subject_digest, happened_at=ts,
            received_at=received_at, kind=kind, payload=body)
        exposure = self.ledger.exposures.get(ref_exposure_id)
        pending = exposure is None
        if exposure is not None and reason is None:
            ep = exposure["payload"]
            if ep["subject_digest"] != subject_digest:
                reason = "subject_mismatch"
            elif ts < exposure["ts"]:
                reason = "feedback_before_exposure"
            if str(content_id) not in [str(i["content_id"]) for i in ep["items"]]:
                reason = reason or "content_not_exposed"
        if reason:
            self._quarantine(kind, event_id, subject_digest, reason, body)
            return {"accepted": False, "reason": reason}
        self.ledger.mark_seen(event_id)
        self._append("feedback_received", body, actor, ts=ts)
        return {"accepted": True, "event_id": event_id, "pending_exposure": pending,
                "board_freeze_at": iso(self._freeze_for(exposure)) if exposure else None}

    def _freeze_for(self, exposure_event):
        from metrics import freeze_at_for_day
        return freeze_at_for_day(exposure_event["payload"].get("day")
                                 or cal.day_key(exposure_event["ts"]))

    def withdraw(self, actor, *, ref_event_id, subject_ref=None, reason="user_withdrawn"):
        """撤回曝光或反馈。追加撤回事件，原始事件保留；指标计算时排除。"""
        actor.require(ROLE_USER, ROLE_PRODUCT, ROLE_CONTENT, ROLE_RISK, ROLE_EDITOR)
        target = None
        for e in self.ledger.feedbacks:
            if e["payload"]["event_id"] == ref_event_id:
                target = e
                break
        if target is None:
            for e in self.ledger.exposures.values():
                if e["payload"]["event_id"] == ref_event_id:
                    target = e
                    break
        if target is None:
            raise DomainError("event_not_found", f"事件 {ref_event_id} 不存在，无法撤回")
        digest_value = target["payload"]["subject_digest"]
        if subject_ref is not None:
            if deidentify(subject_ref, self.pepper) != digest_value:
                raise DomainError("forbidden", "只能撤回自己的事件")
        if ref_event_id in self.ledger.withdrawn:
            raise DomainError("already_withdrawn", "该事件已被撤回，撤回不可重复")
        self.ledger.mark_seen("withdraw:" + ref_event_id)
        self._append("feedback_withdrawn",
                     {"ref_event_id": ref_event_id, "by": actor.id,
                      "reason": reason}, actor)
        return {"withdrawn": ref_event_id, "reason": reason}

    def _quarantine(self, kind, event_id, subject_digest, reason, payload):
        self.ledger.mark_seen(event_id)
        self._append("feedback_quarantined",
                     {"event_id": event_id, "kind": kind, "reason": reason,
                      "subject_digest_prefix": subject_digest[:cal.DIGEST_PREFIX_LEN]},
                     None)

    def quarantine_list(self):
        return [{"seq": e["seq"], "ts": e["ts"], **e["payload"]}
                for e in self.ledger.quarantine]

    # ---------- 隐私控制 ----------

    def set_profile(self, actor, *, subject_ref, enabled, ts=None):
        actor.require(ROLE_USER)
        ts = to_ts(ts) if ts is not None else now_ts()
        subject_digest = deidentify(subject_ref, self.pepper)
        self._append("profile_opt_changed",
                     {"subject_digest": subject_digest, "enabled": bool(enabled)},
                     actor, ts=ts)
        return {"profile_enabled": bool(enabled), "at": iso(ts)}

    def reset_interest(self, actor, *, subject_ref, ts=None):
        actor.require(ROLE_USER)
        ts = to_ts(ts) if ts is not None else now_ts()
        subject_digest = deidentify(subject_ref, self.pepper)
        self._append("interest_reset", {"subject_digest": subject_digest}, actor, ts=ts)
        return {"reset_at": iso(ts)}

    def record_affinity(self, actor, *, subject_ref, category, value, ts=None):
        actor.require(ROLE_USER, ROLE_EDITOR, ROLE_PRODUCT)
        ts = to_ts(ts) if ts is not None else now_ts()
        subject_digest = deidentify(subject_ref, self.pepper)
        self._append("affinity_updated",
                     {"subject_digest": subject_digest, "category": category,
                      "value": float(value)}, actor, ts=ts)
        eff = self.privacy.effective(subject_digest, ts)
        return {"stored": category in eff["affinities"], "at": iso(ts)}

    # ---------- 通道 ----------

    def admit_channel(self, actor, *, channel_id, content_id, reason, note="",
                      source=None):
        actor.require(ROLE_EDITOR, ROLE_CONTENT, ROLE_PRODUCT)
        if reason not in ADMIT_REASONS:
            raise DomainError("bad_reason", f"进入原因必须是 {ADMIT_REASONS} 之一")
        if str(content_id) not in self.channels.contents:
            raise DomainError("content_not_found", f"内容 {content_id} 未登记")
        self._append("channel_admitted",
                     {"channel_id": channel_id, "content_id": str(content_id),
                      "reason": reason, "note": note,
                      "source": source or {"actor": actor.id}}, actor)
        return {"channel_id": channel_id, "content_id": str(content_id), "status": "in"}

    def exit_channel(self, actor, *, channel_id, content_id, reason, detail=""):
        actor.require(ROLE_EDITOR, ROLE_CONTENT, ROLE_RISK, ROLE_PRODUCT)
        if reason not in EXIT_REASONS:
            raise DomainError("bad_reason", f"退出原因必须是 {EXIT_REASONS} 之一")
        self._append("channel_exited",
                     {"channel_id": channel_id, "content_id": str(content_id),
                      "reason": reason, "by": actor.id, "detail": detail}, actor)
        return {"channel_id": channel_id, "content_id": str(content_id), "status": "out"}

    def creator_view(self, actor, creator_id):
        actor.require(ROLE_CREATOR, ROLE_CONTENT, ROLE_RISK)
        if actor.role == ROLE_CREATOR and creator_id != actor.id:
            raise DomainError("forbidden", "只能查询自己名下内容的通道记录")
        views = self.channels.creator_view(creator_id)
        for v in views:
            for entry in v["timeline"]:
                if "at" in entry:
                    entry["iso"] = iso(entry["at"])
        return views

    # ---------- 指标 ----------

    def metrics_report(self):
        return MetricsEngine(self.ledger, self.experiments).compute()

    def compare_arms(self):
        engine = MetricsEngine(self.ledger, self.experiments)
        return engine.compare_segment_arms()

    def verify_journal(self):
        """重算哈希链。"""
        import hashlib
        import json as _json
        prev = "0" * 64
        for event in self.journal.events:
            payload_text = _json.dumps(event["payload"], ensure_ascii=False,
                                       sort_keys=True, separators=(",", ":"))
            expect = hashlib.sha256(
                (prev + "|" + payload_text).encode("utf-8")).hexdigest()
            if event["prev_hash"] != prev or event["hash"] != expect:
                return {"intact": False, "broken_at_seq": event["seq"]}
            prev = event["hash"]
        return {"intact": True, "events": len(self.journal.events)}
