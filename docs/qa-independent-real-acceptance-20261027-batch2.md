# 独立真实验收：批次二（2026-10-27）

## 结论

未通过。动态事实工具修复后的全套本地回归通过，但新空白实验第一轮返回 HTTP502 `memory_decision_invalid`。本批只发出1次真实请求，总累计12次；没有最终回答、没有自动重提。后续轮次已停止，8767临时容器已停止，交回实现和数据库测试槽。未修改应用实现。

## 已验证的范围

- 完整 pytest：523 passed、5 subtests passed、1 warning，94.75s。
- UI Node：7 passed。
- 全新实验：`997f81e9-3243-4346-a224-0946bc3a7aac`，reflection=off，MiniMax-M3，thinking disabled，timeout90、deadline240、context24000。
- 初始 experiences/episodes/memories 均为0。开始时应用代码指纹保存在 `batch2.json`；报告生成前逐文件复核无变化。
- 新故事为“青禾街乐器店与咖啡馆”，与上一批隔离。

## 完整用户输入与实际结果

10月19日我和许澄去了青禾街乐器店。我花72元修节拍器，许澄花35元买了鼓槌。出门时他把装节拍器的纸盒交给我，说：“慢半拍也没关系，我们还在同一首歌里。”我最近练习总怕跟不上，那一刻不再那么着急了。

实际响应：

```json
{
  "status": 502,
  "body": {
    "error": "memory_decision_invalid",
    "message": "记忆判断未通过来源或格式验证，本轮未写入；请重试。"
  }
}
```

## 根因与离线最小复现

provider请求 `89715501-52e0-4391-9a20-c1c45f3ff514` 通过实际云工具 schema 校验，`actions=[]`，没有上一批的事实目标混用。

模型把原文拆成两个事件：第一个事件范围为 `u0001,u0002`（去乐器店及两笔花费），第二个事件范围为 `u0003,u0004`（交纸盒、完整话语及情绪）。然而第一个事件的两条 cue 引用了 `u0003`：

```json
[
  {
    "kind": "person",
    "quote_refs": [
      "u0001",
      "u0002",
      "u0003"
    ],
    "text": "许澄"
  },
  {
    "kind": "object",
    "quote_refs": [
      "u0002",
      "u0003"
    ],
    "text": "节拍器"
  }
]
```

完整实际提案经 `validate_decision` 离线重放，报 `episode_detail_outside_event_scope`。诊断路径显示 `episodes/details`，但实际越界来自 cues（两类共享范围校验）。这是明确的原文引用范围错误，严格拒绝正确；对用户表现为正常叙述无法保存和回答。

仅在离线副本中将第一事件两条 cue 的 quote_refs 限定回该事件已有范围后，整个提案通过校验。没有调用模型、没有写数据库；此操作用于定位，不建议线上静默修改证据范围。

当前 intake 的受控重提仅覆盖Pydantic错误与quote/occurrence冲突，未覆盖已有诊断的事件子引用越界。因此这次在首次失败即终止，实际只发1次请求。建议以既有结构化诊断请求模型在原每轮预算内重提，并明确父事件/子detail、cue引用必须相容；继续保留全部来源与范围校验。测试应覆盖跨两个事件的cue越界，而非仅detail越界。

## 事务、请求与保护验证

- DB audit `0c82bcd3-bb6f-4a3c-89b4-e31897cdc7ea`：intake，memory_decision_invalid，input_tokens=5340、output_tokens=2070。
- 失败后 experiences=0、episodes=0、memories=0、episode_corrections=0。
- 新累计账本12次，limit=null，原授权记录与前11次全部保留。旧238/238及旧20/30账本SHA256仍与baseline一致。
- 临时容器 `memory-independent-qa-20261027` 已停止，原数据库和日志保留。

## 证据与未验范围

证据位于 `data/qa/independent-acceptance-20261027/`：

- `local-tests-round4.txt`、`ui-tests-round4.txt`
- `batch2.json`：完整对话、前后快照、计划与代码指纹
- `provider/89715501-52e0-4391-9a20-c1c45f3ff514.json`：完整实际system/payload/schema/proposal
- `failure-round2-replay.json`：离线原始失败、限定cue副本通过、DB审计
- `requests.json`：持续累计12次

第2—10轮未执行。动态事实工具对真实首次纠错的改善尚未验证；同句细节保留、后续召回、持久偏好及未知事实诚实降级也未宣告通过。不能以本地523项通过代替上述真实行为验收。
