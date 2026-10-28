# v0.4 API 与合成记忆实验

v0.4 保留 `/v1` 路径和已有事实、经历、自我状态接口，新增事件记忆、只读回忆预览与独立实验接口。应用版本是 `0.4.0`，不要求旧客户端改用 `/v4`。当前数据库 schema 版本为 5：`migration_004.sql` 增加事件结构，`migration_005.sql` 为调用审计增加 `model_calls.estimator_version`；旧调用的未知版本保持 `NULL`，既有原文不改写。

## 服务与认证

| 服务 | 默认地址 | 令牌文件 | 数据 |
|---|---|---|---|
| 日常记忆服务 | `http://127.0.0.1:8765` | `.secrets/api_token` | `data/postgres`、`data/journal` |
| 合成实验台 | `http://127.0.0.1:8766/lab` | `data/lab/secrets/api_token` | `data/lab/postgres`、`data/lab/runtime` |

所有业务 API 使用 `Authorization: Bearer <token>`。`/health` 和静态实验台页面无需令牌；页面读取数据前需要输入实验令牌，令牌只保留在当前页面内存，不放入 URL 或浏览器持久存储。

生产配置不注册 `/v1/lab`。实验服务设置 `MEMORY_LAB=true`，普通 `/v1/chat` 等生产业务路由在该实例返回 404；实验只通过 `/v1/lab` 访问。实验令牌、数据库、数据库密码和删除日志独立，仅复用模型 API 密钥与只读本地 embedding 缓存。不能把生产备份导入实验台。

请求采用 JSON。未知字段通常返回 422；时间必须带时区。成功保存与索引完成分开报告。业务错误格式：

```json
{"error":"context_changed","message":"回答期间记忆发生变更，请重试。"}
```

常见状态码：401 未认证；404 不存在或已删除；409 版本/来源冲突或操作忙；413 必需输入超过上下文预算；422 参数无效；429 并发或实验队列满；502/503/504 模型、存储或超时错误。错误响应不会表示未完成事务已经保存成功。

## 原文、事件与回忆

- `experience` 保存完整用户发言、助手回答、记录时间、场景性质和来源；不随近期聊天窗口到期而删除。
- `episode` 描述一次具体事件。一轮对话可以包含多个事件，每个事件有梗概、细节、线索、重要性依据、情绪意义、版本和原文引用。
- 细节引用带原文记录 ID、角色、Unicode 字符起止位置、来源版本与哈希。模型提出的引用必须在当前用户原文中逐字找到。
- 事件与细节的 `source_reliability` 由服务端生成，记录 `quote_verified`、`speaker` 和 `claim`（`reported_unverified/fiction/hypothetical/conversation_only`）。逐字引用已验证不代表现实叙述已经核实；重要性和情绪意义不是可信度。
- 事件的 `time_precision` 为 `unknown/day/range`。能从 `time_quote` 确定完整日期、今天/昨天/前天/昨晚时，按上海时区保存当天起点与次日开区间终点；有明确起止日期的范围同理。只有“大概几年前”或缺失年份等信息时保留原话、结构化时间为空，不补造小时或秒。`recorded_at` 始终是记录时间。
- `encoding_version` 区分结构化事件编码 `episode-encoding-v1` 与本地兼容投影 `legacy-local-v1`；编码和时间元数据随事件版本保留。
- 索引、关联和梗概是可重建的派生数据；原文不由摘要覆盖。关联用于发现候选，不等于事实证据，也不代替权限与删除依赖。
- 活跃度综合时间、重要性、情绪意义、线索匹配和有效复习。旧事件的独特线索仍可触发回忆；年龄不把索引入口归零。
- 回忆按事件检索后提供完整原文片段，不按金额、原话或动作关键词隐藏细节；是否相关由模型结合证据判断。统一预算不足时明确省略整条事件，不裁断原话。历史及兼容投影仍可能以 `hint`、`gist`、`anchor`、`fragment` 等层级表示。
- 只读预览、后台索引和单纯候选命中不算复习。成功对话中通过验证的讨论、确认、补充或延续才记录复习，同一事件有 24 小时冷却。

### 新增的生产接口

| 方法与路径 | 输入 | 输出 |
|---|---|---|
| `GET /v1/episodes` | `limit=50`，1–200；`offset=0` | `items`、`limit`、`offset` |
| `GET /v1/episodes/{id}` | 事件 UUID | 事件正文、细节、线索、原文证据、版本与复习信息 |
| `POST /v1/recall/preview` | 下方请求体 | 当前输入下的候选、选择、上下文和预算解释 |

```json
{
  "query": "紫藤戒指那次买小吃花了多少钱？",
  "recall_mode": "natural",
  "budget": 24000,
  "conversation_id": null
}
```

`query` 为 1–8,000 字符；`recall_mode` 是 `natural` 或 `deep`，默认 `natural`。`deep` 降低激活门槛并允许更充分地展开证据；仍使用同一权限和上下文预算。`budget` 可省略，范围 1,000–100,000，是本地估计预算，不是模型提供方的精确 tokenizer 计数。`conversation_id` 可省略，提供时将该会话的可用近期上下文纳入预览。

预览不生成回答，不新增对话或自我状态，也不增加复习次数。它使用本地检索；向量不可用时可以降级为关键词检索，并报告状态。它仍只选择可用于云端回答的有效证据；查看仅本地原始记录应使用相应事实或经历详情接口。

主要响应字段：

| 字段 | 含义 |
|---|---|
| `query` / `mode` / `as_of` | 查询、回忆模式和业务时间 |
| `policy_version` / `estimator_version` | 激活策略与成本估计版本 |
| `embedding_status` / `vector_search` | 向量状态与搜索方式 |
| `candidates` | 候选事件；包含激活分数、基础活跃度、半衰期、匹配强度、原因、是否选中及排除原因 |
| `activated` | 预算装配前通过激活门槛的候选；不等于最终给模型的证据 |
| `selected` | 经过激活门槛与预算后真正选择的事件 |
| `evidence` | 实际装入的事件表示，含 `level`、`gist`、`details[].quote`、来源位置与歧义标记 |
| `payload` / `context` | 组装的预览上下文；`context` 为 `payload` 的别名 |
| `manifest` / `manifest_hash` | 来源类型、ID、版本、展示级别、原文范围及内容哈希 |
| `budget` | 预算、有效预算、本地估计成本、剩余成本及提供方用量（如有） |
| `omitted` | 因预算等原因没有进入上下文的内容 |
| `ambiguities` | 线索可能对应多件事件，不能将其人物、金额或动作混合 |

候选命中、最终选择、实际给模型的原话是三个不同指标。验证“记住了37元”时，应检查最终上下文里的证据，再检查回答；仅看到候选中出现该事件不够。

上下文默认输入预算为 24,000 个估计 token，保留 20% 安全余量。`estimator_version=utf8-half-v1`，计算实际请求 payload 序列化后的 UTF-8 字节数除以 2、向上取整；系统指令、工具/schema、JSON 结构与转义，以及结构化阶段的原文单元 ID、位置和文本都计入成本。旧经历可以降为梗概或提示；需要的原话保持完整，可选内容装不下时会说明省略原因，当前完整输入与必要协议内容超预算则显式返回 `413 context_budget_exceeded`。这个估计器不等同于 MiniMax 的精确 token 统计，提供方 usage 单独记录。

### 聊天的兼容扩展

`POST /v1/chat` 继续接收 `text`、可选 `conversation_id` 与 `auto_memory`，增加：

```json
{
  "text": "我们讨论的紫藤戒指故事中，那句告白原话是什么？",
  "auto_memory": true,
  "recall_mode": "deep"
}
```

原有 `answer`、`conversation_id`、`sources`、`experience_id`、`experience_sources`、`self_changes`、`memory_changes`、`memory_status`、`history_saved`、`memory_clarification` 继续使用。新增 `episode_ids`、`recall_mode`、`recall_trace`、`calls` 和 `policy_version`。

`calls` 中每项有阶段、状态、用量和耗时，阶段包括 `intake`、必要时的 `reassessment`、`answer`。自动记忆通常两次模型调用；若整理时看到的证据与最终证据发生实质变化，可以进行一次重评，每轮最多三次。重评不能另行修改事实操作或继续扩展检索。`auto_memory=false` 跳过本轮事实提炼和自我整理，仍保存允许记录的完整对话，并使用本地事件兜底表示。

客户端的 `text` 始终是完整字符串。服务端向云模型发送 `intake/reassessment` 结构化请求时，将内部 `current_question` 转成完整连续句/行单元 `{id,start,end,text}`；位置为 Unicode 字符偏移，`end` 不包含在内。按顺序连接所有 `text` 恰好恢复客户端原始输入，保留全部空白、标点和换行。最多 64 组，更多句/行会按相邻完整单元合并，不硬截断文本。回答阶段 `current_question` 仍传完整字符串；这不是要求调用方改传数组的新 API。

例如输入 `我爱你。\n我爱你。`，结构化阶段的内部表示为：

```json
{
  "current_question": [
    {"id": "u0001", "start": 0, "end": 4, "text": "我爱你。"},
    {"id": "u0002", "start": 4, "end": 5, "text": "\n"},
    {"id": "u0003", "start": 5, "end": 9, "text": "我爱你。"}
  ]
}
```

工具提案可用 `"quote_refs":["u0003"]` 引用第二次出现的句子。服务端还原为 `quote="我爱你。"`，事件及其细节/线索同时绑定 `occurrence=1`；多个引用必须严格递增、无重复且来自本轮；服务端取首末单元之间的完整连续范围，包含中间未列出的单元，不拼接跳句。模型使用 `quote_refs` 时应省略 `quote/occurrence`，短原话仍可直接填逐字 `quote`。未知、乱序、重复的 ID，以及引用与显式 `quote/occurrence` 冲突都会使提案验证失败。还原后仍执行正常来源验证：事件顶层引文须覆盖事件，细节、线索及时间引文须处于其范围内。来源引用不会授予跨轮读写权限。

细节的解释字段统一保存为 `meaning`；兼容模型用 `text` 命名该解释，两者同时提供时必须逐字一致。解释不作为引文，不能替代 `quote/quote_refs`，也不能绕过类型、长度、来源范围或权重依据校验。

只有实验配置的聊天响应额外提供 `actual_context`，便于检查最终发送的数据；普通聊天不会附上完整组装 payload。只读预览本身是受鉴权保护的本地诊断接口，会展示其组装的内容。

回答成功后，事实变更、原文、事件、复习、自我版本、来源关系和索引任务一起事务提交。模型失败、引用不合法、来源变更或预算不足时不会提交半个对话。默认单请求云超时 90 秒、整轮截止时间 240 秒；不在后台自动进行云端情绪反思。

### 继续保留的 `/v1` 接口

| 方法与路径 | 用途 |
|---|---|
| `POST /v1/memories` | 手动保存事实；支持 `Idempotency-Key`、`allow_cloud`、主体和有效期 |
| `GET /v1/memories` | 事实分页列表 |
| `GET /v1/memories/{id}?history=true` | 当前事实及版本历史 |
| `PATCH /v1/memories/{id}` | 使用 `expected_revision` 修改字段 |
| `DELETE /v1/memories/{id}` | 删除事实及受影响的派生内容 |
| `POST /v1/memories/{id}/reindex` | 重新索引当前版本 |
| `POST /v1/search` | 事实关键词/语义检索 |
| `GET /v1/conversations/{id}` | 查看近期聊天，支持 `limit/offset` |
| `DELETE /v1/conversations/{id}` | 清理该会话的经历与派生内容；独立事实仍需单独删除 |
| `POST /v1/experiences/search` | 按 query、时间、会话检索原始经历；支持 `include_stale` |
| `GET /v1/experiences/{id}` | 查看原始双方发言和来源 |
| `DELETE /v1/experiences/{id}` | 删除原始经历并清理依赖它的派生内容 |
| `GET /v1/self` | 当前情绪、关系、身份、观点和目标解释 |
| `GET /v1/self/history` | 通过 `domain/key/limit/offset` 查看形成历史 |
| `DELETE /v1/self/{id}` | 删除内部认识及依赖内容 |
| `GET /health` | 版本、计数、数据库与事实/经历/事件索引队列 |

目前不单独开放事件写入或事件删除的生产接口。事件由成功对话产生；需要删除原始经历时使用 `DELETE /v1/experiences/{id}`，保留原有删除日志及恢复重放语义。

## 实验台 API

启动与连接：

```bash
./memory lab up
./memory lab open
./memory lab token-path
```

`up` 只管理 `home-memory-lab` Compose 项目；`open` 只打开页面，不启动服务，不把令牌加入 URL。在本机读取 `token-path` 输出的文件并填入页面。先完成日常服务配置与本地 embedding 下载，再启动实验；实验不会在只读缓存中下载模型。

每个实验有服务端生成的随机 schema、独立删除日志、逻辑时钟与固定策略。客户端不能指定 schema 或文件路径。只有三种初始场景：`park-ring`（告白与37元）、`similar-events`（加入手链、52元等近似事件）、`empty`。模型模式 `mock/live` 与 embedding 模式 `fake/local` 独立选择，默认 `mock` + `fake`。

`fake` 是固定的词面向量，`mock` 返回明确标记的模拟回答。它们验证流程与数据约束，不证明真实语义检索、生成回答或情绪理解的质量。内置事件采用可解释的预编码夹具；真实的自动事件划分需另用完整对话或真实模型验收。

| 方法与路径（均以 `/v1/lab` 开头） | 请求与行为 |
|---|---|
| `GET /scenarios` | 场景清单、版本和有限策略参数 |
| `GET /experiments` | 实验清单；不返回数据库 schema 与日志路径 |
| `POST /experiments` | `name`、`scenario_id`、`model_mode`、`embedding_mode`、`policy_profile`、可选带时区的 `start_time`；返回 201 |
| `GET /experiments/{id}` | 逻辑时间、事件、原文、自我状态/历史、数量及索引状态；列表上限1,000条 |
| `POST /experiments/{id}/clone` | `name`、可选模型/embedding模式与 `policy_profile`；复制相同数据和时间到独立实验 |
| `DELETE /experiments/{id}` | 删除实验及报告；有排队或运行中任务时返回409 |
| `POST /experiments/{id}/events` | `title/text/synthetic:true`，可选 `importance/emotional_salience`、`encoding=fixture或model` |
| `POST /experiments/{id}/advance` | 非负 `days/hours`；只推进逻辑时间，不删除历史，不调用模型 |
| `POST /experiments/{id}/probe` | `query`、可选 `recall_mode/budget/conversation_id`；只读结果放在 `preview`，同时返回耗时和零云调用数 |
| `POST /experiments/{id}/chat` | `text/synthetic:true`、可选 `conversation_id/recall_mode`；使用完整聊天流程 |
| `GET /experiments/{id}/runs` | 该实验报告列表 |
| `POST /experiments/{id}/runs` | `mode=probe或chat`、`days`、`repeats`，返回202；异步运行 |
| `GET /experiments/{id}/runs/{run_id}` | 进度、逐例结果、指标与完整 JSON 报告 |

事件注入的 `fixture` 模式不调用生成模型，重要性等字段直接用于合成夹具；`model` 模式使用当前实验模型走完整聊天，权重由其编码结果决定。真实模型模式需要已配置 MiniMax 密钥。`synthetic:true` 是调用者对输入性质的明确声明，不是系统能从文字证明数据为虚构；实验台不提供生产导入功能。

逻辑时钟单向推进。需要比较更早时间时复制尚未推进的快照，或从同一场景重新创建。记忆衰减与情绪状态使用逻辑时间，任务租约与网络超时仍用真实时间。推进一年不会自动执行聊天保留期清理。

策略只允许 `balanced`、`strict`；不能上传自定义代码或任意评分配置。每个实验及报告固定完整参数：

| 策略 | 版本 | 自然阈值 | 深挖阈值 |
|---|---|---:|---:|
| balanced | `episodic-activation-v1` | 0.36 | 0.14 |
| strict | `episodic-activation-strict-v1` | 0.50 | 0.24 |

两者候选上限80、关联上限20、最终事件上限12。`strict` 更谨慎地唤醒弱匹配事件；独特线索仍可唤醒久远重要记忆。这些是工程策略参数，不是认知心理学测得的通用常数。复制时可以切换策略，原实验与生产默认保持原值。

### 合成 curl 示例

以下示例写入隔离实验服务。令牌从文件读入变量，不打印其值。把创建响应的 `id` 填入 `MEMORY_EXPERIMENT_ID`。

```bash
read -r MEMORY_LAB_TOKEN < data/lab/secrets/api_token
curl --fail-with-body -sS http://127.0.0.1:8766/v1/lab/experiments \
  -H "Authorization: Bearer $MEMORY_LAB_TOKEN" -H 'Content-Type: application/json' \
  --data '{"name":"公园回忆对照","scenario_id":"similar-events","model_mode":"mock","embedding_mode":"fake","policy_profile":"balanced","synthetic":true}'

MEMORY_EXPERIMENT_ID='替换为创建响应的id'
curl --fail-with-body -sS "http://127.0.0.1:8766/v1/lab/experiments/$MEMORY_EXPERIMENT_ID/probe" \
  -H "Authorization: Bearer $MEMORY_LAB_TOKEN" -H 'Content-Type: application/json' \
  --data '{"query":"紫藤戒指那次买小吃花了多少钱？","recall_mode":"natural","budget":24000}'

curl --fail-with-body -sS "http://127.0.0.1:8766/v1/lab/experiments/$MEMORY_EXPERIMENT_ID/advance" \
  -H "Authorization: Bearer $MEMORY_LAB_TOKEN" -H 'Content-Type: application/json' \
  --data '{"days":365}'

curl --fail-with-body -sS "http://127.0.0.1:8766/v1/lab/experiments/$MEMORY_EXPERIMENT_ID/clone" \
  -H "Authorization: Bearer $MEMORY_LAB_TOKEN" -H 'Content-Type: application/json' \
  --data '{"name":"同一时刻的谨慎策略","policy_profile":"strict"}'

curl --fail-with-body -sS "http://127.0.0.1:8766/v1/lab/experiments/$MEMORY_EXPERIMENT_ID/runs" \
  -H "Authorization: Bearer $MEMORY_LAB_TOKEN" -H 'Content-Type: application/json' \
  --data '{"mode":"probe","days":[0,30,365,1825],"repeats":1}'
unset MEMORY_LAB_TOKEN
```

再次执行相同 `probe` 可比较一年后的效果。报告 `days` 是相对于创建该运行时实验的逻辑时钟，而非固定日历日期；如需从初始日期比较，请在推进前运行或创建新的初始实验。

真实模型测试可在创建或复制时设置 `"model_mode":"live","embedding_mode":"local"`，然后调用 `/chat`；`probe` 即使在 live 实验中也不会调用云模型。真实模型 `/chat` 示例请求体：

```json
{"text":"合成故事中的紫藤戒指是什么材质？没有记载请直说。","recall_mode":"deep","synthetic":true}
```

### 异步报告与限额

`runs` 默认 `mode=probe`、`days=[0,30,365,1825]`、`repeats=1`；天数最多6项，每项0–36,500，重复轮数1–3。内置核心集在 `park-ring` 上有4条查询，在 `similar-events` 上有5条。`empty` 没有内置报告用例，可手动 probe/chat。

每一步从同一原始实验快照复制，不让前一步的生成回答成为后一步的记忆。排队后源实验不能修改或删除；当前最多2个执行任务、总计4个排队/运行任务。报告区分 `queued/running/completed/failed/interrupted`；服务重启把未完成运行标成 `interrupted`，不自动重新调用模型。

真实模型运行的请求上限是 `steps × 3`，其中 `steps = 可用用例数 × 时间点数 × 重复轮数`。例如 `similar-events`、4个时间点、1轮最多60次云请求；probe与mock均为0次。失败请求也计入单步限额；报告保留所有尝试，不用后续成功覆盖失败。

`completed` 表示任务执行完毕，并不等于每个质量检查通过。检查 `failed_steps`、每步 `status`、`context_quote_checks`、`answer_quote_checks` 和汇总 `metrics`。chat 的选择与原话检查只使用 `answer.actual_context`，并核对预期事件归属；独立预览结果放在 `preview_*` 字段，不能代替实际回答证据。缺少实际上下文时该步明确失败。报告保存策略/场景/模型/embedding标识、候选命中、选择命中、上下文原话检查及 P50/P95；mock 的 `answer_quality_evaluated=false`。涉及情绪时只检查来源、版本与连续性，不给“真实感情”打分。

## 命令行与自动验收

```bash
./memory episodes
./memory episode 事件ID
./memory recall '紫藤戒指' --recall-mode natural
./memory recall '那次的具体金额' --recall-mode deep --budget 24000
./memory chat '继续讨论上次的合成故事' --recall-mode deep
./memory lab status
./memory lab down
```

日常 CLI 自动读取生产令牌；`lab` 子命令只管理独立实验服务。不要仅将普通 CLI 的 URL 指向实验端口，实验业务使用专属 API 或网页。

```bash
python3 scripts/manage.py test
python3 scripts/verify_memory_v4.py
python3 scripts/verify_memory_v4.py --full-index --repeats 3
python3 scripts/verify_memory_v4.py --real-model --full-index --repeats 3
```

`verify_memory_v4.py` 使用随机临时 schema、独立临时删除日志、固定合成故事，结束后清理临时 schema。它需要已运行的数据库、当前 API 镜像和本地 BGE 缓存。默认验证冻结的30条真实 BGE 查询，并测量50/500/5000条干扰记录下的本地查询；默认规模阶段按全文检索记录，不能称为全量向量性能。

`--full-index` 先为规模数据建立完整向量索引，再比较 ANN 与精确检索；`--repeats` 为规模查询重复次数，范围1–10。`--real-model` 另启用12个固定真实模型场景，并记录实际调用及源证据。它不代表进行主观情感真实性实验。所有验证脚本应顺序运行。

报告写入 `docs/verification-v0.4.json`；查看运行结果、失败列表及对应版本，不将文档中的能力说明当作已经通过验收的证明。历史 v0.3 报告只描述旧版本运行。性能值为当前机器实测，不承诺统一毫秒数。

另可运行 `python3 scripts/verify_encoding_v4.py --real-model`，在独立临时 schema 和账本中执行长合成叙述编码与一年后回忆的三步聊天，最多9次云请求（含生产瞬时故障重试），不重复执行失败场景，输出 `docs/verification-encoding-v0.4.json`。不加开关时不运行云验收。两个脚本覆盖固定报告前，都会将已有 `passed=false` 报告按时间和 UUID 另存到 `docs/verification-history/`，保留失败记录。
