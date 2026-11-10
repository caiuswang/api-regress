# suite 配置格式

suite 是一个 YAML 文件，写明两个目标（基线、候选）和要发的请求。写完先用 `api-regress validate` 检查，见 [validate.md](validate.md)。

> 目前只实现了 `validate`。`timeout`、`concurrency`、`retries`、`compare_headers` 等运行相关的字段已经会被校验，在 `run` / `record` / `compare` 实现后生效。

## 完整示例

```yaml
version: 1

targets:
  baseline:
    base_url: https://api.shop.example.com
    headers:
      Authorization: "Bearer ${BASE_TOKEN}"
  candidate:
    base_url: https://api.shop.example.com      # 只有一个环境时，两侧可以是同一个地址
    headers:
      Authorization: "Bearer ${CAND_TOKEN}"

defaults:
  timeout: 10          # 秒
  concurrency: 4
  retries: 1           # 只对连接错误和超时重试，任何状态码都不重试
  compare: structure   # structure | values
  success: "body.code == 0"      # 只对 JSON 响应求值，见 expressions.md
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

## 字段

未知字段一律报错（防止拼写错误被悄悄忽略）。

**顶层**

| 字段 | 必填 | 说明 |
|---|---|---|
| `version` | 是 | 只能是 `1` |
| `targets` | 是 | `baseline` 和 `candidate` 两个目标，都必须写 |
| `defaults` | 否 | 所有请求共用的设置 |
| `requests` | 是 | 请求列表，至少一条 |

**`targets.baseline` / `targets.candidate`**

| 字段 | 必填 | 说明 |
|---|---|---|
| `base_url` | 是 | `http://` 或 `https://` 开头的完整地址 |
| `headers` | 否 | 只发给这一侧的请求头，例如各自的 token。值必须是字符串 |

**`defaults`**

| 字段 | 默认值 | 说明 |
|---|---|---|
| `timeout` | `10` | 单次请求超时，秒，必须大于 0 |
| `concurrency` | `4` | 并发数，至少 1 |
| `retries` | `1` | 重试次数，只对连接错误和超时重试，任何状态码都不重试 |
| `compare` | `structure` | `structure`：只比结构；`values`：所有字段都比较值，值不同记 suspicious。见 [compare.md](compare.md) |
| `success` | 无 | 业务成功条件，见 [expressions.md](expressions.md) |
| `headers` | `{}` | 两侧都发的请求头 |
| `rules` | `[]` | 对所有请求生效的规则 |

**`requests[]`**

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 只能包含 `A-Z a-z 0-9 _ . -`，suite 内唯一。**区分大小写**：`order-list` 和 `Order-List` 是两条请求，但在 macOS、Windows 上它们的快照文件会互相覆盖，不要这样起名 |
| `method` | 是 | `GET` 或 `HEAD`。写方法（POST/PUT/PATCH/DELETE）一律报错 |
| `path` | 是 | 拼在 `base_url` 后面的路径 |
| `query` | 否 | 查询参数，值必须是字符串（`page: "1"`） |
| `headers` | 否 | 这条请求额外的请求头 |
| `compare_headers` | 否 | 要比较的响应头名字，默认不比较任何响应头 |
| `success` | 否 | 覆盖 defaults 里的 `success`；写成 `success: null` 表示这条请求不判定 success（例如 `/health` 这类 body 里没有业务码的接口）；不写就沿用 defaults |
| `rules` | 否 | 只对这条请求生效的规则 |
| `asserts` | 否 | 同时比较基线和候选的表达式，每项有 `expr`、`level`（默认 suspicious）、`message`，见 [expressions.md](expressions.md) |

**`rules[]`**

| 字段 | 说明 |
|---|---|
| `path` | 规则作用的路径，写法见下文 |
| `ignore` | `true`：这个节点和整棵子树都不比较；`false`：取消更宽泛规则的忽略 |
| `decimal` | `{}` 或 `{allow_string: true}`：比较小数位 |
| `scale_level` | `scale_changed` 的级别，默认 breaking |
| `on_value_change` | 值变了算哪一级：`breaking` / `suspicious` / `info` |
| `array` | 数组怎么对齐：`{mode: schema \| keyed \| positional \| unordered, keys: [...]}`。写了 `keys` 就是 keyed，此时 `mode` 只能是 `keyed`；`mode: keyed` 必须写 `keys` |
| `nullable` | `true`：这个字段可以是 `null` |

除 `path` 外至少要写一个属性，只写 `path` 会报错。各属性对对比结果的影响见 [compare.md](compare.md)。

## 环境变量

字符串值里可以写 `${VAR}` 和 `${VAR:-默认值}`，规则见 [validate.md](validate.md) 的「环境变量」。需要鉴权的接口，先用自己的方式拿到 token，再通过 `${VAR}` 注入请求头。

## 路径写法

| 写法 | 含义 |
|---|---|
| `data.items` | 逐级访问对象 key，开头不写 `$` |
| `data.items[0]` | 数组下标，只在 positional 模式下生效 |
| `data.items[*].price` | 任意数组元素，不管哪种数组模式都能匹配 |
| `meta["x.y"]` | key 里有 `.`、`[`、`]`、`"`、`*`、空白，或以 `$` 开头时用引号括起来；引号内用 `\"`、`\\` 转义 |
| `**.updated_at` | 任意深度（包括 0 层，所以也能匹配根上的 `updated_at`）。`**` 不能放在末尾，也不能连续写两个 |
| `$` | 根节点本身，只能单独写。响应体本身是数组时，用 `path: "$"` 配 `array`，元素写 `[*].price` |

对 schema 或 keyed 模式的数组写了字面下标（如 `items[0].x`），规则不会生效，`validate` 会给出 `index_rule_ineffective` 提示。

## 规则怎么合并

同一个节点被多条规则匹配时，**每个属性单独决定用哪条规则**，比较顺序：

1. 越具体越优先：没有通配的优先于含 `[*]` 的，含 `[*]` 的优先于含 `**` 的；同类通配下，层级越深越优先。
2. 具体程度相同，请求级优先于 defaults。
3. 还相同，后写的优先。

例子：

- defaults 写了 `**.amount: {decimal: {}}`，请求级写了 `data.amount: {on_value_change: breaking}`：两个属性同时生效。
- defaults 写了 `**.updated_at: {ignore: true}`，请求级写了 `data.updated_at: {ignore: false}`：请求级更具体，所以不忽略。

**ignore 会剪掉整棵子树**：

- 一个节点最终是 `ignore: true` 时，它下面的子树都不再比较，子树里再写的规则（包括 `ignore: false`）都不生效。`validate` 能判断出来时给出 `rule_under_ignored` 提示。
- 规则所在的节点本身被另一条更优先的 `ignore: true` 覆盖、而它自己没写 `ignore` 时，同样不生效。例如 defaults 里 `**.updated_at` 忽略，请求级给 `data.updated_at` 配了 `on_value_change` 却忘了写 `ignore: false`。`validate` 也会提示。
