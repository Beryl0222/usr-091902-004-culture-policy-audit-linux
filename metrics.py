"""指标计算：按冻结口径 caliber-v1 从不可变事件重放。

核心保证：
- 归因不可变后确定：任何指标都是完整日志的纯函数，重放结果与当日首次计算一致；
- 日分区封板：曝光日 D 的分区在 D 日结束后 LONG_WINDOW+GRACE 封板，
  封板后到达的任何反馈（迟到/补传/撤回）一律不进指标；
- 撤回级联：撤回某次曝光时，其全部反馈一并排除；撤回单条反馈只排除该条；
- 窗口不滑动：短期=曝光后 24h，长期回访=24h..7d，口径随分段固化；
- 分段不并表：调权前后的分段独立出数，比较时逐段对照，不伪装成一次连续实验；
- k 匿名：任一输出格的去标识化主体数 < K 时整格抑制，防止反推个人记录。
"""

import math
from collections import Counter, defaultdict

from caliber import (CALIBER_VERSION, DAY_BOARD_FREEZE_AFTER_SECONDS,
                     FINISH_THRESHOLDS,
                     K_ANONYMITY, LONG_WINDOW_SECONDS, SHORT_WINDOW_SECONDS,
                     day_key, length_class)

REVISIT_MIN_GAP = SHORT_WINDOW_SECONDS  # 24h 内的再来不算回访，算短期互动


def freeze_at_for_day(day):
    """曝光日分区的封板时刻：该日结束后再过完整观察窗口+宽限。"""
    day_end = (day + 1) * 86400
    return day_end + DAY_BOARD_FREEZE_AFTER_SECONDS


def _safe_ratio(numerator, denominator):
    return round(numerator / denominator, 6) if denominator else None


def _shannon_entropy(categories):
    counts = Counter(categories)
    total = sum(counts.values())
    if total == 0:
        return None
    entropy = -sum((c / total) * math.log(c / total) for c in counts.values())
    k = len(counts)
    if k <= 1:
        return 0.0
    return round(entropy / math.log(k), 6)


class MetricsEngine:
    def __init__(self, ledger, experiments):
        self.ledger = ledger
        self.experiments = experiments  # experiment_id -> Experiment

    def _eligible_impressions(self):
        """构建通过结构与时间双重校验的曝光印象与反馈索引。"""
        impressions = []          # dict per item impression
        feedback_by_exposure = defaultdict(list)
        withdrawn_event_ids = set(self.ledger.withdrawn.keys())

        for event in self.ledger.feedbacks:
            p = event["payload"]
            if p["event_id"] in withdrawn_event_ids:
                continue
            feedback_by_exposure[p["ref_exposure_id"]].append(event)

        for exposure_id, event in self.ledger.exposures.items():
            p = event["payload"]
            if p["event_id"] in withdrawn_event_ids:
                continue  # 曝光被撤回：整组印象与反馈级联排除
            day = day_key(event["ts"])
            if event["recorded_at"] > freeze_at_for_day(day):
                continue  # 曝光本身迟到到封板之后：不可补记
            decision = self.ledger.decisions.get(p["decision_seq"])
            if decision is None:
                continue
            deadline = freeze_at_for_day(day)
            good_feedback = []
            for fb in feedback_by_exposure.get(exposure_id, []):
                fbp = fb["payload"]
                if fb["recorded_at"] > deadline:
                    continue  # 封板后到达：留日志，不进指标
                gap = fb["ts"] - event["ts"]
                if gap < 0 or gap > LONG_WINDOW_SECONDS:
                    continue  # 超出 7 天观察窗（乱序早到同样排除）
                good_feedback.append((fb, gap))
            for item in p["items"]:
                impressions.append({
                    "subject_digest": p["subject_digest"],
                    "experiment_id": p["experiment_id"],
                    "decision_seq": p["decision_seq"],
                    "exposure_id": exposure_id,
                    "exposure_event_id": p["event_id"],
                    "content_id": str(item["content_id"]),
                    "category": item["category"],
                    "length_class": length_class(item["duration_seconds"]),
                    "day": day,
                    "exposure_ts": event["ts"],
                    "feedback": [(fb, gap) for fb, gap in good_feedback
                                 if str(fb["payload"].get("content_id")) == str(item["content_id"])],
                })
        return impressions

    def _cell_key(self, decision, impression):
        dp = decision["payload"]
        return {
            "experiment_id": impression["experiment_id"],
            "segment_id": dp.get("segment_id"),
            "variant": dp.get("variant", "off"),
            "strategy": dp.get("strategy", "none"),
            "policy_id": dp.get("policy_id"),
            "policy_version": dp.get("policy_version"),
            "caliber": dp.get("caliber", CALIBER_VERSION),
            "day": impression["day"],
            "length_class": impression["length_class"],
        }

    def _exclusion_audit(self):
        """统计每类合法但不进指标的事件数量（重复/非法事件在入库层隔离，另计）。"""
        counts = Counter()
        withdrawn = set(self.ledger.withdrawn.keys())
        exposures = self.ledger.exposures
        for eid, event in exposures.items():
            p = event["payload"]
            day = day_key(event["ts"])
            if p["event_id"] in withdrawn:
                counts["exposure_withdrawn"] += 1
            elif event["recorded_at"] > freeze_at_for_day(day):
                counts["exposure_arrived_after_freeze"] += 1
        for event in self.ledger.feedbacks:
            p = event["payload"]
            if p["event_id"] in withdrawn:
                counts["feedback_withdrawn"] += 1
                continue
            exposure = exposures.get(p["ref_exposure_id"])
            if exposure is None:
                counts["feedback_unattributable"] += 1
                continue
            ep = exposure["payload"]
            day = day_key(exposure["ts"])
            if ep["event_id"] in withdrawn:
                counts["feedback_excluded_by_exposure_withdrawal"] += 1
            elif event["recorded_at"] > freeze_at_for_day(day):
                counts["feedback_arrived_after_freeze"] += 1
            else:
                gap = event["ts"] - exposure["ts"]
                if gap < 0:
                    counts["feedback_before_exposure"] += 1
                elif gap > LONG_WINDOW_SECONDS:
                    counts["feedback_outside_window"] += 1
        return dict(counts)

    def compute(self):
        """全量重放计算。返回格指标列表（格=实验×分段×变体×日×时长档）。"""
        impressions = self._eligible_impressions()
        excluded = self._exclusion_audit()
        cells = defaultdict(lambda: {
            "subjects": set(), "exposures": 0, "finished": 0, "favorites": 0,
            "discussions": [], "short_engaged": set(), "revisited": set(),
            "categories": [],
        })
        cell_meta = {}

        for imp in impressions:
            decision = self.ledger.decisions.get(imp["decision_seq"])
            if decision is None:
                continue
            meta = self._cell_key(decision, imp)
            key = tuple(sorted(meta.items()))
            cell_meta[key] = meta
            cell = cells[key]
            cell["subjects"].add(imp["subject_digest"])
            cell["exposures"] += 1
            cell["categories"].append(imp["category"])

            threshold = FINISH_THRESHOLDS[imp["length_class"]]
            for fb, gap in imp["feedback"]:
                kind = fb["payload"]["kind"]
                if kind == "playback" and gap <= SHORT_WINDOW_SECONDS:
                    cell["short_engaged"].add(imp["subject_digest"])
                    if fb["payload"].get("progress", 0) >= threshold:
                        cell["finished"] += 1
                elif kind == "finish" and gap <= SHORT_WINDOW_SECONDS:
                    cell["short_engaged"].add(imp["subject_digest"])
                    cell["finished"] += 1
                elif kind == "favorite" and gap <= SHORT_WINDOW_SECONDS:
                    cell["short_engaged"].add(imp["subject_digest"])
                    cell["favorites"] += 1
                elif kind == "discussion" and gap <= SHORT_WINDOW_SECONDS:
                    cell["short_engaged"].add(imp["subject_digest"])
                    cell["discussions"].append(fb["payload"]["quality"])
                elif kind == "revisit" and REVISIT_MIN_GAP <= gap <= LONG_WINDOW_SECONDS:
                    cell["revisited"].add(imp["subject_digest"])

        out = []
        for key in sorted(cell_meta.keys()):
            cell = cells[key]
            n_subjects = len(cell["subjects"])
            entry = {
                **cell,
                "subjects_exposed": n_subjects,
                "suppressed": n_subjects < K_ANONYMITY,
                "diversity": _shannon_entropy(cell["categories"]),
            }
            if n_subjects >= K_ANONYMITY:
                entry.update({
                    "finish_rate": _safe_ratio(cell["finished"], cell["exposures"]),
                    "favorite_rate": _safe_ratio(cell["favorites"], cell["exposures"]),
                    "discussion_quality": (
                        round(sum(cell["discussions"]) / len(cell["discussions"]), 6)
                        if cell["discussions"] else None
                    ),
                    "short_engagement_rate": _safe_ratio(
                        len(cell["short_engaged"]), n_subjects),
                    "revisit_rate_7d": _safe_ratio(len(cell["revisited"]), n_subjects),
                })
            entry.pop("subjects", None)
            entry.pop("short_engaged", None)
            entry.pop("revisited", None)
            entry.pop("categories", None)
            entry.pop("discussions", None)
            # 元数据置后，便于阅读
            meta = dict(key)
            entry["key"] = meta
            out.append(entry)
        return {"caliber": CALIBER_VERSION, "k_anonymity": K_ANONYMITY,
                "excluded": excluded, "cells": out}

    def compare_segment_arms(self, report=None):
        """同分段内 treatment vs baseline 对齐比较（短期与长期同口径）。

        绝不跨分段合并：每个分段一行；未达 k 匿名或缺失对照臂的格给出标记而非数字。
        """
        report = report or self.compute()
        by_seg_day = defaultdict(dict)
        for cell in report["cells"]:
            k = cell["key"]
            if k["segment_id"] is None:
                continue
            by_seg_day[(k["segment_id"], k["day"], k["length_class"])][k["variant"]] = cell

        comparisons = []
        for (segment_id, day, length_class), arms in sorted(by_seg_day.items()):
            treat = arms.get("treatment")
            base = arms.get("baseline")
            row = {
                "caliber": CALIBER_VERSION,
                "segment_id": segment_id,
                "day": day,
                "length_class": length_class,
                "treatment_policy_id": (treat or {}).get("key", {}).get("policy_id"),
                "treatment_policy_version": (treat or {}).get("key", {}).get("policy_version"),
            }
            for metric in ("finish_rate", "favorite_rate", "discussion_quality",
                           "short_engagement_rate", "revisit_rate_7d", "diversity"):
                tv = treat.get(metric) if treat and not treat["suppressed"] else None
                bv = base.get(metric) if base and not base["suppressed"] else None
                row[metric] = {"treatment": tv, "baseline": bv,
                               "delta": round(tv - bv, 6) if tv is not None and bv is not None else None}
            row["note"] = None
            if treat is None or base is None:
                row["note"] = "missing_arm"
            elif treat["suppressed"] or base["suppressed"]:
                row["note"] = "suppressed_k_anonymity"
            comparisons.append(row)
        return comparisons
