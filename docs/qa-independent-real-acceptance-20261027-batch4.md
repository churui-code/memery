# 独立真实验收：批次四（2026-10-27）

## 结论

本批未通过。前两轮正常保存，第3轮跨会话召回在intake阶段返回HTTP502 `memory_decision_invalid`，没有生成回答。精确原因是 `unsupported event time`：模型把历史日期“10月17日”填入本轮experience.time_quote，但当前用户只说“上次”。严格来源校验正确拒绝，当前流程未对这类可定位错误受控重提。

真实请求累计30次，本批5次。第4—10轮停止，8767临时容器已停止，实现和数据库测试槽交回。QA未修改应用实现；旧失败及账本均保留。

## 本地验证

- pytest完整回归：528 passed、5 subtests passed、1 warning，94.49s。
- Node UI：7 passed。
- 日志：`local-tests-round6.txt`、`ui-tests-round6.txt`。
- 全新实验：`cf0cc9f9-4038-4d68-9e62-a75596f8c5b5`。
- MiniMax-M3，thinking disabled，reflection off，timeout90、deadline240、context24000，每轮最多3请求。

## 最小复现

当前用户原文：

> 上次我和沈鹿去梧桐街钟表铺，我们各花多少钱做什么？她递木盒时的完整原话是什么？

实际provider请求：`c9b45d91-a7cc-43be-832c-a598a62ef59a`。

实际提案的episode.time_quote为“上次”，但experience.time_quote为“10月17日”。后者存在于历史证据，不存在于本轮用户原文。云schema校验通过，随后 `_validate_development` 抛 `ValueError('unsupported event time')`，诊断为 `unsupported_event_time`，路径 `experience/time_quote`。

完整原提案离线重放稳定失败；仅在离线副本中将experience.time_quote替换为当前原文已有的“上次”，完整校验通过。离线操作无云调用、无数据库写入。不可把此定位操作直接变成线上静默修正，也不应放宽为任意历史日期都可作为本轮time_quote。

本轮只有1次provider请求，无重提、无answer。此前添加的episode父子范围重提不覆盖experience的时间引用错误。建议把本类明确定位、可由模型修正的来源错误纳入原预算内受控重提，同时保持严格校验。

## 数据与审计

- DB审计：`c8ea5f40-782f-470a-838b-74da0459c579`，intake，memory_decision_invalid，input_tokens=6604、output_tokens=906。
- 失败前后events、originals、self_state、self_history、counts完全一致。
- 最终experiences=2、episodes=2、memories=0、episode_corrections=0。
- 前两轮：93元修怀表、37元皮表带及完整赠言正常记录；单次热闹写作请求未形成长期偏好。本次写作也确实为两句。

## 证据与剩余范围

证据目录 `data/qa/independent-acceptance-20261027/`：

- `batch4.json`：完整实际对话、快照及冻结版本指纹
- `provider/c9b45d91-a7cc-43be-832c-a598a62ef59a.json`：完整实际payload、schema及响应
- `failure-round4-replay.json`：原始失败、离线单字段修正通过、前后快照比较及DB审计
- `requests.json`：累计30次，总上限null

首次纠错成功提交、纠错后跨会话排旧、保留其他细节、长期偏好实际保存、重复纠错及相似独立事件不误改仍未取得完整真实通过证据。前三批已验证的诚实降级范围不受本次结论影响。

## Full transcript (unaltered)

### Turn 1

User:

10月17日我和沈鹿去了梧桐街钟表铺。我花93元修怀表，沈鹿花37元买了皮表带。临走时她把装怀表的木盒递给我，说：“不用追着每一分钟跑，歇一歇也来得及。”我最近总担心赶不上进度，听完心里轻松了一些。

API response:

我记住了：10月17日，你和沈鹿去了梧桐街的钟表铺。你花93元修怀表，沈鹿花37元买了皮表带。临走时，她把装怀表的木盒递给你，说：“不用追着每一分钟跑，歇一歇也来得及。”这句话让你最近因赶进度而产生的担心缓解了一些，心里轻松了些。

Provider IDs: 8717977c-bd2a-419b-a7a7-534d786ed776, 42e7f558-7272-4673-a3d5-2a584ab24db6

### Turn 2

User:

只针对这一次展览邀请，请写两句热闹的开场，多用感叹号。

API response:

各位朋友，展览终于来啦！快来一起开启这场精彩绝伦的视觉盛宴吧！

Provider IDs: 952cc5a2-e254-4549-bd31-09749bf52ced, 4672fa7b-a4f8-43fa-8181-4c96996eb416

### Turn 3

User:

上次我和沈鹿去梧桐街钟表铺，我们各花多少钱做什么？她递木盒时的完整原话是什么？

API response:

{"error": "memory_decision_invalid", "message": "记忆判断未通过来源或格式验证，本轮未写入；请重试。"}

Provider IDs: c9b45d91-a7cc-43be-832c-a598a62ef59a

Frozen app fingerprints and both protected ledgers verified unchanged.
