# TODO

> 已知缺陷和后续计划。缺陷的详细描述见 `doc/spec/v1-mvp/tasks.md` 的"已知问题"表（编号 K1–K20）。
> 涉及行为变化的，先改 spec 并和用户确认，再动代码。

## 后续功能

### HAR 导入
- 浏览器 DevTools 导出 HAR → 生成 suite 请求清单。
- HAR 也可直接作为基线快照，配合 `snapshot compare` 使用。
- 需要决定：只导入 GET/HEAD，其余请求如何提示；请求头（含 token）不写入 suite 和快照，鉴权通过 `${VAR}` 注入；导入时是否按 URL 去重、过滤静态资源和非 JSON 响应。
- 开工前先在 spec 中补充行为定义（当前 spec 的"本期不做"里列了 HAR 导入）。

## 已知缺陷

### 影响结果正确性
- K1 共享 cookie jar：基线的 `Set-Cookie` 会带到后续请求和候选，两侧请求不再独立。
- K2 `base_url` 带 query / fragment 时 path 拼接错误，两侧都请求错地址却"通过"。
- K3 基线不是 JSON 或不满足 success 时，仍可能报 `business_error`，并跳过 `body_kind_changed`。
- K4 unordered 数组元素个数变化被记成数组路径的值变化，导致建议 ignore 整个数组。
- K5 HEAD / 204 也解析 body，空 body 配 JSON Content-Type 时每次出 `json_parse_failed`。
- K7 基线第 2 次请求为 429 / 5xx 时仍参与不稳定字段建议。
- K8 重复的 `Content-Type` 头被拼成 `a, b`，判为非 JSON，误报 breaking。
- K11 `compare_headers` 中重复或仅大小写不同的名字产生重复 `finding_id`。

### 输出与提示
- K9 基线 401 / 5xx 时仍求 success，多出 `success_eval_error` 提示。
- K10 `--fail-on never` 有 breaking 时，退出原因仍写"全部通过"。
- K12 第 2 个基线 sample 的 `success_eval_error` 消息没标明是第 2 次。
- K13 run 的 warning 未记 WARNING 日志。
- K14 `--only`、覆盖地址出错时不显示用法。
- K15 `result.json` Schema 中 `finding_id` 是可选字段，应改必填并重新导出 Schema。
- K17 run.md 对"未命中统计"的口径描述与实现不完全一致。

### 性能与维护
- K6 `unmatched_rules` 逐节点逐规则匹配，大 body 下比 diff 本身慢 25–50 倍。
- K18 代码重复和未使用的常量（退出码常量、`_print_issues` / `_print_validation`、`REQUEST_STATUSES`、`LEVELS` 等）。
- K19 解析后的 body 整个运行期间留在内存；`evaluate` 跑在事件循环上，拉高其他请求的 `elapsed_ms`。

### 待确认是否为问题
- K20 只有候选返回 204 时判 inconclusive；`ReadError` / `RemoteProtocolError` 不重试；基线第 2 次请求排在其他请求后面。

> K16 已在快照任务中修复，不再列入。
