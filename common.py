"""跨模块共享的基础工具：错误类型、时间口径、哈希与标识。

设计原则：
- 所有时间一律使用 Unix 秒整数，外部 ISO 字符串仅在边界转换；
- 哈希使用 HMAC-SHA256（带服务端胡椒），避免对去标识化标识做裸哈希被字典反推；
- 同一原始标识在不同命名空间（分流桶 / 反馈主体）派生不同摘要，降低关联风险。
"""

import hashlib
import hmac
import time
from datetime import datetime, timezone

# 默认服务端胡椒。生产环境应由环境变量覆盖；测试与试运行使用固定值以保证可复现。
DEFAULT_PEPPER = "culture-policy-audit-dev-pepper"

# 支持的信号名（目标侧可引用），顺序即指标输出的稳定顺序。
SIGNALS = (
    "finish_rate",       # 完播率
    "favorite",          # 收藏
    "revisit",           # 回访
    "discussion_quality",  # 讨论质量
    "diversity",         # 多样性
)

# 内容时长分档（秒）：口径冻结于 caliber-v1。
LONG_CONTENT_MIN_SECONDS = 15 * 60  # >=15 分钟记为长内容


class DomainError(Exception):
    """业务规则冲突。HTTP 层统一映射为 409/422，不产生 500。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class AuthError(Exception):
    """角色或权限不满足。"""


def now_ts():
    """当前 Unix 秒。允许测试通过 set_clock 注入固定时钟。"""
    return CLOCK[0]()


def to_ts(value):
    """把外部时间（Unix 秒或 ISO 8601 字符串）归一为 Unix 秒整数。"""
    if isinstance(value, bool):  # bool 是 int 子类，显式拒绝
        raise DomainError("bad_time", "时间不支持布尔值")
    if isinstance(value, (int, float)):
        if value <= 0:
            raise DomainError("bad_time", "时间必须为正的 Unix 秒")
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise DomainError("bad_time", f"无法解析时间: {value}") from exc
        if dt.tzinfo is None:
            raise DomainError("bad_time", "时间必须带时区")
        return int(dt.astimezone(timezone.utc).timestamp())
    raise DomainError("bad_time", f"不支持的时间类型: {type(value).__name__}")


def iso(ts):
    """Unix 秒转 UTC ISO 字符串，仅用于展示。"""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def digest(namespace, raw, pepper=DEFAULT_PEPPER):
    """对原始标识做键控哈希，返回十六进制摘要。

    namespace 隔离用途，raw 只允许字符串/整数。摘要不可逆，但对相同输入稳定，
    因此仍属于伪标识：系统不持久化原始标识，聚合输出另有 k 匿名抑制。
    """
    if not isinstance(raw, (str, int)):
        raise DomainError("bad_identity", "标识只允许字符串或整数")
    msg = f"{namespace}|{raw}".encode("utf-8")
    return hmac.new(pepper.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def stable_bucket(seed, subject_digest, buckets):
    """[0, buckets) 的确定性分桶。同一分段种子+主体永远落入同一桶。"""
    h = hmac.new(seed.encode("utf-8"), subject_digest.encode("utf-8"), hashlib.sha256).digest()
    return int.from_bytes(h[:8], "big") % buckets


# ---- 可注入时钟（试运行需要制造迟到/乱序/未来事件） ----

CLOCK = [time.time]


def set_clock(func=None, fixed_ts=None):
    if fixed_ts is not None:
        CLOCK[0] = lambda: fixed_ts
    elif func is not None:
        CLOCK[0] = func
    else:
        CLOCK[0] = time.time
