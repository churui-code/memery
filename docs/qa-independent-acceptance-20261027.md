# 独立验收：本地回归及云协议阻塞（2026-10-27）

**本轮未通过，真实验收暂缓。** 原有及修复回归合计 516 项、另 5 个子测试通过；随后按主修复对话提出的边界追加三个独立 MiniMax 适配器协议测试，三项均失败。缺少 `old_quote` 确实可提交宽范围纠错，已由隔离数据库结果确认；不能以之前的完整回归通过代替该边界验收。

真实请求使用 **0/60，剩余60**，新账本尚未创建。没有启动临时 HTTP 服务、没有触碰旧10轮实验、没有部署或升级8765/8766。所有本地数据库测试串行运行，测试 schema 由 fixture 创建并清理；测试槽现已释放给主修复对话。

## 已执行检查

完整回归使用已有依赖镜像和只读工作区挂载，未重建镜像：

```text
docker compose run --rm --no-deps -T -v /Users/churui/Project/memory:/app:ro tests python -m pytest -q tests
516 passed, 2 warnings, 5 subtests passed in 94.47s
```

警告为现有 Starlette 测试客户端依赖弃用提示，以及只读挂载无法写入 pytest cache；无测试失败。日志：`data/qa/independent-acceptance-20261027/local-tests.txt`。

追加测试使用真实 `MiniMax.decide_memories/_structured/reassess_memories/chat` 与真实 `MemoryService`、Repository，只把 `_request` 替换成合成提供商响应，避免任何云调用。它验证云协议入口与事务后果，不评价真实模型语义。原故事由已有 FakeModel 在隔离 schema 中创建；测试未直接插入纠错关系。

```text
python -m pytest -q tests/test_qa_independent_repairs.py -k live_adapter -p no:cacheprovider
3 failed, 5 deselected
```

确认日志：`data/qa/independent-acceptance-20261027/protocol-tests-confirmed.txt`。测试最初把重评工具名写错，首份 `protocol-tests.txt` 属测试装置错误，不用作产品缺陷证据；修正名称后的 `protocol-tests-corrected-harness.txt` 和增加数据库诊断后的 confirmed 日志一致复现以下三个协议绕过。最终测试还加入了拒绝后不能进入重评、不能新增经历/纠错关系的断言，交由下一轮验证修复效果。

## P1：真实模型返回边界未落实云引用协议

位置：`app/model.py:178–185`（只检查工具名、stop_reason 和 input 为字典）；`app/corrections.py:18–19`（本地兼容允许 old_quote 缺失）；`app/automatic.py:188–220`（随后按本地兼容协议验证）。

初始合成原文：`红榆巷纸品店那次，我花31元买了笔记本，顾禾花72元买了信封。`

纠正输入：`红榆巷纸品店那次笔记本金额说错了，实际是13元。`

| 提供商违反的云协议 | 实际服务结果 | 数据库后果 |
| --- | --- | --- |
| correction 缺少云 schema 必填 old_quote，仍给完整旧 detail 范围 | `history_saved=true`、`memory_status=committed` | 插入 `episode_corrections(start_offset=0,end_offset=31)`，即完整旧段；有效事件不再含未纠正的72元；本地原文仍完整 |
| episode 同时提供合法一致的 quote 和 quote_refs（云schema不允许quote） | `history_saved=true`、`memory_status=unchanged` | 本轮经历保存，云协议没有拒绝该双引用 |
| episode 仅提供本地旧 quote，漏掉云 schema 必填 quote_refs | `history_saved=true`、`memory_status=unchanged` | 本轮经历保存，云协议没有拒绝缺必填来源字段 |

这里的后两项没有伪造来源，也没有导致宽范围纠错；它们证明云端输出的“只用必填quote_refs”尚未形成独立的服务端边界。第一项具有实际数据影响：合法旧兼容路径把缺漏的子串定位退化为整段遮罩，误删另一笔本可安全保留的金额。

最小修复建议：在真实 MiniMax 返回解析边界验证对应云工具的引用字段契约，明确拒绝漏掉 old_quote、缺少 quote_refs、以及同时返回 quote/occurrence 的输出；错误进入现有有限重试并保留失败调用计数。直接调用本地 `validate_decision` 的旧 quote 兼容保持原意。修复验收应证明第一次非法、第二次合法可成功且最多三次总请求；连续非法停止且无经历/纠错写入；原始用户引用不合法仍不能因重试获得写权限。

## 保守降级用例的判定

已按既定文档边界修正无标点“分别花……”用例。无法保留主体对应关系的句子应整体退出有效上下文，原文保持；不要求把83元残片拼接成新quote。这个断言更正已单独记录在 `docs/qa-independent-recheck-20261027-round3.md`。旧 detail 解释和英文句号两个确定缺陷的修复，已通过本次完整回归收集的独立测试。

## 指纹及账本完整性

完整代码基线保存于 `data/qa/independent-acceptance-20261027/baseline.json`，包括36个Python/SQL文件 SHA-256。Git HEAD为 `3fa240a3ef0fa9cbb3b9df3b3abbe752556316e4`，工作区有未提交改动。测试结束核对所有记录的 app 文件指纹均未变化。

| 文件 | SHA-256 |
| --- | --- |
| `app/corrections.py` | `213ad7cb4a45d111b7b4644cffa9ae5dcbfabb9f3decb275f9f4692ff05dc52b` |
| `app/service.py` | `e0344ab8128d3248f0ed5fd51dbdb4d1841a19fa08868813c8c2063898ec73ae` |
| `app/model.py` | `21edf25a98ea7250597c20bbe3b38a70deacbb5fc1c96c961643460e5f06e524` |

`data/qa/reflection-v0.5-budget/ledger.json`（原冻结238/238）与 `data/qa/chat-repairs-20261027/requests.json`（此前20/30）的SHA-256前后一致。没有清空、增加或绕过旧账本。

当前阻塞为真实模型协议边界；真实跨会话、连续纠错、当次/长期偏好、未知细节及主体动作忠实度尚未复验。收到修复后的代码就绪交接后，先复验这三个协议测试，再启动新故事的第一组约30次以内真实请求。
