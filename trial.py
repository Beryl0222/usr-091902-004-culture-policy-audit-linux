#!/usr/bin/env python3
"""端到端试运行：长短内容、紧急回滚、事件乱序下的策略治理验收。

运行：python3 trial.py
产出：控制台逐步报告，并把事件日志落到 trial_out/trial.journal（可重新打开复现）。

时间线（UTC 自然日）：
  D0  策略草拟→双批准→seg1 上线；240 名去标识化用户分流，长短内容混排，
      产生收藏/讨论/回访等反馈，夹带重复、撤回、乱序、超窗事件；
  D1  编辑部要调权：被拒（分段在跑）→ 关 seg1 → p1 升 v2 重新双批准 → seg2 另开；
  D2  风险负责人紧急回滚 v2：seg2 关闭、策略纳入通道的内容级联退出；
  D9+ 补传事件到达，D0 分区已封板 → 留日志但绝不改指标。
"""

import hashlib
import json
import os
import sys
import tempfile

import common
from app import Actor, Application, derive_exposure_id
from caliber import K_ANONYMITY, day_key
from common import DomainError, iso

DAY = 86400
HOUR = 3600
MINUTE = 60
ANCHOR_DAY = (1750000000 // DAY) * DAY  # 对齐到 UTC 日界
D0 = ANCHOR_DAY + 9 * HOUR

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trial_out")
JOURNAL = os.path.join(OUT_DIR, "trial.journal")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"  [{'✓' if cond else '✗'}] {name}" + (f" —— {detail}" if detail else ""))
    if not cond:
        check.failed += 1
check.failed = 0


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def rng(*parts):
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2 ** 64


def at(ts):
    """把"服务器当前时间"拨到 ts：模拟事件的真实到达时刻。"""
    common.set_clock(fixed_ts=ts)


# ---------- 角色 ----------
PM = Actor("lin_chanpin", "product_manager")
CONTENT_OWNER = Actor("chen_neirong", "content_owner")
RISK_OWNER = Actor("feng_fengxian", "risk_owner")
EDITOR = Actor("bianji_zu", "content_editor")
CREATOR_A = Actor("kunqu_master", "creator")
CREATOR_B = Actor("suxiu_studio", "creator")
USER_PRIVACY = "user-privacy-007"

# (content_id, 标题, 创作者, 类别, 时长秒, 信号特征)
CATALOG = [
    ("c_kunqu_3h", "牡丹亭·经典课文三小时全本", "kunqu_master", "戏曲经典", 3 * 3600,
     {"finish_rate": 0.42, "favorite": 0.82, "revisit": 0.78,
      "discussion_quality": 0.74, "diversity": 0.5}),
    ("c_suxiu_live", "苏绣非遗慢直播·劈针之夜", "suxiu_studio", "非遗手艺", 2 * 3600,
     {"finish_rate": 0.38, "favorite": 0.80, "revisit": 0.72,
      "discussion_quality": 0.66, "diversity": 0.6}),
    ("c_bronze_lecture", "青铜器修复十五讲", "bowuguan_laoshi", "知识讲解", 47 * MINUTE,
     {"finish_rate": 0.55, "favorite": 0.62, "revisit": 0.60,
      "discussion_quality": 0.70, "diversity": 0.45}),
    ("c_chibi_read", "《赤壁赋》全文讲读", "yuwen_zu", "经典课文", 52 * MINUTE,
     {"finish_rate": 0.58, "favorite": 0.66, "revisit": 0.58,
      "discussion_quality": 0.72, "diversity": 0.4}),
    ("c_papercut_short", "一分钟看懂民间剪纸", "suxiu_studio", "非遗手艺", 60,
     {"finish_rate": 0.93, "favorite": 0.30, "revisit": 0.18,
      "discussion_quality": 0.25, "diversity": 0.35}),
    ("c_food_short", "节气美食六十秒", "shenghuozu", "生活风物", 60,
     {"finish_rate": 0.95, "favorite": 0.28, "revisit": 0.15,
      "discussion_quality": 0.20, "diversity": 0.30}),
    ("c_beiying_film", "《背影》课文影像", "yuwen_zu", "经典课文", 8 * MINUTE,
     {"finish_rate": 0.88, "favorite": 0.45, "revisit": 0.40,
      "discussion_quality": 0.55, "diversity": 0.40}),
    ("c_shadow_play", "皮影戏折子戏精选", "kunqu_master", "戏曲经典", 12 * MINUTE,
     {"finish_rate": 0.84, "favorite": 0.50, "revisit": 0.42,
      "discussion_quality": 0.50, "diversity": 0.42}),
]
CONTENT_IDS = [c[0] for c in CATALOG]
BY_ID = {c[0]: c for c in CATALOG}
LONG_IDS = {c[0] for c in CATALOG if c[4] >= 15 * MINUTE}
TOP_N = 5  # 每次曝光记录前 5 条，使双臂都含长内容格


def register_catalog(app):
    for cid, title, creator, cat, dur, feats in CATALOG:
        actor = CREATOR_A if creator == "kunqu_master" else (
            CREATOR_B if creator == "suxiu_studio" else EDITOR)
        app.register_content(actor, content_id=cid, title=title, creator_id=creator,
                             category=cat, duration_seconds=dur, features=feats)


def expose_top(app, subj, rank, happened_ts, tag):
    """按决策排序的前 TOP_N 生成曝光项。到达时刻 = 发生时刻（正常有序投递）。"""
    items = [{"content_id": r["content_id"], "category": BY_ID[r["content_id"]][3],
              "duration_seconds": BY_ID[r["content_id"]][4]}
             for r in rank["ranked"][:TOP_N]]
    ev_id = f"ex-{tag}-{subj}"
    at(happened_ts)
    exp = app.log_exposure(EDITOR, event_id=ev_id, subject_ref=subj,
                           decision_seq=rank["decision_seq"], items=items,
                           happened_at=happened_ts)
    return ev_id, exp, items


def send_behavior(app, subj, rank, ev_id, items, t_expose, tag):
    """按变体与时长档模拟反馈；每条事件的到达时刻=发生时刻，随后续窗口推进。"""
    variant = rank["variant"]
    for idx, item in enumerate(items):
        cid = item["content_id"]
        is_long = cid in LONG_IDS
        p_finish = (0.90 if not is_long else 0.30)
        if variant == "treatment":
            p_finish = 0.85 if not is_long else 0.52
        if rng(tag, subj, cid, "play") < p_finish:
            threshold = 0.5 if is_long else 0.8
            ts = t_expose + 30 * MINUTE
            at(ts)
            app.receive_feedback(EDITOR, event_id=f"fb-play-{tag}-{subj}-{cid}",
                                 subject_ref=subj,
                                 ref_exposure_id=derive_exposure_id(ev_id),
                                 content_id=cid, kind="playback", happened_at=ts,
                                 payload={"progress": threshold + 0.1})
        p_fav = 0.70 if (variant == "treatment" and is_long) else (
            0.25 if is_long else 0.20)
        if rng(tag, subj, cid, "fav") < p_fav:
            ts = t_expose + 2 * HOUR
            at(ts)
            app.receive_feedback(EDITOR, event_id=f"fb-fav-{tag}-{subj}-{cid}",
                                 subject_ref=subj,
                                 ref_exposure_id=derive_exposure_id(ev_id),
                                 content_id=cid, kind="favorite", happened_at=ts)
        if rng(tag, subj, cid, "disc") < (0.6 if variant == "treatment" and is_long else 0.25):
            ts = t_expose + 5 * HOUR
            at(ts)
            app.receive_feedback(EDITOR, event_id=f"fb-disc-{tag}-{subj}-{cid}",
                                 subject_ref=subj,
                                 ref_exposure_id=derive_exposure_id(ev_id),
                                 content_id=cid, kind="discussion", happened_at=ts,
                                 payload={"quality": 0.8 if is_long else 0.4})
        p_rev = (0.65 if is_long else 0.15) if variant == "treatment" else (
            0.18 if is_long else 0.12)
        if rng(tag, subj, cid, "rev") < p_rev:
            ts = t_expose + 3 * DAY + idx * HOUR  # 24h..7d 内的回访
            at(ts)
            app.receive_feedback(EDITOR, event_id=f"fb-rev-{tag}-{subj}-{cid}",
                                 subject_ref=subj,
                                 ref_exposure_id=derive_exposure_id(ev_id),
                                 content_id=cid, kind="revisit", happened_at=ts)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    if os.path.exists(JOURNAL):
        os.remove(JOURNAL)
    at(D0)
    app = Application(JOURNAL, pepper="trial-fixed-pepper")

    # ============ D0：审批与上线 ============
    section("D0｜内容登记、策略草拟与双负责人批准")
    register_catalog(app)
    app.admit_channel(EDITOR, channel_id="culture_spotlight",
                      content_id="c_beiying_film", reason="editorial_pick",
                      note="编辑部本周主推经典课文")

    weights_v1 = {"finish_rate": 0.35, "favorite": 0.20, "revisit": 0.20,
                  "discussion_quality": 0.20, "diversity": 0.05}
    app.draft_policy(PM, policy_id="p_multi_signal", objectives=[
        "让数小时知识讲解、非遗慢直播与经典课文影像获得基本曝光",
        "用收藏/回访/讨论质量补充完播率，同时盯住多样性避免新茧房"],
        weights=weights_v1, audience={"name": "all"},
        start_ts=ANCHOR_DAY, end_ts=ANCHOR_DAY + 30 * DAY,
        traffic_percent=10, rationale="完播率单指标导致长内容零曝光")

    try:
        app.approve_policy(PM, "p_multi_signal", "content_owner")
        check("产品经理不能充当批准人", False)
    except DomainError as exc:
        check("产品经理不能充当批准人", exc.code == "forbidden", exc.code)
    app.submit_policy(PM, "p_multi_signal")
    check("提交后进入待批准", app.policy_view("p_multi_signal")["status"] == "pending")
    app.approve_policy(CONTENT_OWNER, "p_multi_signal", "content_owner",
                       comment="目标与权重与编辑导向一致")
    try:
        app.approve_policy(CONTENT_OWNER, "p_multi_signal", "risk_owner")
        check("同一人不能兼任两名批准人", False)
    except DomainError as exc:
        check("同一人不能兼任两名批准人", exc.code == "self_approval", exc.code)
    app.approve_policy(RISK_OWNER, "p_multi_signal", "risk_owner",
                       comment="流量≤10%，人群与有效期合规")
    check("双批准集齐后状态 approved",
          app.policy_view("p_multi_signal")["status"] == "approved")

    try:
        app.draft_policy(PM, policy_id="p_too_big", objectives=["x"],
                         weights={"finish_rate": 1.0}, audience={"name": "all"},
                         start_ts=ANCHOR_DAY, end_ts=ANCHOR_DAY + 10 * DAY,
                         traffic_percent=30)
        check("小流量上限 10% 强制", False)
    except DomainError as exc:
        check("小流量上限 10% 强制", exc.code == "bad_traffic", exc.message)

    section("D0｜实验 exp_culture 与不可变分段 seg1")
    app.create_experiment(PM, "exp_culture",
                          "多维信号相较完播单指标，能否提升长内容曝光与 7 日回访")
    seg1 = app.open_segment(PM, "exp_culture", "p_multi_signal",
                            start_ts=ANCHOR_DAY, end_ts=ANCHOR_DAY + 7 * DAY,
                            traffic_percent=10)
    check("seg1 固化 v1 权重", seg1["weights"] == weights_v1)
    check("分段自带独立种子", len(seg1["seed"]) == 32)
    check("分段口径冻结为 caliber-v1", seg1["caliber_version"] == "caliber-v1")
    check("开启分段时策略激活",
          app.policy_view("p_multi_signal")["status"] == "active")

    # 长内容因实验策略进入独立通道，来源（策略/版本/分段）留痕
    app.admit_channel(EDITOR, channel_id="long_form_boost",
                      content_id="c_kunqu_3h", reason="experiment_policy",
                      note="多信号策略下获得独立通道曝光",
                      source={"policy_id": "p_multi_signal", "policy_version": 1,
                              "experiment_id": "exp_culture",
                              "segment_id": seg1["segment_id"]})

    # ============ D0：240 个去标识化主体分流 ============
    section("D0｜长短内容混排分流（240 个去标识化主体）")
    at(D0)
    subjects = [f"trial-user-{i:04d}" for i in range(240)]
    app.set_profile(Actor(USER_PRIVACY, "user"), subject_ref=USER_PRIVACY, enabled=True)
    app.record_affinity(EDITOR, subject_ref=USER_PRIVACY, category="非遗手艺", value=0.9)

    arms_day0 = {"treatment": 0, "baseline": 0, "off": 0}
    top1_long = {"treatment": 0, "baseline": 0, "off": 0}
    sample_treatment = None
    for subj in subjects:
        rank = app.rank(EDITOR, subject_ref=subj, content_ids=CONTENT_IDS,
                        user_attrs={}, experiment_id="exp_culture", ts=D0)
        arms_day0[rank["variant"]] += 1
        if rank["ranked"][0]["content_id"] in LONG_IDS:
            top1_long[rank["variant"]] += 1
        if sample_treatment is None and rank["variant"] == "treatment":
            sample_treatment = subj
        ev_id, exp, items = expose_top(app, subj, rank, D0 + MINUTE, "d0")
        send_behavior(app, subj, rank, ev_id, items, D0 + MINUTE, "d0")
    print(f"  分流构成：{arms_day0}；首位为长内容的计数：{top1_long}")
    check("treatment/对照/未入组三类同时存在", all(v > 0 for v in arms_day0.values()),
          str(arms_day0))
    treat_share = top1_long["treatment"] / arms_day0["treatment"]
    base_share = top1_long["baseline"] / max(1, arms_day0["baseline"])
    check("多信号策略把长内容顶到首位（treatment 占比高于对照）",
          treat_share > base_share, f"treatment {treat_share:.2f} vs baseline {base_share:.2f}")

    # ============ 隐私：关闭画像/重置兴趣 ============
    section("D0｜隐私闸门：关闭画像、兴趣重置后历史偏好不得延续")
    app.rank(EDITOR, subject_ref=USER_PRIVACY, content_ids=CONTENT_IDS,
             user_attrs={}, experiment_id="exp_culture", ts=D0)
    app.set_profile(Actor(USER_PRIVACY, "user"), subject_ref=USER_PRIVACY,
                    enabled=False, ts=D0 + HOUR)
    after_off = app.rank(EDITOR, subject_ref=USER_PRIVACY, content_ids=CONTENT_IDS,
                         user_attrs={}, experiment_id="exp_culture", ts=D0 + 2 * HOUR)
    check("关闭画像后决策 personalized=false", after_off["personalized"] is False)
    check("关闭画像后个性化加成 alpha=0", after_off.get("personalization_alpha", 0.0) == 0.0)
    at(D0 + 3 * HOUR)
    stored = app.record_affinity(EDITOR, subject_ref=USER_PRIVACY,
                                 category="戏曲经典", value=0.8, ts=D0 + 3 * HOUR)
    check("画像关闭期间的亲和更新不落库", stored["stored"] is False)
    app.reset_interest(Actor(USER_PRIVACY, "user"), subject_ref=USER_PRIVACY,
                       ts=D0 + 4 * HOUR)
    app.set_profile(Actor(USER_PRIVACY, "user"), subject_ref=USER_PRIVACY,
                    enabled=True, ts=D0 + 5 * HOUR)
    import feedback as feedback_mod
    eff = app.privacy.effective(
        feedback_mod.deidentify(USER_PRIVACY, "trial-fixed-pepper"), D0 + 6 * HOUR)
    check("重置后旧偏好（非遗手艺 0.9）不恢复", eff["affinities"] == {}, str(eff["affinities"]))
    check("画像可重新开启但只能从零积累", eff["profile_enabled"] is True)

    # ============ 污染事件注入 ============
    section("D0｜污染事件：重复、撤回、乱序、超窗、未来、无法归因、跨主体")
    # 1) 重复曝光 + 重复反馈，随后整条曝光撤回（其反馈级联失效）
    victim = subjects[0]
    at(D0 + 8 * HOUR)
    v_rank = app.rank(EDITOR, subject_ref=victim, content_ids=CONTENT_IDS,
                      user_attrs={}, experiment_id="exp_culture", ts=D0 + 8 * HOUR)
    v_items = [{"content_id": r["content_id"], "category": BY_ID[r["content_id"]][3],
                "duration_seconds": BY_ID[r["content_id"]][4]}
               for r in v_rank["ranked"][:TOP_N]]
    at(D0 + 8 * HOUR + 2 * MINUTE)
    v_exp = app.log_exposure(EDITOR, event_id="ex-dup-demo", subject_ref=victim,
                             decision_seq=v_rank["decision_seq"], items=v_items,
                             happened_at=D0 + 8 * HOUR + MINUTE)
    dup = app.log_exposure(EDITOR, event_id="ex-dup-demo", subject_ref=victim,
                           decision_seq=v_rank["decision_seq"], items=v_items,
                           happened_at=D0 + 8 * HOUR + MINUTE)
    check("重复曝光被隔离（duplicate）",
          v_exp["accepted"] and dup["accepted"] is False and dup["reason"] == "duplicate")
    at(D0 + 9 * HOUR)
    r1 = app.receive_feedback(EDITOR, event_id="fb-dup-demo", subject_ref=victim,
                              ref_exposure_id=derive_exposure_id("ex-dup-demo"),
                              content_id=v_items[0]["content_id"], kind="favorite",
                              happened_at=D0 + 9 * HOUR)
    r2 = app.receive_feedback(EDITOR, event_id="fb-dup-demo", subject_ref=victim,
                              ref_exposure_id=derive_exposure_id("ex-dup-demo"),
                              content_id=v_items[0]["content_id"], kind="favorite",
                              happened_at=D0 + 9 * HOUR)
    check("重复反馈被隔离（duplicate）",
          r1["accepted"] and not r2["accepted"] and r2["reason"] == "duplicate")
    at(D0 + 9 * HOUR + MINUTE)
    wd = app.withdraw(Actor(victim, "user"), ref_event_id="ex-dup-demo",
                      subject_ref=victim, reason="user_deleted_history")
    check("撤回以追加事件留痕（原事件不删）", wd["withdrawn"] == "ex-dup-demo")

    # 2) 单条反馈撤回（曝光保留）
    solo = subjects[10]
    at(D0 + 12 * HOUR)
    s_rank = app.rank(EDITOR, subject_ref=solo, content_ids=CONTENT_IDS,
                      user_attrs={}, experiment_id="exp_culture", ts=D0 + 12 * HOUR)
    s_cid = s_rank["ranked"][0]["content_id"]
    s_items = [{"content_id": r["content_id"], "category": BY_ID[r["content_id"]][3],
                "duration_seconds": BY_ID[r["content_id"]][4]}
               for r in s_rank["ranked"][:TOP_N]]
    app.log_exposure(EDITOR, event_id="ex-solo-withdraw", subject_ref=solo,
                     decision_seq=s_rank["decision_seq"], items=s_items,
                     happened_at=D0 + 12 * HOUR + MINUTE)
    at(D0 + 12 * HOUR + 30 * MINUTE)
    app.receive_feedback(EDITOR, event_id="fb-solo-fav", subject_ref=solo,
                         ref_exposure_id=derive_exposure_id("ex-solo-withdraw"),
                         content_id=s_cid, kind="favorite",
                         happened_at=D0 + 12 * HOUR + 30 * MINUTE)
    at(D0 + 12 * HOUR + 31 * MINUTE)
    app.withdraw(Actor(solo, "user"), ref_event_id="fb-solo-fav",
                 subject_ref=solo, reason="user_unfaved")
    check("单条反馈可独立撤回", app.ledger.is_withdrawn("fb-solo-fav"))

    # 3) 乱序：反馈先到（待归因）→ 曝光后补，重放时自动连接
    oo = subjects[1]
    at(D0 + 10 * HOUR)
    oo_rank = app.rank(EDITOR, subject_ref=oo, content_ids=CONTENT_IDS,
                       user_attrs={}, experiment_id="exp_culture", ts=D0 + 10 * HOUR)
    oo_cid = oo_rank["ranked"][0]["content_id"]
    at(D0 + 10 * HOUR + 25 * MINUTE)  # 反馈先到达服务器
    fb_oo = app.receive_feedback(EDITOR, event_id="fb-oo-early", subject_ref=oo,
                                 ref_exposure_id=derive_exposure_id("ex-oo"),
                                 content_id=oo_cid, kind="favorite",
                                 happened_at=D0 + 10 * HOUR + 20 * MINUTE)
    check("反馈早于曝光到达：入账但标记待归因",
          fb_oo["accepted"] and fb_oo["pending_exposure"] is True)
    at(D0 + 10 * HOUR + 30 * MINUTE)  # 曝光后到达
    oo_items = [{"content_id": r["content_id"], "category": BY_ID[r["content_id"]][3],
                 "duration_seconds": BY_ID[r["content_id"]][4]}
                for r in oo_rank["ranked"][:TOP_N]]
    exp_oo = app.log_exposure(EDITOR, event_id="ex-oo", subject_ref=oo,
                              decision_seq=oo_rank["decision_seq"], items=oo_items,
                              happened_at=D0 + 10 * HOUR + MINUTE)
    check("曝光补齐后乱序反馈在重放时可归因", exp_oo["accepted"] is True)

    # 4) 未来事件
    at(D0)
    future = app.receive_feedback(EDITOR, event_id="fb-future", subject_ref=subjects[2],
                                  ref_exposure_id=derive_exposure_id("ex-d0-" + subjects[2]),
                                  content_id="c_food_short", kind="favorite",
                                  happened_at=D0 + 30 * DAY)
    check("声称发生在未来的事件被隔离（future_event）",
          future["accepted"] is False and future["reason"] == "future_event", future["reason"])

    # 5) 无法归因：引用了一个永远不会出现的曝光
    at(D0 + 11 * HOUR)
    ghost = app.receive_feedback(EDITOR, event_id="fb-ghost", subject_ref=subjects[3],
                                 ref_exposure_id="expo_does_not_exist",
                                 content_id="c_food_short", kind="favorite",
                                 happened_at=D0 + 11 * HOUR)
    check("无法归因反馈入账留痕但指标层排除",
          ghost["accepted"] and ghost["pending_exposure"] is True)

    # 6) 跨主体：把反馈挂到别人的曝光上
    cross = app.receive_feedback(EDITOR, event_id="fb-cross", subject_ref=subjects[5],
                                 ref_exposure_id=derive_exposure_id("ex-d0-" + subjects[4]),
                                 content_id="c_food_short", kind="favorite",
                                 happened_at=D0 + 11 * HOUR + MINUTE)
    check("跨主体反馈被隔离（subject_mismatch）",
          cross["accepted"] is False and cross["reason"] == "subject_mismatch", cross["reason"])

    # 7) 超 7 天窗口但封板前到达：引用一个日界附近的曝光
    early_subj = subjects[11]
    at(ANCHOR_DAY + 5 * MINUTE)
    e_rank = app.rank(EDITOR, subject_ref=early_subj, content_ids=CONTENT_IDS,
                      user_attrs={}, experiment_id="exp_culture", ts=ANCHOR_DAY + 5 * MINUTE)
    e_items = [{"content_id": r["content_id"], "category": BY_ID[r["content_id"]][3],
                "duration_seconds": BY_ID[r["content_id"]][4]}
               for r in e_rank["ranked"][:TOP_N]]
    app.log_exposure(EDITOR, event_id="ex-early-day", subject_ref=early_subj,
                     decision_seq=e_rank["decision_seq"], items=e_items,
                     happened_at=ANCHOR_DAY + 10 * MINUTE)
    beyond_ts = ANCHOR_DAY + 7 * DAY + 2 * HOUR
    at(beyond_ts)  # 封板时刻 = 次日 00:00 + 7天+1小时 ≈ D8+1h，此刻尚未封板
    beyond = app.receive_feedback(EDITOR, event_id="fb-beyond-window",
                                  subject_ref=early_subj,
                                  ref_exposure_id=derive_exposure_id("ex-early-day"),
                                  content_id=e_items[0]["content_id"], kind="revisit",
                                  happened_at=beyond_ts)
    check("超出 7 天观察窗：入账留痕、指标排除（outside_window）",
          beyond["accepted"] is True)

    # ============ D1：中途调权 → 另开分段 ============
    section("D1｜中途调权：禁止改权重伪装连续实验")
    D1 = ANCHOR_DAY + DAY + 9 * HOUR
    at(D1)
    try:
        app.revise_policy(PM, "p_multi_signal",
                          weights={"finish_rate": 0.20, "favorite": 0.20, "revisit": 0.20,
                                   "discussion_quality": 0.20, "diversity": 0.20})
        check("分段在跑时禁止修订", False)
    except DomainError as exc:
        check("分段在跑时禁止修订（必须先关分段）", exc.code == "segment_live", exc.code)
    app.close_segment(PM, "exp_culture", reason="weight_change",
                      detail="编辑部希望提高多样性权重，防止形成新兴趣茧房")
    app.revise_policy(PM, "p_multi_signal",
                      weights={"finish_rate": 0.20, "favorite": 0.20, "revisit": 0.20,
                               "discussion_quality": 0.20, "diversity": 0.20})
    v2 = app.policy_view("p_multi_signal", 2)
    check("调权产生 v2，v1 权重冻结保留",
          v2["version"] == 2
          and app.policy_view("p_multi_signal", 1)["weights"] == weights_v1)
    check("v2 回到 draft，须重新双批准", v2["status"] == "draft", v2["status"])
    app.submit_policy(PM, "p_multi_signal", 2)
    app.approve_policy(CONTENT_OWNER, "p_multi_signal", "content_owner", version=2,
                       comment="多样性提升至 0.2，抑制新茧房")
    app.approve_policy(RISK_OWNER, "p_multi_signal", "risk_owner", version=2,
                       comment="继续 10% 流量上限")
    seg2 = app.open_segment(PM, "exp_culture", "p_multi_signal", version=2,
                            start_ts=D1, end_ts=ANCHOR_DAY + 14 * DAY,
                            traffic_percent=10)
    check("seg2 是新分段且种子不同于 seg1",
          seg2["segment_id"] == "exp_culture-seg2" and seg2["seed"] != seg1["seed"])
    check("seg2 固化的是 v2 权重", seg2["weights"]["diversity"] == 0.20)
    exp_view = app.experiment_view("exp_culture")
    check("同一实验保留两个分段且 seg1 已关闭",
          [(s["segment_id"], s["status"]) for s in exp_view["segments"]]
          == [("exp_culture-seg1", "closed"), ("exp_culture-seg2", "live")])

    arms_d1 = {"treatment": 0, "baseline": 0, "off": 0}
    for subj in subjects:
        rank = app.rank(EDITOR, subject_ref=subj, content_ids=CONTENT_IDS,
                        user_attrs={}, experiment_id="exp_culture", ts=D1)
        arms_d1[rank["variant"]] += 1
        ev_id, exp, items = expose_top(app, subj, rank, D1 + MINUTE, "d1")
        send_behavior(app, subj, rank, ev_id, items, D1 + MINUTE, "d1")
    check("seg2 同样三类分流", all(v > 0 for v in arms_d1.values()), str(arms_d1))

    # ============ D2：紧急回滚 ============
    section("D2｜紧急回滚：风险负责人一键熔断、级联退出")
    D2 = ANCHOR_DAY + 2 * DAY + 10 * HOUR
    at(D2)
    try:
        app.rollback_policy(RISK_OWNER, "p_multi_signal", "   ", version=2)
        check("回滚必须填写原因", False)
    except DomainError as exc:
        check("回滚必须填写原因", exc.code == "rollback_without_reason", exc.code)
    app.rollback_policy(RISK_OWNER, "p_multi_signal",
                        "监控发现 seg2 下非遗类别曝光占比异常抬升，疑似诱发新茧房",
                        version=2)
    seg2_state = next(s for s in app.experiment_view("exp_culture")["segments"]
                      if s["segment_id"] == "exp_culture-seg2")
    check("回滚级联关闭 seg2（policy_rollback）",
          seg2_state["status"] == "closed"
          and seg2_state["closed"]["reason"] == "policy_rollback")
    kunqu = next(c for c in app.creator_view(CREATOR_A, "kunqu_master")
                 if c["content_id"] == "c_kunqu_3h")
    exit_events = [t for t in kunqu["timeline"] if t["action"] == "exited"]
    check("策略纳入通道的内容带原因级联退出（policy_rolled_back）",
          bool(exit_events) and exit_events[-1]["reason"] == "policy_rolled_back",
          json.dumps(exit_events[-1] if exit_events else {}, ensure_ascii=False)[:200])
    after = app.rank(EDITOR, subject_ref=subjects[6], content_ids=CONTENT_IDS,
                     user_attrs={}, experiment_id="exp_culture", ts=D2 + HOUR)
    from caliber import BASELINE_WEIGHTS
    check("回滚后无在跑分段，分流回落基线权重",
          after["weights_used"] == BASELINE_WEIGHTS
          and after["reason"] in ("no_live_segment", "policy_not_active"),
          after["reason"])

    # ============ D8+：迟到补传撞封板 ============
    section("D8+｜迟到补传：D0 分区已封板，日志保留但指标不改写")
    # D0 分区封板时刻 = D1 00:00 + 7天 + 1小时宽限 = D8+1h。此刻 D8+2h 已封板，
    # 但事件本身仍在"可信迟到"界内（发生至今 < 8天+1小时），故入账留痕、指标排除。
    D8LATE = ANCHOR_DAY + 8 * DAY + 2 * HOUR
    at(D8LATE)
    late_expo = app.ledger.exposures[derive_exposure_id("ex-d0-" + subjects[7])]
    late_cid = late_expo["payload"]["items"][0]["content_id"]
    late = app.receive_feedback(EDITOR, event_id="fb-late-day0", subject_ref=subjects[7],
                                ref_exposure_id=derive_exposure_id("ex-d0-" + subjects[7]),
                                content_id=late_cid, kind="favorite",
                                happened_at=ANCHOR_DAY + 10 * HOUR)
    check("封板后到达的迟到反馈仍入账留痕", late["accepted"] is True)
    # 超过可信迟到界（>8天+1小时）的补传直接隔离
    D10 = ANCHOR_DAY + 10 * DAY
    at(D10)
    absurd_expo = app.ledger.exposures[derive_exposure_id("ex-d0-" + subjects[8])]
    absurd = app.receive_feedback(EDITOR, event_id="fb-absurd-late",
                                  subject_ref=subjects[8],
                                  ref_exposure_id=derive_exposure_id("ex-d0-" + subjects[8]),
                                  content_id=absurd_expo["payload"]["items"][0]["content_id"],
                                  kind="favorite",
                                  happened_at=ANCHOR_DAY + 10 * HOUR)
    check("超过可信迟到界的补传被隔离（implausibly_late）",
          absurd["accepted"] is False and absurd["reason"] == "implausibly_late",
          absurd["reason"])

    # ============ 复现 ============
    section("复现｜D0/D1 每次分流采用的策略可逐日重放")
    rep0 = app.reproduce_day("exp_culture", day_key(D0))
    check("D0 全部决策可复现（分桶/排序/目录哈希/权重一致）",
          rep0["all_reproducible"], f"{rep0['count']} 条决策")
    rep1 = app.reproduce_day("exp_culture", day_key(D1))
    check("D1 全部决策可复现", rep1["all_reproducible"], f"{rep1['count']} 条决策")
    sample = rep0["decisions"][0]
    print(f"  样例：seq={sample['decision_seq']} {iso(sample['ts'])} "
          f"strategy={sample['strategy']} variant={sample['variant']} "
          f"policy={sample['policy_id']} v={sample.get('policy_version')}")
    v0 = {(d["policy_version"], d["strategy"]) for d in rep0["decisions"]
          if d["strategy"] == "treatment"}
    v1 = {(d["policy_version"], d["strategy"]) for d in rep1["decisions"]
          if d["strategy"] == "treatment"}
    check("D0 treatment 全挂 v1、D1 treatment 全挂 v2（分段不并表）",
          v0 == {(1, "treatment")} and v1 == {(2, "treatment")}, f"D0={v0} D1={v1}")
    check("哈希链完整", app.verify_journal()["intact"])

    with tempfile.NamedTemporaryFile("w+", suffix=".journal", delete=False,
                                     encoding="utf-8") as tmp:
        with open(JOURNAL, encoding="utf-8") as src:
            lines = src.readlines()
        # 找到策略草拟事件并篡改其权重
        idx = next(i for i, ln in enumerate(lines)
                   if json.loads(ln)["kind"] == "policy_drafted")
        evil = json.loads(lines[idx])
        evil["payload"]["spec"]["weights"]["diversity"] = 0.99
        lines[idx] = json.dumps(evil, ensure_ascii=False, sort_keys=True) + "\n"
        tmp.writelines(lines)
        tmp_path = tmp.name
    try:
        try:
            Application(tmp_path, pepper="trial-fixed-pepper")
            check("篡改任一日志行会被哈希链识破", False)
        except DomainError as exc:
            check("篡改任一日志行会被哈希链识破", exc.code == "journal_corrupt", str(exc)[:80])
    finally:
        os.remove(tmp_path)

    # ============ 指标 ============
    section("指标｜caliber-v1 冻结口径：短期互动 vs 7 日回访，分段独立比较")
    report = app.metrics_report()
    exc_counts = report["excluded"]
    print(f"  排除审计：{json.dumps(exc_counts, ensure_ascii=False)}")
    check("撤回曝光被排除", exc_counts.get("exposure_withdrawn", 0) >= 1)
    check("被撤回曝光下的反馈级联排除",
          exc_counts.get("feedback_excluded_by_exposure_withdrawal", 0) >= 1)
    check("单条撤回反馈被排除", exc_counts.get("feedback_withdrawn", 0) >= 1)
    check("封板后迟到反馈被排除", exc_counts.get("feedback_arrived_after_freeze", 0) >= 1)
    check("超窗反馈被排除", exc_counts.get("feedback_outside_window", 0) >= 1)
    check("无法归因反馈被排除", exc_counts.get("feedback_unattributable", 0) >= 1)
    quarantined = {q["reason"] for q in app.quarantine_list()}
    check("入库层隔离原因可追溯",
          {"duplicate", "future_event", "subject_mismatch"} <= quarantined,
          str(sorted(quarantined)))
    raw_dump = json.dumps(app.quarantine_list(), ensure_ascii=False) + json.dumps(
        report, ensure_ascii=False)
    check("隔离清单与指标输出均不含原始主体标识", "trial-user-" not in raw_dump)

    comps = app.compare_arms()
    print("\n  长内容双臂对比：")
    for r in [r for r in comps if r["length_class"] == "long"]:
        s, rv = r["short_engagement_rate"], r["revisit_rate_7d"]
        print(f"    {r['segment_id']} day={r['day']} "
              f"短期互动 t={s['treatment']} b={s['baseline']} Δ={s['delta']} | "
              f"7日回访 t={rv['treatment']} b={rv['baseline']} Δ={rv['delta']} "
              f"({r['note'] or 'ok'})")
    seg1_long = [r for r in comps if r["segment_id"] == "exp_culture-seg1"
                 and r["day"] == day_key(D0) and r["length_class"] == "long"]
    if seg1_long and seg1_long[0]["note"] is None:
        r = seg1_long[0]
        check("seg1 长内容 treatment 7 日回访高于对照（同一冻结口径）",
              r["revisit_rate_7d"]["delta"] is not None
              and r["revisit_rate_7d"]["delta"] > 0,
              f"Δ={r['revisit_rate_7d']['delta']}")
        check("短期与长期指标均以 caliber-v1 同一口径出数", r["caliber"] == "caliber-v1")
    else:
        check("seg1 长内容双臂格均达到 k 匿名且可比较", False,
              str(seg1_long[0]["note"] if seg1_long else "无长内容格"))
    n_supp = sum(1 for c in report["cells"] if c["suppressed"])
    print(f"\n  k 匿名（k={K_ANONYMITY}）抑制格：{n_supp}/{len(report['cells'])}")
    check("小样本格只输出抑制标记、不输出任何比率",
          all("finish_rate" not in c for c in report["cells"] if c["suppressed"]))

    # ============ 重启复现 ============
    section("持久化｜新进程重放事件日志后结论一致")
    app2 = Application(JOURNAL, pepper="trial-fixed-pepper")
    rep0b = app2.reproduce_day("exp_culture", day_key(D0))
    report2 = app2.metrics_report()
    check("重建后 D0 复现一致",
          rep0b["all_reproducible"] and rep0b["count"] == rep0["count"])
    check("重建后指标格数与排除审计一致",
          report2["excluded"] == report["excluded"]
          and len(report2["cells"]) == len(report["cells"]))
    check("重建后哈希链完好", app2.verify_journal()["intact"])
    check("重建后幂等集合恢复（重复事件仍被识别）",
          "fb-dup-demo" in app2.ledger.event_ids)

    # ============ 创作者透明 ============
    section("透明｜创作者可查内容进出独立通道的完整原因链")
    entry = next(c for c in app.creator_view(CREATOR_A, "kunqu_master")
                 if c["content_id"] == "c_kunqu_3h")
    reasons = [(t["action"], t["reason"]) for t in entry["timeline"]]
    check("可查进入（experiment_policy）与回滚退出（policy_rolled_back）",
          ("admitted", "experiment_policy") in reasons
          and ("exited", "policy_rolled_back") in reasons, str(reasons))
    check("创作者视图不含任何观看主体信息",
          "subject" not in json.dumps(entry, ensure_ascii=False))

    section("试运行汇总")
    total = len(results)
    passed = total - check.failed
    print(f"  {passed}/{total} 项检查通过")
    if check.failed:
        for name, ok, detail in results:
            if not ok:
                print(f"    ✗ {name} {detail}")
        return 1
    print("  全部通过：长短内容、调权分段、紧急回滚、事件乱序下治理闭环成立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
