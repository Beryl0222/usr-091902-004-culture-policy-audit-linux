"""caliber-v1：冻结的指标与实验口径。

口径一旦在试运行中使用即冻结：任何修改都必须升版本号（caliber-v2），
旧分段仍按创建时记录的口径版本解释，保证"不偷换口径"。

本模块常量是评审过的制度值，不接受按请求传入覆盖。
"""

CALIBER_VERSION = "caliber-v1"

# 对照组策略：改版前的平台默认——完播率单一指标。
BASELINE_WEIGHTS = {"finish_rate": 1.0}
BASELINE_POLICY_ID = "baseline_finish_only"

# 指标窗口（秒）。
SHORT_WINDOW_SECONDS = 24 * 3600   # 短期互动：曝光后 24 小时
LONG_WINDOW_SECONDS = 7 * 86400    # 长期回访：曝光后 7 天

# 反馈迟到宽限：超过窗口+宽限才到达的反馈一律拒绝入账（绝不改历史指标）。
LATE_GRACE_SECONDS = 60 * 60

# 完播口径按内容时长分档（避免 3 小时长内容天然吃亏）。
FINISH_THRESHOLDS = {
    "short": 0.80,  # 短内容（<15 分钟）：观看 >=80% 记完播
    "long": 0.50,   # 长内容（>=15 分钟）：观看 >=50% 记完播
}
LONG_CONTENT_MIN_SECONDS = 15 * 60

# 多样性：曝光目录的归一化香农熵（1.0 = 各类别完全均匀）。
DIVERSITY_EPSILON = 1e-12

# 隐私：任何分组维度小于该样本数的聚合输出一律抑制（防反推个人）。
K_ANONYMITY = 5

# 去标识化：摘要在输出中截断的前缀长度（完整摘要仅存日志链，不外发）。
DIGEST_PREFIX_LEN = 12

# 每日固化时间（UTC）：指标按天分区，迟到反馈只影响"反馈到达日"之后
# 尚未封板的分区；已封板分区永不重写。
DAY_BOARD_FREEZE_AFTER_SECONDS = LONG_WINDOW_SECONDS + LATE_GRACE_SECONDS


def length_class(duration_seconds):
    if not isinstance(duration_seconds, (int, float)) or duration_seconds <= 0:
        from common import DomainError
        raise DomainError("bad_content", "内容时长必须为正数")
    return "long" if duration_seconds >= LONG_CONTENT_MIN_SECONDS else "short"


def day_key(ts):
    """UTC 自然日分区键。"""
    return ts // 86400
