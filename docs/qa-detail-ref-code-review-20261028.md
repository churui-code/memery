# detail_ref与聚合诊断独立代码审查

2026年10月28日检查corrections.py、service.py、model.py、automatic.py及新增回归；未发现阻塞项。

- correction_targets只从本轮experience_evidence的user details生成，排除legacy与assistant，按事件/修订/来源/范围去重，不修改冻结证据。
- 云schema移除旧坐标，只暴露detail_ref/old_quote/quote_refs；未知引用及额外坐标拒绝。MiniMax从本次prompt内的experience_evidence重新生成映射，不信任同名correction_targets字段；映射时深拷贝提案，随后沿用来源绑定与原严格校验。
- 原有old_quote唯一子串、当前quote来源、重叠范围、版本/云权限/删除状态及事务锁内复查仍在。无记录分支仍丢弃development，不解析无权引用。
- 实际wire与预算都通过同一helper生成引用表；已有新回归断言实际完整请求估计相等。
- 云契约聚合诊断全局最多12项，固定码与schema字段路径，不输出模型值和未知字段名称。它仅聚合已知契约错误，完整语义校验仍由后续验证器完成。

独立离线探针使用真实MiniMax适配器、假_request返回值，没有provider或数据库请求。验证伪造引用表被忽略、原provider响应未被修改、未知ref/混入坐标被拒、无记录分支未知根字段仍被拒、空展示来源不能获得纠错权限；全部通过。输出见 `data/qa/independent-acceptance-20261027/detail-ref-independent-static-probes.txt`。

未重复全量DB测试；主修复聊天已报告551项及5子测试、UI7项通过。自然模型成功率与failed正文遵循尚未由此次代码审查证明。下一次真实实验固定为batch10的6轮，计划见 `data/qa/independent-acceptance-20261027/batch10-preregistered-plan.json`，SHA256 `5d8121788f0337a46325898d6adcf05d51c2ece8a0c52aa2616599da828cd549`。
