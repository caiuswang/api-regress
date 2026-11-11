# api-regress 产品需求 v1

> 状态：草案 r3（已经过 2 轮共 6 份 review）
> 来源：`spec-01.md` + 需求澄清讨论（2026-11-09）
> 配套文档：实现约束见 [spec-v1-design.md](spec-v1-design.md)
> 决策记录见 §13。

## 1. 背景与目标

发版后，线上接口可能被意外改坏：字段被删、类型被改、金额精度变化、状态码变化。这类问题单元测试很难覆盖，人工回归又容易漏看。

api-regress 是一个 CLI 小工具，主要给 QA 做回归，开发也能用。它把同一组 GET/HEAD 请求发给**基线（baseline）**和**候选（candidate）**两个目标（两个环境，或者两个版本），对比两边 JSON 响应的结构和字段，按严重级别出报告，并给出 CI 可用的退出码。

**核心原则**
- **结构性破坏零配置即可发现**：删字段、改类型、状态码变化，不写任何规则就能报出来。
- **金额精度需要配置 `decimal` 规则**：给 `**.amount`、`**.price` 这类路径加上规则即可。
- **值的比较只在显式配置时进行**。

### 1.1 典型场景

| # | 场景 | 用法 |
|---|---|---|
| S1 | 预发和生产同时在线，发版前对比 | `run`：两边都实时请求 |
| S2 | 只有一个环境：发版前录基线，发版后对比 | `snapshot record` → 发版 → `snapshot compare` |
| S3 | 差异是预期的变更，需要更新基线 | `snapshot accept` |
| S4 | 接入 CI，遇到 breaking 时阻断流水线 | `run` / `snapshot compare` + `--fail-on` + JUnit |

### 1.2 目标用户
- **QA**：维护请求清单（suite），看报告。日常使用不需要写代码。
- **开发**：在本地验证改动的影响；需要复杂对比时，用 `asserts`（高级用法，可选）。

## 2. 范围

### 2.1 v1 包含
1. CLI 命令：`validate`、`run`、`snapshot`（子命令 `record`、`compare`、`accept`）、`demo`
2. 只发 **GET / HEAD** 请求，只比较 **JSON** 响应
3. YAML 格式的 suite，用 `${VAR}` 注入环境变量
4. 默认只比较**结构**：字段增删、类型、状态码；配了 `decimal` 的字段还比较小数位
5. 字段规则：`ignore`、`decimal`、`on_value_change`、`array`、`nullable`
6. `success` 业务成功条件
7. `asserts` 表达式（高级用法）
8. 三个差异级别：breaking / suspicious / info
9. 报告：HTML、Markdown、JUnit XML，以及 `result.json`
10. **不稳定字段建议**：基线请求 2 次，两次不同的字段列为"建议 ignore"，**不会自动忽略**
11. 提示"本次未命中的规则"
12. 关键节点日志

### 2.2 本期不做
- **写方法**：suite 里出现写方法，按配置错误处理。
- **自动噪音识别**：只给出 §7.8 的"建议 ignore"，不自动排除差异。
- **HAR 导入**
- **脱敏**：见 §11 L5。
- **主机白名单、dry-run**

### 2.3 非目标（长期不做）
- **不做接口管理平台**：不做 Apifox / Postman 那一套，包括登录流程、请求串联、变量提取、前后置脚本、Mock、接口文档。
  内置的 `demo` 服务（§4.2）只提供固定的演示数据，不可配置，不算 Mock 功能（D19）。
- **不做断言编辑器或界面**：`asserts` 和 `success` 只是 YAML 里的一行表达式，用于对比基线与候选、判断业务是否成功，不会发展成单接口断言平台。
- **鉴权由使用者自理**：需要鉴权的接口，使用者先用自己的方式拿到 token，再通过 `${VAR}` 注入请求头。

## 3. 术语

| 术语 | 含义 |
|---|---|
| suite | 一个 YAML 配置文件，包含目标和请求清单 |
| target | 固定两个：`baseline`、`candidate` |
| sample | 一条请求的一次响应记录 |
| snapshot | 落盘的基线 sample 集合，可以替代实时请求基线 |
| finding | 一条差异：路径、类型、级别、两侧的值。**参与退出码计算** |
| warning | 一条提示，例如降级、基线为空。**不是 finding，不参与退出码** |
| 请求状态 | 每条请求最终属于 `ok` / `error` / `inconclusive` / `unsupported` 之一，见 §7.2 |
| run 目录 | 一次运行的输出目录 |

## 4. CLI

```
api-regress validate <suite.yaml>
api-regress run      <suite.yaml> [--out DIR] [--fail-on LEVEL] [--baseline-url URL] [--candidate-url URL]
api-regress snapshot record  <suite.yaml> -d DIR [--clean] [--baseline-url URL]
api-regress snapshot compare <suite.yaml> -d DIR [--out DIR] [--fail-on LEVEL] [--candidate-url URL]
api-regress snapshot accept  <run-dir>    -d DIR
api-regress demo     [--host HOST] [--port PORT] [--write-suites DIR]
```

**命令分组**（D21）：顶层只放两侧都实时请求的 `run`，以及 `validate`、`demo`；和快照有关的三个命令收在 `snapshot` 命令组下。快照目录用 `-d` / `--dir` 指定（必填）。下文和其他文档里单独写 `record` / `compare` / `accept` 时，指的就是 `snapshot record` / `snapshot compare` / `snapshot accept`。

**通用参数**
- `--only NAME`：可以重复，只处理指定的请求。
- `--log-level`：默认 INFO。
- `--log-file`
- `--format html,md,junit`（只对 `run`、`snapshot compare` 有效）：默认全部输出；`result.json` 总会输出。可以逗号分隔，也可以重复（`--format md --format junit`，和 `--only` 一样），重复的只算一次；`--format none` 只输出 `result.json`。取值不在这几个里、为空、或 `none` 和其他格式一起用时按参数错误处理，退出码 2（D22）。

**参数取值**
- `--fail-on`：`breaking`（默认）/ `suspicious` / `info` / `never`
- `--out`：默认 `./api-regress-out/<YYYYmmdd-HHMMSS>/`

### 4.1 各命令的行为、输出与退出码

| 命令 | 需要的 target | 行为 | stdout 摘要 | 退出码 |
|---|---|---|---|---|
| `validate` | 无（只做静态检查） | 校验 suite 的语法和字段、路径、表达式白名单。缺少环境变量**按 target 分别列出，只给 warning**（code `env_var_missing`）；静态可判定的无效规则也给 warning（§6.1、§6.3、§7.6） | `OK, N requests` + warning 列表 | 0 / 2 |
| `run` | 两侧 | 基线串行请求 2 次，候选请求 1 次，然后对比 | 各级别 finding 数、各请求状态数、报告路径 | 0 / 1 / 2 / 3 / 4 |
| `snapshot record` | 只需要 baseline | 每条请求串行请求 2 次，**按请求合并**写入快照。失败的请求保留旧 sample；`--clean` 会先清空快照 | 写入数、未写入的请求及原因、**不稳定字段建议** | 0 / 2 / 3 / 4 |
| `snapshot compare` | 只需要 candidate | 基线读快照，候选请求 1 次，然后对比 | 同 `run` | 0 / 1 / 2 / 3 / 4 |
| `snapshot accept` | 无 | 把 run 目录里的候选响应**按请求合并**进快照，见 §8.3 | 接受数、被拒的请求及原因 | 0 / 2 / 3 / 4 |
| `demo` | 无 | 启动本地演示服务，或用 `--write-suites` 写出示例 suite，见 §4.2 | 服务地址；写出的文件 | 0 / 2 / 3 |

**退出码定义**

| 码 | 含义 |
|---|---|
| 0 | 全部成功：没有达到 `--fail-on` 的 finding，所有请求都是 `ok` 或 `unsupported`；record / accept 全部写入 |
| 1 | 存在达到 `--fail-on` 级别的 finding |
| 2 | suite 有问题：不合法、本命令需要的环境变量缺失、出现写方法、表达式没通过白名单、快照 manifest 版本不兼容；命令行参数写错（同时打印用法说明） |
| 3 | 工具自身出错：无法写入报告或快照、快照损坏、内部异常 |
| 4 | 至少有一条请求是 `error` 或 `inconclusive`（record / accept 指：至少有一条请求没写入），并且没有触发 1 |

多个条件同时满足时，按 **2 > 3 > 1 > 4 > 0** 取值。单条请求出问题不会中断整个运行。warning 不影响退出码。

`accept` 的 `--only` 写了 run 目录里不存在的 name 时，按命令行参数错误处理（退出码 2）（D20）。

### 4.2 demo 服务

用于不联网试用和自测（D19）。

- **数据固定、不可配置**：`/v1/...` 是基线，`/v2/...` 是候选；v2 故意改坏几处，覆盖 `field_removed`、`field_added`、`type_changed`、`scale_changed`、`value_changed`、`element_missing`、`business_error`、`status_changed`、`header_changed`、`assert_failed`。另提供不比较的辅助接口：指定状态码、延迟返回、纯文本。
- **只用标准库**，只响应 GET / HEAD；HEAD 只回状态码，不带 body 和 Content-Type。数字按原文输出（保留 `12.50` 的末尾 0）。
- **参数**：`--host` 默认 `127.0.0.1`；`--port` 默认 `18765`，`0` 表示随机；端口被占用时报错，退出码 3（不开 `SO_REUSEADDR`，否则 macOS 上会和占用通配地址的进程同时绑定成功）。Ctrl+C 停止，退出码 0。
- **`--write-suites DIR`**：写出 `pass.yaml`（两侧都是 `/v1`，预期退出码 0）和 `regress.yaml`（`/v1` 对 `/v2`，预期退出码 1）后退出，不启动服务；已有同名文件时一个都不写，按参数错误处理，退出码 2。suite 的地址写成 `${DEMO_URL:-http://127.0.0.1:18765}`。
- **日志**：每个请求一行 INFO，只有请求行和状态码，不记请求头。
- **分层**：`demo` 包不依赖 `core`、`cli`、`report`，`core` 也不依赖 `demo`（import-linter 契约）。

## 5. 配置格式（suite.yaml）

下面是一个完整的电商示例，§12 的端到端流程也基于它。

```yaml
version: 1

targets:
  baseline:
    base_url: https://api.shop.example.com
    headers:
      Authorization: "Bearer ${BASE_TOKEN}"
  candidate:
    base_url: https://api.shop.example.com      # S2：两侧可以是同一个地址
    headers:
      Authorization: "Bearer ${CAND_TOKEN}"

defaults:
  timeout: 10          # 秒
  concurrency: 4
  retries: 1           # 只对连接错误和超时重试，任何状态码都不重试
  compare: structure   # structure | values
  success: "body.code == 0"      # 只对 JSON 响应求值，见 §7.3
  headers:
    Accept: application/json
  rules:
    - path: "**.updated_at"
      ignore: true
    - path: "**.amount"
      decimal: {}
    - path: "**.price"
      decimal: {}

requests:
  - name: health
    method: HEAD
    path: /health

  - name: order-list
    method: GET
    path: /v1/orders
    query: { page: "1", size: "20" }   # 值一律加引号
    # 分页列表数据会变，保持默认的 schema 模式，不要用 keyed

  - name: order-detail
    method: GET
    path: /v1/orders/10086
    compare_headers: [Content-Type]
    rules:
      - path: data.amount
        on_value_change: breaking    # 固定订单，金额也要比值
      - path: data.items             # array 规则写在数组本身的路径上
        array: { keys: [sku_id] }    # 写了 keys 即为 keyed 模式
      - path: data.coupon
        nullable: true
```

**规则说明**
- **name**：只能包含 `[A-Za-z0-9_.-]`，suite 内唯一。
- **字段名拼写**：拼错会报错，并给出出错的行号。
- **环境变量**：只替换字符串值里的 `${VAR}`，支持 `${VAR:-默认值}`：变量未设置**或为空**时取默认值（与 shell 一致）。默认值里不能出现 `$` 和 `}`，不支持嵌套；无法识别的 `${...}` 写法（如 `${A-x}`、`${ A }`、`${A:-${B}}`）直接报配置错误。每个命令只解析它需要的那一侧 target（见 §4.1）。
- **YAML 常见坑**（在 doc/user 里写清楚）：
  - `007` 会被解析成数字 7，`1.10` 会被解析成 1.1，`on`/`no` 会被解析成布尔值。
  - 所以 query 的值和数字字面量都要加引号。
- **数组对齐只有一种写法**：`array: {mode: schema|keyed|positional|unordered, keys: [...]}`。写了 `keys` 就默认用 keyed。原需求里的 "array_keys" 就是这里的 `keys`。写了 `keys` 时 `mode` 只能是 `keyed`；`mode: keyed` 必须写 `keys`。
- **每条规则至少要有一个属性**（`ignore`、`decimal`、`scale_level`、`on_value_change`、`array`、`nullable` 之一），只写 `path` 视为配置错误。
- **同一个映射里出现重复的 YAML key** 视为配置错误（YAML 解析器默认会静默取后一个）。
- **请求级 `success`**：写了就覆盖 defaults 里的 `success`；写成 `success: null` 表示这条请求不判定 success；没写就沿用 defaults。

## 6. 路径与规则

### 6.1 路径语法
| 写法 | 含义 |
|---|---|
| `data.items` | 逐级访问对象 key，开头不写 `$` |
| `data.items[0]` | 数组下标（只在 positional 模式下才有意义） |
| `data.items[*].price` | 任意数组元素，不管哪种模式都能匹配 |
| `meta["x.y"]` | key 里有特殊字符时用引号括起来，见下方"其他限制" |
| `**.updated_at` | 任意深度，也包括 0 层，所以能匹配根上的 `updated_at` |
| `$` | 根节点本身，只能单独写。例如响应体本身是数组时：`path: "$"` 加 `array: {keys: [id]}`；根数组的元素写 `[*].price` |

**其他限制**：除了单独的 `$`，路径开头不写 `$`；`**` 不能放在末尾，也不能连续出现；字段名里有 `.`、`[`、`]`、`"`、`*` 或空白，或者以 `$` 开头时，用 `["..."]` 括起来，引号内用 `\"`、`\\` 转义。

**下标写法的限制**：对 schema 或 keyed 模式的数组写了字面下标（例如 `items[0].x`），规则不会生效，并产生 warning。

### 6.2 报告里的路径
- **具体路径**：
  - 对象、positional 模式：`data.items[3].price`
  - keyed 模式：`data.items[sku_id=A12].price`。这种写法**只用于展示**，不能写进规则。
  - schema 模式：等于泛化路径，同时附一个出问题元素的下标作为示例。
- **泛化路径**：把下标和 key 都换成 `[*]`。报告里给出泛化路径，可以直接粘贴到规则里。
- **非 body 的 finding 用伪路径**：`$status`、`$header.Content-Type`、`$success`、`$assert[0]`；`body_kind_changed` 用 `$body`，两侧的值为 `json` / `non-json`（D17）。

### 6.3 规则怎么生效
每条规则都由若干属性组成：`ignore`、`decimal`、`scale_level`、`on_value_change`、`array`、`nullable`。

**合并方式**：同一个节点被多条规则匹配时，**每个属性单独决定用哪条规则**，比较顺序如下：
1. 越具体越优先：没有通配的优先于 `[*]`，`[*]` 优先于 `**`；同类通配下，层级越深越优先。
2. 具体程度相同，请求级优先于全局。
3. 还相同，后写的优先。

**示例**
- 全局写了 `**.amount: {decimal}`，请求级写了 `data.amount: {on_value_change: breaking}`。结果是两个属性同时生效。
- 全局写了 `**.updated_at: {ignore: true}`，请求级写了 `data.updated_at: {ignore: false}`。请求级更具体，所以不忽略。

**ignore 剪枝**：
- 一个节点最终 `ignore: true` 时，它的整棵子树都不再对比。
- 子树里再写的规则（包括 `ignore: false`）**都不生效**。能静态判断出这种情况时，`validate` 给出 warning。
- 规则所在的节点本身被另一条更优先的 `ignore: true` 覆盖、而它自己没写 `ignore` 时（例如全局 `**.updated_at` 忽略、请求级给 `data.updated_at` 配了 `on_value_change` 却忘了写 `ignore: false`），这条规则同样不生效，`validate` 也给出 warning。

## 7. 对比语义

### 7.1 单条请求的流程
1. **拿 sample**：
   - 基线：实时串行请求 2 次，或者读快照。
   - 候选：请求 1 次。
   - 基线第 1 次失败：请求状态为 `error`。
   - 基线第 2 次失败，或者第 2 次不满足 `success`：不出不稳定字段建议，产生 warning。
2. **判定请求状态**（§7.2，按表格顺序，**第一条匹配生效**）。只有状态为 `ok` 时才继续往下走。
3. **状态码不同**：产生 `status_changed`，**这条请求就此结束**，不再执行 success、body、asserts。
4. **业务成功判定**（§7.3）。
5. **对比 `compare_headers`**。
6. **对比 body**（§7.4 – 7.6），仅在两侧都是 JSON 时进行。
7. **执行 asserts**（§7.7）。
8. **计算不稳定字段建议**（§7.8）。

基线有 2 个 sample 时，**只用第 1 个和候选对比**，第 2 个只用来计算建议。

### 7.2 请求状态判定（第一条匹配生效）

| # | 情况 | 状态 | 说明 |
|---|---|---|---|
| 1 | 任一侧出现网络错误或超时，重试后仍然失败 | `error` | |
| 2 | 快照里没有这条请求，或者指纹不一致（§8.2） | `inconclusive` | 提示"请 record 或 accept" |
| 3 | 任一侧返回 401 / 403 / 429 | `inconclusive` | 提示"检查该侧 token 或限流" |
| 4 | 基线返回 5xx | `inconclusive` | 提示"基线不可用" |
| 5 | 配了 `success`，且基线不满足 | `inconclusive` | 提示"基线业务失败" |
| 6 | 两侧都不是 JSON，而且不是 HEAD 或 204 | `unsupported` | 只比状态码 |
| 7 | 其他 | `ok` | |

`ok` 状态下的几种特殊情况：
- **候选 5xx、基线 2xx**：在 §7.1 第 3 步产生 `status_changed`（breaking）。
- **两侧状态码相同但都不是 2xx，且没配 `success`**：照常对比，同时产生 warning："两侧都没有成功，测试数据可能已失效"。
- **HEAD / 204**：只比较状态码和 `compare_headers`，不判定 success。
- **3xx**：工具不跟随重定向，3xx 状态码本身就参与对比。
- **`unsupported` 仍比较状态码**：两侧状态码不同时照样产生 `status_changed`（D17）。
- **"HEAD 或 204"**：方法是 HEAD，或者任一侧返回 204（D17）。

**body 的判定**
- **是否为 JSON**：Content-Type 是 `application/json` 或任意 `*/*+json`（如 `application/problem+json`、`model/gltf+json`）时直接解析；没有 Content-Type 时尝试解析一次。
- **声明是 JSON 但解析失败**：这一侧按非 JSON 处理，并产生 warning。
- **一侧是 JSON、另一侧不是**：`body_kind_changed`（breaking）。
- **出现 NaN、Infinity、重复的 key**：产生 warning（每个 body 每类最多一条）。重复 key 取最后一个值。
- **嵌套超过 200 层**（只数对象和数组）：按"声明是 JSON 但解析失败"处理。charset 不是文本编码（如 `hex`）时按 UTF-8 解码。
- **孤立的 UTF-16 代理字符**（如 `"\ud800"`）：替换成 `U+FFFD`，不产生 warning。

**响应头对比**：默认不比较。列在 `compare_headers` 里的头按以下规则：
- 候选缺失：breaking。
- 值变化：suspicious。
- `Content-Type` 的媒体类型变化：breaking；只改了 charset 等参数不算，不产生 finding。
- 基线没有、候选新出现：`header_changed`（suspicious）（D17）。
- 头名不区分大小写；伪路径里的名字按 `compare_headers` 的写法。

**请求怎么发**（D17）
- URL：`base_url` 去掉末尾的 `/`，拼上 `path`，再附加 `query`。`--baseline-url` / `--candidate-url` 覆盖对应一侧的 `base_url`，同样必须是 http(s) 地址，否则按命令行参数错误处理（退出码 2）。
- 请求头按 defaults → target → 请求级的顺序合并，名字不区分大小写，越具体越优先。
- 两侧同时发出；基线的 2 次串行，第 1 次失败（重试后）就不再发第 2 次。
- `--only` 写了 suite 里不存在的 name：命令行参数错误，退出码 2。
- `base_url`（含覆盖地址）不能带用户名或密码（`user:pass@`），否则按 suite / 参数错误处理（退出码 2）：URL 会写进日志和 run 目录，凭据只能放在请求头里（D18）。解析不了的地址（缺 `]`、端口越界、全角冒号等）同样是退出码 2。
- `timeout` 是**单次请求的总时限**（从发出到读完 body），不是 httpx 的单次读写超时，超过即按超时处理（参与重试）（D18）。
- 网络错误的原因只写异常类型和来自网络的说明；发送前校验请求时的错误（例如请求头的值有首尾空白、换行、非 ASCII 字符）只写固定说明，不能带出请求头的值（D18）。

### 7.3 业务成功条件 `success`
很多接口用 `200 {code, msg, data}` 返回业务错误，只看状态码会漏掉。`success` 是一条表达式，可以在 defaults 里写，也可以在单条请求上覆盖。表达式里能用 `status`、`headers`、`body` 这几个变量。

**求值范围**：只对 JSON 响应求值。HEAD、204、非 JSON 的响应跳过这一步。表达式抛出异常时，视为**不满足**，并产生 warning。

| 情况 | 处理 |
|---|---|
| 基线不满足 | 请求状态为 `inconclusive`（§7.2 第 5 条） |
| 基线满足、候选不满足 | 产生 `business_error`（breaking），**跳过 body 对比和 asserts** |
| `record` 时不满足 | 这条请求不写入快照，保留旧 sample |

### 7.4 finding 类型与默认级别

| 类型 | 触发条件 | 默认级别 |
|---|---|---|
| `status_changed` | 状态码不同 | breaking |
| `body_kind_changed` | 一侧是 JSON，另一侧不是 | breaking |
| `business_error` | 见 §7.3 | breaking |
| `header_missing` / `header_changed` | 见 §7.2 响应头对比 | breaking / suspicious（Content-Type 媒体类型变化记 breaking） |
| `field_removed` | 基线有、候选没有的字段 | breaking |
| `type_changed` | JSON 类型变化，**包括对象或数组变成 null** | breaking |
| `nullability_changed` | **标量**字段在 null 与非 null 之间变化 | 非 null → null：suspicious；null → 非 null：info |
| `scale_changed` | 配了 decimal 的字段，小数位不同 | breaking（可以用 `scale_level` 修改） |
| `field_added` | 候选新增了字段 | info |
| `array_length_changed` | schema / positional 模式下，数组长度不同 | 空 ↔ 非空：suspicious；其他：info |
| `element_missing` / `element_added` | keyed 模式：某个 key 只在一侧出现。positional 模式：超出另一侧长度的下标 | suspicious / info |
| `value_changed` | 配了 `on_value_change` 或 `compare: values` 才会比较。unordered 模式下集合不相等时，记在数组路径上 | 由规则决定；`compare: values` 时为 suspicious |
| `assert_failed` | assert 结果为假 | 由 assert 决定，默认 suspicious |
| `assert_error` | assert 求值时抛出异常 | suspicious（固定） |

**补充说明**
- **类型**：取值为 object、array、string、number、boolean、null。int、float、Decimal 都算 number，所以 `1` 和 `1.0` 不算类型变化。
- **只报最高层节点**：对象被删除、或者类型变化（包括变成 null）时，只在这个节点上报一条，不再展开它的子字段。
- **`nullable: true`**：声明这个字段可以是 null。标量在 null 与非 null 之间变化时不再报 `nullability_changed`；对象、数组与 null 之间变化时也不再报 `type_changed`（D12）。两个非 null 类型之间的变化照常报。
- **字段改名**：会报成 `field_removed` 和 `field_added` 两条。这符合预期，doc/user 里要配一个示例说明。
- **一个节点最多报一条结构类 finding**（缺失、类型、可空性三者之一）。值类 finding 可以叠加在上面：
  - `scale_changed` 和 `value_changed` 可以同时出现。
  - 标量在 null 与非 null 之间变化、并且配了值比较（`on_value_change` 或 `compare: values`）时，除了 `nullability_changed`，还按值比较的级别报 `value_changed`；`nullable: true` 只去掉前者（D10）。
- **数值相等**：值比较按数值进行，`1` 等于 `1.0`；`NaN` 和 `NaN` 视为相等。

### 7.5 decimal 规则
- **小数位怎么算**：按 JSON 原文计算。

  | 原文 | 小数位 |
  |---|---|
  | `1.10` | 2 |
  | `1.1` | 1 |
  | `1` | 0 |
  | `1.50E1` | 1 |
  | `1E2` | 0 |
  | `-0.00` | 2 |

- **判定**：两侧小数位不同时，记 `scale_changed`。只有同时配了 `on_value_change`，才会比较数值本身。
- **`allow_string: true`**：`"12.50"` 这种字符串也按 decimal 处理。但如果一侧是字符串、另一侧是数字，仍然算 `type_changed`。字符串必须符合 JSON 数字的写法，`"abc"`、`"NaN"`、`"1_000"` 不参与小数位比较；值比较时 `"12.50"` 和 `"12.5"` 相等。

### 7.6 数组对齐

| mode | 行为 | 适用场景 |
|---|---|---|
| `schema`（默认） | 把所有元素合并成一张表：每个字段出现过哪些类型、哪些小数位。只比较两侧的这张表和数组长度，不逐个比较元素 | 分页列表、实时数据 |
| `keyed` | 按 `keys` 配对元素，可以用多个字段组合成一个 key。配对上的元素递归比较 | 数据固定、元素有 id 的数组 |
| `positional` | 按下标配对，配对上的元素递归比较 | 顺序有意义的固定数组 |
| `unordered` | 把元素当作多重集合比较值，属于值比较 | 顺序不定的小型固定数组 |

**schema 模式的细则**
- **字段消失**：某个字段在基线的任一元素里出现过，而候选的所有元素里都没有，记 `field_removed`。
- **类型比较**：先去掉 null，再比较两侧的类型集合；候选出现了新的类型，记 `type_changed`，不再比较它下面的字段。
- **null 的出现与消失**（D11，与 D2 一致）：
  - 候选新出现了 null：基线在这里出现过对象或数组时记 `type_changed`（breaking），否则记 `nullability_changed`（suspicious）。
  - 基线只出现过 null、候选出现了值：值里有对象或数组时记 `type_changed`（breaking），否则记 `nullability_changed`（info）。
  - 基线出现过 null 和值、候选只剩值：记 `nullability_changed`（info）。
  - 配了 `nullable: true` 时，以上都不报（D12）。
- **子字段的缺失要看父节点**：只有父节点在另一侧仍然出现过对象（或数组），子字段（或数组元素）的消失和新增才单独报。父节点在另一侧已经不是对象，说明变化发生在父节点上，子字段不再重复报。
- **decimal**：候选的小数位集合不是基线的子集，记 `scale_changed`。finding 里同时给出两侧的小数位集合，以及一个示例下标。
- **一侧为空、另一侧非空**：只比较长度，并产生 warning："某侧为空，未比较元素结构"。两侧都为空时不比较，也不提示（D15）。
- **嵌套数组**（D14）：schema 数组元素里的数组也并入同一张表，按 schema 方式比较，示例下标是外层数组的下标。
  - 内层数组的长度不比较。
  - 内层数组在一侧全部为空、另一侧有元素时，不报 `field_removed` / `field_added`，只产生 `array_empty` warning。
  - 外层元素没有配对，内层也无法配对：写在内层数组上的非 schema `array` 规则不生效，运行时产生 `rule_ineffective_in_schema` warning，`validate` 能静态判断时也给出。
- **值比较不生效**：`on_value_change` 和 `compare: values` 在 schema 数组内部不起作用，运行时产生 warning（同一个泛化路径只提示一次）。如果需要比较值，改用 keyed 模式。
- **keyed 降级**：key 字段缺失或者重复时，这个数组降级为 schema 模式，并产生 warning。元素不是对象、key 的值是 null、对象或数组，都按"缺失"处理。

**keyed 模式的细则**
- **key 相等**：按类型和值比较，数字按数值（`1` 与 `1.0` 是同一个 key），`1` 与 `"1"` 是不同的 key。
- **路径展示**：多个 key 用逗号连接，如 `items[id=1,color=red]`；字符串值会和数字、`true`/`false`/`null` 混淆，或含空白、`,`、`=`、方括号、引号时，加双引号。

**unordered 模式的细则**（D13）
- **结构**：元素的结构按 schema 模式比较（字段消失、类型、可空性、decimal 都和 schema 一样），所以元素里删字段、改类型仍是 breaking。数组长度不单独报。
- **值**：把元素当作多重集合比较，不相等时在数组路径上记一条 `value_changed`，级别取数组节点的 `on_value_change`，没配时为 suspicious。
  - 数字按数值比较；配了 `decimal.allow_string` 的字段，数字字符串也按数值比较。
  - 元素里被 ignore 的字段不参与比较。
  - 元素里的嵌套数组：配了 `mode: positional` 时按顺序比较，其他情况按多重集合比较。
- **元素内部的值规则不生效**：元素字段上的 `on_value_change` 和 schema 模式一样产生 `rule_ineffective_in_schema` warning。

### 7.7 asserts（高级用法，可选）

```yaml
asserts:
  - expr: "len(cand.body.data['items']) >= len(base.body.data['items'])"
    level: suspicious
    message: "订单条目变少"
```

- **变量**：`base` 和 `cand` 各有 `.status`、`.headers`（不区分大小写）、`.body` 三个属性。
  - body 里的字段可以用点号访问。
  - 字段名和函数名冲突、或者不是合法标识符时，改用 `["..."]`。
  - 原始响应不受 ignore 影响。
- **内置函数**：`len`、`scale`、`get(obj, "path", default)`、`exists(obj, "path")`、`is_number`、`is_string`、`keys`、`set`、`all`、`any`、`abs`、`min`、`max`。
- **执行条件**：只在请求状态为 `ok`、状态码相同、两侧都是 JSON、并且没有产生 `business_error` 时执行。
- **校验**：只能使用白名单里的语法和函数。`validate` 和每次运行开始前都会检查，不通过时退出码为 2。
- **语法范围**（`success` 和 asserts 相同）：
  - 字面量：整数、小数、字符串、`True` / `False` / `None`、列表、元组。**小数字面量按原文转成 Decimal**（`1.50` 的小数位是 2），整数保持整数。
  - 运算：`+ - * / // %`、比较（含 `in`、`not in`、链式比较）、`and` / `or` / `not`、`a if c else b`。
    - `/` 按 Decimal 精确计算，整数相除也一样（`1 / 10 == 0.1`）；`//` 和 `%` 按 Decimal 语义向零取整（`-7 // 2 == -3`），两边都是整数时结果仍是整数。
    - **按 JSON 类型运算**：布尔值不是数字。`==` 两边类型不同时为假（`false == 0`、`true == 1` 都为假）；`in` 判断数组元素时用同样的规则（`true in [1, 2]` 为假）。数组与数组（含列表字面量、元组）逐个元素比较。NaN 不等于任何值，包括它自己。
    - `+`：数字相加、字符串拼接、数组拼接，拼接结果不能超过 10000 个元素/字符；`-`：数字相减或集合求差；`* / // %`：只能用于数字。布尔值参与算术运算算求值出错。
    - `< <= > >=`：两边都是数字或都是字符串；其他组合（含布尔值、NaN）算求值出错。
    - `in`：右边是对象时判断 key，是数组或集合时判断元素，是字符串时判断子串（左边也必须是字符串）。
    - `is` / `is not` 只能和 `None`、`True`、`False` 比较，比较值用 `==`。
    - 类型不匹配时，错误信息用 JSON 类型名描述（如 `+ 不能用于数字和字符串`），不出现 Python 内部类型名。
  - 访问：`.字段`、`["字段"]`、`[下标]`（可以是负数，不能是布尔值）。`[...]` 只能用在对象、数组、字符串上。`.` 只能用在对象上；`base`/`cand` 只有 `status`、`headers`、`body` 三个属性，`status` 是整数；`base`/`cand` 本身不能直接参与运算或比较，只能写 `cand.status` / `cand.headers` / `cand.body`，或者作为 `get` / `exists` 的第一个参数。字段不存在、下标越界、对 `null` 用 `.` 都算求值出错。字段名是 Python 关键字（如 `from`、`class`）、含全角字符或以 `_` 开头时，必须用 `["..."]`。
  - 推导式：`[x.price for x in ... if ...]` 和生成器表达式。**生成器只能作为 `all` / `any` / `set` / `min` / `max` 的唯一参数**，其他位置请写成列表 `[...]`。生成器是惰性的，`any`/`all` 得到结果就停止。`for` 只能遍历数组、对象（得到 key）、字符串。循环变量只能是单个名字，只在推导式内部可见，不能以 `_` 开头，不能和变量名（`status`、`headers`、`body`、`base`、`cand`）、函数名重名。
  - 函数调用的参数个数在校验时检查。函数的参数类型：`len` 用于对象、数组、字符串；`abs` 用于数字；`min` / `max` 的元素必须全是数字或全是字符串，不能为空、不能含 NaN；`set` 的元素不能是对象或数组，也不能同时有布尔值和数字；`all` / `any` 用于数组或生成器。
  - 不支持：`**`、位运算、切片、dict/set 字面量、f-string、lambda、海象运算符、关键字参数、`*args`、无效的字符串转义（如 `'\d'`），以及任何方法调用（如 `x.keys()`，改用 `keys(x)`）。表达式最长 2000 个字符。
- **计算量限制**：一次求值中推导式最多迭代 10000 次；整数乘积不超过 8192 位；另有总计算量上限（按访问的 JSON 节点数和元素数累计）。超出时算求值出错。Decimal 运算精度为 50 位有效数字。
- **`get` / `exists` 的路径**：用 §6.1 的路径语法，只能写 key 和字面下标，不能用 `[*]`、`**`；`$` 表示对象本身。第一个参数可以是 body 里的对象/数组、列表字面量、`keys()` 的结果，也可以是 `headers` 或 `base`/`cand`（如 `get(cand, "body.total", 0)`）。路径上任一步缺失（key 不存在、下标越界、走进了标量）时，`get` 返回默认值（不写为 `None`），`exists` 返回假；值为 `null` 也算存在。
- **`headers`**：取值和 `in` 不区分大小写；`keys(headers)` 返回小写的名字。
- **结果必须是 `True` 或 `False`**：结果是元组、列表等在校验时就报错（常见于误写的逗号，如 `"a == 1, 'msg'"`）；运行时结果不是布尔值（例如 `body.code`）算求值出错。`success` 为 `True` 即满足；assert 为 `False` 即 `assert_failed`。
- **缺失的环境变量**：表达式原文里引用了没有设置的 `${VAR}` 时，`validate` 跳过这条表达式的检查，设置变量后再检查。
- **建议**：路径可能缺失时用 `get(...)`，可以减少 `assert_error`。

### 7.8 不稳定字段建议
- **前提**：只有在基线有 2 个 sample，而且两个 sample 都满足 `success` 时，才会计算。
- **比较方式**：按 `compare: values` 的方式比较基线的这 2 个 sample，数组按配置对齐。以下两种字段会被列出：
  - 值不一样的叶子字段
  - 只在其中一个 sample 里出现的**对象字段**
- **以下情况不计入建议**：
  - 数组元素多出来或者少了（只是数据量变化）
  - schema 数组内部的值
  - 已经被 ignore 的路径
- **输出**：
  - 每条建议给出泛化路径，以及一段可以直接复制的 YAML 规则。
  - 这些字段**不会被自动忽略**。
  - `record` 的 stdout 也会打印这些建议。

## 8. 快照

### 8.1 内容
```
snapshot/
  manifest.json      # schema_version、工具版本、每条请求的指纹、录制时间和来源（record / accept）
  requests/<name>.json
```

- **每个 sample 保存的内容**：状态码、`Content-Type` 及 `compare_headers` 列出的响应头、body 原文、耗时。
- **不保存请求头的值**，所以 token 和 Set-Cookie 不会落盘。
- **写入是原子的**：写到一半中断，不会留下半份快照。

**格式细则**（D20）
- `manifest.json`：`schema_version`（当前 1）、`tool_version`、`fingerprint_version`、`updated_at`，以及 `requests`：每条请求的 `fingerprint`、`recorded_at`、`source`（`record` / `accept`）、sample 数。
- `requests/<name>.json`：和 run 目录的 `baseline/<name>.json` 格式相同（§9）。record 写 1–2 个 sample：基线第 2 次请求失败、不是 JSON 或不满足 `success` 时只写第 1 个（同时给 `second_sample_unavailable`）；accept 写 1 个。
- **版本**：`schema_version` 不是当前版本时按"快照 manifest 版本不兼容"处理（退出码 2）；`fingerprint_version` 不同时，所有请求都按指纹不一致处理。manifest 解析不了、manifest 列出的请求文件缺失，算快照损坏（退出码 3）。compare 读到解析不了的请求文件也算损坏；record / accept 只检查 manifest 和请求文件是否存在，没变的请求文件按字节原样复制，不解析（大快照上 `--only` 不必解析全部 body）。
- **快照目录的保护**：`--dir` 指向已存在的非空目录时，必须是快照（含 `manifest.json`），否则拒绝写入（退出码 3），在发请求之前检查。`--clean` 只要求含 `manifest.json`，不读它的内容，所以损坏或版本不兼容的快照可以用 `--clean` 重录。
- **原子替换**：先在同级目录写好完整的新快照（`.<name>.tmp-*`），再 `snap → snap.old`、新目录 → `snap`，最后删除 `snap.old`（新快照已就位后删除失败只给 WARNING 日志，不算写入失败）。record / accept 开始时处理上次中断的残留：`snap` 不存在而 `snap.old` 存在时恢复它，`snap` 存在时删除 `snap.old`；删除 `.<name>.tmp-*`。**compare 只读**：不删除、不改名任何东西；`snap` 不存在而 `snap.old` 存在时直接读 `snap.old`（给 WARNING），留给下次 record / accept 恢复。不支持多个命令同时写同一个快照。

**各命令怎么用快照**（D20）
- **record**：每条请求按 §7.2 的规则只对基线判定（两侧都看成基线）：`ok`、`unsupported` 写入；`error`、401/403/429、5xx、不满足 `success` 不写入，保留旧 sample。快照里有、suite 里没有（或不在 `--only` 里）的请求原样保留。`--clean` 时新快照只包含本次写入的请求。**一条都没写入时不改动快照，`--clean` 也一样**（基线不可用或 token 过期时，不能把好的快照清空）。
- **compare**：快照目录不存在时当作空快照（每条请求都是"快照里没有这条请求"）。快照缺失或指纹不一致的请求照样请求候选，并写进 run 目录的 `candidate/`，所以随后可以 `accept`。快照里有 2 个 sample 时照常给不稳定字段建议。

### 8.2 请求指纹
- **计算依据**：method、path 和 query 的**模板**（`${VAR}` 替换前）、请求头的**名称**。
- **不参与计算**：target 级的 headers 和 base_url 都不算，所以 S1、S2 可以共用同一份 suite。
- **不一致时**：请求状态为 `inconclusive`，处理方法是 `snapshot record --only NAME` 或者 `snapshot accept`。

### 8.3 accept
- **来源与写入方式**：读取 run 目录里候选的响应和指纹，按请求合并进快照，可以用 `--only` 限定范围。快照不存在时会新建。
- **可以接受**：状态为 `ok` 的请求，以及**只是因为指纹不一致或快照缺失**而变成 `inconclusive` 的请求。所以新增请求、改了 query 的请求，都可以直接 accept。
- **会拒绝**：`error`、因为鉴权/限流/基线不可用/业务失败而 `inconclusive`、以及产生了 `business_error` 的请求。被拒的请求会在 stdout 里列出，退出码为 4。
- **判定细则**（D20）：被接受的候选响应会成为新的基线，所以除了看请求状态，还按 record 的标准检查候选本身。按顺序，第一条匹配的生效：
  1. 请求是 `error`：拒绝。
  2. 请求是 `inconclusive`，但不是因为快照缺失或指纹不一致：拒绝。
  3. run 目录里没有这条请求的候选响应，或候选是网络错误：拒绝。
  4. 候选返回 401 / 403 / 429：拒绝（检查候选的 token 或限流）。
  5. 候选返回 5xx：拒绝（不能作为基线）。
  6. 候选不满足 `success`（包括产生了 `business_error`，以及快照缺失时候选不满足 `success`）：拒绝。
  7. 其他（`ok`、`unsupported`、只因快照而 `inconclusive`）：接受。
- **需要的信息**：accept 不读 suite，判定所需的信息都来自 run 目录：`result.json` 的每条请求记录 `snapshot_problem`（`missing` / `fingerprint_mismatch`，只有 compare 会填）和 `candidate_success`（候选的 success 结果，没有求值为 null；状态码不同时流程虽然就此结束，也照样对候选求值，只是出错时不给 warning）；候选响应和指纹来自 `candidate/<name>.json`。run 目录不是 api-regress 写的、解析不了，或请求名不符合 name 的写法（防止改过的文件让路径穿出目录）时退出码 3；快照 manifest 里的请求名同样检查，不合法算快照损坏。
- **只有 1 个 sample**：被接受的请求只有 1 个 sample，见 §11 L4。

## 9. 输出

**run 目录**

| 文件 | 内容 |
|---|---|
| `report.html` | 单个文件，不依赖外部资源；响应内容一律转义 |
| `report.md` | 适合贴到 PR 或 IM |
| `junit.xml` | 每条请求是一个 testcase：有达到 `--fail-on` 级别的 finding 记 failure；否则 `error` 或 `inconclusive` 记 error；`unsupported` 记 skipped（D22） |
| `result.json` | 结构化结果，契约见 design §4，供以后的界面使用 |
| `baseline/`、`candidate/` | 两侧响应。`candidate/` 给 `accept` 用，`baseline/` 用于排查 |

**报告内容**
1. **汇总**：请求数、各级别 finding 数、各请求状态的数量、退出码及原因；本次的对比模式（`structure` / `values`）（D24）。
2. **每条请求**：
   - 完整的请求 URL
   - 两侧的状态码、耗时、重试次数
   - 可以直接复现的 curl 命令（请求头的值显示为 `<redacted>`）
   - finding 列表：具体路径、泛化路径、类型、级别、两侧的值、命中的规则，以及 finding 的说明（asserts 的 `message` 等）
   - **报出 finding 的 suite 配置要能在报告里直接看到**（D24），不用回头翻 suite：命中的规则写出位置、路径和规则内容（如 `rules[0] data.amount（on_value_change: breaking）`）；`assert_failed` / `assert_error` 写出是第几个 assert 和它的表达式（配了 `message` 时表达式也要显示）；`business_error` 写出 `success` 表达式及其来源（`defaults.success` 或请求级 `success`）
   - 两侧 body 的对照 diff（只在 HTML 报告里有，过大时截断）：两侧并排，只显示有差异的行和上下 3 行；行内差异只在被替换的一段里按顺序一对一配对后高亮，超过 200 字符的行不做行内高亮、整行标色。生成耗时随行数线性增长
3. **不稳定字段建议**：附带 YAML 片段。
4. **本次未命中的规则**：
   - **统计范围**：本次实际运行的请求。
   - **判定**：规则在两侧的原始 body 里都没有匹配到任何路径，就算未命中。这个判定和对比模式无关。
   - **措辞**：只提示"本次未命中"，不直接说规则已过期。
5. **warning 列表**

报告样例见 §12.2。

**run 目录的细则**（D17）
- `baseline/<name>.json`、`candidate/<name>.json`：`name`、`fingerprint`、`fingerprint_version`，以及 `samples` 列表（基线 1–2 个，候选 1 个）。每个 sample 存 URL、状态码、`Content-Type` 和 `compare_headers` 列出的响应头、body 原文（UTF-8 解码失败时存 `body_b64`）、耗时、重试次数；网络错误的 sample 只有 URL、`error` 和重试次数。格式和快照相同，不保存请求头。
- `--out` 指向已存在的非空目录时，只有它是之前的 run 目录（`result.json` 能解析，且有 `schema_version`、`command`、`tool_version`）才复用，并先清掉其中的 `baseline/`、`candidate/`；否则拒绝写入，退出码 3。`--log-file` 指向的文件不算"无关文件"。默认目录同一秒内重复时加 `-2`、`-3` 后缀（D18）。
- **`--out` 在发请求之前检查**，不能用就一个请求都不发；写入阶段失败时，返回值和 stdout 里的退出码、原因都是 3，stdout 的 result 行写"未写入"（D18）。
- **取消**（`cancel_event`）：已经发出的请求会跑完；还没开始的请求（包括在排队等并发名额的）记为 `error`（原因"运行已取消"），`result.json` 里 `cancelled` 为真（D18）。
- **单条请求出了工具自身的错误**（意外异常）：这条记为 `error`，原因写异常类型，其他请求照常，`result.json` 照常写，退出码 3。进度回调抛异常同样记为退出码 3，不中断运行（D18）。
- **本次未命中的规则**：只统计至少有一侧拿到 JSON body 的请求。defaults 里的规则在所有这些请求里都没命中才列出（只列一次）；请求级规则按请求列出。
- **报告的写入**（D22）：顺序是两侧响应 → 报告 → `result.json`（最后写）。报告先全部在内存里生成：某个报告生成失败时，退出码改成 3（原因写明是哪个报告），其余报告按改过的结果重新生成，所以写出的报告、`result.json` 和进程的退出码一致。写文件失败（例如磁盘满）时删掉本次已经写出的报告，不写 `result.json`，退出码 3。
- **复用 run 目录**（D22）：先删掉旧的 `result.json`，再删两侧响应和旧报告（`report.html`、`report.md`、`junit.xml`）。这样本次中途失败或被打断时，目录里没有 `result.json`，不会出现旧结果配新响应、被 `accept` 误用的情况。没有 `result.json`、只有两侧目录（里面只有 `.json` 文件）和报告的目录，视为上次中断留下的 run 目录，可以复用。
- **报告里怎么展示**（D22）：Markdown 只展开有 finding 或状态不是 `ok` 的请求（`unsupported` 也展开，附原因），其余合成一行，表格和列表里转义 `|`、`*`、`_` 等 Markdown 符号；HTML 全部列出，有问题的默认展开。schema 数组 finding 两侧的集合写成 `{null, object}`、`小数位 {1, 2}`；字段缺失写 `(缺失)`；字符串加引号，数字按原文小数位（对象、数组里嵌套的数字也一样）。curl 只带 suite 里配置的请求头名字，值写 `<redacted>`。

## 10. 非功能需求
- **语言与协议**：Python 3.12；配置用 YAML；外部交互只走 HTTP/HTTPS。
- **代码约束**：
  - 用 radon 检查，每个函数的等级不超过 B（CC ≤ 10）。
  - 单个文件不超过 1000 行。
  - 只在复杂逻辑处写注释。
- **可扩展**：
  - core 是纯库，以后的 Web 界面直接调用 core，不需要改动对比逻辑。
  - core 不依赖 CLI 和报告模块。
  - 分层与接口见 design §1。
- **日志**：
  - 以下关键节点必须记录：suite 加载结果、每条请求的发出和返回（target、方法、URL、状态码、耗时、重试次数）、每条请求的状态和 finding 数、汇总、文件写入路径、退出码及原因。
  - 任何级别的日志都**不输出请求头的值**。
  - 详细规定见 design §6。
- **文档**：
  - `doc/user/`：命令、suite 格式、YAML 常见坑、规则与路径、数组模式怎么选、success 和 asserts、怎么看报告、退出码。
  - `doc/dev/`：模块职责、关键路径（只写文件名或函数名，不写行号）、主流程图、result.json 契约。
  - `doc/spec/`：需求文档。
  - `README.md`：产品简介和快速上手。

## 11. 已知限制与风险

| # | 限制 | 应对 |
|---|---|---|
| L1 | 基线和候选的数据源不同时，值比较几乎全部是差异，可空字段和数组长度也会频繁变化 | 默认只比结构；可空性和长度变化做了降级 |
| L2 | 不稳定字段建议只基于 2 个 sample，低频变化的字段（按小时变、缓存、A/B 实验）会漏掉 | 只作为建议；以后可以考虑采样 N 次 |
| L3 | 基线和候选不是同时请求的，中间数据可能发生变化 | 以结构对比为主 |
| L4 | accept 进来的请求只有 1 个 sample，所以没有不稳定字段建议 | 需要时用 `record --only NAME` 重录 |
| L5 | 不做脱敏，响应体会进入快照、报告和 run 目录 | 请求头的值和大部分响应头不落盘；文档提醒不要把快照和报告提交到公开的地方 |
| L6 | schema 模式不比较单个元素的值 | 改用 keyed 模式，或者写 asserts |
| L7 | 动态 key 的对象（例如 `{"10086": {...}}`）每次都会报字段增删 | 用 ignore；以后可以考虑 map 模式 |
| L8 | `run` 对基线发 2 倍的请求，会给生产环境带来额外负载 | 用 `--only` 和 `concurrency` 控制 |
| L9 | 只在候选侧不稳定的字段，建议里不会列出 | 接受这个限制 |
| L10 | 分页列表或实时列表用 keyed 模式，会因为数据移动而持续报 `element_missing` | 这类数组用 schema 模式（文档里给出选型表） |
| L11 | 后端用 float 输出金额、不保留末尾的 0（如 `12.5`）时，原本就是 2 位的值也可能显示成 1 位，无法判断精度是否变了 | 文档说明：decimal 规则需要后端稳定输出固定的小数位 |
| L12 | 请求 name 区分大小写，`order-list` 和 `Order-List` 是两条请求；快照文件按 name 命名，在不区分大小写的文件系统（macOS 默认、Windows）上会互相覆盖 | 文档提醒不要用只差大小写的 name |

## 12. 端到端示例与验收

### 12.1 S2 走查（使用 §5 的 suite）

| 步骤 | 命令 | 预期结果 |
|---|---|---|
| 1 | `api-regress validate suite.yaml` | `OK, 3 requests`，退出码 0 |
| 2 | 发版前执行 `BASE_TOKEN=… api-regress snapshot record suite.yaml -d snap/` | 3 条写入；stdout 列出建议，例如 `data.server_time` 建议 ignore；退出码 0 |
| 3a | 发版后执行 `snapshot compare`；场景：`coupon_amount` 改名为 `discount_amount` | 产生 `field_removed`（breaking）和 `field_added`（info）；退出码 1 |
| 3b | 场景：列表里的 price 从 `12.50` 变成 `12.5` | `order-list` 产生 `scale_changed`（breaking），路径为 `data.list[*].price`，附示例下标；退出码 1 |
| 3c | 场景：候选的 token 过期，返回 401 | 3 条请求都是 `inconclusive`，提示检查候选 token；退出码 4 |
| 3d | 场景：列表顺序发生变化 | schema 模式下没有 finding；退出码 0 |
| 3e | 场景：有意修改了 `order-detail` 的返回 | 执行 `snapshot accept out/<ts> -d snap/ --only order-detail`；之后再 `snapshot compare`，退出码 0 |

### 12.2 报告样例（Markdown 节选）

```
## 汇总  退出码 1：存在 breaking
请求 3 | ok 3 | breaking 1 | suspicious 0 | info 1

### order-detail  GET /v1/orders/10086  200 → 200
| 级别 | 类型 | 路径 | 基线 | 候选 |
|---|---|---|---|---|
| breaking | field_removed | data.coupon_amount | "5.00" | (缺失) |
| info | field_added | data.discount_amount | (缺失) | "5.00" |

### 建议 ignore
- data.server_time（基线两次的值不同）
    - path: data.server_time
      ignore: true
```

### 12.3 验收标准
所有用例都用本地 mock HTTP 服务自动化执行，**mock 服务返回固定内容**。

**配置与命令**

| ID | 验收项 |
|---|---|
| AC-01 | suite 出现未知字段、写方法、重复 name，或缺少本命令需要的 `${VAR}` 时：`run` 返回 2，并指出出错行；`validate` 只把缺少的变量列为 warning |
| AC-02 | `${VAR:-x}` 在变量未设置时取 `x`；数值字段写成 `"${T}"` 能正确转换类型 |
| AC-03 | `record` 只用 baseline，`compare` 只用 candidate；缺少另一侧的变量时照常运行 |

**结构对比**

| ID | 验收项 |
|---|---|
| AC-04 | 字段被删除时，产生 `field_removed`（breaking），退出码 1 |
| AC-05 | number 变成 string 时，产生 `type_changed`（breaking）。对象 `data` 变成 null 时，产生 `type_changed`（breaking），且只有这 1 条 finding |
| AC-06 | 标量从非 null 变成 null 时，产生 `nullability_changed`（suspicious），退出码 0；配了 `nullable: true` 后不再报 |
| AC-07 | 没有 decimal 规则时，`1` 变成 `1.0` 不产生 finding。有规则时，`12.50` 变成 `12.5` 产生 `scale_changed`。§7.5 表中每个示例的小数位都计算正确 |
| AC-08 | `compare: structure` 下值变化不产生 finding；配了 `on_value_change: suspicious` 后产生 suspicious |
| AC-09 | 新增字段时产生 info，退出码 0 |

**状态与流程**

| ID | 验收项 |
|---|---|
| AC-10 | 200 变成 500 时，只产生 `status_changed`（breaking），不再出现 body 和 success 相关的 finding |
| AC-11 | 基线返回 500 或 401、或候选返回 401 时，请求为 `inconclusive`，退出码 4 |
| AC-12 | 没有配 `success` 时，两侧都返回 404：请求为 `ok` 并产生 warning，退出码 0 |
| AC-13 | 一侧返回 JSON、另一侧返回 HTML 时，产生 breaking。两侧都返回 HTML 时，请求为 `unsupported`，退出码 0 |
| AC-14 | 连接被拒绝时，重试 1 次后请求为 `error`，其余请求照常执行，退出码 4 |
| AC-15 | 同一次运行里既有 breaking 又有 error 时，退出码为 1；同时存在配置错误时，退出码为 2 |

**数组**

| ID | 验收项 |
|---|---|
| AC-16 | 数组顺序打乱：keyed 模式不产生 finding；`compare: values` 下用 positional 模式会产生 `value_changed`；`compare: structure` 下用 positional 模式，如果元素结构相同，不产生 finding |
| AC-17 | keyed 模式下 key 重复时，降级为 schema 模式并产生 warning；finding 的路径格式为 `[k=v]` |
| AC-18 | schema 模式下，候选的所有元素都缺少某个字段时，产生 `field_removed`；候选数组为空时，只产生 `array_length_changed`，外加 warning |
| AC-19 | schema 模式加 decimal 规则：`items[*].price` 出现 1 位小数时，产生 `scale_changed`，并附带示例下标 |
| AC-20 | positional 模式下候选多出 2 个元素时，产生 2 条 `element_added` 和 1 条 `array_length_changed` |

**规则**

| ID | 验收项 |
|---|---|
| AC-21 | `ignore` 会作用到整棵子树。请求级的 `ignore: false` 能覆盖全局的 `**.x: ignore`。父节点已经被 ignore 时，子节点的 `ignore: false` 不生效，并产生 warning |
| AC-22 | §6.3 的两个示例，以及优先级比较的每一项，各有一个用例，全部通过 |

**success 与 asserts**

| ID | 验收项 |
|---|---|
| AC-23 | 候选返回 `200 {code:500}` 时，产生 `business_error`，不再有 body 相关的 finding。基线不满足 success 时，请求为 `inconclusive`。success 表达式抛异常时，视为不满足并产生 warning。HEAD 请求跳过 success |
| AC-24 | assert 为假时，产生 `assert_failed`；抛异常时，产生 `assert_error`，其余 assert 照常执行 |
| AC-25 | `cand.body.data.items` 取到的是字段值，而不是 dict 方法；`x.items[0]` 和 Decimal 的运算结果正确；`__import__`、`x._y`、`obj.method()` 在 `validate` 阶段就被拒绝 |

**快照与 accept**

| ID | 验收项 |
|---|---|
| AC-26 | 先 `record` 再 `compare` 同一个固定的服务，结果是 0 条 finding |
| AC-27 | 修改了 query：`compare` 时该请求为 `inconclusive`；对它执行 `accept` 后再 `compare`，结果是 0 条 finding。新增一条请求时，结果同样如此 |
| AC-28 | `record --only A` 只更新 A，其他请求保持不变；`record` 过程中失败的请求保留旧的 sample，退出码 4 |
| AC-29 | `accept --only A` 只更新 A；状态为 error 的请求会被拒绝，退出码 4 |

**输出**

| ID | 验收项 |
|---|---|
| AC-30 | 基线两次请求中，值不同的字段和只出现一次的对象字段都会列入建议；数组元素数量的增减不会列入；建议给出的 YAML 能被正常解析；这些字段不会被自动忽略 |
| AC-31 | "本次未命中的规则"只列出真正没命中的规则；写在 schema 数组元素上、已经命中的规则不会出现在这里 |
| AC-32 | 响应里有 `<script>` 时，`report.html` 中会被转义 |
| AC-33 | `junit.xml` 能被标准解析器读取，failure 和 error 的数量符合 §9 的定义 |
| AC-34 | `result.json` 能通过仓库里已提交的 JSON Schema 校验；同一个差异在两次运行中的 `finding_id` 相同；keyed 数组中不同元素的 finding_id 互不相同 |

**日志与工程**

| ID | 验收项 |
|---|---|
| AC-35 | INFO 日志包含 §10 列出的全部节点。在请求头里放一个哨兵 token，它不会出现在 stderr、日志文件、快照、run 目录和报告的任何地方 |
| AC-36 | `radon cc -n C src/` 没有输出；`src/` 下没有超过 1000 行的文件；import-linter 检查通过 |
| AC-37 | 启动 demo 服务后，`pass.yaml` 退出码 0、没有 finding；`regress.yaml` 退出码 1，§4.2 列出的每种 finding 都出现在预期的请求和路径上；端口被占用时报错；`--write-suites` 不覆盖已有文件 |

## 13. 决策记录与待定问题

| # | 问题 | 状态 |
|---|---|---|
| D1 | 新增 `success` 业务成功条件（§7.3），用于发现"返回 200 但 body 里是业务错误"的情况 | 已确认（2026-11-09） |
| D2 | 标量在 null 与非 null 之间变化，记为 `nullability_changed`（suspicious/info）；对象或数组变成 null 仍记 breaking | 已确认（2026-11-09） |
| D3 | `array_length_changed` 默认记 info，只有空与非空之间的变化记 suspicious | 已确认（2026-11-09） |
| D4 | 文档拆成需求本体和 `spec-v1-design.md` 两份 | 已确认（2026-11-09） |
| D5 | CLI 框架用 argparse 还是 Typer | 用 Click：argparse 自带功能太少，子命令、参数类型校验等都要自己写（已确认，2026-11-10） |
| D6 | 请求级 `success: null` 的含义 | 表示该请求不判定 success，不再继承 defaults（已确认，2026-11-10） |
| D7 | 根节点（如响应体本身是数组）怎么写规则 | 单独的 `$` 表示根节点（§6.1）（已确认，2026-11-10） |
| D8 | 请求 name 查重是否区分大小写 | 区分大小写；快照文件名冲突的风险见 §11 L12（已确认，2026-11-10） |
| D9 | 规则正好落在被 ignore 的节点本身时是否提示 | 自身没写 `ignore` 时给 warning（§6.3）（已确认，2026-11-10） |
| D10 | 同一个节点上可以同时有哪些 finding | 结构类（缺失、类型、可空性）只报一条；值类可以叠加：`scale_changed` 与 `value_changed` 并存；标量变 null 且配了值比较时，另报 `value_changed`，`nullable` 不影响它（§7.4）（已确认，2026-11-10） |
| D11 | schema 数组里对象/数组字段与 null 之间的变化 | 与 D2 一致，记 `type_changed`（breaking）；标量仍记 `nullability_changed`（§7.6）（已确认，2026-11-10） |
| D12 | `nullable: true` 是否也放过对象/数组变成 null | 放过：`nullable` 表示 null 是合法值，对象、数组与 null 之间的变化不再报 `type_changed`（§7.4）（已确认，2026-11-10） |
| D13 | unordered 模式下元素的结构变化（删字段、改类型）怎么报 | 元素结构按 schema 模式比较，照常报 breaking；多重集合的值比较叠加在上面（§7.6）（已确认，2026-11-10） |
| D14 | schema 数组元素里的嵌套数组 | 一律按 schema 展平；内层长度不比较；内层一侧全空只给 warning；内层的非 schema `array` 规则给 warning（§7.6）（已确认，2026-11-10） |
| D15 | schema 数组两侧都为空时是否提示 | 不提示，只在一侧为空、另一侧非空时提示（§7.6）（已确认，2026-11-10） |
| D16 | 表达式里布尔值、生成器、`base`/`cand` 的语义 | 按 JSON 类型运算：布尔值不是数字（`false == 0` 为假，算术和比较大小报错）；生成器只能作为 `all`/`any`/`set`/`min`/`max` 的唯一参数；`base`/`cand` 不能直接参与运算（§7.7）（已确认，2026-11-10） |
| D17 | 实现 `run` 时补齐的细节：`unsupported` 仍比较状态码；"HEAD 或 204"的判定；响应头新出现记 suspicious，Content-Type 只改参数不报；URL 拼接与请求头合并顺序；`body_kind_changed` 的伪路径 `$body`；`--only` 未知 name 和非法覆盖地址按参数错误；run 目录格式与复用规则；取消；未命中规则的统计范围（§7.2、§6.2、§9） | 已确认（2026-11-11） |
| D18 | `run` 的安全与健壮性（T4 code review 后补充）：`base_url` 不能带 userinfo；`timeout` 是单次请求的总时限；网络错误原因不带请求头的值；`--out` 先检查、只复用 api-regress 写的 run 目录、日志文件不算无关文件、写入失败时退出码和原因一致；取消对排队中的请求生效；单条请求的意外异常不中断运行、退出码 3（§7.2、§9） | 已确认（2026-11-11） |
| D19 | 内置 `demo` 演示服务，方便不联网试用（用户提出，2026-11-11）。和非目标里的 Mock 的区别：数据固定、不可配置，只用于演示和自测（§2.3、§4.2） | 已确认（2026-11-11） |
| D20 | 实现 T5 时补齐的快照细节：manifest 字段与版本处理、快照目录的保护与 `--clean`、原子替换的残留恢复；record 只对基线按 §7.2 判定，`unsupported` 也写入，第 2 个 sample 不可用时只写 1 个，一条都没写入时（含 `--clean`）不改动快照；compare 只读，不清理残留；compare 在快照缺失时仍请求候选并写 run 目录；accept 按 record 的标准检查候选（候选 5xx 也拒绝），判定信息来自 `result.json` 新增的 `snapshot_problem`、`candidate_success`；accept 的 `--only` 未知 name 退出码 2（§4.1、§8） | 待确认（2026-11-11） |
| D22 | 实现 T6 时补齐的报告细节：`--format` 只对 `run` / `snapshot compare` 有效，可重复，`none` 只输出 `result.json`，非法取值退出码 2；junit 里 `unsupported` 记 skipped，failure 优先于 error；报告先在内存里全部生成再写，生成失败退出码 3 且其余报告和 `result.json` 的退出码一致，写文件失败删掉本次的报告、不写 `result.json`；`result.json` 最后写，复用 run 目录时先删旧的 `result.json` 和旧报告，中断留下的目录可以复用；Markdown 展开有问题的请求（含 `unsupported`）（§4、§9）。T6 code review 后修订（2026-11-11） | 待确认（2026-11-11） |
| D23 | HTML body 对照不再用 `difflib.HtmlDiff`（K21）：它在整段被替换的行里两两比较找最相似的行，1500 行都变了时要约 11 s，且每条请求各算一次。改为自己渲染并排表格：行级对齐用 `SequenceMatcher`，行内高亮只在 replace 块里按顺序一对一配对（git、diff2html 的做法），超过 200 字符的行整行标色；截断阈值不变（1500 行 / 20 万字符）。否决：降低截断阈值、body 过大只给提示（用户否决）；cydifflib（复杂度不变，只快约 2 倍）；diff2html 等 JS 库（违反报告不带 JS）（§9） | 已确认（2026-11-11） |
| D24 | 报告里补上报出 finding 的 suite 配置（用户提出，2026-11-11）：原来配了 `message` 的 assert 只显示 message、表达式丢失，`$assert[0]` 看不出是哪条；`business_error` 不显示 `success` 表达式；命中的规则只有路径，看不出级别由哪个属性决定；汇总里没有对比模式。改为在「命中的规则」一列写出规则内容、assert 表达式、`success` 表达式，汇总写出对比模式。只改报告，`result.json` 不变（§9） | 待确认（2026-11-11） |
| D21 | 快照相关命令收进 `snapshot` 命令组（用户提出，2026-11-11）：`record` / `compare` / `accept` 和 `run` 并列在顶层，用户分不清哪个是实时对比、哪个和快照有关。改为 `snapshot record` / `snapshot compare` / `snapshot accept`，快照目录参数从 `--snapshot` 改为 `-d` / `--dir`；不保留旧的顶层命令（v1 未发布）。`result.json` 的 `command` 仍是 `run` / `compare`，manifest 的 `source` 仍是 `record` / `accept`（§4） | 已确认（2026-11-11） |
