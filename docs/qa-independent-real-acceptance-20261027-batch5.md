# 独立真实验收：批次五（2026-10-27）

## 结论

首轮两次intake均无效，最终HTTP502 `memory_decision_invalid`，没有answer、没有业务写入。受控重提确实执行，但模型把越界日期改成空字符串后又失败，未能完成当前正常叙述。QA未放宽断言或修改应用。

完整pytest：529 passed、5 subtests passed、1 warning（98.72s）；Node UI 7 passed。本批实际2次provider请求，累计账本从30增至32（主chat交接中的31与实际账本不符，采用账本值）。无总额度上限。8767已停止，槽位交回。

## 实际输入和结果

用户：

> 10月16日我和程苒去了白榆路陶器铺。我花87元修茶壶，程苒花31元买了青瓷杯。出门时她把装茶壶的竹篮递给我，说：“慢慢来，裂缝补好以后还能继续泡茶。”我那阵子总怕项目做不好，听完安定了一些。

API：HTTP502，`memory_decision_invalid`，消息“记忆判断未通过来源或格式验证，本轮未写入；请重试。”

## 两次失败的精确重放

第一次provider：`748021c5-9792-41b1-9d4a-7b7fe97c2797`。

- 模型拆成两个事件，第二事件只引用u0003、u0004（递篮、赠言及情绪）。
- 第二事件的time_quote却为“10月16日”，日期只出现在u0001。
- 原schema校验通过，语义校验抛 `episode_time_outside_event_scope`，路径 `episodes/1/time_quote`。

第二次provider：`341a8483-4861-4268-9e8c-ab848b3917f2`。

- 实际请求携带上述精确proposal_errors，证明受控重提走通。
- 模型将该字段改成 `""`，触发Pydantic `string_too_short`：time_quote要求非空字符串或null。
- 对外受控诊断路径为 `episodes/1/field`，因为当前字段白名单未包括time_quote；原始Pydantic定位仍明确是 `episodes.1.time_quote`。
- 离线仅将第二次提案该字段设为null后，完整validate_decision通过。此操作仅定位，不建议线上自动把空串改成null。

两次失败都使用完整实际provider payload和schema重放，没有新增云请求。没有放宽来源或扩大父事件引用。当前最多一次重提的上限正确执行；不建议再加一次请求来掩盖这个提示/协议表达问题。

建议明确告诉模型：没有本事件范围内的准确时间短语时，使用JSON null或省略可选字段，不能写空字符串。现有“时间不明确就留空”等文字可能让模型把null理解成空串；这是提示歧义的可能解释，不是已证明的模型内部原因。可同时保留time_quote精确受控诊断。

## 隔离与事务

- 新空白实验：`e04d5f92-84b1-49e8-ae76-95beb5f5148a`。
- 最终experiences、episodes、memories、episode_corrections、self_observations均为0。
- 审计有两条intake失败：`62bd45c9-dd81-4f30-8c30-daa316b9a0a7`、`7f578d3c-5b9a-49c1-a0cd-3de4502c77ed`。
- MiniMax-M3、thinking disabled、reflection off、90秒模型超时、240秒整轮期限、24000上下文预算未变。

## 证据与未完成范围

证据在 `data/qa/independent-acceptance-20261027/`：`batch5.json`、上述provider JSON、`failure-round5-replay.json`、`requests.json`、`local-tests-round7.txt`、`ui-tests-round7.txt`。

本批第2—10轮未执行。首次纠错提交、跨会话排旧保留其他细节、明确偏好保存、重复纠错及相似独立事件的完整真实验收仍未完成，不能以本地529项通过替代。
