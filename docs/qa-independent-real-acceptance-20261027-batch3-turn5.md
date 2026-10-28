# 第三批第5轮：首次纠错提交失败

## 结论

首次金额纠错返回HTTP503 `memory_commit_failed`。已在完整数据库隔离副本中使用实际provider提案、实际审计source_refs重放，确认内部异常为 `ValueError: episode_target_not_available`，SQLSTATE为null。这不是SQL执行错误，而是持久化前的目标校验错误被外层包装为503。

第4轮完整降级已验证并经主chat授权继续；本报告记录继续后出现的新阻断。累计真实请求25次，本批13次。后续第6—10轮停止；临时8767已停止，应用未修改，实现和数据库测试槽交回。

## 真实请求

用户：

> 我核对了10月18日银杏巷修伞铺的收据，之前修蓝色长伞的金额记错了，实际是64元。按核对结果，我那次花了多少钱？

API响应：

```json
{"error":"memory_commit_failed","message":"本轮记忆事务未完成，没有确认保存成功。"}
```

- intake：`e4ab8d46-e32d-4023-b6fc-7d109a63acf7`
- reassessment：`698f6089-1511-4a9a-8f50-fef75755ea28`
- answer：`810e671c-d399-429b-8b66-d7e7b559632e`

实际answer模型已生成“按核对后的收据，你那次修蓝色长伞实际花了 **64元**。”，但事务失败，所以API没有把它作为成功回答返回。

## 精确原因

1. intake的corrections合法：目标 `30f3d7eb-c273-45d5-aa63-a8fbefc60570`，原文experience `6ab3b68e-7c25-46e2-aaef-413b6bb8a8db`，old_quote为“81元”，校验收窄至 `[21,24)`。intake的新事件未设target_id。
2. 重评把 `episodes[0].target_id` 设成同一个正在纠正的旧事件。`validate_reassessment` 单独通过，没有与intake已确定的corrections交叉检查。
3. 服务的context_roots会排除纠错影响对象。根据实际model_calls.source_refs与实际correction_context重建，该轮最终roots确实为 `[]`，不是重放时随意省略。
4. `save_answer`先写入归档及纠错，再进入 `_save_episodes`。后者调用 `validate_episode_proposals(..., allowed_existing=[])`，发现重评追加目标不在允许集合，于 `app/episodes.py:190` 抛出 `episode_target_not_available`。
5. 调用链：`app/repository.py:440` → `app/episode_repository.py:30` → `app/episodes.py:190`。外层 `app/service.py`将ValueError包装为memory_commit_failed。

建议在重评工具和最终计划校验阶段排除与纠错冲突的追加目标，复用已验证的诚实降级路径；保留事务层校验。不能通过把纠错目标重新塞回允许追加集合来绕过冲突，也不能静默删除target_id视作模型已提出新事件。

## 回滚验证

- 实际失败后：experiences=3、episodes=3、memories=0、episode_corrections=0，旧81元记录仍为未纠正状态。
- 实际DB审计：intake_completed、reassessment_completed、answer的commit_failed。
- 完整源schema经pg_dump复制到独立schema `qa_independent_turn5_clone_20261027`，包含数据及约束。重放不调用云模型。
- 从实际审计重建roots后调用原始save_answer，以强制回滚事务包裹。复现相同ValueError，所有表重放前后逐行序列化SHA256完全一致；哈希清单保存于证据文件。
- 原始实验与副本均保留，未删除原文、日志或失败记录。

## 证据

均位于 `data/qa/independent-acceptance-20261027/`：

- `batch3.json`：第5轮完整响应及快照
- `provider/`：上述3个请求的完整实际输入和响应
- `failure-round3-turn5-replay.json`：初次强制回滚重放与实际DB审计
- `failure-round3-turn5-clone-replay.json`：完整副本、实际roots、精确堆栈、SQLSTATE及全部表前后哈希
- `turn5-original-schema.sql`、`turn5-clone-schema.sql`：仅含本次合成实验的隔离副本材料

此前525项测试、5子测试和UI7项通过不能覆盖此“合法intake纠错＋重评追加同一旧目标”的跨阶段组合。首次纠错成功、后续召回、偏好保存等剩余场景尚未通过真实验收。
