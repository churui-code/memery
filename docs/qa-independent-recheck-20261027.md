# 独立修复复查，第 2 轮（2026-10-27）

**结论：仍有阻塞项，暂不能通过。** 本轮完成六项改动的静态复查和四个无数据库、无模型的函数检查。检查结果为 1 通过、3 失败；新增独立回归文件为 `tests/test_qa_independent_repairs.py`，其中三个断言会在当前候选失败，不能按现有全绿结论忽略。第一轮报告及原真实失败记录保持原样。

新真实预算使用 **0/60，剩余 60**。尚未创建新请求账本、未启动数据库测试、未调用云模型、未启动或升级 8765/8766、未启用反思。等待主对话交接数据库测试占用。

## 代码指纹

Git 基线仍为 `3fa240a3`，以下 SHA-256 对应工作区候选，并非干净提交：

| 文件 | SHA-256 |
| --- | --- |
| `app/corrections.py` | `8914ab57f48bbe7d2475f3bfcd6048310c25d7c4643ee459a21df753f5aed5b8` |
| `app/service.py` | `d4994e0cbb4b91807758b94e9471aa264b067ec15b807ad56e0aa56f1ab2329e` |
| `app/model.py` | `21edf25a98ea7250597c20bbe3b38a70deacbb5fc1c96c961643460e5f06e524` |
| `app/automatic.py` | `907623fcd93484d41da4106f51d8ecf53fc59f89ba1930d0b5268491202fd3a6` |
| `app/episode_repository.py` | `5197c8e5e82ed625240b5bfcb8054021765fe39bc456f59331fff4ca13eac7e6` |
| `app/recall.py` | `a5db5166903fdffc5d73efcead11c763d3e22eda9b58daa9d56301d5f6149922` |
| `app/context.py` | `f9667aa2257121914731206494fdc1188295eab89938741046a56b45d9c46ba8` |

## 确定缺陷

### P1：未重叠 detail 的解释和理由仍把旧金额送入模型 payload

位置：`app/corrections.py:59–61,71–79`；`app/recall.py:158–174,181–192`；`app/service.py:107–120`。

合成输入：`修琴花了29元。宁秋说：“声音亮起来了。”`。已保存 speech detail 的逐字 quote 是 `宁秋说：“声音亮起来了。”`，meaning/importance_reason/emotional_reason 是 `宁秋肯定这次29元修琴的效果`。这些字段满足当前提案结构与来源校验：quote 是连续原文，解释可以引用事件背景。

纠正 `修琴花了29元。` 后，speech quote 与遮罩不重叠，`safe_parts()` 原样返回 detail。只有被拆分且失去 id 的 detail 才清除解释字段；因此该 detail 的三个解释字段都留下旧 29 元。`present_episode()` 选择并深拷贝完整 detail，`ContextComposer` 把它加入 `experience_evidence`；`_wire()` 不会删除这些字段。

独立检查实际组装出的 payload 保留：

```json
{"quote":"宁秋说：“声音亮起来了。”","meaning":"宁秋肯定这次29元修琴的效果","importance_reason":"宁秋肯定这次29元修琴的效果","emotional_reason":"宁秋肯定这次29元修琴的效果"}
```

这是实际进入 payload 的路径，与上一轮已撤回的 `matched_cues` 泄漏判断不同。最小修复是对受纠错影响事件的所有保留 detail 重建或清除解释性文字，而不仅处理被切分的 detail；也需检查 cue.text 等派生字段是否存在相同背景依赖。保留逐字 quote 与来源坐标。验收须覆盖三类解释字段，并核对新会话 intake/reassessment/answer 全文中不再出现旧的错误断言；当前纠正输入明确否定旧金额的文字不应被误计为泄漏。

### P1：无分隔符的并列事实仍被整句丢弃

位置：`app/corrections.py:63–69`。

合成输入：`我和宁秋分别花29元和83元买了琴弦与琴谱。`，整个句子为一个合法 detail。只把用户 29 元纠正为 92 元，`old_quote=29元`，当前纠正不重述宁秋的金额。唯一旧子串定位成功，但分句过滤找不到安全边界，整句被删除，83 元及其主体/物件对应关系也退出有效证据。

目前修复确实解决了逗号分隔的例子，但不能据此宣布“同句未纠正细节已保留”。原始本地档案仍在；丢失的是后续可用的有效证据。最小且诚实的处理是把无法拆分明确标为未满足的保留场景，或增加有来源的字段级重建/核对机制；不能从 `meaning` 猜出保留事实，也不能把整个旧句重新放进模型以回避过滤。验收需要新会话正确回答宁秋 83 元且可追溯来源，同时旧的用户 29 元断言不进入模型。

### P2：过滤器不识别英文句号，误删独立的后一句

位置：`app/corrections.py:64`。来源单元生成器支持英文句号，但这里的分句正则没有英文句号边界。

合成输入：`I spent 29 yuan. Ningqiu spent 83 yuan.`，两个句子属于同一合法 detail。纠正第一句时，第二句也被移除。此场景可由事件覆盖多个单元或本地回退形成，不需要非法提案。

最小修复：使用与 `source_units()` 一致的英文句尾规则，再在句内处理安全的中文/英文分句边界；不能简单按所有 `.` 切分小数。验收保留完整的 `Ningqiu spent 83 yuan.`，补上金额小数与引号边界回归。

## 六项修复的核对结果

| 改动 | 静态结论及剩余验证 |
| --- | --- |
| 必填 quote_refs、一次带诊断重试 | 云工具 schema 已收窄，原本地 quote 兼容保留；只有来源通过才写入。`len(calls)` 预留 answer，传输重试和结构重试合计不能超过三次；不足重评时清空写入计划。需要隔离服务确认实际状态、用量和第二次无效提案的行为。 |
| old_quote 定位与分句保留 | 唯一子串定位与最终 planned payload 排除 old_quote 已实现；逗号示例通过。上述三个边界未通过。 |
| 重叠区间拒绝 | 校验阶段与事务检查都按同源区间重叠拒绝，相邻区间允许。尚未运行数据库事务回归。 |
| matched_cues 过滤 | SQL 已排除与已纠正范围重叠的 cue；第一轮候选恢复问题的主要路径已修。频率子查询仍统计已纠正 cue，可能使另一有效 cue 的 frequency 偏高、distinctive_cue 失效；需隔离数据库验证评分影响。这不是旧 quote 直接进入 payload 的证据。 |
| 最近两条用户历史补充检索 | `_inputs()` 使用默认 cloud_only=True 的 history，辅助 query 仅来自该允许列表，最多两条、每条末 1000 字，仍受四次本地扩展上限；未发现因此新增云请求或读取私有历史的路径。指代命中效果待真实测试。 |
| current_speaker 与回答忠实度 | wire 中加入 current_speaker=user，系统提示明确主体/动作约束；属于模型语义改进，不能用静态存在替代真实验收。 |

另一个需要明确的边界：`model.py` 云 schema 把 old_quote 设为必填，但 `_structured()` 仅检查工具名及 input 是字典；`EpisodeCorrection.old_quote` 在本地又允许省略。提供商若仍漏填 old_quote，就会走兼容路径按完整 detail 纠错，重现同句过度遮罩。考虑在真实模型返回边界单独执行云协议的必填检查并触发受预算限制的重试，同时继续允许直接本地旧协议输入。此项由代码路径确认，尚未执行真实/完整服务复现。

## 本轮检查方法与限制

宿主 Python 未安装 pydantic。为遵守数据库尚由主对话占用的要求，本轮用标准库 AST 加载 `app/corrections.py` 中**未修改的** `overlaps`、`sanitize_episode` 函数，配合正常导入的 `app.recall.present_episode` 与 `app.context.ContextComposer` 执行独立断言；没有替换过滤逻辑或模拟模型语义。

`tests/test_qa_independent_repairs.py` 使用真实模块导入，待交接后可在现有依赖环境正式运行。当前四项函数检查：解释字段旧金额失败、无分隔并列金额失败、英文后一句失败、逗号分隔另一人的完整分句通过。这些结果不能代表数据库事务、HTTP、真实召回或模型回答已通过。
