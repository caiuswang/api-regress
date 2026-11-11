# run：两侧实时请求并对比

```
api-regress run <suite.yaml> [--out DIR] [--fail-on LEVEL] [--baseline-url URL] [--candidate-url URL]
                             [--only NAME ...] [--log-level LEVEL] [--log-file FILE]
```

基线和候选都在线时用（例如预发和生产）。只有一个环境时，用 `record` 录快照、`compare` 和快照对比，见 [snapshot.md](snapshot.md)；`compare` 的流程、输出和退出码与本页相同。每条请求：**基线串行请求 2 次，候选请求 1 次**，用基线的第 1 次和候选对比，基线的第 2 次只用来找不稳定的字段。

| 参数 | 说明 |
|---|---|
| `--out DIR` | run 目录，默认 `./api-regress-out/<YYYYmmdd-HHMMSS>/`（同一秒内重复运行时加 `-2`、`-3`） |
| `--fail-on LEVEL` | `breaking`（默认）/ `suspicious` / `info` / `never`：有达到这一级的 finding 时退出码为 1 |
| `--baseline-url` / `--candidate-url` | 覆盖 suite 里对应一侧的 `base_url`，必须是 `http://` 或 `https://` 开头，不能带用户名或密码 |
| `--only NAME` | 只跑指定的请求，可以写多次；name 不存在时报错，退出码 2 |

`run` 需要两侧的环境变量：`targets.baseline`、`targets.candidate`、`defaults`、`requests` 里引用的变量缺一个就报错（退出码 2），并指出所在行。

> 目前只输出 `result.json` 和两侧响应；HTML、Markdown、JUnit 报告还没有实现。

## 请求怎么发

- **URL**：`base_url` 去掉末尾的 `/`，拼上 `path`，再附加 `query`。`base_url` 可以带路径前缀，例如 `https://host/api`。
- **请求头**：按 `defaults.headers` → `targets.<side>.headers` → 请求级 `headers` 的顺序合并，名字不区分大小写，后面的覆盖前面的。
- **只发 GET / HEAD**，**不跟随重定向**：3xx 本身就参与对比。
- **超时、并发、重试**：`defaults.timeout`（秒）是单次请求从发出到读完响应的**总时限**，慢慢吐数据的响应也会按时超时；`defaults.concurrency`、`defaults.retries`。重试**只针对连接错误和超时**，任何状态码都不重试。基线第 1 次失败（重试后仍失败）就不再发第 2 次。
- 单条请求出问题不会中断整个运行：网络错误记为 `error`；工具自身的意外错误也只让这一条记为 `error`（原因写异常类型，详情在日志里），其他请求照常，退出码为 3。
- 请求头的值有首尾空白、换行或非 ASCII 字符时（常见于从 Windows 换行的 `.env` 读出的 token），这一侧记为 `error`，原因只写"请求不合法"之类的固定说明，不带出值。

## 每条请求的流程

1. **判定请求状态**（下表，第一条匹配的生效）。只有 `ok` 才继续往下走；`unsupported` 只比较状态码。
2. **状态码不同**：报 `status_changed`（breaking），这条请求到此结束，不再看 success、响应头、body。
3. **候选的 `success`**：基线满足、候选不满足时报 `business_error`（breaking），并**跳过 body 对比和 asserts**。
4. **对比 `compare_headers`** 列出的响应头。
5. **对比 body**（两侧都是 JSON 时），规则见 [compare.md](compare.md)。HEAD 和 204 不比 body。
6. **执行 asserts**，见 [expressions.md](expressions.md)。
7. **不稳定字段建议**（见下文）。

### 请求状态

| # | 情况 | 状态 | 提示 |
|---|---|---|---|
| 1 | 任一侧网络错误或超时，重试后仍然失败 | `error` | 哪一侧失败、原因、重试次数 |
| 2 | （只有 `compare`）快照里没有这条请求，或者指纹不一致 | `inconclusive` | 请 record 或 accept，见 [snapshot.md](snapshot.md) |
| 3 | 任一侧返回 401 / 403 / 429 | `inconclusive` | 检查该侧的 token 或限流 |
| 4 | 基线返回 5xx | `inconclusive` | 基线不可用 |
| 5 | 配了 `success`，基线不满足 | `inconclusive` | 基线业务失败 |
| 6 | 两侧都不是 JSON，而且不是 HEAD、也没有一侧是 204 | `unsupported` | 只比较状态码 |
| 7 | 其他 | `ok` | |

- **候选 5xx、基线 2xx**：状态是 `ok`，报 `status_changed`（breaking）。
- **两侧状态码相同但都不是 2xx，而且没配 `success`**：照常对比，同时给出 `both_non_2xx` 提示——测试数据可能已经失效（例如订单被删了，两边都 404）。
- **是否为 JSON**：Content-Type 是 `application/json` 或任意 `*/*+json` 时解析；没有 Content-Type 时尝试解析一次。

### 非 body 的差异

这些差异没有 body 路径，用伪路径表示：

| 类型 | 伪路径 | 什么时候出现 | 级别 |
|---|---|---|---|
| `status_changed` | `$status` | 状态码不同 | breaking |
| `body_kind_changed` | `$body` | 一侧是 JSON、另一侧不是；两侧的值是 `json` / `non-json` | breaking |
| `business_error` | `$success` | 基线满足 `success`，候选不满足 | breaking |
| `header_missing` | `$header.<名字>` | `compare_headers` 里的头，基线有、候选没有 | breaking |
| `header_changed` | `$header.<名字>` | 值变了，或者候选新出现；`Content-Type` 只看媒体类型（变了记 breaking，只改 charset 等参数不报） | suspicious；Content-Type 为 breaking |
| `assert_failed` | `$assert[<序号>]` | assert 结果为 False | assert 的 `level`，默认 suspicious |
| `assert_error` | `$assert[<序号>]` | assert 求值出错 | suspicious |

响应头名字不区分大小写，伪路径里的名字按 `compare_headers` 的写法。

## 不稳定字段建议

基线的两个 sample 按 `compare: values` 的方式比较（数组按配置的模式对齐），以下字段列为"建议 ignore"：

- 两次值不一样的字段（时间戳、traceId 等）
- 只在其中一次里出现的对象字段

数组元素的增减（数据量变化）、schema 数组内部的值、已经被 ignore 的路径都不计入。每条建议给出泛化路径和一段可以直接粘贴进 `rules` 的 YAML：

```yaml
- path: data.server_time
  ignore: true
```

建议**不会自动生效**，要自己加进 suite。基线第 2 次请求失败、不是 JSON、或者不满足 `success` 时没有建议，并给出 `second_sample_unavailable` 提示。

## 本次未命中的规则

一条规则在本次运行的请求的原始 body 里**一个路径都没匹配到**，就列在 `result.json` 的 `unmatched_rules` 里。判定和 ignore、数组模式无关，写在 schema 数组元素里的规则只要匹配到了字段就算命中。

- 只统计至少有一侧拿到了 JSON body 的请求（HEAD、出错的请求不算）。
- defaults 里的规则在所有这些请求里都没命中才列出，只列一次；请求级规则按请求列出。
- 只是"本次未命中"，不代表规则已经过期：用了 `--only`、或者数据刚好没有这个字段时都会出现。

## 提示（warning）

除了 [compare.md](compare.md) 里和 body 有关的提示，`run` 还会给出：

| code | 含义 |
|---|---|
| `both_non_2xx` | 两侧状态码相同、都不是 2xx，并且没配 `success` |
| `success_eval_error` | `success` 表达式求值出错，视为不满足 |
| `second_sample_unavailable` | 基线第 2 次请求失败、不是 JSON 或不满足 `success`，这次没有不稳定字段建议 |

提示不影响退出码。

## 输出

stdout 摘要：

```
requests 3 | ok 2 | error 0 | inconclusive 1 | unsupported 0
findings: breaking 1 | suspicious 0 | info 1
  order-list: inconclusive（候选返回 401，检查候选的 token 或限流）
warnings 0 | 建议 ignore 1 | 本次未命中的规则 0
result: api-regress-out/20261111-101500/result.json
exit 1: 存在 1 条达到 breaking 级别的 finding
```

不是 `ok` 的请求各占一行，附上原因。suite 有问题时和 `validate` 一样输出 `INVALID, N error(s)` 和每个错误的位置。

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 没有达到 `--fail-on` 的 finding，所有请求都是 `ok` 或 `unsupported` |
| 1 | 存在达到 `--fail-on` 级别的 finding |
| 2 | suite 有问题（不合法、缺环境变量、写方法、表达式不合法），或者命令行参数写错（`--only` 的 name 不存在、覆盖的地址不合法） |
| 3 | 工具自身出错：`--out` 不能用、无法写入 run 目录、内部异常 |
| 4 | 至少有一条请求是 `error` 或 `inconclusive`，并且没有触发 1 |

同时满足多个时按 **2 > 3 > 1 > 4 > 0** 取值。例如既有 breaking 又有 error 时为 1。

### run 目录

| 文件 | 内容 |
|---|---|
| `result.json` | 结构化结果，字段见下文 |
| `baseline/<name>.json` | 基线的 2 个 sample（`compare` 时是快照里的 1–2 个；快照里没有这条请求时不写） |
| `candidate/<name>.json` | 候选的 1 个 sample，`accept` 从这里读 |

每个 sample 记录 URL、状态码、`Content-Type` 和 `compare_headers` 列出的响应头、body 原文（不是 UTF-8 时存 base64，字段名 `body_b64`）、耗时、重试次数；网络错误的 sample 只有 URL、错误原因和重试次数。**请求头一律不保存**，所以 token 不会落盘；其他响应头（如 `Set-Cookie`）也不保存。但响应体原样保存，不要把 run 目录提交或上传到公开的地方。

`--out` 指向已存在的非空目录时，只有它是之前的 run 目录（里面的 `result.json` 是 api-regress 写的）才会复用（先清掉旧的 `baseline/`、`candidate/`）；否则拒绝写入，退出码 3，以免把文件混进无关的目录或删掉别人的文件。`--log-file` 放在 `--out` 目录里不影响判断。

`--out` 在发请求之前就检查，不能用时一个请求都不发。写入阶段才失败时（例如磁盘满），退出码为 3，stdout 的 result 行显示 `result: 未写入`。

### result.json

结构由仓库里的 JSON Schema（`schema/result.schema.json`）约束，主要字段：

- 顶层：`schema_version`、`tool_version`、`command`（`run` / `compare`）、`suite`、`started_at`、`finished_at`、`fail_on`、`cancelled`、`exit_code`、`exit_reason`、`summary`（请求数、各状态数、各级别 finding 数、warning 数）、`requests`、`unmatched_rules`。
- `requests[]`：`name`、`method`、`status`、`reason`、`fingerprint`、`snapshot_problem`、`candidate_success`、`baseline`（0–2 个 sample 的 URL、状态码、耗时、重试次数、Content-Type、错误；`compare` 时快照里没有这条请求则为空）、`candidate`、`findings`、`warnings`、`suggestions`。
  - `snapshot_problem`：只有 `compare` 会填，`missing`（快照里没有）或 `fingerprint_mismatch`（指纹不一致），其他情况为 `null`。
  - `candidate_success`：候选的 `success` 结果（`true` / `false`），没有求值时（没配 `success`、HEAD、204、非 JSON、请求没走到这一步）为 `null`。状态码不同时流程虽然就此结束，也照样求值（出错时不给 `success_eval_error`）。
  - 这两项给 `accept` 判断能不能接受候选响应。
- `findings[]`：`finding_id`、`type`、`level`、`path`（具体路径或伪路径）、`generalized_path`（可以直接写进规则）、`path_tokens`、`base_value` / `cand_value`、`base_type` / `cand_type`、`example_index`、`rule_refs`、`message`。
  - 字段不存在时值为 `{"absent": true}`，和 `null` 区分；小数按原文的小数位输出成字符串（`12.50` → `"12.50"`），看 `base_type` / `cand_type` 区分数字和字符串。
  - `finding_id` 由请求名、类型和具体路径算出：同一个差异在两次运行中的 id 相同，keyed 数组里不同元素的 id 不同。

## 日志

INFO 级别会记录：suite 加载结果、每次请求的发出和返回（target、方法、URL、状态码、耗时、重试次数）、每条请求的状态和 finding 数、汇总、写入的文件、退出码及原因。重试记为 WARNING。DEBUG 级别额外输出响应体的前 2KB。

**任何级别都不输出请求头的值**，只输出名字。
