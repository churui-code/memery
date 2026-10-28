# 独立真实验收：批次七（2026-10-27）

## 结论

新阻断：第3轮普通跨会话提问被模型错误表示为对旧事件的追加，且真实性类型不一致，最终HTTP502 `memory_decision_invalid`。精确cause为 `episode_target_context_type_mismatch`，未重提、未生成回答、业务数据未变化。

本批6次provider请求，总累计57次。完整pytest：538 passed、5 subtests passed、1 warning（97.37s）；UI 7 passed。第4—10轮暂停，8767已停止，实现和数据库测试槽交回。未修改app。

## 实际失败与重放

用户原文：

> 上次我和夏棠去杉木街裁缝铺，我们各花多少钱做什么？她递布包时的完整原话是什么？

provider：`28b344c3-1edb-448c-8622-883c3fc48165`。

- 实际证据中的旧事件 `f4804415-3d60-474d-8ae9-19330024d9be` 类型为reported。
- 模型提案 `episodes[0].context_type=conversation`，却把target_id设为该reported旧事件。
- 本轮没有新的旧事件细节，只是在询问花费与原话。提示要求这种提问新建本轮讨论事件，不应追加旧事件。
- 原始完整proposal通过云schema，随后严格本地校验拒绝跨类型合并。
- 受控诊断：`episode_target_context_type_mismatch`，路径 `episodes/target_id`。
- 离线只将该target_id设为null后，完整validate_decision通过。此为定位操作；线上没有自动删target或修改类型。

本例不应通过强制把conversation改成reported或取消真实性类别校验来处理。应让模型重提符合当前行为的新讨论事件，或从工具边界更清楚地区分追加与普通回顾。当前普通召回因为留存提案无效而完全没有answer，属于可用性阻断。

## 审计与保护

- DB audit `a6912d6a-ecca-46d8-926b-d4aa2cdac7ee`：intake、memory_decision_invalid，input_tokens=7131、output_tokens=1234。
- 失败前后events、originals、self_state、self_history、counts相同。
- 最终experiences=2、episodes=2、memories=3、episode_corrections=0、self_observations=1。
- 第1轮经历及双方金额实际保存；首提temporary事实缺少所需信息，经现有一次重提后成功。第2轮单次写作要求未新增长期偏好。
- 表达质量另记：第1轮回答含未替换的“[记忆ID前8位]”；第2轮写作超过所要求的两句。不能把这些表达问题与核心记忆通过混淆。

## 证据与剩余范围

新空白实验 `22abf72b-50ee-4760-be63-ae0645f42f18`。证据位于 `data/qa/independent-acceptance-20261027/`：

- `batch7.json`：完整对话、响应、快照、代码指纹
- 对应 `provider/*.json`：实际system、完整payload、工具schema、模型响应
- `failure-round7-replay.json`：精确失败及离线单字段改变通过、DB审计
- `local-tests-round9.txt`、`ui-tests-round9.txt`、`requests.json`

联合纠错修复尚未在本批运行至第5轮，不能宣布其真实行为通过。成功两次纠错、跨会话排旧、原话及未纠正金额保留、独立相似事件不误改的完整验收仍待继续。此前第6批实际通过的偏好保存、单次例外识别、未知事实不编造证据保留；不提前撰写总体通过结论。

## Full transcript (unaltered)

### Turn 1

User:

10月14日我和夏棠去了杉木街裁缝铺。我花76元改大衣，夏棠花33元买了围巾。临走时她把装大衣的布包递给我，说：“尺寸可以慢慢改，你不用急着变成别人的样子。”我最近总担心新团队不接纳我，听完安心了一些。

API response:

听你这么说，我能明白为什么这句话让你安心了一些。

夏棠没有要求你立刻变得符合某种标准，而是告诉你：“尺寸可以慢慢改，你不用急着变成别人的样子。”这份允许本身就很温柔。面对一个刚加入的新团队，担心自己不被接纳是很正常的，但你有时间慢慢适应，也有机会慢慢找到自己的位置；不必为了尽快获得认同，就急着把自己改得不像自己。

她递给你的不仅是装大衣的布包，也像是在把一种安心交给你：**你不必靠立刻成为别人来换取留下。** [记忆ID前8位]

Provider IDs: 6f22ccdb-e6e9-45a8-b77f-d94ce81e16ba, a114b345-5378-4f89-be7a-0e074c9a4b99, 902b2c62-ef72-4a37-a8de-076c924c2993

### Turn 2

User:

只针对这一次手作展邀请，请写两句热闹的开头，多用感叹号。

API response:

手作展终于来啦！快来一起看看现场有多热闹吧！✨🎨

一件件手作好物都准备好和你见面啦，这个秋天，把惊喜和满满巧思一起带回家吧！🎪🧵

Provider IDs: da6ebc39-7b96-49e5-9783-efd09b4f12a3, 8dc964bf-4d36-48db-b36f-f87a721c11f3

### Turn 3

User:

上次我和夏棠去杉木街裁缝铺，我们各花多少钱做什么？她递布包时的完整原话是什么？

API response:

{"error": "memory_decision_invalid", "message": "记忆判断未通过来源或格式验证，本轮未写入；请重试。"}

Provider IDs: 28b344c3-1edb-448c-8622-883c3fc48165

Frozen code and protected ledgers verified unchanged.
