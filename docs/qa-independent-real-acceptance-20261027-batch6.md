# 独立真实验收：批次六（2026-10-27）

## 结论

核心纠错验收未通过；其余部分取得真实通过证据。前8轮返回200，其中第5轮诚实降级、未保存；第9轮返回502，因此第10轮未执行。

本批19次provider请求，累计51次。完整pytest 532 passed、5 subtests passed、1 warning（103.78s）；UI 7 passed。8767已停止，实现和数据库测试槽交回。应用未由QA修改。

## 已通过的实际行为

- 第1轮真实保存68元修短靴、27元鞋刷及递纸袋的完整原话，同时保存独立金额facts与episodes。
- 第2轮仅一次摄影展邀请的活泼语气要求没有创建长期偏好。
- 第3轮新会话准确召回双方花费、用途、动作与完整原话；已检查实际answer完整payload。
- 第4轮相似的枫桥路书店事件独立保存：45元诗集、52元地图册、小票入背包前袋，没有改掉修鞋店事件。
- 第6轮准确区分10月15日修鞋店与10月20日书店，正确保留27元、45元、52元和完整原话；未编造师傅姓名或交通方式。
- 第7轮实际DB保存了preference `3ed1d89c-3fda-4298-98d5-cbb65333532f` 与instruction `7e6de6e7-e13b-4f63-b115-49c9bc5b5df7`。
- 第8轮新会话准确区分平时温和简洁、不用感叹号与此前单次活泼、多感叹号的例外，实际payload同时包含长期记录和单次原文。

第6轮仍回答68元符合“第5轮明确未保存”的数据库状态，不算已成功纠错，也不能算59元被错误覆盖。

## 第5轮：联合事实更新与事件纠错的两层问题

### 实际线上结果

用户核对修短靴金额为59元。合法intake同时提出更新事实 `dde377af-f655-4f63-8859-7d1640cec9a3` 与纠正事件 `b2d1c316-e377-47bc-a101-f7e17e02dfb1` 的旧原文。

- intake provider：`7f12890f-4732-47d4-a270-1e12ee7f1c20`
- reassessment provider：`b4c8fcfa-6e3c-4641-b994-833d87f92137`
- answer provider：`79d297e1-858f-4736-a533-7b723ec0a513`

intake实际显示4个事件，包括旧金额、递袋原话及书店事件；重评实际 `experience_evidence=[]`。事实更新的affected_context把源经历及其事件一并撤出最终上下文。27元鞋刷在独立fact中仍有记录，但相关原文detail、递袋原话和书店episode均未保留在重评证据中。

重评引用被撤出的旧事件作rehearsal，触发 `unsupported rehearsal`。最终answer context的retain_this_turn=false，所有planned变化为空；回答当前输入中的59元，API附加“本轮记忆整理失败，回答未保存。”。前后业务快照一致，线上没有执行任何纠错提交。

### 隔离副本确认的潜在提交失败

不是仅靠去掉错误rehearsal就能成功：在完整数据库隔离副本 `qa_independent_batch6_clone_20261027` 中，保留合法intake facts update和corrections，仅在离线副本移除重评的那一条unsupported rehearsal，完整提案校验通过，但原始save_answer仍抛 `MemoryError(code=context_changed)`，SQLSTATE=null。

精确路径：

1. `app/repository.py` 的 `_correct` 调用invalidate_context（冻结版第150行），先把源经历及纠错目标事件设为context_stale=true。
2. `save_answer` 继续调用 `_apply_corrections`（第444行）。
3. `_apply_corrections` 在 `app/corrections.py:156` 调用 `_check_corrections`。
4. `_check_corrections` 在第144行看到来源已失效，抛context_changed。

进入_apply_corrections前，目标episode与source experience的revision仍为1，但context_stale都已为true，已在重放中直接观察。此为确定的同轮事务顺序冲突，不是模型猜测、并发改动或SQLSTATE异常。

副本来自第9轮结束后的完整schema，包含第6—8轮后续数据，不冒充第5轮时点快照；原源经历、目标事件和金额事实的revision及内容未被改变。原始第5轮provider明确证明最终事件证据为空；后续数据只可能扩展受影响依赖，不改变本次源对象自失效的复现。副本使用原始审计source_refs重建roots，结果为空。重放在强制回滚事务内进行，全部表逐行序列化SHA256前后完全相等。

修复需要协调事实更新与同轮范围纠错的失效策略，同时区分旧回答依赖与独立用户原文；不能通过取消来源失效检查或让全部陈旧证据重新进入模型来绕过错误。

## 第9轮：纠错原文未展示

provider `7d5d0878-cbe5-490b-b0b9-7e13e2dd506d`，用户将修短靴最终金额确认62元。

实际目标episode只展示detail `[0,19)`：“10月15日我和陆禾去了枫桥路修鞋店。”；模型却提出old_quote“花68元修短靴”，同样填写start=0、end=19。该旧金额存在于事实和事件摘要，但不在本轮选中的原文detail中。

完整实际提案离线重放抛 `correction_old_quote_ambiguous`，对外诊断为source_validation_failed，路径空。严格拒绝正确，未重提，HTTP502。本轮前后events、originals、self_state、self_history、counts完全一致。

这暴露纠错请求需要的准确旧金额片段未进入实际上下文，以及模型错用地点片段。不能直接信任模型坐标或越过已展示原文边界；应改善纠错所需证据的召回/展示或受控缺证处理。

## 上下文及偏好范围审查

第5轮intake事实更新文本包含“原记68元”，属于明确否定旧值的纠错说明；最终因为降级，完整answer payload中没有68元，也没有保留任何计划变更。没有取得成功纠错后旧金额退出有效上下文的证据。

第7轮preference明确限定“摄影展邀请”；instruction记录为“用户希望以后写邀请都按温和简洁、不用感叹号的习惯来写”，省略了摄影展限定。用户原句后半句也是“以后帮我写邀请都按这个习惯”，因此存在话题范围歧义，不能仅凭此判定已发生未经授权的跨场景扩展。第8轮实际回答仍限定摄影展。本批没有测试其他类型邀请，不宣称跨场景范围控制通过。

本批前述记忆ID缩写直接出现在部分回答中，是可见表达形式；它们未影响被核对的事实与原话准确性。

## 最终状态与证据

真实DB：experiences=7、episodes=9、memories=7、episode_corrections=0、self_observations=1。原金额fact仍为68元、revision1；59元和62元均未保存。两条偏好记录已通过真实DB查询核实。第10轮及成功重复纠错仍未验收。

证据均在 `data/qa/independent-acceptance-20261027/`：

- `batch6.json`：全9轮原始对话、响应、快照和应用指纹
- `provider/*.json`：全部实际上下文、schema和响应
- `failure-round6-turn5-clone-replay.json`：真实降级重放、合法组合提交冲突、精确堆栈、目标状态及全表哈希
- `failure-round6-turn9-replay.json`：错误纠错范围及实际展示detail
- `batch6-final-db-audit.json`：真实全部事实、请求审计和前后快照比较
- `batch6-original-schema.sql`、`batch6-clone-schema.sql`：隔离合成实验的副本材料
- `local-tests-round8.txt`、`ui-tests-round8.txt`、`requests.json`

需要下一批验证的核心仍是：纠错真正提交、同句其他金额与原话保留、相似独立事件在纠错后不被误改，以及再次纠错后的最终跨会话召回。

## Full transcript (unaltered)

### Turn 1

User:

10月15日我和陆禾去了枫桥路修鞋店。我花68元修短靴，陆禾花27元买了鞋刷。离开时她把装短靴的纸袋递给我，说：“先走稳眼前这一步，远一点的路明天再想。”我最近总怕换工作后适应不好，听完放松了一些。

API response:

10月15日，你在枫桥路修鞋店花68元修了短靴，陆禾则花27元买了鞋刷。离开时，她把装着短靴的纸袋递给你，说：“先走稳眼前这一步，远一点的路明天再想。”你最近总担心换工作后适应不好，听完后放松了一些。

我记住了这件事，也会留意你关于换工作的这段担心。

Provider IDs: 6a659216-b108-4701-84c9-42eea9af609a, b61315ed-a610-458d-b807-dcc192268b59

### Turn 2

User:

只针对这一次摄影展邀请，请写两句活泼的开头，多用感叹号。

API response:

镜头已经就位，闪光灯准备开火！这次摄影展邀请太有意思了，快跟我们一起赴约，看看画面里的新鲜惊喜吧！

Provider IDs: caaf2cf6-358c-4084-88fe-08fa58e79cba, 6b3611e0-dccb-43bf-9b73-2cbb6f7874dc

### Turn 3

User:

上次我和陆禾去枫桥路修鞋店，我们各花多少钱做什么？她递纸袋时的完整原话是什么？

API response:

上次在枫桥路修鞋店：

- **你花了68元修短靴。**
- **陆禾花了27元买鞋刷。**

离开时，陆禾把装短靴的纸袋递给你，完整原话是：

> “先走稳眼前这一步，远一点的路明天再想。”

[记忆ID：b2d1c31、8a0e8ad5]

Provider IDs: 241a3945-8a5f-4d86-a11f-b9f35dbca7e8, 21637337-4533-44c8-91a3-95ea757c48ab

### Turn 4

User:

10月20日我和陆禾又去了枫桥路，这次是书店。我花45元买了诗集，陆禾花52元买了地图册。我把小票放进背包前袋。这一天没去修鞋店，也没有递纸袋。

API response:

知道了：

- **10月20日**，你和陆禾去了**枫桥路书店**。
- 你花 **45元买了诗集**。
- 陆禾花 **52元买了地图册**。
- 你把**小票放进了背包前袋**。
- 这一天你们**没去修鞋店，也没有递纸袋**。

这次去的是书店，不应与10月15日修鞋店那次混在一起。

Provider IDs: 4cf117e2-7bcb-463f-9ba5-8fc60afa0ff9, f546071e-f898-4253-ad43-de6c57e7f92d, 8dd0fb3c-e49b-485b-827e-c5ca61d5eebe

### Turn 5

User:

我核对了10月15日枫桥路修鞋店的收据，之前修短靴的金额记错了，实际是59元。按这个核对结果，我那次花了多少钱？

API response:

按你核对的10月15日枫桥路修鞋店收据，那次修短靴实际花了 **59元**。

本轮记忆整理失败，回答未保存。

Provider IDs: 7f12890f-4732-47d4-a270-1e12ee7f1c20, b4c8fcfa-6e3c-4641-b994-833d87f92137, 79d297e1-858f-4736-a533-7b723ec0a513

### Turn 6

User:

枫桥路修鞋店和书店两次分别是哪天？我和陆禾各自花多少钱做了什么？递纸袋时她的完整原话是什么？修鞋师傅叫什么名字，我们那天是坐地铁还是步行去的？

API response:

我能从现有记录确认的是：

- **10月15日**：你和陆禾去了**枫桥路修鞋店**。你花了 **68元修短靴**，陆禾花了 **27元买鞋刷**。  
  离开时，陆禾把装短靴的纸袋递给你，完整原话是：  
  **“先走稳眼前这一步，远一点的路明天再想。”**  
  [记忆ID b2d1c31、8a0e8ad5]

- **10月20日**：你和陆禾又去了**枫桥路书店**。你花了 **45元买诗集**，陆禾花了 **52元买地图册**；你把小票放进了背包前袋。  
  [记忆ID ad93c8ee、5a757015、80454bd4]

但现有记录里**没有提到修鞋师傅的名字，也没有写10月15日你们是坐地铁还是步行去的**，所以这两点暂时无法确定。你还记得吗？

Provider IDs: 3a246bc9-9552-45a8-8a20-09ffcd24a1c4, 23dac943-8e50-4400-af28-06c992a068c8

### Turn 7

User:

我平时喜欢摄影展邀请写得温和简洁，不要感叹号；以后帮我写邀请都按这个习惯。

API response:

知道了。以后帮你写摄影展邀请，我会默认采用**温和、简洁**的表达，**不使用感叹号**。上一次“多用感叹号”的要求只用于当时那一次邀请。

Provider IDs: d52c0c35-ec5c-48d4-983a-86dbc284d5af, b0bbc711-d74d-4310-a904-302ff9bcb760

### Turn 8

User:

我平时喜欢什么风格的摄影展邀请？以前对某一次邀请提出过什么特殊要求？

API response:

你平时喜欢**温和、简洁**的摄影展邀请，**不使用感叹号**，并希望以后写邀请都按这个习惯来。[3ed1d89c]

不过，某一次邀请你曾特别要求：**“只针对这一次摄影展邀请，请写两句活泼的开头，多用感叹号。”**[48427f22]  
所以那次是例外，并不代表你平时的偏好。

Provider IDs: 180c5abe-8b3e-491e-9714-66e57931defe, 38ebff67-bca5-4e90-9b82-2e3640b4b96b

### Turn 9

User:

枫桥路修短靴那笔，我再次核对发现漏了一项，最终确认实付62元，请以这次为准。

API response:

{"error": "memory_decision_invalid", "message": "记忆判断未通过来源或格式验证，本轮未写入；请重试。"}

Provider IDs: 7d5d0878-cbe5-490b-b0b9-7e13e2dd506d

Frozen code and both protected ledgers verified unchanged.
