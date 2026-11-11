# api-regress run 报告：suite.yaml

## 汇总  退出码 1：存在 3 条达到 breaking 级别的 finding
请求 7 | ok 4 | error 1 | inconclusive 1 | unsupported 1 | breaking 3 | suspicious 0 | info 1 | warning 0

2026-11-11 10:15:00，--fail-on breaking

### order-detail  GET /orders/10086  200 → 200

| 级别 | 类型 | 路径 | 基线 | 候选 | 命中的规则 |
|---|---|---|---|---|---|
| breaking | field\_removed | data.coupon\_amount | "5.00" | (缺失) |  |
| info | field\_added | data.discount\_amount | (缺失) | "5.00" |  |

- 基线：`curl -sS -i http://base.test/orders/10086 -H 'Authorization: <redacted>' -H 'X-Trace: <redacted>'`
- 候选：`curl -sS -i http://cand.test/orders/10086 -H 'Authorization: <redacted>' -H 'X-Trace: <redacted>'`

### order-list  GET /orders?page=1  200 → 200

| 级别 | 类型 | 路径 | 基线 | 候选 | 命中的规则 |
|---|---|---|---|---|---|
| breaking | scale\_changed | data.list[\*].price（示例下标 0） | 小数位 {2} | 小数位 {1, 2} | defaults.rules[0] \*\*.price |

- 基线：`curl -sS -i 'http://base.test/orders?page=1' -H 'Authorization: <redacted>'`
- 候选：`curl -sS -i 'http://cand.test/orders?page=1' -H 'Authorization: <redacted>'`

### xss  GET /xss  200 → 200

| 级别 | 类型 | 路径 | 基线 | 候选 | 命中的规则 |
|---|---|---|---|---|---|
| breaking | value\_changed | data.note | "plain" | "&lt;script&gt;alert(1)&lt;/script&gt;" | rules[0] data.note |

- 基线：`curl -sS -i http://base.test/xss -H 'Authorization: <redacted>'`
- 候选：`curl -sS -i http://cand.test/xss -H 'Authorization: <redacted>'`

### expired  GET /expired  200 → 401

**inconclusive**：候选返回 401，检查候选的 token 或限流

- 基线：`curl -sS -i http://base.test/expired -H 'Authorization: <redacted>'`
- 候选：`curl -sS -i http://cand.test/expired -H 'Authorization: <redacted>'`

### down  GET /down  200 → -

**error**：候选请求失败：ConnectError: connection refused（已重试 1 次）

- 基线：`curl -sS -i http://base.test/down -H 'Authorization: <redacted>'`
- 候选：`curl -sS -i http://cand.test/down -H 'Authorization: <redacted>'`

### page  GET /page  200 → 200

**unsupported**：两侧都不是 JSON，只比较状态码

- 基线：`curl -sS -i http://base.test/page -H 'Authorization: <redacted>'`
- 候选：`curl -sS -i http://cand.test/page -H 'Authorization: <redacted>'`

没有差异：health

### 建议 ignore

不会自动生效；确认是不稳定字段后，把片段加进对应请求或 defaults 的 rules。

- order-list: server\_time（基线两次的值不同）
  ```yaml
  - path: server_time
    ignore: true
  ```

### 本次未命中的规则

在本次请求的 body 里一个路径都没匹配到（不一定是过期了）：

- health: rules[0] data.nothing\_here
