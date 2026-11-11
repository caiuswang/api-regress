# success 与 asserts 表达式

`success` 和 `asserts` 都是写在 YAML 里的一行表达式，语法是 Python 表达式的一个很小的子集。`validate` 和每次运行开始前都会检查，不合法时退出码为 2，并指出出错的列。

> 表达式在流程里什么时候执行见 [run.md](run.md) 的「每条请求的流程」。各种组合下的实际结果见 [expr-combinations.md](../dev/expr-combinations.md)。

## success：业务是否成功

```yaml
defaults:
  success: "body.code == 0"
requests:
  - name: health
    method: HEAD
    path: /health
    success: null          # 这条请求不判定 success
```

可用的变量：

| 变量 | 含义 |
|---|---|
| `status` | 状态码（整数） |
| `headers` | 响应头，名字**不区分大小写**：`headers["content-type"]` |
| `body` | 解析后的 JSON 响应体 |

表达式出错（例如字段不存在）时视为**不满足**，并给出 `success_eval_error` 提示。

**只对 JSON 响应求值**：HEAD、204、非 JSON 的响应跳过这一步。

| 情况 | 结果 |
|---|---|
| 基线不满足 | 请求状态为 `inconclusive`（基线业务失败），不再对比 |
| 基线满足、候选不满足 | `business_error`（breaking），跳过 body 对比和 asserts |
| 基线第 2 次请求不满足 | 没有不稳定字段建议，给出 `second_sample_unavailable` 提示 |

## asserts：同时比较基线和候选

```yaml
asserts:
  - expr: "len(cand.body.data['items']) >= len(base.body.data['items'])"
    level: suspicious        # 默认 suspicious
    message: "订单条目变少"
```

变量是 `base` 和 `cand`，各有且只有 `.status`、`.headers`、`.body` 三个属性。拿到的是原始响应，不受 ignore 规则影响。

- 结果为 `False`：`assert_failed`，级别由 `level` 决定。
- 求值出错：`assert_error`（固定为 suspicious），其余 assert 照常执行。
- 只在请求状态为 `ok`、状态码相同、两侧都是 JSON、并且没有 `business_error` 时执行。
- 伪路径是 `$assert[<序号>]`，序号从 0 开始；`message` 写进 finding 的 `message`，没写时用表达式原文。报告的「命中的规则」一列总会写出表达式（`asserts[0] <表达式>`），配了 `message` 也一样。

## 结果必须是 True 或 False

- `"body.code == 0, status == 200"`、`"len(x) > 0, '条目为空'"` 这种带逗号的写法是**元组**，`validate` 会直接报错。多个条件用 `and` 连接；说明文字写在 `message` 里。
- 运行时结果不是布尔值，例如 `success: "body.code"`，算求值出错（success 不满足，assert 记 `assert_error`）。要写成 `body.code == 0`。布尔字段本身可以直接用：`body.ok`。

## 怎么写

| 想做的事 | 写法 |
|---|---|
| 访问字段 | `body.data.amount`、`body["data"]["amount"]` |
| 字段名不是合法标识符、是 Python 关键字、含全角字符、或以 `_` 开头 | `body["a.b"]`、`body["from"]`、`body["字段１"]`、`body["_id"]` |
| 数组下标 | `body.data.list[0]`、`body.data.list[-1]` |
| 字段可能不存在 | `get(body, "data.coupon.amount", 0)`、`exists(body, "data.coupon")` |
| 两侧取同一路径 | `get(cand, "body.total", 0) >= get(base, "body.total", 0)` |
| 比较金额 | `body.data.amount == 12.50`（小数按 Decimal 精确比较，`0.1 + 0.2 == 0.3` 为真） |
| 除法 | `body.cents / 100 == 12.34`（整数相除也是精确小数） |
| 小数位 | `scale(body.data.amount) == 2`（`12.50` 是 2，`12.5` 是 1，整数是 0） |
| 遍历数组 | `all(x.price > 0 for x in body.data.list)`、`[x.id for x in body.data.list if x.on_sale]` |
| 集合比较 | `set(x.sku_id for x in cand.body.data["items"]) == set(x.sku_id for x in base.body.data["items"])` |
| 对象的 key | `keys(body.data)`、`"coupon" in body.data` |
| 是否为 null | `body.data.coupon is None` |
| 响应头是否存在 | `"x-request-id" in cand.headers`（不区分大小写） |

注意：
- `body.x.items` 取的是 JSON 里叫 `items` 的字段，不是 Python 的方法；`body.len`、`body.keys` 也都是字段。
- **布尔值不是数字**：`false == 0`、`true == 1`、`true in [1, 2]` 都为假；`true + 1`、`true < 2`、`abs(true)` 会报错。所以 `success: "body.code == 0"` 在 `code` 为 `false` 时**不满足**。
- 数组之间逐个元素比较：`body.items == [1, 2]`、`body.items == (1, 2)` 都可以。
- `any` / `all` 配合生成器时，得到结果就停止，后面的元素不再求值。**生成器只能作为 `all` / `any` / `set` / `min` / `max` 的唯一参数**，其他地方请写成列表：`len([x for x in body.list if x.on_sale])`。
- `base` / `cand` 本身不能参与运算或比较（`cand == base` 会被拒绝），要比较的是 `cand.body == base.body`、`cand.status == base.status`。
- `keys(headers)` 返回**小写**的名字，所以判断响应头是否存在请用 `"X-Request-Id" in headers`，不要用 `in keys(headers)`。
- 对值为 `null` 的字段用 `.` 会出错（`值为 null，不能用 .amount 访问`）；可能为 null 时用 `get(...)`。

### 内置函数

| 函数 | 参数个数 | 说明 |
|---|---|---|
| `len(x)` | 1 | 对象/数组/字符串的长度 |
| `scale(x)` | 1 | 数字的小数位 |
| `get(obj, "路径", 默认值)` | 2~3 | 取值，缺失时返回默认值（不写就是 `None`） |
| `exists(obj, "路径")` | 2 | 路径是否存在；值为 `null` 也算存在 |
| `is_number(x)`、`is_string(x)` | 1 | 类型判断（`true`/`false` 不算数字） |
| `keys(obj)` | 1 | 对象的 key 列表 |
| `set(x)` | 0~1 | 转成集合，可以用 `-` 求差集；元素不能是对象或数组，也不能同时有布尔值和数字 |
| `all(x)`、`any(x)` | 1 | 参数是数组或生成器 |
| `abs(x)` | 1 | 数字的绝对值 |
| `min(...)`、`max(...)` | 至少 1 | 一个参数时取其中的元素；元素必须全是数字或全是字符串，不能为空、不能有 NaN |

`get` / `exists` 的路径和[规则路径](suite.md#路径写法)写法一样（`data.items[0].price`、`meta["x.y"]`），但不能用 `[*]` 和 `**`。第一个参数可以是 body 里的对象/数组、列表字面量、`keys()` 的结果、`headers`，或者 `base`/`cand`。

### 支持的语法

- 字面量：整数、小数、字符串、`True` / `False` / `None`、列表 `[...]`、元组 `(...)`
- 运算：`+ - * / // %`、`== != < <= > >=`、`in` / `not in`、`and` / `or` / `not`、`a if 条件 else b`
  - `+`：数字相加、字符串拼接、数组拼接（结果不能超过 10000 个元素/字符）；`-`：数字相减或集合求差；`* / // %`：只能用于数字
  - `//` 和 `%` 向零取整：`-7 // 2 == -3`、`-7 % 2 == -1`（和 Python 的整数运算不同，和 Decimal 一致）
  - `< <= > >=`：两边都是数字或都是字符串
  - `in`：右边是对象时判断 key，是数组时判断元素，是字符串时判断子串（左边也要是字符串）
  - `is` / `is not` 只能和 `None`、`True`、`False` 比较
  - `[...]` 下标只能是整数，用在对象、数组、字符串上
- 推导式和生成器表达式。`for` 只能遍历数组、对象（得到 key）、字符串。循环变量只能是单个名字，只在推导式内部可见，不能以 `_` 开头，也不能叫 `status`、`headers`、`body`、`base`、`cand` 或内置函数名

类型不对时，错误信息会用 JSON 的类型名说明，例如 `+ 不能用于数字和字符串`、`< 只能比较两个数字或两个字符串，这里是布尔值和数字`。

### NaN 和 Infinity

响应里出现 `NaN` / `Infinity` 时（会有 warning），它们按数字处理，但：
- `NaN` 不等于任何值，包括它自己：`body.v == body.v` 为假。
- `NaN` 不能比较大小，也不能用于 `min` / `max`，会报 `NaN 不能比较大小`。
- `Infinity - Infinity` 这类没有意义的运算会报 `数值运算没有意义`。

### 不支持（validate 会直接报错）

| 写法 | 原因 / 替代 |
|---|---|
| `body.data.keys()`、`s.upper()` | 不能调用方法，用 `keys(body.data)` |
| `x._y`、`x.__class__` | 不能访问 `_` 开头的属性，字段名以 `_` 开头时用 `x["_y"]` |
| `cand.boby`、`cand.header` | `base`/`cand` 只有 `status`、`headers`、`body` |
| `cand == base`、`len(cand)` | `base`/`cand` 不能直接使用，比较 `cand.body == base.body` |
| `not (x for x in xs)`、`len(x for x in xs)` | 生成器只能作为 `all`/`any`/`set`/`min`/`max` 的唯一参数，其他地方用 `[...]` |
| `__import__(...)`、`open(...)`、`int(...)` | 只能调用上面列出的内置函数 |
| `exists(x, "a", False)`、`len()` | 参数个数不对 |
| `max(xs, key=...)` | 不支持关键字参数 |
| `status is 200` | `is` 只能和 None/True/False 比较，用 `==` |
| `2 ** 10`、`1 << 3`、`a \| b` | 不支持幂运算和位运算 |
| `xs[1:3]`、`{...}`、`f"..."`、`lambda`、`:=` | 不支持 |
| `'\d'` | 无效的字符串转义，写成 `'\\d'` |

表达式最长 2000 个字符。

## 计算量限制

为了防止一条表达式把运行卡住：一次求值中推导式最多迭代 10000 次，整数乘积不超过 8192 位，并且有总计算量上限（大致按访问的 JSON 节点数和元素数累计，约 500 万）。超出时这条表达式算求值出错，不影响其他请求。
