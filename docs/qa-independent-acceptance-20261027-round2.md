# 独立验收第2批本地检查：云协议修复通过，cue频率仍需修复

**结论：三个真实适配器协议边界已通过；尚有一个已复现的检索评分缺陷，真实验收暂缓。** 新真实预算仍为0/60，剩余60。没有启动真实HTTP服务或模型请求，旧账本SHA-256与初始记录一致。数据库测试槽已释放。

## 云协议修复结果

`tests/test_qa_independent_repairs.py` 原三个协议断言保持，实际MiniMax适配器配合模拟提供商响应、真实服务与隔离数据库执行。缺少old_quote、双quote表示、缺少顶层quote_refs三种输出均在intake边界拒绝，未进入重评，未新增经历或纠错关系。独立文件中此前五项纠错保留检查也通过。

追加cue频率检查后，独立文件结果为 **8 passed, 1 failed**。日志：`data/qa/independent-acceptance-20261027/independent-round2.txt`。

## P2：被纠正cue仍计入其它有效cue的frequency

位置：`app/episode_repository.py:308–310` 的频率子查询。外层已排除被遮罩的cue，内层 `count(DISTINCT f.episode_id)` 未采用同样的有效来源条件。

独立数据库复现：先创建互相无依赖的两个合成事件，均有 `text=松纹书签` 的cue，分别引用 `第一枚松纹书签花了31元。` 与 `第二枚松纹书签花了72元。`。第一枚被纠正为枫叶书签，旧cue的完整来源已被遮罩。随后调用 `episode_candidates('松纹书签')`，第二枚的有效cue仍返回frequency=2，`activation_components()` 返回distinctive_cue=false。

预期应是frequency=1、distinctive_cue=true。旧cue虽然不再作为当前证据，仍影响其它事件的唯一性和激活分；本项未声称旧quote直接进入模型payload。修复时对频率子查询中的每一个cue来源使用与外层相同的纠错遮罩排除条件。独立回归 `test_independent_corrected_cue_does_not_inflate_live_cue_frequency` 已保留，不降低断言。

## 完整回归中的三个夹具不一致

本轮完整回归结果：**516 passed, 3 failed, 5 subtests passed**，耗时92.62秒。日志：`data/qa/independent-acceptance-20261027/local-tests-round2.txt`。

失败位置：

- `tests/test_dialogue_v4.py:284` 的invalid_tool与invalid_input两种情况：第一次模拟协议失败后，第二次“成功”响应仍只含 `actions=[]`，缺少当前云schema必填的retain_turn与self_updates，因此被新验证器正确拒绝。
- `tests/test_transport_backup.py:70` 的accepted=True参数：同样把只含actions的提供商响应当作合法；新云协议下已不合法。

这三项不能作为已修复协议再次失效的证据。应更新模拟成功响应以满足当前云契约，并保留原来的重试次数、失败usage与审计断言；不要放松云验证器来迁就旧夹具。本轮未擅自修改这两个由主修复对话维护的测试文件。

## 代码指纹与准备状态

完整指纹：`data/qa/independent-acceptance-20261027/code-round2.json`。工作区基于Git `3fa240a3`，未提交改动仍在。

| 文件 | SHA-256 |
| --- | --- |
| app/model.py | 0554f5d45b8b4aa52d76f0bcfc90043f55142c0ac74960a38675b2761c74258c |
| app/service.py | 21ffeaf2f0206833ca983a56bbe7bb897d3a51d013f06725441024d7ce5da414 |
| app/episode_repository.py | 5197c8e5e82ed625240b5bfcb8054021765fe39bc456f59331fff4ca13eac7e6 |

已准备新的 `scripts/verify_independent_acceptance.py`，包含全新装帧店/照相馆合成故事、跨会话与连续纠错步骤、实际提供商请求留档和共享60次持久账本。脚本未启动，账本尚不存在；不得把脚本准备完成当作真实验收通过。修复上述频率与夹具后，先运行相关回归，再开始首批约30次以内的真实请求。

## 是否需要独立记忆模型agent

本轮确定缺陷是云协议未落实、旧解释失效不完整、分句边界及SQL有效来源计数；这些问题需要程序控制器修复，新增一个LLM不能替代来源验证、事务或有效性过滤。现有MemoryService已经有整理、核验、重评、回答、提交与预算控制，继续收窄工具并明确失败/提交状态即可处理已确认问题，不必为了这些缺陷增加独立模型agent。

如果“记忆agent”指有明确步骤和权限的程序模块，可以在现有控制器上整理职责，未必增加任何云调用。如果指另一模型独立审查每轮记忆，通常至少增加一次请求；当前正常两次、最多三次的预算已要容纳重试或重评，必须重新分配预算，且增加延迟与tokens。真实语义验收尚未开始，本轮没有证据证明新增模型审查的收益能覆盖这些成本；应先完成当前来源/状态机和真实验收，再根据剩余语义失败决定是否需要额外模型审核。本评估未修改实现或扩大验收范围。
