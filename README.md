# 家庭 AI 本地记忆 v0.2

同一个管家，一套共享记忆。长期记忆、版本、中文向量、聊天记录和删除账本保存在本机；MiniMax-M3 按需接收当前问题、近期对话和相关记忆。对话默认自动判断该记什么、怎样整理、何时更新或遗忘；仍保留明确保存和管理接口。

## 启动

需要 Docker Desktop 正常运行、宿主机 Python 3。宿主机无需安装 Python 依赖。

```bash
cd /Users/churui/Project/memory
python3 scripts/manage.py up
```

启动脚本生成本地访问令牌、数据库密码和备份密钥；从已有 `whiteboard-x6-demo/.env` 复制 MiniMax 配置，不修改来源文件。已有本项目配置不会被覆盖。密钥放在 `.secrets/`，不输出到终端、不进入镜像。首次启动需要下载约 90 MB 的中文向量模型，网络较慢时会延长。

Docker Desktop 中分组名为 **home-memory**：

| 服务 | 作用 |
|---|---|
| `db` | PostgreSQL 16 + pgvector；无宿主机数据库端口 |
| `api` | 本地接口、检索与 M3 对话，默认 `127.0.0.1:8765` |
| `worker` | 本地向量索引、失败重试、聊天保留期清理 |

接口说明：[http://127.0.0.1:8765/docs](http://127.0.0.1:8765/docs)。业务接口需要 Bearer 令牌，可在本机 `.secrets/api_token` 查看并填入文档页的 Authorize；命令行自动读取，无需手工配置。

## 命令行

```bash
./memory remember '卧室睡觉时我喜欢空调26度'
./memory list
./memory search '睡觉时冷气应该调几度'
./memory chat '你记得我睡觉时喜欢的温度吗？'
./memory chat
```

`chat` 不带问题时进入交互会话，支持 `/new` 新会话、`/exit` 退出；带问题时输出 JSON，其中 `conversation_id` 可用 `--conversation ID` 继续。默认自动整理值得保留的信息，**不需要说“记住”或运行 `remember`**。`remember` 适合确定性保存或仅本地记录。

## 对话自动记忆

```bash
./memory chat
# 我平时睡觉喜欢卧室空调26度。
# 我现在睡觉改为28度了。
# 请忘掉我睡觉时的卧室空调温度偏好。
```

| 用户表达 | 默认处理 |
|---|---|
| 稳定偏好、明确个人事实、持续的互动习惯 | 分成独立事实，保存主体、类别及来源原话 |
| 与已知内容相同 | 不重复新增 |
| 明确改口、纠正或偏好改变 | 更新对应记忆，保留版本证据，清除旧索引及派生聊天 |
| 今晚/明天的安排或状态 | 单独存为有期限的临时记忆，保留长期偏好 |
| 妹妹的偏好 | 标记主体为妹妹；仍在同一套共享记忆中 |
| 普通问题、假设、玩笑、小说引文、助手的推测 | 不提炼为个人事实 |
| 事实主体或指代不清，确有记录意图 | 不写，必要时自然追问 |
| 明确要求遗忘某条已知信息 | 通过删除账本清理该记录及其派生内容，不保存本轮遗忘对话 |
| 明确说本轮不要记录，或本轮包含密码/验证码/密钥 | 模型应跳过提炼并不留存本轮聊天 |

M3 先通过结构化工具提出操作，本地服务再验证当前原话的逐字引用、目标 ID、版本、类别及有效期。判断阶段只使用当前用户陈述、最多 4 条近期用户消息（用于理解指代）及最多 12 条相关可上云记忆（每条最多 2,000 字符）；不以助手回答作为新事实来源，也不扫描旧聊天补记。

回答生成后，记忆变更、来源证据、索引任务与本轮聊天在同一个数据库事务内提交，再返回回答。超时、来源验证失败、版本冲突或事务失败时不返回保存成功。自然语言遗忘的独立删除意图先落盘，异常回滚后仍可能在下次启动被保守重放。

响应增加 `memory_changes`（create/update/forget、ID、版本、内容、类别、有效期）、`memory_status`（committed/unchanged/disabled）、`memory_clarification` 和 `history_saved`。交互命令行同时显示实际已提交的变更。查看 `show ID --history` 可追溯每版自动记忆的原话和来源会话 ID。

`./memory chat --no-auto-memory` 或 API `auto_memory=false` 关闭**本轮长期提炼**，普通聊天仍按保留期保存。确定不希望将内容交给云模型时使用 `remember --local-only`；这类内容不能通过云端 M3 自动理解。自动判断依赖模型，可能漏记、误解或重复，置信分数只是模型自评；可随时列表查看、纠正和删除。普通问题不追问是否要保存，只有模糊记录需要澄清。

```bash
./memory show 记忆ID --history
./memory correct 记忆ID '卧室睡觉时我现在喜欢28度'
./memory correct 记忆ID '新的内容' --revision 2
./memory forget 记忆ID
./memory reindex 记忆ID
./memory history 会话ID
./memory clear-history 会话ID
```

`correct` 默认读取当前版本再提交；`--revision` 可指定预期版本。并发修改返回冲突，防止静默覆盖。`forget` 删除指定记录的所有历史正文、向量、任务和引用它的派生聊天；同一事实如果另外独立保存过，需要分别删除。

```bash
./memory remember '只留在本机的内容' --local-only
./memory remember '妹妹不吃香菜' --subject 妹妹
./memory remember '这条记忆明早过期' --until '2026-10-24T07:00:00+08:00'
```

`--local-only` 记忆仍能在本地检索，但不发送给 M3。本版本没有本地生成模型，因此 M3 无法使用这类内容回答。人物标签用于分清事实主体，不是个人分库或成员权限。

## 接口

| 方法与路径 | 行为 |
|---|---|
| `POST /v1/memories` | 保存；可用 `Idempotency-Key` 重试 |
| `GET /v1/memories?limit=20&offset=0` | 分页列表 |
| `GET /v1/memories/{id}?history=true` | 查看当前内容和版本历史 |
| `PATCH /v1/memories/{id}` | 传 `expected_revision` 和修改字段 |
| `DELETE /v1/memories/{id}` | 删除并清理派生内容 |
| `POST /v1/memories/{id}/reindex` | 手动重新索引 |
| `POST /v1/search` | 传 `query`、可选 `limit/subject`，返回证据与检索方式 |
| `POST /v1/chat` | 传 `text`、可选 `conversation_id`、`auto_memory`（默认 true） |
| `GET /v1/conversations/{id}?limit=50&offset=0` | 查看保留期内的聊天 |
| `DELETE /v1/conversations/{id}` | 清空当前聊天；以后可继续使用该会话 ID |
| `GET /health` | 数据库、有效存储数量及索引队列计数；无需令牌 |

保存示例：

```json
{"text":"我睡觉时喜欢26度","speaker_id":"user","subject":"user","allow_cloud":true}
```

原话本地事务提交后返回 `storage_status=durable`；`index_status` 独立表示 `pending/running/done/failed/cancelled`。索引尚未完成也可按 ID 查看和关键词搜索。日期必须包含时区。原文最长 8,000 字符；向量索引分块，保留完整原话。同一个幂等键用于不同内容返回 409，删除后重用该键也返回 409。

更新示例：

```json
{"expected_revision":1,"text":"我现在喜欢28度","allow_cloud":false}
```

回答阶段 M3 获得最多 8 条相关记忆、总计最多 12,000 字符的证据、最多 12 条近期聊天以及已验证的本轮变更提案；旧版本和将遗忘的内容从回答证据与派生历史中排除。响应 `sources` 可定位对应本地记录，包括本轮新建或更新的记忆。找不到证据时要求模型说明不知道。来源列表不证明每句话均由证据支持。

同一会话的并发提问返回 409；全服务同时最多 3 个对话任务，额外请求返回 429，以保留本地存储连接。采用保守的全局变更版本：其他对话的自动写入、纠错、删除或历史清理都可能让在途回答返回 409，请重新提问。

## 数据与配置

```text
data/postgres/                 PostgreSQL 数据、历史与向量
data/models/                   中文模型文件缓存
data/journal/deletions.jsonl   独立删除意图账本（仅 ID、时间、类型）
data/backups/*.hmem            AES-256-GCM 加密备份
.secrets/                      本地密钥
```

数据库应用账号为 `memory_app`，初始化管理员为 `memory_admin`。启动时按版本初始化 schema，并先重放删除账本，完成后开放 API。保存和后台任务同事务提交；索引任务使用租约及来源版本，避免故障重试和旧任务回写。

关键默认值：MiniMax-M3、Anthropic 兼容接口、关闭思考、每次云请求 90 秒超时；自动记忆对话顺序进行判断与回答两次请求，通常比单次对话更慢，命令行等待上限 210 秒。判断阶段固定关闭思考，通过 [MiniMax 官方工具接口](https://platform.minimax.cn/docs/api-reference/text-anthropic-api) 取得结构化提案；不延续工具推理会话，不保存思考。本地 `BAAI/bge-small-zh-v1.5`、512 维、CPU 推理、精确向量检索；向量模型缺失或故障时返回 `mode=keyword`，不影响原话保存。

`.env` 可配置 `MINIMAX_BASE_URL`、`MINIMAX_THINKING=enabled`、`MEMORY_PORT`、`HISTORY_DAYS`（默认 30）。修改配置后重新运行启动脚本。长期记忆保存到明确修改、删除或指定有效期；过期记忆退出检索但保留记录，彻底清除使用 `forget`。普通聊天由 worker 按保留期清理。

云请求只记录调用状态、来源 ID、模型名及用量，不记录密钥、请求正文或思考内容。已经发送给云端的片段不能通过本地删除收回，云服务端的保留政策由提供方决定。

## 加密备份与恢复

```bash
./memory backup
./memory restore /Users/churui/Project/memory/data/backups/某个备份.hmem
```

备份通过管道加密，不把明文 dump 写到磁盘；密钥为 `.secrets/backup_key`。请独立保管该密钥，以及最新 `data/journal/deletions.jsonl`。没有密钥无法恢复；缺少账本、账本截断或账本比备份中记录的删除历史更旧时会拒绝恢复。

恢复先验证备份并导入独立临时数据库、重放删除账本。验证成功后短暂停止 API/worker，备份当前数据库，再次重放最新删除记录，然后切换数据库并验证健康。失败时保留或恢复原库，成功后移除临时旧库；当前库的加密回退备份仍保留。

首版手动备份；数据库和备份在同一硬盘上不能抵御整盘损坏。需要恢复到其他主机时，另行拷贝加密备份、备份密钥、最新删除账本，并在新环境初始化后恢复。无法证明一份被回滚的账本包含备份之后的所有删除，因此独立最新账本的保存是恢复前提。

本版备份面向小型初始数据，单份 PostgreSQL 压缩 dump 上限 256 MiB，恢复暂存和加密会占用内存；到达上限时需升级为流式备份格式。删除撤销在线可见性并清理逻辑副本，PostgreSQL 的 WAL、旧磁盘页与历史加密备份不保证立即物理擦除。

## 维护与测试

```bash
python3 scripts/manage.py status
python3 scripts/manage.py down
python3 scripts/manage.py up
python3 scripts/manage.py test
docker compose logs --tail=100 api worker
```

`down` 不移除本地数据。请勿手工删除 `data/postgres` 或删除账本；启动脚本发现已有数据库而账本丢失时会停止。健康检查中的队列计数用于观察索引，服务进程状态用 Compose `ps` 查看。

测试创建独立 PostgreSQL schema，并单独保存测试删除账本；模型使用模拟实现，不清空正式记忆。完整恢复演练使用独立临时 Compose 项目和数据目录。真实 M3 验收只用合成偏好，测试结束清理在线合成数据。

完整存储验收可运行 `python3 scripts/verify.py --real-model`，建立独立 `home-memory-qa` 分组，进行三轮合成对话，再关闭测试分组；自动模式每轮两次 M3 请求。结果见 [存储验收记录](docs/verification.json)。省略 `--real-model` 只验证本地存储、真实向量、断网和恢复（断网连接失败测试不会到达提供方）。

`python3 -m scripts.verify_automatic` 另外使用真实 M3 验证自动提炼、重复判断、跨会话找回、自然纠错、遗忘和五类边界表达；同样只使用隔离 QA 库和合成数据。结果见 [自动记忆验收记录](docs/automatic-verification.json)。两份 QA 脚本依次运行，不能同时运行。

v0.2 启动时自动将 v0.1 数据库增量升级，不删除旧记忆；恢复 v0.1 备份后也会先升级再重放删除账本。清空聊天不等于删除独立长期记忆，长期事实需使用自然遗忘或 `forget`。自动遗忘只作用于检索到并准确定位的记录，不承诺跨所有独立记录的语义批量擦除。

当前不包含知识图谱、网页前端、声纹或家居控制。早期研究见 [设计讨论](docs/memory-design-v0.1.md)，本 README 描述实际 v0.2 范围。
