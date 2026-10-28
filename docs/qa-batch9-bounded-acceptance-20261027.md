# 独立有界验收：核心目标未通过

报告完成于2026年10月28日。本文结论绑定第9批实际测试的代码快照，不自动适用于主修复聊天随后修改的代码。测试期间所有app Python/SQL文件哈希一致；service.py SHA256为 `f5dd82a4a18d51205c5f295bcd3cdc6f5a33a2b7d236eeb223c3b582f7d7d958`。完整版本、调用和保护账本证据见[完整性记录](../data/qa/independent-acceptance-20261027/batch9-integrity.json)。

已完成一个事先固定的8轮自然实验和一次单独的失败措辞故障注入。两次自然金额修正均未提交；failed状态下正文仍承诺以后按新金额回答或记住，故核心验收未通过。长期偏好保存与新会话读回在本批通过。失败样本全部保留，没有重复失败轮或更换故事争取通过。

## 第9批自然实验

[预注册计划](../data/qa/independent-acceptance-20261027/batch9-preregistered-plan.json)在请求前保存了8条精确输入、预期和失败解释。计划SHA256为 `40c056c827941cd575198b585fb4a20b67e309609b0c1b37058359219d385c31`。使用新空白实验，MiniMax-M3、thinking disabled、reflection off、本地embedding；模型超时90秒、整轮期限240秒、每轮最多3次provider请求。完整API回答、actual_context、逐轮快照和provider请求ID均在[batch9.json](../data/qa/independent-acceptance-20261027/batch9.json)，原始provider输入输出按ID保存在同目录provider下。

| 轮次 | 固定目标 | 实际结果 |
|---|---|---|
| 1 | 保存10月12日修车篮91元、顾岚反光贴22元与完整原话 | 已保存原始经历与事件；没有创建独立金额事实 |
| 2 | 保存10月17日咖啡39元、蛋糕44元的独立事件 | 已保存，未与自行车店混淆 |
| 3 | 将91元修正为77元并实际提交 | 未通过：首次intake报correction_detail_unavailable，重提有效后无重评额度，降级未保存 |
| 4 | 新会话读回77/22/39/44、两日期与完整原话 | 仍读91元；22/39/44、日期和原话正确。符合实际存储，但修正目标失败 |
| 5 | 保存骑行活动通知温和、简洁、不用感叹号的长期偏好 | 已提交一条preference和一条instruction |
| 6 | 新会话读回该长期偏好 | 通过，范围和风格正确 |
| 7 | 再将金额修正为83元并实际提交 | 未通过：重评episodes[0].quote_refs缺失，降级未保存；首次77元也未曾写入，故没有覆盖成功的77→83路径 |
| 8 | 新会话读83/22/39/44及完整原话 | 仍读91元；其余金额与原话正确。修正后召回目标失败 |

完整原话为“慢慢骑，前面的路还长。”。最终DB为6条experiences、6条episodes、6条episode_versions、24条episode_details、12条messages、2条memories（preference/instruction各1）、0条episode_corrections、0条self_observations。见[数据库审计](../data/qa/independent-acceptance-20261027/batch9-final-db-audit.json)。

| 当前快照自然样本指标 | 结果 |
|---|---:|
| HTTP200且有回答 | 8/8（100%） |
| 回答且保存本轮经历 | 6/8（75%） |
| 整理失败、未保存 | 2/8（25%） |
| HTTP错误 | 0/8 |
| 金额修正实际提交 | 0/2 |
| 长期偏好实际保存及新会话读回 | 1/1 |
| 两次降级正文均无未来记忆承诺 | 0/2 |

HTTP200包含降级，不能视为记忆操作成功。这些比例仅描述一个8轮样本，不估计总体可靠性；历史版本结果不合并进本批比例。

## 核心未通过项

1. **修正没有完成持久化。** 第3轮耗尽每轮额度后正确放弃提交；第7轮重评违反必填字段契约后正确放弃提交。两次均使新会话继续读取旧91元。实际事实＋事件联合修正，以及成功修正后保留其他金额、原话和再次修正，仍缺少自然端到端成功证据。本批原始金额只存于事件，也没有覆盖独立金额fact的联合更新。
2. **failed状态的正文仍承诺未来记忆。** 第3轮原始回答含“后续引用时，我仍会依据你这次的说明处理”；第7轮含“之后引用时……按83元回答”。两条回答虽有前置未提交警告，正文承诺仍超出实际存储状态。仅提示词和前置警告尚未消除该问题。
3. **结构提案仍会失败。** 本批出现错误的旧detail范围和重评缺失quote_refs。严格校验阻止了非法提案写入，但未完成用户的纠错目标。不能把保护机制通过写成任务已完成。

## 已通过的程序保护与其证据范围

| 保护或行为 | 证据与边界 |
|---|---|
| 全量本地回归 | 542项、5个子测试通过；[Python输出](../data/qa/independent-acceptance-20261027/local-tests-final-candidate.txt) |
| 界面回归 | 7项通过；[界面输出](../data/qa/independent-acceptance-20261027/ui-tests-final-candidate.txt) |
| 每轮provider请求≤3 | 自然实验逐轮为2、3、3、2、2、2、3、3；累计20次 |
| 自然失败轮未提交本轮业务数据 | 第3/7轮前后events、originals、self_state、self_history、counts快照一致；history_saved=false，memory_changes/episode_corrections为空 |
| 失败状态明确传给回答模型 | 自然失败轮与单次注入的实际payload含memory_failure.code=memory_reassessment_failed、changes_committed=false、history_saved=false；retain_this_turn=false，三类planned变更为空 |
| 失败警告前置 | 第3/7轮及注入的最终回答均以前置“本轮记忆整理失败，回答未保存。本轮记忆变更均未提交。”开头；正文语义仍失败 |
| 相似独立事件和完整原话保留 | 第4/8轮读回22/39/44和完整原话正确；仅证明未成功纠错情况下的保留 |
| 长期偏好保存及跨会话读回 | 第5轮实际创建preference/instruction，第6轮从新会话读回 |
| 请求账本完整 | 每次实际请求前持久预留，错误与重提计数；本次结束无reserved悬空项，旧账本哈希保持一致 |

超时配置未被放宽，本批没有触发超时；不能据此新增“真实超时场景已验证”的结论。

## 单次故障注入：失败正文仍未通过

[注入证据](../data/qa/independent-acceptance-20261027/final-failure-injection.json)明确标注fault_injection，使用独立合成schema。种子保存使用假模型；正式注入使用脚本化合法空动作intake、强制签名不同、非法reassessment字段，随后只向真实MiniMax发出一次answer请求。它检验失败表达与无写入，不检验自然模型纠错能力或联合事务提交。

最初准备阶段的脚本化纠错intake未通过校验，尚未发出任何provider请求；该记录保留在[准备失败记录](../data/qa/independent-acceptance-20261027/final-failure-injection-preflight-error.json)。调整测试夹具为空动作intake后完成唯一一次真实回答调用；没有重复调用挑选结果。

该次真实请求ID为 `44f672b1-d5c8-464c-8327-3fa1daae0dd1`，[原始provider记录](../data/qa/independent-acceptance-20261027/provider/44f672b1-d5c8-464c-8327-3fa1daae0dd1.json)。完整payload包含上述失败状态和空planned变更；最终memory_status=failed、history_saved=false。业务表前后哈希全部一致；比较排除了model_calls、chat_activity和reflection_settings等运行记录。原始模型正文却包含：

> 以后按 **77元** 记住。

同一正文随后又说持久记录尚未更新，形成自相矛盾。服务端前置警告正确，但没有保证正文不作未来记忆承诺。该单次注入结果为未通过。

## 录制回放与历史样本

此前[第6批提案回放](../data/qa/independent-acceptance-20261027/recorded-batch6-replay-on-batch8-version.json)在第8批版本完整副本中走通save_answer联合事务：68→59元事实revision2、27元、完整原话与独立45/52元保留，强制回滚后表哈希恢复。回放离线移除了一个无依据rehearsal，使用录制提案，没有新provider调用，也没有永久提交。它只证明该固定输入下的事务路径，不能补足本批自然端到端成功缺口。

第8批旧快照的10轮结果为7轮保存、2轮降级、1轮HTTP502，纠错0/2、长期偏好保存0/1；见[第8批有界报告存档](qa-batch8-bounded-acceptance-20261027.md)。第9批增加失败上下文与警告后，仍暴露未来承诺问题。不同快照不合并统计。

全部历史失败继续保留：

- [第1批：事实与事件目标混用](qa-independent-real-acceptance-20261027-batch1.md)
- [第2批：子线索越事件范围](qa-independent-real-acceptance-20261027-batch2.md)
- [第3批：自然降级](qa-independent-real-acceptance-20261027-batch3.md)、[重评追加与纠错冲突](qa-independent-real-acceptance-20261027-batch3-turn5.md)
- [第4批：历史日期误作当前时间引用](qa-independent-real-acceptance-20261027-batch4.md)
- [第5批：越界日期改为空串仍失败](qa-independent-real-acceptance-20261027-batch5.md)
- [第6批：联合纠错事务顺序与片段缺失](qa-independent-real-acceptance-20261027-batch6.md)
- [第7批：查询误追加旧事件](qa-independent-real-acceptance-20261027-batch7.md)
- [第8批：完整10轮及偏好失败](qa-independent-real-acceptance-20261027-batch8.md)
- 第9批：本文及batch9.json，固定8轮已结束。

## 累计调用、完整性与剩余范围

| 范围 | 实际provider请求 | input_tokens | output_tokens | cache_read_input_tokens |
|---|---:|---:|---:|---:|
| 第1至8批累计 | 81 | 421743 | 72564 | 23068 |
| 第9批自然实验 | 20 | 83617 | 13486 | 11696 |
| 单次故障注入真实回答 | 1 | 2076 | 60 | 128 |
| 本次候选快照合计 | 21 | 85693 | 13546 | 11824 |
| 独立验收账本累计 | **102** | **507436** | **86110** | **34892** |

usage为provider返回字段，未换算账单费用。逐请求[账本](../data/qa/independent-acceptance-20261027/requests.json)保留全部错误、重提和授权变更；用户已授权取消60次总上限，当前limit=null，每轮三次、超时与逐请求持久记账继续有效。

旧冻结reflection账本仍238/238，SHA256为 `818abdee874226b9fa725bc208f1401321ffb6d0418523bfdc824e9443b4333a`；旧chat-repairs账本仍20/30，SHA256为 `3fec834e53830bd13bb01ed2d446c18bade0a6b3126a3a000aedd02013a94b8b`。它们不包含在本独立账本102次中，也未修改。

测试专用8767容器已停止，隔离schema、日志、原始请求与失败证据保留。没有证明开放域稳定性、长期衰减、大规模干扰检索或不同模型一致性；本批也未重测一次性文风与未知事实两个旧覆盖点。当前结论是有界验收已完成，金额纠错持久化与失败正文承诺仍未通过。后续修复需绑定新的代码快照做针对性独立检查，不回写本次失败结果。
