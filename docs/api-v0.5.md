# v0.5 反思 API

沿用 `/v1` Bearer 令牌鉴权及既有聊天接口。生产服务位于 `127.0.0.1:8765`，实验台位于独立 Compose 项目的 `127.0.0.1:8766/lab`。生产不注册实验注入、时间推进或实验运行接口。

| 方法与路径 | 用途 |
|---|---|
| `GET /v1/reflection/settings` | 自动开关、策略、真实日配额、到期信息 |
| `PATCH /v1/reflection/settings` | 请求体 `{"enabled":true}` 或 `false`；暂停自动调度 |
| `POST /v1/reflection/preview` | 请求体 `{}` 或 `{"automatic":true}`；本地只读资格、证据、预算预览 |
| `POST /v1/reflection/runs` | 手动排队，返回运行 ID；由独立 reflector 执行 |
| `GET /v1/reflection/runs` | `limit` 1–200、`offset` ≥0 的运行列表 |
| `GET /v1/reflection/runs/{id}` | 状态、来源、状态版本变化、调用次数及用量 |

预览不调用模型、不消耗材料、不改变活跃度或自我状态。手动执行与自动执行都受来源权限、证据去重及每日两轮／四次请求限制。暂停只控制自动反思；显式手动排队仍可使用。

例如，已有本地令牌环境变量时：

```sh
curl -H "Authorization: Bearer $MEMORY_TOKEN" http://127.0.0.1:8765/v1/reflection/settings
curl -X POST -H "Authorization: Bearer $MEMORY_TOKEN" -H 'Content-Type: application/json' \
  -d '{}' http://127.0.0.1:8765/v1/reflection/preview
curl -X POST -H "Authorization: Bearer $MEMORY_TOKEN" http://127.0.0.1:8765/v1/reflection/runs
curl -X PATCH -H "Authorization: Bearer $MEMORY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"enabled":false}' http://127.0.0.1:8765/v1/reflection/settings
```

## 实验接口扩展

创建／复制实验支持 `reflection_profile: "off" | "scheduled"`，新建与旧实验默认关闭。模型仍默认为 `mock`；真实 MiniMax-M3 必须显式选择 `model_mode:"live"`。

- `GET /v1/lab/experiments/{id}/reflection-preview`：只读反思资格和证据预览。
- `POST /v1/lab/experiments/{id}/reflection-runs`：请求体 `{"automatic":false}`，明确运行一次反思。
- `GET /v1/lab/experiments/{id}/reflection-runs` 与 `GET …/{run_id}`：查询运行。
- 原批量 `POST …/{id}/runs` 增加 `reflection_mode:"off"|"on"|"paired"`，默认 `off`。成对实际对话应同时传 `mode:"chat"`。

每个成对场景从相同快照建立关闭／开启两条路径。一次关闭路径聊天最多三次请求，开启路径反思最多两次加聊天最多三次；整个运行共享不能由逻辑时间重置的总上限。时间推进只改变业务时钟，不隐式执行反思。报告包含失败结果、源证据、认识前后差异和下一轮实际回答上下文。

实验原文和提示只允许合成材料。实验 schema、数据库、令牌、删除账本和数据目录独立；实验标识由服务端登记，不能传任意 schema 访问生产数据。
