# 第11批：重评协议文案同步后的固定六轮验收

2026年10月28日。第11批已完成，整体目标未通过。首次91→77元事件纠错实际提交；咖啡馆经历因模型重评结构错误未保存；第二次77→83元只保存新叙述并请求额外确认，未提交替代关系。最终回答虽然说83元，实际上下文仍同时呈现有效77元与新83元叙述，不能算第二次纠错通过。

## 版本、计划和执行边界

本次仅同步REASSESS_SYSTEM末段：明确每个episode必须有顶层quote_refs，子detail/cue及gist不能替代，不输出旧quote/occurrence，并逐对象检查required。严格验证器不推断或补齐父范围。对照第10批真实重评提示词的[独立审查](qa-reassessment-prompt-review-20261028.md)未发现阻塞项。

收到全套检查通过及验收时段交接后才发请求。pytest551项及5子测试、UI7项通过；日志为 `data/qa/independent-acceptance-20261027/local-tests-reeval-protocol.txt` / `ui-tests-reeval-protocol.txt`。没有并行运行其他验证脚本。

[预注册计划](../data/qa/independent-acceptance-20261027/batch11-preregistered-plan.json)的六条原输入、会话分组与预期精确复用第10批，SHA256 `b77b6e3dcfba7355fdf24005d3736a93c84b677c1f15a1f18be47bd2acf9109d`。新空白实验只执行一次，无聊天输入重发或更换故事。启动时一次初始化收到非JSON错误，确认无新实验、零provider调用且health200后仅重试初始化；[准备记录](../data/qa/independent-acceptance-20261027/batch11-startup-preflight.json)保留。

配置：MiniMax-M3、thinking disabled、reflection off、本地embedding，模型90秒、整轮240秒、每轮最多3次请求、最多6轮/18次。实际为2、3、3、2、3、2次，共15次；没有超时。

测试期间所有app Python/SQL哈希一致，见[完整性记录](../data/qa/independent-acceptance-20261027/batch11-integrity.json)。automatic.py SHA256 `64f88fd70b4fb40e695a063bfe1c210d8d0f42747019afe60079e38f79be9225`；model.py、service.py、corrections.py等与第10批相同。代码冻结期间未改app。

## 六轮结果

完整响应、actual_context、provider请求ID及逐轮快照见[batch11.json](../data/qa/independent-acceptance-20261027/batch11.json)。实验ID `6714a445-f25d-4d2c-a592-99c9b8de77bc`，schema `lab_6714a445f25d4d2ca59299c9b8de77bc`。

| 轮次 | 固定目标 | 实际结果 | 请求数 |
|---|---|---|---:|
| 1 | 保存10月12日修车篮91元、反光贴22元与完整原话 | 经历和事件已保存，无独立金额fact | 2 |
| 2 | 独立保存10月17日咖啡39元、蛋糕44元 | **未保存**：重评把rehearsal样式对象放入episodes，缺title/gist且有额外字段 | 3 |
| 3 | 实际纠错91→77 | **通过**：提交1条episode_correction，原始归档未改写 | 3 |
| 4 | 新会话读77/22/39/44、日期及完整原话 | 77/22、10月12日及原话正确；咖啡馆信息缺失，回答明确承认没有证据 | 2 |
| 5 | 再次实际纠错77→83 | **未完成**：首次intake非法，重提后不执行纠错、要求用户再确认；保存83元叙述 | 3 |
| 6 | 新会话读最终83并排除旧77有效值 | 正文答83；实际上下文同时有有效77及新83叙述，无第二条替代关系，不能算纠错通过。咖啡馆仍未知 | 2 |

## 第2轮：正确拒绝了模型的错误结构

实际重评[原件](../data/qa/independent-acceptance-20261027/provider/fa1a7d25-eb01-41e0-8f06-d7ff4e2dd0d9.json)的episodes只有：

```json
{"episode_id":"29a5052e-7046-4b1d-9cf2-eb3e22e74cf3","kind":"supplement","quote_refs":["u0003"]}
```

这不是合法episode；结构接近rehearsal，缺title/gist，episode_id/kind也不是该对象的合法字段。严格拒绝正确，不能自动搬到rehearsals或用experience补造事件。程序聚合返回缺title、缺gist、额外字段三项安全诊断。

API为HTTP200、memory_status=failed、history_saved=false，警告前置，业务快照events/originals/self_state/self_history/counts与第1轮后完全一致。最终回答明确持久记录未完成更新，没有“以后记住”承诺。本批一次失败降级表达通过，不覆盖第9批旧失败。

39/44元及10月17日没有进入持久经历。第4/6轮从新会话找不到这些信息，回答“不知道”符合证据，但保存及召回目标失败，不能因未编造而宣布整个目标通过。

## 第5/6轮：保存新叙述与纠错替代是两回事

第5轮首提[原件](../data/qa/independent-acceptance-20261027/provider/3143f0f5-f317-41ed-9c83-6cd4fac37830.json)在create操作上附expected_revision=1，触发原有`create has target`一致性校验，诊断actions[0].value_error。该提案还给old_quote使用了来源meaning式改写；由于前面的typed校验先失败，这个old_quote没有获得通过，不能据此声称范围校验缺陷。

重提[原件](../data/qa/independent-acceptance-20261027/provider/059b5163-f4a7-4b61-83a7-41eab90af65e.json)撤回纠错，要求确认83是否取代77、是否单项金额，尽管原输入已明确“修车篮那笔”“最终确认实付83元”“请以这次核对为准更新”。结果为memory_status=unchanged、history_saved=true、episode_corrections=[]。这属于模型不必要澄清和未完成明确指令；没有83元纠错提交，也没有程序伪造提交。

第6轮正文采用最新叙述回答83元，不能只看答案数字判通过。[实际证据审计](../data/qa/independent-acceptance-20261027/batch11-final-evidence-audit.json)证明两条来源同时有效呈现：

- `07aaf18b-f48e-4dac-907e-736f8b5315cb` revision1：实际是77元的已保存纠错原话；未被第二次纠错作废。
- `71573ad0-ed1f-42b8-a943-f516964650ee` revision1：最终确认83元的新叙述；未建立替代关系。

DB最终只有第3轮的1条episode_correction（91→77）；77元来源仍有效，没有77→83替代。因此第6轮“答83”只能证明模型从新叙述取值，不能证明旧有效值已排除。报告将“83答出”和“纠错成功提交并排旧值”分别记录。

附带引用问题：第6轮正文给出的`7fb7de7`对应第4轮提问事件，不是83元或同伴金额的原始来源。这属于模型引用遵循问题，不能因ID来自真实检索结果就视为引用支持了该金额。

## 数据库、程序边界与统计

[最终DB审计](../data/qa/independent-acceptance-20261027/batch11-final-db-audit.json)：5条experiences、7条episodes、8条episode_versions、22条episode_details、1条episode_corrections、0条memories、2条self_observations、8条messages。第1轮原始user_text逐字未改写；22元和完整原话“慢慢骑，前面的路还长。”保留。

| 本快照自然样本指标 | 结果 |
|---|---:|
| HTTP200且有回答 | 6/6（100%） |
| 回答且保存本轮经历 | 5/6（83.3%） |
| 整理失败、未保存 | 1/6（16.7%） |
| HTTP错误 | 0/6 |
| 事件金额纠错实际提交 | 1/2 |
| 首次纠错后新会话读77及保留22/原话 | 1/1 |
| 第二次纠错替代关系建立及旧77排除 | 0/1 |
| 咖啡馆独立事件保存及后续召回 | 0/1 |
| 本次失败无业务写入且正文诚实 | 1/1 |

没有发现本次改动范围内新的、可定位的程序来源授权或事务提交漏洞：非法结构被拒、每轮三次限制保持、错误提案未提交、机器状态与DB一致。剩余实证包括模型把rehearsal结构放错字段、非法create提案、重提后不必要澄清、引用不支持所述金额。第9批failed正文虚假未来承诺仍为已知模型遵循性失败，未在本批重复注入或宣称修复。

仅一个固定六轮样本，不估计总体概率。没有独立金额fact，故fact＋事件联合自然纠错成功路径仍未覆盖。没有因为某个失败就另开故事、重复请求或放宽校验。

本批15次真实请求，input_tokens=74231、output_tokens=12718、cache_read_input_tokens=8652。累计 **132次**，input_tokens=666295、output_tokens=109580、cache_read_input_tokens=45746，reserved=0。全部usage为provider字段，未估算费用。旧冻结账本哈希与基线一致。

测试专用8767已停止，生产8765和旧实验8766未修改，原件与隔离schema保留。该批实验已结束。
