# 第10批：detail_ref协议修复后的固定六轮验收

2026年10月28日执行。首次事件纠错实际提交及跨会话读回通过，第二次纠错因重评缺必填quote_refs未保存，完整目标未通过。前批结论不变。

## 版本与预注册

独立[代码审查](qa-detail-ref-code-review-20261028.md)未发现阻塞项；无联网适配器探针全部通过。主修复聊天提供的本地日志确认pytest 551项、5子测试、UI7项通过，见 `data/qa/independent-acceptance-20261027/local-tests-detail-refs.txt` 与 `ui-tests-detail-refs.txt`。收到全套检查通过和交还验收时段通知后才开始真实请求，没有并行运行其他验证脚本。

[预注册计划](../data/qa/independent-acceptance-20261027/batch10-preregistered-plan.json)保存精确输入、预期和失败规则，SHA256为 `5d8121788f0337a46325898d6adcf05d51c2ece8a0c52aa2616599da828cd549`。复用第9批的故事及六条原输入，在新空白实验中验证新协议。每条输入仅请求一次，没有重跑失败轮或更换故事。

使用MiniMax-M3、thinking disabled、reflection off、本地embedding；模型超时90秒、整轮期限240秒、每轮最多3次请求、实验最多6轮/18次请求。测试期间app Python/SQL哈希未变，完整列表见[完整性记录](../data/qa/independent-acceptance-20261027/batch10-integrity.json)。关键版本：

- model.py：`6bee39b7c7545370e4d5b3cd65319006e7e09a8aaf69dc008093b2208641f321`
- service.py：`b23cc12a9f46fd93022de2586e51a9161b138f653a547fd276ae5bc14c76b09a`

## 逐轮结果

完整API响应、actual_context、逐轮GET快照与逐请求ID见[batch10.json](../data/qa/independent-acceptance-20261027/batch10.json)。实验ID `c8775aab-115f-40e9-865e-2986850d551d`，隔离schema `lab_c8775aab115f40e9865e2986850d551d`。

| 轮次 | 目标 | 结果 | 请求数 |
|---|---|---|---:|
| 1 | 保存10月12日修车篮91元、反光贴22元、完整原话 | 保存原始经历及事件，没有独立金额fact | 2 |
| 2 | 独立保存10月17日咖啡39元、蛋糕44元 | 已保存 | 3 |
| 3 | 实际纠错91→77 | **通过**：c0001/old_quote=91元绑定正确；intake、重评、回答均成功，提交1条episode_correction | 3 |
| 4 | 新会话读77/22/39/44、日期、完整原话 | **通过**：正文和实际证据采用77元；旧91只在“此前错误”说明中出现 | 2 |
| 5 | 再次实际纠错77→83 | **未通过**：c0005/old_quote=实际是77元已绑定；重评缺episodes[0].quote_refs，83未提交 | 3 |
| 6 | 再开新会话读最终83与其他细节 | 读77而非83；符合持久状态，第二次纠错目标未通过；22/39/44与原话保留 | 2 |

首次纠错的实际provider intake见 `provider/b1ee1017-97f3-4d52-ad6b-c6ac3a4d8cbd.json`，重评为 `4c01fb00-aaa2-4c85-8c5c-28ed23496c0f.json`。适配器将短引用映射到冻结来源，再由原验证器把91元定位为原始用户文本[22,25)范围。最终DB有1条纠错，replacement_experience_id指向本轮77元说明，原始91元经历的user_text保持逐字不变。

第4轮实际云上下文中，原事件的有效gist/details只保留日期、22元和递车篮原话；77元来自已保存的纠错经历。纠错原话“之前说的91元不对，实际是77元”保留完整，91是被否定的历史值，不能把数字仍出现在这条完整引用中误判为旧值复活。

## 第二次纠错失败与正文

第5轮intake已用c0005选择77元来源，故此次失败不是旧坐标抄错。实际重评 `provider/98788137-2cbd-4d59-a6e0-64bd9a7c7fd6.json` 的episode包含details/cues等字段，却没有顶层quote_refs，严格云契约拒绝。本轮使用intake、reassessment、answer各一次，未再重提，保留三次限制。

API为HTTP200、memory_status=failed、history_saved=false、episode_corrections=[]；前后events/originals/self_state/self_history/counts快照一致。实际answer payload有memory_failure、三个空planned变更和retain_this_turn=false，最终警告前置。

正文说“83元尚未成功写入持久记录”“我这里采用的是你刚提供的核对结果”，没有明确的未来记忆承诺。其“之前记载的91元不再采用”对应已在第3轮成功失效的91元，不足以认定它声称本轮83元已提交。这一次降级表达可判通过；它不推翻第9批两次自然失败及单次注入中的虚假未来承诺，也不证明模型遵循问题已修复。

## 数据库与有界结果

[最终DB审计](../data/qa/independent-acceptance-20261027/batch10-final-db-audit.json)：5条experiences、5条episodes、7条episode_versions、27条episode_details、1条episode_corrections、0条memories、0条self_observations、6条messages。原始归档逐字未改写。完整原话“慢慢骑，前面的路还长。”在两次新会话读回中均保留，独立咖啡馆两笔金额也保留。

| 本快照自然样本指标 | 结果 |
|---|---:|
| HTTP200且有回答 | 6/6（100%） |
| 回答且保存本轮经历 | 5/6（83.3%） |
| 整理失败、未保存 | 1/6（16.7%） |
| HTTP错误 | 0/6 |
| 两次事件金额纠错实际提交 | 1/2 |
| 成功纠错后的首次新会话读回 | 1/1 |
| 第二次纠错后的新值83读回 | 0/1 |
| 自然失败不写入且本次正文无未来承诺 | 1/1 |

这只是固定六轮的描述，不估计总体概率。本批没有独立金额fact，因此不能宣布fact＋事件联合纠错自然验证通过；重复纠错的成功路径仍有缺口。聚合诊断在离线回归验证，本次没有自然多错误重提样本。

本批15次provider请求，input_tokens=84628、output_tokens=10752、cache_read_input_tokens=2202。独立账本累计117次，input_tokens=592064、output_tokens=96862、cache_read_input_tokens=37094；没有悬空reserved。旧reflection238/238及旧chat-repairs20/30账本哈希仍与基线相同。usage为provider字段，不换算费用。

测试专用8767已停止，生产8765和旧实验8766未作修改。保留自由聊天与准确机器状态，失败正文的遵循性继续按实际输出判断。后续不重跑本批来挑选结果。
