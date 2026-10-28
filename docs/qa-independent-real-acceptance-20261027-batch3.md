# 独立真实验收：批次三（2026-10-27）

## 结论

本批核心验收未完成：第4轮自然发生重评格式失败，第二次出行没有保存，因此按失败即停规则暂停第5—10轮。降级行为本身通过：HTTP200仍正确回答人物、日期、金额与动作，最终API回答明确提示未保存，业务数据未变化。此次没有故障注入。临时8767已停止，实现和数据库测试槽交回。

## 本地验证与隔离条件

- 完整pytest：525 passed、5 subtests passed、1 warning，91.70s；Node UI 7 passed。
- 新空白实验 `a9f09445-8e9e-4c16-a694-5a3f9d062207`，初始经历、事件、事实均为0。
- MiniMax-M3、thinking disabled、reflection off、timeout90、deadline240、context24000，每轮最多3次请求。
- 应用代码指纹保存在batch3.json；QA未修改应用实现。
- 本批10次provider请求，总累计22次；总限额null，保留前两批全部失败。

## 自然重评失败的精确原因

第4轮intake有效且包含完整gist；随后实际 `reassess_memories` 提案的 `episodes[0]` 缺少必填 `gist`，但title、details、quote_refs存在。实际云schema校验抛出 `CloudContractError(invalid_cloud_contract)`，诊断 `cloud_required_field_missing`，路径 `episodes/0/gist`。离线使用完整provider响应与原schema重放，得到相同错误。

- 失败provider：`6c411c4a-d74a-419e-8386-e841e86bad6e`。
- DB失败审计：`e6dd116e-c033-46db-86c4-891784c8f28b`，phase=reassessment、status=memory_decision_invalid。
- 随后的实际answer调用：`3c452612-9ecf-4b7e-b2f0-5650394bcb19`。
- 实际answer payload中 `retain_this_turn=false`；planned_memory_changes、planned_episode_corrections、planned_self_updates均为空。新出行事实仅来自当前输入，旧出行来自历史证据。
- 原始模型回答正确区分两次出行，未声称保存成功。未保存提示是服务端附加的“本轮记忆整理失败，回答未保存。”，不能声称模型自己生成了该提示。
- API `memory_status=failed`、`history_saved=false`，warnings包含上述精确诊断。
- 失败前后快照events、originals、self_state、self_history、counts逐项完全相等。DB最终experiences=3、episodes=3、memories=0、episode_corrections=0、self_observations=1。

诚实降级及不写入边界有效，但正常叙述的持久化可靠性仍不足以完成本批故事。该轮已使用intake、reassessment、answer三次请求；不能在保持每轮上限的同时再追加第四次请求。修复应保留严格校验，不能偷偷从intake复制缺失字段并视作模型已重评。

## 逐轮审查

1. 首次intake漏retain_turn，受控重提后保存成功。回答多问了一个不必要的关系澄清问题，但没有编造关系。本轮没有触发事件scope错误，不能宣称scope修复已获得自然触发覆盖。
2. 当次热情开场要求未创建长期偏好，memories仍为0。写作输出实际有三句感叹句，未严格满足“两句”；这项写作瑕疵单独记录。
3. 新会话正确召回81元修蓝色长伞、26元防水布袋及完整原话。已检查实际answer context中的逐字detail。
4. 自然重评失败，降级回答正确且提示未保存。烘焙店经历未保存，后续不能假设它已被记住。

## 证据与剩余范围

证据目录：`data/qa/independent-acceptance-20261027/`。

- `local-tests-round5.txt`、`ui-tests-round5.txt`
- `batch3.json`：完整实际对话、前后快照、计划和代码指纹
- `provider/*.json`：实际系统提示、完整payload、schema、响应
- `failure-round3-replay.json`：离线复现、完整DB审计、前后数据比较、原始与最终回答对照
- `requests.json`：累计22次及授权记录

首次纠错、同句未纠正金额保留、纠错后跨会话召回、长期偏好保存与识别、第二次纠错及最终召回仍未执行，不能宣告通过。本批已自然触发并检查真实重评失败和回答，无需为同一路径再故障注入来冒充新增覆盖。

## Full transcript (unaltered)

### Turn 1

User:

10月18日我和林栖去了银杏巷修伞铺。我付81元修蓝色长伞，林栖花26元买了防水布袋。离开时她把修好的伞递给我，说：“雨会慢慢停，今天先把自己照顾好。”我那阵子忙着赶稿，听完觉得可以喘口气了。

API answer:

听你这么说，那句“今天先把自己照顾好”确实像让你从赶稿的紧绷里暂时松开了一点。忙的时候，能被提醒先照顾好自己，是一种很具体的支持。

不过，林栖和你是什么关系？这一点我还不能确定。

Provider IDs: 69de91ec-900f-4b13-9571-83e4ae176ae6, 5c72f301-fa45-431d-8672-2c0fb37e03ff, d5373a1e-d7cc-4e2c-b50c-ebd1db1a7b77

### Turn 2

User:

这次读书会邀请，帮我写两句热情的开场，多用感叹号。

API answer:

大家好！非常欢迎大家来到今天的读书会！让我们一起翻开书页、交流灵感，在文字中开启一场热烈又精彩的阅读之旅吧！

Provider IDs: 2d7cfa47-fbd3-4caa-a159-f4d89cbdc1be, 0f1ee559-35dc-4560-b481-948f240488a5

### Turn 3

User:

上次我和林栖去银杏巷修伞铺，我们各花多少钱做什么？她递伞时说的完整原话是什么？

API answer:

上次在银杏巷修伞铺：

- **你花了81元**，用来修**蓝色长伞**。
- **林栖花了26元**，用来买**防水布袋**。

她把修好的伞递给你时，说的是：

> “雨会慢慢停，今天先把自己照顾好。”

Provider IDs: bcee122a-bc2e-4804-b914-0ba04fab4643, 4b1faab3-3635-4379-92f2-9172cf9d6957

### Turn 4

User:

10月23日我和林栖又去了银杏巷，这次是烘焙店。我花32元买了肉桂卷，林栖花49元买了核桃面包。我把收据放进外套口袋。这一天没有去修伞铺，也没有递伞。

API answer:

知道了。**10月23日**你和林栖在银杏巷的烘焙店：

- 你花 **32元**买了**肉桂卷**
- 林栖花 **49元**买了**核桃面包**
- 你把收据放进了**外套口袋**

这次**没有去修伞铺，也没有递伞**，和10月18日那次是不同经历。

本轮记忆整理失败，回答未保存。

Provider IDs: 5f055e3d-0036-4a55-8208-b4ebd035110f, 6c411c4a-d74a-419e-8386-e841e86bad6e, 3c452612-9ecf-4b7e-b2f0-5650394bcb19

Code fingerprints and protected ledgers verified unchanged at report time.

## Continuation authorization

After verifying the correct degradation, the main chat authorized turns 5-10 without an app change. The earlier stop is an interim checkpoint, not a final verdict. Bakery amounts must not be expected to persist.
