# 家庭 AI 记忆设计：三位子 agent 评审记录

日期：2026-10-23。主持人为主 agent。按公司职责分配三位子 agent，完成独立研究及第二轮交叉评审。以下是各方观点与主持裁决的整理，不是逐字聊天记录。

主方案：[memory-design-v0.1.md](./memory-design-v0.1.md)。本文件记录研究阶段的讨论；后续 v0.1 已开发部署，实际能力与范围见 [README](../README.md)，验收见 [记录](verification.json)。

**最新设计修订（同日，按用户反馈）：同一个家庭管家共享一套记忆；未来声纹用于识别说话人，不切换个人记忆空间。能说什么由 AI 结合人物、在场者、场景和明确嘱咐判断。第一版仍只服务本人，不建设多人账户、声纹、访客模式或成员级记忆 ACL，也不保留用于个人分库的 owner_id。换房间由统一服务承接。以下保留原讨论过程，原有 owner/unknown、成员空间切换与记忆隔离裁决已被替代，以主方案为准。**

## 1. 职责与讨论产出

| 职责 | 子 agent | 主责 | 对方案产生的实际修改 |
|---|---|---|---|
| 后端/数据架构负责人 | `memory_architect` | 存储、数据契约、并发、多端一致性、技术选型 | PG 事实主库；投影可重建；outbox；Hindsight 作为可选适配器；补未索引原话读取 |
| AI 算法/记忆策略负责人 | `memory_cognition` | 记什么、怎样更新、推断/事实、检索与评估 | 增显式原话；分开保存/理解/索引状态；区别纠错与变化；人格受控；中文连续用例 |
| 安全与可靠性负责人 | `memory_reliability` | 家庭身份、隐私、出网、删除、备份恢复与故障 | 独立删除账本；在途结果隔离；单人版也保留 unknown；本地处理与保存分开 |

主持工作：明确单人本地保存、按需云端的约束；要求三方挑战自研负担、现成框架默认行为和家庭专属风险；收敛第一阶段的实施边界与验收标准。

## 2. 第一轮共识

- 长期记忆应独立于具体 LLM，能导出、纠错、删除、更换模型。
- 全屋共享一个记忆服务；人物身份、房间位置、终端、会话分别标识。
- 原始事件、明确事实、情节摘要、模型推断有不同语义；摘要不是新的独立证据。
- 当前事实和历史版本同时存在，有效时间与系统记录时间分开。
- 实时设备状态与执行授权独立于记忆。
- 本地存储优先；向量和摘要是可重新生成的派生结构，但不能越过删除和保留政策重建。

## 3. 第二轮分歧及裁决

| 议题 | 争论与证据 | 主持裁决 |
|---|---|---|
| 全部自研还是直接接框架 | 算法方认为全自研会把项目拖入抽取、时间、冲突和检索基础设施；架构方认为仅接框架难满足可靠回执和生命周期契约 | 自有小而严谨的数据与行为契约；复杂抽取/检索可借组件；Hindsight 首先对照，收益不足就不用 |
| “记住”是否立即结构化 | 初案希望显式保存后按结构化字段立即可读；两位负责人指出复杂表达可能被错误压成三元组 | 原话可独立保存与检索；可靠解析才添断言；持久化成功、理解成功、索引完成分开 |
| 能否以引擎成功响应说记住了 | Hindsight 文档描述 retain 成功但生成零条 memory 的正常情况，原 document 不一定能由 recall 找到 | 本地提交与基础读取作为回执依据；同时承认 Hindsight 有 verbatim/chunks 模式，可缓解原文检索问题，不据此否定其全部能力 |
| 中文是否开箱可用 | Hindsight 生成中文不等于默认 embedding/reranker/全文都适合中文；官方当前默认含英语组件 | 显式配置中文或多语组件与分词；评测记录配置，不照搬英文跑分 |
| 是否立刻上知识图谱 | Graphiti 的时效和关系检索有价值；架构/算法方认为初期关系表足够，安全方关注额外维护面 | 先做时间、来源和关系语义；独立图数据库由真实查询收益触发 |
| 事件不可变与删除冲突 | 架构希望可重放；可靠性方反对无限保存家庭对话 | 正常更新留版本，隐私删除与到期可清理/脱敏；删除后重建遵守账本；审计默认无正文 |
| 单人是否无需访问隔离 | 原评审考虑未来家庭音箱可能被访客/电视触发 | 按用户最新要求：统一管家记忆，只区分说话人和内容涉及的人物，不建设成员级记忆 ACL；个人入口保留基本访问验证 |
| 需不需要立刻做复杂认证 | 安全方强调共享音箱不证明主人身份；算法方强调第一步范围 | 首版文字端；未来声纹更新说话人与场景，仍访问同一套记忆，AI 判断如何回答；模型判断效果另行验证 |
| 删除是否只清数据库 | 缓存、摘要、排队任务、慢云请求、旧备份都可能重新引入 | 同事务撤销可见性与 epoch；清理派生副本；独立删除账本；隔离恢复后再开放 |
| 评估能否选最高分方案 | 模型、数据、预算、版本和评分器不同 | 同一中文家庭用例、同一模型与预算做对照；公开 benchmark 用于能力覆盖，不能直接排名 |

## 4. 技术方案比较

下面“能力”来自所链接一手资料；“本项目选择”是工程判断。官方文档会变化，实施时应固定 release/commit、镜像 digest、模型与配置，复核行为。未实测的效率、准确率与成本没有作为结论。

| 路线 | 核验到的能力与定位 | 本项目选择 |
|---|---|---|
| PostgreSQL + pgvector | 关系事务、访问控制与向量检索可在本地同库管理；向量精确/近似模式可选择 | 确定底座；它不会自动解决事实抽取和记忆语义 |
| Hindsight | retain/recall/reflect；事实/经历/归纳；关键词、向量、关系与时间检索；提供 PostgreSQL 存储路线 | 首个候选适配器；补中文配置和自有契约；有收益才保留 |
| Mem0 OSS | 当前官方支持库和自托管 server 两种方式；模型、embedder、vector store、reranker 可配置 | 备选记忆整理组件；部署形态与 API 代际分别验证 |
| Graphiti / Zep | Graphiti 为开源时态图框架，需要自备图后端及周边服务；Zep 是相关商业平台 | 借鉴双时间、事实失效和来源；复杂关系查询验证后再评估图后端 |
| Letta | 本次当前 Agent SDK 文档为 git-backed MemFS、常驻 system/ 与后台 dreaming；另有 legacy V1 blocks 文档 | 借鉴版本化、少量常驻和后台整理；第一步不引入完整 agent runtime |
| LangMem | 语义/情节/程序性记忆概念，前台与后台写入，核心转换与存储可分离 | 可用于候选抽取/合并；提交和授权仍由自有规则处理 |
| MemOS | 当前项目分别提供 cloud、自托管服务与特定 agent 的本地插件；完整自托管与 SQLite 本地插件依赖不同 | 列入跟踪；需选定具体形态，不能将“本地插件”的属性套给整个产品 |

直接依据：[pgvector](https://github.com/pgvector/pgvector)、[Hindsight 项目](https://github.com/vectorize-io/hindsight)、[Mem0 OSS](https://docs.mem0.ai/open-source/overview)、[Graphiti](https://github.com/getzep/graphiti)、[Letta Agent SDK Memory](https://docs.letta.com/agent-sdk/memory)、[LangMem](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)、[MemOS](https://github.com/MemTensor/MemOS)。

## 5. 关键资料、日期与实际借鉴

### 5.1 记忆结构与工程

1. [MemGPT 原论文，2023](https://arxiv.org/abs/2310.08560)：借鉴上下文与外部存储分层，不把整个生活历史塞进每次 prompt。属于思想来源，不代表当前 Letta 产品固定实现。
2. [LangMem Core Concepts](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)：借鉴事实、经历和程序性记忆，以及交互时/后台形成记忆的两条路径。
3. [Graphiti 官方项目](https://github.com/getzep/graphiti)：借鉴有效时间、记录时间、历史失效和来源；图后端和服务周边由部署者承担。项目还明确参考 MCP/REST server 不是完整生产服务。
4. [Hindsight 原论文，2025-12](https://arxiv.org/abs/2512.12818)：借鉴证据、经历和归纳信念分开，以及 retain/recall/reflect 的职责区分。论文指标只代表作者实验。
5. [Hindsight 存储](https://hindsight.vectorize.io/developer/storage)：核对其 PostgreSQL 路线，说明采用它不必额外部署专用图数据库。
6. [Hindsight 多语言配置](https://hindsight.vectorize.io/developer/multilingual)：当前默认英语 embedding/reranker 和 native 英语全文会影响中文检索；PGroonga/pg_search 要求带相应扩展的独立 PostgreSQL，默认嵌入式 pg0 不包含它们。
7. [Hindsight Retain](https://hindsight.vectorize.io/developer/retain)：抽取零条是可能的成功结果；原 document 与可被 recall 搜到的 memory 需要区分。文档也提供 verbatim/chunks 模式。
8. [Letta 当前 Memory](https://docs.letta.com/agent-sdk/memory)：MemFS 用 git 管理，system/ 内容常驻，其他按需读取；dreaming 后台整理。旧 blocks 路线在另一版本导航中。
9. [Mem0 OSS](https://docs.mem0.ai/open-source/overview)：确认可本地部署与可配置组件。旧教程中的固定后端、graph 开关、自动更新流程不能直接推定适用于所有当前 API。
10. [MemOS 官方项目](https://github.com/MemTensor/MemOS)：核对当前自托管 Neo4j/Qdrant 路线与 SQLite 本地插件的区别；不采用供应商自报排名作为裁决。

### 5.2 最新评估方法

| 原始资料 | 实际作用 | 限制 |
|---|---|---|
| [LoCoMo，ACL 2024](https://aclanthology.org/2024.acl-long.747/) | 长对话、跨会话和事件回忆的基础参考 | 不等于中文家庭语音或设备可靠性认证 |
| [LongMemEval，2024/ICLR 2025](https://arxiv.org/abs/2410.10813) | 覆盖抽取、跨会话、时间、更新和应当不知道 | 不直接覆盖本项目全部删除、身份及恢复要求 |
| [LoCoMo-Plus，2026-02](https://arxiv.org/abs/2602.10715) | 提醒评估没有被直接询问、却应影响回答的隐含约束 | 本项目仍需构造自己的中文场景 |
| [LongMemEval-V2，2026-05](https://arxiv.org/abs/2605.12493) | 参考动态状态、工作流程、环境经验与前提意识 | 面向 web agent；论文标注 Work in Progress，不能当家庭控制效果证明 |
| [EvalMem，2026-09](https://arxiv.org/abs/2609.22231) | 将错误定位到编码、检索和生成，便于知道该修哪层 | 诊断方法可借鉴，作者实验分数不能直接推到本机 |

### 5.3 家庭入口、隔离与恢复

- [Home Assistant 语音架构](https://contributors.home-assistant.io/architecture/platform/voice/)：本地/云端各阶段可分别选择；后续全屋入口可借此对接。
- [Home Assistant 本地语音](https://www.home-assistant.io/voice_control/voice_remote_local_assistant)：语音请求自身不带可靠用户身份；“以某用户运行”不等于认证说话人。
- [Home Assistant 实体状态](https://contributors.home-assistant.io/core/internals/states/)：当前状态及 unknown/unavailable 从状态系统读取，不能从记忆回推。
- [PostgreSQL RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)：表拥有者通常能绕过，超级用户/BYPASSRLS 角色能绕过；正确角色与应用授权同样必要。
- [PostgreSQL WAL 可靠性](https://www.postgresql.org/docs/current/wal-reliability.html)、[连续归档恢复](https://www.postgresql.org/docs/current/continuous-archiving.html)：数据库设置、存储硬件和备份共同决定恢复能力；日备份不等于无数据损失。
- [OWASP Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)：检索到的内容可能含恶意指令；RAG 本身不消除注入，需要服务端边界。

## 6. 保留的反对意见

**反对意见 A：这些表和生命周期对单人聊天太复杂。** 若只做演示，直接调用框架会更快。但当前目标包括长期居家、多端与未来设备控制，来源、删除、回执和身份属于早期必须确定的数据契约。裁决是缩小实现规模，而不是省去这些语义。

**反对意见 B：自有检索与 Hindsight 重复存储。** 成立。它增加依赖、资源、删除工作和升级成本。因此本方案只确定自有底座，将 Hindsight 作为候选；中文对照收益不足就保持基础检索。

**反对意见 C：限制推断会让她显得不够主动。** 部分成立。可以主动提出“我发现你最近常把灯调暗，要记作偏好吗”，保留探索与陪伴能力；明确事实、人格和控制规则仍采用受控变更。

**反对意见 D：先存全部音视频，以后再处理最完整。** 不采纳。原始采集、长期记忆价值与保留期限是不同决策；全量音视频不会自动解决错人、时效和错误归纳。首版以文字和最小来源证据验证记忆闭环。

## 7. 主持最终裁决

确定本地 PostgreSQL + pgvector、统一记忆服务、显式原话可靠保存、版本与来源、可替换检索、删除恢复闭环。按用户后续反馈，首个实施目标收缩为：**先与本人交互；重启后还在；能准确找回；说错能改；要求遗忘后不再出现；每个回忆可解释来自哪里。** 长期维持同一个管家、一套共享记忆；房间切换由统一服务承接。未来声纹识别人，AI 结合场景与明确嘱咐决定表达方式，不按成员切换记忆库；这种判断不作为确定性的保密保证。

没有将“最新技术”“最长上下文”“最高公开跑分”当作交付标准。框架、模型和硬件最终通过本项目用例选择；目前没有实测证据支持锁定某个引擎或采购规格。
