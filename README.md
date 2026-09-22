# 文化推荐策略治理（culture-policy-audit）

在**不训练新模型**的前提下管住推荐策略：策略必须带目标/权重/人群/有效期，经内容与风险
双负责人批准后才能小流量（≤10%）分发；实验中途调权必须另开分段；去标识化反馈即使
迟到、撤回、重复、乱序也不污染指标；任何一天、任何一次分流采用了哪一版策略都可以复现；
用户关闭画像或重置兴趣后历史偏好立即失效；任何角色都无法从聚合输出反推个人观看记录。

## 快速开始

```bash
python3 service.py --check     # 基础自检
npm test                       # 32 个单元/集成测试 + 61 项端到端试运行检查
python3 trial.py               # 单独跑端到端试运行（约 8 秒）
python3 service.py --port 8000 # 启动 HTTP 服务（默认内存库，重启即空）
CULTURE_POLICY_DB=data/prod.journal python3 service.py --port 8000  # 持久化
```

试运行产物在 `trial_out/trial.journal`：删掉后重新运行 `python3 trial.py` 即可再生；
用新进程打开同一文件，复现结论与指标完全一致（试运行最后一段会自证）。

## 治理规则如何落地

| 诉求 | 机制 | 位置 |
| --- | --- | --- |
| 产品提交目标/权重/人群/有效期，双负责人批准后才小流量 | 状态机 `draft→pending→approved→active`，两角色必须不同自然人；权重和为 1、有效期 ≤90 天、流量 1..10% | `policy.py`, `app.py` |
| 说清策略变化、不伪装连续实验 | 调权只能 `revise` 出新版本并重新走完整审批；分段关闭→重开，独立种子、独立时间窗、独立指标格 | `experiment.py` |
| 迟到/撤回/重复/乱序不污染指标 | 入库层隔离（重复、未来、跨主体、非法字段）+ 日分区封板（曝光日结束后 7 天+1 小时）+ 撤回级联；指标是不可变事件的纯函数 | `feedback.py`, `metrics.py` |
| 复现某日每次分流 | `rank_decided` 固化策略版本、权重、分桶、目录哈希、排序；`/experiments/{id}/replay?day=N` 用冻结输入重算并比对 | `experiment.py`, `app.py` |
| 不偷换口径比较短期互动与长期回访 | 口径冻结为 `caliber-v1`：短期=24h、回访=24h..7d、完播按长短内容分档阈值、多样性归一化香农熵；分段内 treatment/baseline 对齐比较 | `caliber.py`, `metrics.py` |
| 创作者知道为何进出独立通道 | 进出都强制原因码与来源（策略/版本/实验/分段）；紧急回滚级联退出并留痕；创作者视图不含任何观看主体 | `channel.py` |
| 关闭画像/重置兴趣后不被历史偏好影响 | 画像开关与重置墓碑均为追加事件；关闭期不落亲和、重置点前的亲和即使迟到到达也丢弃；决策 `personalized=false`、加成为 0 | `privacy.py` |
| 不可反推个人观看记录 | 边界做 HMAC 去标识化（库内无原始 ID）；聚合 k=5 匿名，小样本格只给抑制标记；隔离事件只存摘要前缀 | `common.py`, `metrics.py` |
| 全程可审计、防篡改 | 所有状态变更追加到哈希链日志（`prev_hash`+载荷哈希），任一行被改在重放时即报 `journal_corrupt` | `journal.py` |

## 架构

```
service.py     HTTP 入口（角色鉴权在请求体 actor，联调用；JSON 错误码）
app.py         应用门面：命令编排、角色门禁、回滚/结束级联、按日复现
policy.py      策略版本状态机与规格校验（目标/权重/人群/有效期/流量）
experiment.py  实验、不可变分段、确定性分桶、打分排序、决策结构
feedback.py    去标识化反馈台账：幂等、隔离判定、撤回索引
metrics.py     封板/窗口/归因/多样性/k 匿名，双臂对比
channel.py     独立通道进出登记与创作者视图
privacy.py     画像开关、兴趣重置墓碑、亲和生效规则
caliber.py     caliber-v1 冻结口径常量
journal.py     追加式哈希链事件日志（内存或每行一个 JSON 的文件）
common.py      错误类型、时钟注入、HMAC 摘要、确定性分桶原语
trial.py       端到端试运行（长短内容、调权分段、紧急回滚、事件乱序）
```

事件溯源：聚合根不存可改状态，全部由日志重放构建；进程重启后行为一致，
幂等集合也从日志恢复（重启后重复事件仍被识别）。

## HTTP 接口（均为 JSON）

写接口在请求体带 `"actor": {"id": "...", "role": "..."}`。

- `POST /content/register` 登记内容（信号特征与时长）
- `POST /policies/draft` → `/policies/{id}/submit` → `/approve`（分别以
  `content_owner`、`risk_owner` 各调一次）/`/reject`；`/policies/{id}/revise` 派生新版本；
  `/rollback`（强制 reason，级联关段退通道）；`/end`
- `POST /experiments`、`POST /experiments/{id}/segments`、`.../segments/close`
- `POST /rank` 分流（返回 `decision_seq`、变体、采用权重与排序）
- `POST /exposures`、`POST /feedback`、`POST /feedback/withdraw`
- `POST /privacy/profile`（开关画像）、`/privacy/reset`（重置兴趣）、`/privacy/affinity`
- `POST /channels/admit`、`/channels/exit`
- `GET /policies`、`/policies/{id}[:vN]`、`/experiments/{id}`、
  `/experiments/{id}/replay?day=N`、`/metrics`、`/metrics/compare`、
  `/quarantine`、`/creators/{id}/channels`、`/journal/verify`、`/health`

错误使用稳定错误码（`bad_weights`、`segment_live`、`duplicate`、`future_event`、
`subject_mismatch`、`journal_corrupt` 等），HTTP 状态 403/404/409/422。

## 试运行演什么

`trial.py` 用固定胡椒和可控时钟演四天：D0 八条长短内容（3 小时昆曲全本、2 小时非遗
慢直播、60 秒短视频……）在多信号策略下分流 240 个去标识化主体；注入重复、撤回、
乱序（反馈先到曝光后补）、未来、跨主体、超窗、封板后补传共七类污染事件；D1 调权被
拦后走"关段→升 v2→重新双批准→seg2"；D2 风险负责人一键回滚，seg2 关闭、通道内容
带原因级联退出；随后逐日重放决策、篡改日志被哈希链识破，并以同一 caliber-v1 口径
输出 seg1/seg2 各自的短期互动与 7 日回访双臂对比。

## 边界说明（当前版本有意不做）

- 鉴权角色以请求体声明，仅用于联调；接入真实身份后应在网关替换为签名身份。
- 人群匹配是结构化属性的精确匹配（`attrs` 全等），不含标签平台/DMP 集成。
- 去标识化为服务端胡椒的 HMAC；运维仍可通过日志访问摘要，真正的"任何人不可反推"
  还需要密钥分权与留存期策略配合（聚合 k 匿名已在应用层保证）。
