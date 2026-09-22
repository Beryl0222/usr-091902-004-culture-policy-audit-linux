"""领域单元测试：策略门、分段冻结、反馈去污染、口径指标、隐私、通道。

所有用例使用内存 Journal 与固定时钟，互不依赖、不访问网络。
"""

import unittest

import common
from app import Actor, Application, derive_exposure_id
from caliber import (FINISH_THRESHOLDS, K_ANONYMITY, LONG_WINDOW_SECONDS,
                     SHORT_WINDOW_SECONDS, day_key)
from common import DomainError
from experiment import assign_variant
from journal import Journal
from feedback import FeedbackLedger, deidentify
from metrics import MetricsEngine, _shannon_entropy, freeze_at_for_day
from policy import build_spec, validate_weights
from privacy import PrivacyRegistry

DAY = 86400
HOUR = 3600
T0 = 1750000000
DAY0 = (T0 // DAY) * DAY

PM = Actor("pm", "product_manager")
CO = Actor("co", "content_owner")
RO = Actor("ro", "risk_owner")
ED = Actor("ed", "content_editor")

W1 = {"finish_rate": 0.4, "favorite": 0.2, "revisit": 0.2,
      "discussion_quality": 0.15, "diversity": 0.05}
CATALOG = [
    ("long_a", "非遗慢直播", "cr1", "非遗", 3 * HOUR,
     {"finish_rate": 0.4, "favorite": 0.8, "revisit": 0.7,
      "discussion_quality": 0.7, "diversity": 0.5}),
    ("long_b", "三小时讲解", "cr2", "知识", 45 * 60 + 1,
     {"finish_rate": 0.5, "favorite": 0.6, "revisit": 0.6,
      "discussion_quality": 0.6, "diversity": 0.4}),
    ("short_a", "六十秒美食", "cr3", "生活", 60,
     {"finish_rate": 0.95, "favorite": 0.2, "revisit": 0.1,
      "discussion_quality": 0.2, "diversity": 0.3}),
]
IDS = [c[0] for c in CATALOG]


def make_app():
    app = Application(pepper="unit-pepper")
    for cid, title, creator, cat, dur, feats in CATALOG:
        app.register_content(ED, content_id=cid, title=title, creator_id=creator,
                             category=cat, duration_seconds=dur, features=feats)
    return app


def approve_and_open(app, pid="p1", weights=None, traffic=10):
    weights = weights or W1
    app.draft_policy(PM, policy_id=pid, objectives=["o"], weights=weights,
                     audience={"name": "all"}, start_ts=DAY0, end_ts=DAY0 + 30 * DAY,
                     traffic_percent=traffic)
    app.submit_policy(PM, pid)
    app.approve_policy(CO, pid, "content_owner")
    app.approve_policy(RO, pid, "risk_owner")
    app.create_experiment(PM, "exp1", "h")
    app.open_segment(PM, "exp1", pid, start_ts=DAY0, end_ts=DAY0 + 14 * DAY,
                     traffic_percent=traffic)


class ClockMixin:
    def setUp(self):
        common.set_clock(fixed_ts=T0)

    def tearDown(self):
        common.set_clock()


class PolicyRulesTest(unittest.TestCase):
    def test_weights_must_sum_to_one(self):
        with self.assertRaises(DomainError) as c:
            validate_weights({"finish_rate": 0.5, "favorite": 0.4})
        self.assertEqual(c.exception.code, "bad_weights")

    def test_unknown_signal_rejected(self):
        with self.assertRaises(DomainError):
            validate_weights({"clickbait": 1.0})

    def test_traffic_capped_at_10_percent(self):
        with self.assertRaises(DomainError) as c:
            build_spec(objectives=["o"], weights={"finish_rate": 1.0},
                       audience={"name": "all"}, start_ts=DAY0,
                       end_ts=DAY0 + DAY, traffic_percent=11)
        self.assertEqual(c.exception.code, "bad_traffic")

    def test_validity_max_90_days(self):
        with self.assertRaises(DomainError):
            build_spec(objectives=["o"], weights={"finish_rate": 1.0},
                       audience={"name": "all"}, start_ts=DAY0,
                       end_ts=DAY0 + 91 * DAY, traffic_percent=5)


class PolicyLifecycleTest(ClockMixin, unittest.TestCase):
    def test_full_gate_then_revise_is_new_version(self):
        app = make_app()
        app.draft_policy(PM, policy_id="p1", objectives=["o"], weights=W1,
                         audience={"name": "all"}, start_ts=DAY0,
                         end_ts=DAY0 + 30 * DAY, traffic_percent=10)
        # draft 不能直接批准
        with self.assertRaises(DomainError):
            app.approve_policy(CO, "p1", "content_owner")
        app.submit_policy(PM, "p1")
        # 角色不能串
        with self.assertRaises(DomainError):
            app.approve_policy(CO, "p1", "risk_owner")
        app.approve_policy(CO, "p1", "content_owner")
        # 单批准仍 pending
        self.assertEqual(app.policy_view("p1")["status"], "pending")
        # 重复批准
        with self.assertRaises(DomainError):
            app.approve_policy(CO, "p1", "content_owner")
        # 同一人不能兼两个角色
        with self.assertRaises(DomainError) as c:
            app.approve_policy(Actor("co", "risk_owner"), "p1", "risk_owner")
        self.assertEqual(c.exception.code, "self_approval")
        app.approve_policy(RO, "p1", "risk_owner")
        self.assertEqual(app.policy_view("p1")["status"], "approved")
        # 产品无权回滚未生效？回滚需要 active；先激活
        app.create_experiment(PM, "e1", "h")
        app.open_segment(PM, "e1", "p1", start_ts=DAY0, end_ts=DAY0 + 10 * DAY)
        with self.assertRaises(DomainError) as c:
            app.rollback_policy(PM, "p1", "x")
        self.assertEqual(c.exception.code, "forbidden")
        # 回滚必须有原因
        with self.assertRaises(DomainError):
            app.rollback_policy(RO, "p1", "  ")

    def test_rejection_returns_to_draft_and_clears_approvals(self):
        app = make_app()
        app.draft_policy(PM, policy_id="p1", objectives=["o"], weights=W1,
                         audience={"name": "all"}, start_ts=DAY0,
                         end_ts=DAY0 + 30 * DAY, traffic_percent=10)
        app.submit_policy(PM, "p1")
        app.approve_policy(CO, "p1", "content_owner")
        app.reject_policy(RO, "p1", "risk_owner", reason="多样性不足")
        view = app.policy_view("p1")
        self.assertEqual(view["status"], "draft")
        self.assertEqual(view["approvals"], {})

    def test_revise_requires_closing_live_segment(self):
        app = make_app()
        approve_and_open(app)
        with self.assertRaises(DomainError) as c:
            app.revise_policy(PM, "p1", weights={"finish_rate": 1.0})
        self.assertEqual(c.exception.code, "segment_live")
        app.close_segment(PM, "exp1", reason="weight_change")
        app.revise_policy(PM, "p1", weights={"finish_rate": 1.0})
        self.assertEqual(app.policy_view("p1", 1)["weights"], W1)  # v1 冻结
        self.assertEqual(app.policy_view("p1", 2)["status"], "draft")


class SegmentDeterminismTest(ClockMixin, unittest.TestCase):
    def test_bucket_is_pure_function_of_seed_and_subject(self):
        spec = {"seed": "s1", "traffic_percent": 10}
        first = assign_variant(spec, "digest-x")
        for _ in range(5):
            self.assertEqual(assign_variant(spec, "digest-x"), first)
        spec2 = {"seed": "s2", "traffic_percent": 10}
        # 新分段种子独立，桶可不同
        self.assertIn(assign_variant(spec2, "digest-x")[2],
                      ("treatment", "baseline", "off"))

    def test_segment_traffic_cannot_exceed_policy(self):
        app = make_app()
        approve_and_open(app, traffic=5)
        app.close_segment(PM, "exp1", reason="weight_change")
        app.revise_policy(PM, "p1", weights={"finish_rate": 1.0})
        app.submit_policy(PM, "p1", 2)
        app.approve_policy(CO, "p1", "content_owner", version=2)
        app.approve_policy(RO, "p1", "risk_owner", version=2)
        with self.assertRaises(DomainError):
            app.open_segment(PM, "exp1", "p1", version=2, start_ts=T0 + HOUR,
                             end_ts=DAY0 + 12 * DAY, traffic_percent=8)

    def test_treatment_requires_audience_match(self):
        app = make_app()
        # 定向人群：只有 region=jiangnan 命中
        app.draft_policy(PM, policy_id="p1", objectives=["o"], weights=W1,
                         audience={"name": "jiangnan", "attrs": {"region": "jiangnan"}},
                         start_ts=DAY0, end_ts=DAY0 + 30 * DAY, traffic_percent=10)
        app.submit_policy(PM, "p1")
        app.approve_policy(CO, "p1", "content_owner")
        app.approve_policy(RO, "p1", "risk_owner")
        app.create_experiment(PM, "exp1", "h")
        app.open_segment(PM, "exp1", "p1", start_ts=DAY0, end_ts=DAY0 + 10 * DAY)
        # 找到一个入组 treatment 的主体
        hit = None
        for i in range(200):
            subj = f"u{i}"
            rank = app.rank(ED, subject_ref=subj, content_ids=IDS,
                            user_attrs={"region": "jiangnan"}, experiment_id="exp1",
                            ts=DAY0 + HOUR)
            if rank["variant"] == "treatment":
                hit = subj
                break
        self.assertIsNotNone(hit, "流量 10% 下 200 人内应出现 treatment")
        mismatch = app.rank(ED, subject_ref=hit, content_ids=IDS,
                            user_attrs={"region": "saibei"}, experiment_id="exp1",
                            ts=DAY0 + HOUR)
        # 同一主体同桶（treatment 侧），但人群不符 -> 回落基线
        self.assertEqual(mismatch["reason"], "audience_mismatch")


class FeedbackLedgerTest(ClockMixin, unittest.TestCase):
    def test_quarantine_reasons(self):
        app = make_app()
        approve_and_open(app)
        rank = app.rank(ED, subject_ref="u1", content_ids=IDS, user_attrs={},
                        experiment_id="exp1", ts=DAY0 + HOUR)
        items = [{"content_id": c[0], "category": c[3], "duration_seconds": c[4]}
                 for c in CATALOG]
        ok = app.log_exposure(ED, event_id="e1", subject_ref="u1",
                              decision_seq=rank["decision_seq"], items=items,
                              happened_at=DAY0 + HOUR + 60)
        self.assertTrue(ok["accepted"])
        # 重复
        dup = app.log_exposure(ED, event_id="e1", subject_ref="u1",
                               decision_seq=rank["decision_seq"], items=items,
                               happened_at=DAY0 + HOUR + 60)
        self.assertEqual(dup["reason"], "duplicate")
        expo_id = ok["exposure_id"]
        # 非法进度
        bad = app.receive_feedback(ED, event_id="f_bad", subject_ref="u1",
                                   ref_exposure_id=expo_id, content_id=IDS[0],
                                   kind="playback", happened_at=DAY0 + 2 * HOUR,
                                   payload={"progress": 1.5})
        self.assertFalse(bad["accepted"])
        # 跨主体
        cross = app.receive_feedback(ED, event_id="f_x", subject_ref="u2",
                                     ref_exposure_id=expo_id, content_id=IDS[0],
                                     kind="favorite", happened_at=DAY0 + 2 * HOUR)
        self.assertEqual(cross["reason"], "subject_mismatch")
        reasons = {q["reason"] for q in app.quarantine_list()}
        self.assertIn("duplicate", reasons)
        self.assertIn("bad_progress", reasons)
        self.assertIn("subject_mismatch", reasons)

    def test_withdraw_blocks_repeat_and_cascades(self):
        app = make_app()
        approve_and_open(app)
        rank = app.rank(ED, subject_ref="u1", content_ids=IDS, user_attrs={},
                        experiment_id="exp1", ts=DAY0 + HOUR)
        items = [{"content_id": c[0], "category": c[3], "duration_seconds": c[4]}
                 for c in CATALOG]
        app.log_exposure(ED, event_id="e1", subject_ref="u1",
                         decision_seq=rank["decision_seq"], items=items,
                         happened_at=DAY0 + HOUR + 60)
        app.receive_feedback(ED, event_id="f1", subject_ref="u1",
                             ref_exposure_id=derive_exposure_id("e1"),
                             content_id=IDS[0], kind="favorite",
                             happened_at=DAY0 + 2 * HOUR)
        app.withdraw(Actor("u1", "user"), ref_event_id="f1", subject_ref="u1")
        with self.assertRaises(DomainError) as c:
            app.withdraw(Actor("u1", "user"), ref_event_id="f1", subject_ref="u1")
        self.assertEqual(c.exception.code, "already_withdrawn")
        self.assertTrue(app.ledger.is_withdrawn("f1"))

    def test_user_cannot_withdraw_others_event(self):
        app = make_app()
        approve_and_open(app)
        rank = app.rank(ED, subject_ref="u1", content_ids=IDS, user_attrs={},
                        experiment_id="exp1", ts=DAY0 + HOUR)
        items = [{"content_id": c[0], "category": c[3], "duration_seconds": c[4]}
                 for c in CATALOG]
        app.log_exposure(ED, event_id="e1", subject_ref="u1",
                         decision_seq=rank["decision_seq"], items=items,
                         happened_at=DAY0 + HOUR + 60)
        with self.assertRaises(DomainError):
            app.withdraw(Actor("u2", "user"), ref_event_id="e1", subject_ref="u2")


class MetricsEngineDirectTest(ClockMixin, unittest.TestCase):
    """直接向 Journal 喂事件，精确控制格成员与到达时间。"""

    def _engine_with(self, builders):
        journal = Journal()
        ledger = FeedbackLedger()
        journal.subscribe(ledger.apply)
        for build in builders:
            build(journal)
        return MetricsEngine(ledger, {}), ledger

    def _decision(self, seq_subject, variant="treatment", segment="seg1", day=DAY0,
                  strategy="treatment"):
        subj, seq = seq_subject
        return {
            "seq": seq, "kind": "rank_decided", "actor": "ed",
            "ts": day + HOUR, "recorded_at": day + HOUR,
            "prev_hash": "", "hash": "",
            "payload": {"experiment_id": "exp1", "ts": day + HOUR, "day": day_key(day + HOUR),
                        "subject_digest": subj, "segment_id": segment,
                        "variant": variant, "strategy": strategy,
                        "policy_id": "p1", "policy_version": 1,
                        "ranked": [{"content_id": "long_a"}],
                        "weights_used": W1, "catalog_hash": "x",
                        "catalog_snapshot": []}}

    def test_k_anonymity_suppresses_small_cells(self):
        journal = Journal()
        ledger = FeedbackLedger()
        journal.subscribe(ledger.apply)
        # 2 个主体 -> 格 < k=5 -> suppressed，不得出现任何比率
        for i in range(2):
            subj = deidentify(f"u{i}", "unit-pepper")
            ev = journal.append("rank_decided",
                                self._decision((subj, 0))["payload"],
                                actor="ed", ts=DAY0 + HOUR)
            journal.append("exposure_logged",
                           {"event_id": f"e{i}", "exposure_id": f"x{i}",
                            "subject_digest": subj, "decision_seq": ev["seq"],
                            "experiment_id": "exp1",
                            "items": [{"content_id": "long_a", "category": "非遗",
                                       "duration_seconds": 3 * HOUR}]},
                           actor="ed", ts=DAY0 + HOUR + 60)
        report = MetricsEngine(ledger, {}).compute()
        self.assertEqual(len(report["cells"]), 1)
        cell = report["cells"][0]
        self.assertTrue(cell["suppressed"])
        self.assertNotIn("finish_rate", cell)
        self.assertNotIn("revisit_rate_7d", cell)

    def test_windows_and_length_thresholds(self):
        journal = Journal()
        ledger = FeedbackLedger()
        journal.subscribe(ledger.apply)
        n = K_ANONYMITY + 1
        for i in range(n):
            subj = deidentify(f"u{i}", "unit-pepper")
            ev = journal.append("rank_decided", self._decision((subj, 0))["payload"],
                                actor="ed", ts=DAY0 + HOUR)
            seq = ev["seq"]
            journal.append("exposure_logged",
                           {"event_id": f"e{i}", "exposure_id": f"x{i}",
                            "subject_digest": subj, "decision_seq": seq,
                            "experiment_id": "exp1",
                            "items": [{"content_id": "long_a", "category": "非遗",
                                       "duration_seconds": 3 * HOUR}]},
                           actor="ed", ts=DAY0 + HOUR + 60)
            t0 = DAY0 + HOUR + 60
            # 25 小时后的播放：超出短期窗口，不计完播/短期互动
            journal.append("feedback_received",
                           {"event_id": f"late{i}", "subject_digest": subj,
                            "ref_exposure_id": f"x{i}", "content_id": "long_a",
                            "kind": "playback", "progress": 0.9},
                           actor="ed", ts=t0 + 25 * HOUR)
            # 12 小时 revisit：落在 24h 内，不算回访
            journal.append("feedback_received",
                           {"event_id": f"earlyrev{i}", "subject_digest": subj,
                            "ref_exposure_id": f"x{i}", "content_id": "long_a",
                            "kind": "revisit"},
                           actor="ed", ts=t0 + 12 * HOUR)
            # 3 天 revisit：计长期回访
            journal.append("feedback_received",
                           {"event_id": f"rev{i}", "subject_digest": subj,
                            "ref_exposure_id": f"x{i}", "content_id": "long_a",
                            "kind": "revisit"},
                           actor="ed", ts=t0 + 3 * DAY)
            # 长内容播放进度 0.55（>=0.5 阈值）：计完播
            journal.append("feedback_received",
                           {"event_id": f"play{i}", "subject_digest": subj,
                            "ref_exposure_id": f"x{i}", "content_id": "long_a",
                            "kind": "playback", "progress": 0.55},
                           actor="ed", ts=t0 + 30 * 60)
        report = MetricsEngine(ledger, {}).compute()
        cell = report["cells"][0]
        self.assertFalse(cell["suppressed"])
        self.assertEqual(cell["finish_rate"], 1.0)       # 0.55 >= 长内容阈值 0.5
        self.assertEqual(cell["revisit_rate_7d"], 1.0)   # 3 天回访计入
        self.assertEqual(cell["subjects_exposed"], n)

    def test_late_arrival_after_freeze_excluded(self):
        journal = Journal()
        ledger = FeedbackLedger()
        journal.subscribe(ledger.apply)
        n = K_ANONYMITY + 1
        for i in range(n):
            subj = deidentify(f"u{i}", "unit-pepper")
            common.set_clock(fixed_ts=DAY0 + HOUR)
            ev = journal.append("rank_decided", self._decision((subj, 0))["payload"],
                                actor="ed", ts=DAY0 + HOUR)
            seq = ev["seq"]
            journal.append("exposure_logged",
                           {"event_id": f"e{i}", "exposure_id": f"x{i}",
                            "subject_digest": subj, "decision_seq": seq,
                            "experiment_id": "exp1",
                            "items": [{"content_id": "long_a", "category": "非遗",
                                       "duration_seconds": 3 * HOUR}]},
                           actor="ed", ts=DAY0 + HOUR + 60)
            # 发生在 D0，但 D8+2h 才到达（封板时刻 D8+1h）
            common.set_clock(fixed_ts=DAY0 + 8 * DAY + 2 * HOUR)
            journal.append("feedback_received",
                           {"event_id": f"late{i}", "subject_digest": subj,
                            "ref_exposure_id": f"x{i}", "content_id": "long_a",
                            "kind": "favorite"},
                           actor="ed", ts=DAY0 + 10 * HOUR)
        common.set_clock(fixed_ts=T0)
        report = MetricsEngine(ledger, {}).compute()
        self.assertEqual(report["excluded"].get("feedback_arrived_after_freeze"), n)
        # 迟到反馈全部被排除：收藏计数为 0（而非计入后稀释比率）
        self.assertEqual(report["cells"][0]["favorites"], 0)

    def test_shannon_entropy(self):
        self.assertEqual(_shannon_entropy(["a", "a", "a"]), 0.0)
        self.assertAlmostEqual(_shannon_entropy(["a", "b", "c", "d"]), 1.0, places=6)
        self.assertIsNone(_shannon_entropy([]))

    def test_segments_are_never_merged(self):
        journal = Journal()
        ledger = FeedbackLedger()
        journal.subscribe(ledger.apply)
        for i, seg in enumerate(("seg1", "seg2"), start=1):
            for j in range(K_ANONYMITY + 1):
                subj = deidentify(f"{seg}-u{j}", "unit-pepper")
                ev = journal.append(
                    "rank_decided", self._decision((subj, 0), segment=seg)["payload"],
                    actor="ed", ts=DAY0 + HOUR)
                seq = ev["seq"]
                journal.append("exposure_logged",
                               {"event_id": f"e-{seg}-{j}", "exposure_id": f"x-{seg}-{j}",
                                "subject_digest": subj, "decision_seq": seq,
                                "experiment_id": "exp1",
                                "items": [{"content_id": "long_a", "category": "非遗",
                                           "duration_seconds": 3 * HOUR}]},
                               actor="ed", ts=DAY0 + HOUR + 60)
        report = MetricsEngine(ledger, {}).compute()
        seg_ids = {c["key"]["segment_id"] for c in report["cells"]}
        self.assertEqual(seg_ids, {"seg1", "seg2"})


class PrivacyTest(ClockMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.r = PrivacyRegistry()
        self.d = "digest-1"

    def _ev(self, kind, payload, ts):
        return {"kind": kind, "ts": ts, "payload": payload}

    def test_default_has_no_profile_and_no_affinity(self):
        eff = self.r.effective(self.d, T0)
        self.assertFalse(eff["profile_enabled"])
        self.assertEqual(eff["affinities"], {})

    def test_opt_out_then_old_affinity_invisible(self):
        self.r.apply(self._ev("profile_opt_changed",
                              {"subject_digest": self.d, "enabled": True}, T0 - 10 * HOUR))
        self.r.apply(self._ev("affinity_updated",
                              {"subject_digest": self.d, "category": "非遗", "value": 0.9},
                              T0 - 9 * HOUR))
        self.r.apply(self._ev("profile_opt_changed",
                              {"subject_digest": self.d, "enabled": False}, T0 - HOUR))
        eff = self.r.effective(self.d, T0)
        self.assertFalse(eff["profile_enabled"])
        self.assertEqual(eff["affinities"], {})

    def test_reset_tombstone_kills_old_prefs_new_accumulate(self):
        self.r.apply(self._ev("profile_opt_changed",
                              {"subject_digest": self.d, "enabled": True}, T0 - 10 * HOUR))
        self.r.apply(self._ev("affinity_updated",
                              {"subject_digest": self.d, "category": "非遗", "value": 0.9},
                              T0 - 9 * HOUR))
        self.r.apply(self._ev("interest_reset", {"subject_digest": self.d}, T0 - 8 * HOUR))
        # 重置之后才到达的、发生于重置前的旧更新：必须丢弃
        self.r.apply(self._ev("affinity_updated",
                              {"subject_digest": self.d, "category": "戏曲", "value": 0.8},
                              T0 - 9 * HOUR + 1))
        eff = self.r.effective(self.d, T0)
        self.assertEqual(eff["affinities"], {})
        # 重置后的新积累生效
        self.r.apply(self._ev("affinity_updated",
                              {"subject_digest": self.d, "category": "音乐", "value": 0.3},
                              T0 - HOUR))
        eff = self.r.effective(self.d, T0)
        self.assertEqual(eff["affinities"], {"音乐": 0.3})

    def test_affinity_while_opted_out_not_stored(self):
        self.r.apply(self._ev("affinity_updated",
                              {"subject_digest": self.d, "category": "非遗", "value": 0.9},
                              T0))
        self.assertEqual(self.r.effective(self.d, T0)["affinities"], {})


class ChannelTest(ClockMixin, unittest.TestCase):
    def test_admit_exit_and_double_exit(self):
        app = make_app()
        app.admit_channel(ED, channel_id="ch", content_id="long_a",
                          reason="editorial_pick")
        with self.assertRaises(DomainError):
            app.admit_channel(ED, channel_id="ch", content_id="long_a",
                              reason="editorial_pick")
        app.exit_channel(ED, channel_id="ch", content_id="long_a",
                         reason="editorial_remove")
        with self.assertRaises(DomainError):
            app.exit_channel(ED, channel_id="ch", content_id="long_a",
                             reason="editorial_remove")

    def test_creator_sees_only_own_content_with_reasons(self):
        app = make_app()
        app.admit_channel(ED, channel_id="ch", content_id="long_a",
                          reason="experiment_policy",
                          source={"policy_id": "p", "policy_version": 1})
        view = app.creator_view(Actor("cr1", "creator"), "cr1")
        self.assertEqual({c["content_id"] for c in view}, {"long_a"})
        timeline = view[0]["timeline"]
        self.assertEqual(timeline[0]["reason"], "experiment_policy")

    def test_rollback_cascades_channel_exit(self):
        app = make_app()
        approve_and_open(app)
        app.admit_channel(ED, channel_id="boost", content_id="long_a",
                          reason="experiment_policy",
                          source={"policy_id": "p1", "policy_version": 1,
                                  "experiment_id": "exp1", "segment_id": "exp1-seg1"})
        app.rollback_policy(RO, "p1", "风险熔断演练")
        view = app.creator_view(Actor("cr1", "creator"), "cr1")
        exits = [t for c in view for t in c["timeline"] if t["action"] == "exited"]
        self.assertTrue(any(t["reason"] == "policy_rolled_back" for t in exits))


class ReproduceTamperTest(ClockMixin, unittest.TestCase):
    def test_reproduce_day_recomputes_buckets_and_ranking(self):
        app = make_app()
        approve_and_open(app)
        seqs = []
        for i in range(60):
            rank = app.rank(ED, subject_ref=f"u{i}", content_ids=IDS, user_attrs={},
                            experiment_id="exp1", ts=DAY0 + 2 * HOUR)
            seqs.append(rank["decision_seq"])
        rep = app.reproduce_day("exp1", day_key(DAY0 + 2 * HOUR))
        self.assertTrue(rep["all_reproducible"])
        self.assertEqual(rep["count"], 60)

    def test_identical_weights_different_segment_not_one_continuous_run(self):
        # 即使新旧权重碰巧相同，调权流程也必须留下 seg1(closed)/seg2(live) 两段
        app = make_app()
        approve_and_open(app)
        app.close_segment(PM, "exp1", reason="weight_change", detail="例行复盘分段")
        app.revise_policy(PM, "p1", weights=dict(W1))  # 相同权重
        app.submit_policy(PM, "p1", 2)
        app.approve_policy(CO, "p1", "content_owner", version=2)
        app.approve_policy(RO, "p1", "risk_owner", version=2)
        app.open_segment(PM, "exp1", "p1", version=2, start_ts=DAY0 + DAY,
                         end_ts=DAY0 + 12 * DAY)
        statuses = [(s["segment_id"], s["status"], s["policy_version"])
                    for s in app.experiment_view("exp1")["segments"]]
        self.assertEqual(statuses, [("exp1-seg1", "closed", 1),
                                    ("exp1-seg2", "live", 2)])


if __name__ == "__main__":
    unittest.main()
