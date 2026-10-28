# 当前独立有界验收：核心目标仍未全部通过

**状态：草稿，尚未经过最终审核。** 用户已要求收尾停下；这里只保留已经产生的验收结果和暂定结论，不再追加验收、模型请求、测试或改造。

**最新阶段（2026年10月28日，已完成）**：[当前实现实验版独立验收](current-version-independent-acceptance-20261028.md)。工程交付通过（实验版），功能部分通过，生产未验收、不得发布；本轮26次，累计158次。以下第11批及更早的历史汇总原文保留，均按旧快照理解。

阶段目标调整（2026年10月28日）：后续按[当前实现实验版验收口径](current-version-acceptance-goals-20261028.md)分别判断工程交付、功能验证与生产就绪。新阶段验收尚未完成；以下旧批次的失败、统计和版本归属保持不变。

后续代码变更：第11批之后已按用户要求删除检索细节的场景词分类、关键词过滤与固定4/8条上限，改由统一预算装配原文片段。本页真实模型结果仍绑定第11批冻结快照，不代表这次收缩后的新版本验收；本次离线全量回归552项及5子测试通过，没有新增provider调用。

更新于2026年10月28日，当前证据为[第11批固定六轮报告](qa-independent-real-acceptance-20261027-batch11.md)。重评协议文案同步后，91→77元实际纠错与跨会话读回通过；咖啡馆经历未保存，77→83元替代关系未建立。最终正文虽答83元，实际证据仍同时呈现有效77元和新83元叙述，不能算纠错成功。累计真实provider请求 **132次**。

各批结论绑定各自代码快照，历史失败保留，不跨版本合并成功率。第9批快照报告已[存档](qa-batch9-bounded-acceptance-20261027.md)，不代表当前版本结果。

## 当前快照与有界结果

本次仅修改REASSESS_SYSTEM末段，把旧quote/occurrence说明统一为每个episode显式顶层quote_refs、完整连续父范围及逐对象required检查；没有程序补齐缺失范围或放宽验证器。[独立提示词审查](qa-reassessment-prompt-review-20261028.md)未发现阻塞项。主修复聊天全量pytest551项、5子测试和UI7项通过后，才执行本次实验。

[预注册计划](../data/qa/independent-acceptance-20261027/batch11-preregistered-plan.json)精确复用第10批六条输入、会话分组与配置，在新空白实验中只执行一次。MiniMax-M3、thinking disabled、reflection off、本地embedding，模型90秒、整轮240秒、每轮最多3次请求。实际逐轮2、3、3、2、3、2次，共15次。

| 当前快照自然样本指标 | 结果 |
|---|---:|
| HTTP200且有回答 | 6/6（100%） |
| 回答且保存本轮经历 | 5/6（83.3%） |
| 整理失败、未保存 | 1/6（16.7%） |
| HTTP错误 | 0/6 |
| 两次事件纠错实际提交 | 1/2 |
| 首次纠错后读77并保留22元与原话 | 1/1 |
| 第二次纠错建立替代关系并排除旧77 | 0/1 |
| 咖啡馆独立事件保存及后续召回 | 0/1 |
| 本次失败无业务写入且正文诚实 | 1/1 |

HTTP200和正文答出83元都不等于纠错已提交。这些比例只描述当前固定六轮，不估计总体可靠性。完整原件在[batch11.json](../data/qa/independent-acceptance-20261027/batch11.json)。

## 核心未通过项与程序保护

| 范围 | 最新实际证据 |
|---|---|
| **独立事件保存与召回** | 第2轮模型把`{episode_id,kind:supplement,quote_refs}`放入episodes，缺title/gist、有额外字段，严格拒绝。39/44元和10月17日未保存；后续回答承认缺数据，保存及召回目标仍失败 |
| **重复纠错提交** | 第5轮首提create附expected_revision，被一致性校验拒绝。重提后模型要求额外确认，未提交77→83纠错，仅保存83原话 |
| **旧有效值排除** | 第6轮答83，但actual_context中77元已保存纠错和83元新叙述同时有效呈现；最终DB只有91→77的一条episode_correction，第二次替代目标未完成 |
| **模型引用遵循** | 第6轮正文引用的7fb7de7是第4轮提问事件，不能支持金额结论 |
| **失败正文虚假未来承诺** | 第9批两次自然失败及单次注入仍是有效失败证据；本批一次诚实降级不能覆盖它们，也未重复注入证明修复 |
| 首次纠错成功提交及读回 | 第3轮91→77提交，第4轮读77/22与完整原话正确，旧91只在明确否定的历史纠错说明中保留 |
| 原始归档与未纠正细节 | 第1轮user_text逐字不变；22元及“慢慢骑，前面的路还长。”保留 |
| 短引用来源权限与事务复查 | 新回归、独立离线适配器探针及真实首次提交支持；未知ref、额外坐标等拒绝，原来源/范围/版本/权限复查保留 |
| 第2轮非法提案不写入 | history_saved=false，events/originals/self_state/self_history/counts前后快照一致；不会自动把错误对象搬到其他字段 |
| 失败状态与额度 | 实际payload有memory_failure、空planned变更和retain_this_turn=false；警告前置，实际每轮≤3次 |
| 长期偏好 | 第9批保存和跨会话读回1/1通过；当前第11批未重测 |
| fact＋事件联合自然纠错 | 当前批没有独立金额fact，成功路径仍未覆盖；旧录制回放不能代替自然成功样本 |

在本次改动范围内，未发现新的、可定位的程序来源授权或事务提交漏洞。模型非法提案被拒、错误操作未提交、机器状态与DB一致；模型重提后不必要澄清使明确纠错目标未完成。按用户要求保留自由聊天与准确机器状态，失败正文承诺按模型遵循性失败记录，不以固定程序回复规避。

[数据库审计](../data/qa/independent-acceptance-20261027/batch11-final-db-audit.json)最终有5经历、7事件、8事件版本、22详情、1纠错、0独立事实、2自我观察、8消息。[最终证据审计](../data/qa/independent-acceptance-20261027/batch11-final-evidence-audit.json)保存了同时呈现77与83的实际云证据，避免只看答案数字判通过。

## 历史版本与证据类型

| 样本 | 结果与限制 |
|---|---|
| 第8批：10轮、24次请求 | 7轮保存、2轮降级、1轮HTTP502；纠错0/2、长期偏好保存0/1；正文误称此前记录不再沿用 |
| 第9批：8轮、20次请求 | 6轮保存、2轮降级；纠错0/2，偏好保存读回1/1；两次失败正文仍承诺以后按新金额回答 |
| 单次故障注入：1次真实answer | 业务表哈希不变，失败payload与警告正确；正文“以后按77元记住”，遵循未通过 |
| 第10批：6轮、15次请求 | 5轮保存、1轮降级；91→77实际提交并读回，第二次因重评缺quote_refs未保存；独立39/44及原话保留 |
| **第11批：6轮、15次请求** | 5轮保存、1轮降级；91→77提交，咖啡馆未保存；83只存新叙述，77未替代，正文83不能算纠错完成 |

单次注入脚本化intake和错误重评，只调用真实answer，不能算自然纠错成功。其零provider调用准备错误也保留。详见[第9批报告](qa-batch9-bounded-acceptance-20261027.md)。

[第6批提案录制回放](../data/qa/independent-acceptance-20261027/recorded-batch6-replay-on-batch8-version.json)离线移除一个无依据rehearsal，在第8批版本副本走通固定联合事务后强制回滚；没有新provider调用或永久提交，不混入自然成功率。

历史失败原件继续保留：

- [第1批：事实与事件目标混用](qa-independent-real-acceptance-20261027-batch1.md)
- [第2批：子线索越事件范围](qa-independent-real-acceptance-20261027-batch2.md)
- [第3批：自然降级](qa-independent-real-acceptance-20261027-batch3.md)、[重评追加与纠错冲突](qa-independent-real-acceptance-20261027-batch3-turn5.md)
- [第4批：历史日期误作当前时间引用](qa-independent-real-acceptance-20261027-batch4.md)
- [第5批：越界日期改为空串仍失败](qa-independent-real-acceptance-20261027-batch5.md)
- [第6批：联合纠错事务顺序与片段缺失](qa-independent-real-acceptance-20261027-batch6.md)
- [第7批：查询误追加旧事件](qa-independent-real-acceptance-20261027-batch7.md)
- [第8批：完整10轮与偏好失败](qa-independent-real-acceptance-20261027-batch8.md)、[当时有界汇总](qa-batch8-bounded-acceptance-20261027.md)
- [第9批：两次未保存及虚假未来承诺](qa-batch9-bounded-acceptance-20261027.md)
- [第10批：首次提交通过、重复纠错失败](qa-independent-real-acceptance-20261027-batch10.md)
- [第11批：错误episode结构及未建立第二次替代关系](qa-independent-real-acceptance-20261027-batch11.md)

## 调用、版本与完整性

| 范围 | 实际provider请求 | input_tokens | output_tokens | cache_read_input_tokens |
|---|---:|---:|---:|---:|
| 第1至8批累计 | 81 | 421743 | 72564 | 23068 |
| 第9批自然实验 | 20 | 83617 | 13486 | 11696 |
| 单次注入真实answer | 1 | 2076 | 60 | 128 |
| 第10批自然实验 | 15 | 84628 | 10752 | 2202 |
| 第11批自然实验 | 15 | 74231 | 12718 | 8652 |
| 独立账本累计 | **132** | **666295** | **109580** | **45746** |

[逐请求账本](../data/qa/independent-acceptance-20261027/requests.json)包含错误、重提和授权变更，reserved=0。用户授权取消60次总上限，limit=null；每轮三次、超时与逐请求持久记账保持。usage为provider返回字段，不换算费用。

第11批开始与结束app Python/SQL哈希一致，见[完整性记录](../data/qa/independent-acceptance-20261027/batch11-integrity.json)。automatic.py为 `64f88fd70b4fb40e695a063bfe1c210d8d0f42747019afe60079e38f79be9225`；model.py、service.py等与第10批一致。计划SHA256 `b77b6e3dcfba7355fdf24005d3736a93c84b677c1f15a1f18be47bd2acf9109d`。

旧reflection账本仍238/238，SHA256 `818abdee874226b9fa725bc208f1401321ffb6d0418523bfdc824e9443b4333a`；旧chat-repairs账本仍20/30，SHA256 `3fec834e53830bd13bb01ed2d446c18bade0a6b3126a3a000aedd02013a94b8b`，均与基线一致，不包含在独立账本132次中。

测试专用8767已停止，全部隔离实验及原件保留，生产8765和旧实验8766未修改。本批已结束，不再另开故事或重复请求挑选通过结果。尚未证明开放域稳定性、长期衰减、大规模干扰检索或不同模型一致性。
