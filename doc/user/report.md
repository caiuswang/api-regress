# 怎么看报告

`run` 和 `snapshot compare` 跑完后，在 run 目录里写出报告：

| 文件 | 用途 |
|---|---|
| `report.html` | 排查问题用。单个文件，不依赖外部资源，浏览器直接打开；有两侧 body 的对照 |
| `report.md` | 贴到 PR 或 IM。只展开有问题的请求 |
| `junit.xml` | 给 CI 用，见下文「JUnit」 |
| `result.json` | 机器可读的完整结果，总会输出，字段见 [run.md](run.md#resultjson) |

用 `--format` 选择要哪几种，逗号分隔，默认 `html,md,junit` 全部输出；`result.json` 不受影响：

```bash
api-regress run suite.yaml --format md,junit
```

> 报告里有响应体的原文（finding 的值、HTML 的 body 对照），不要把报告提交或上传到公开的地方。请求头的值不会出现在报告里。

## 报告的结构

1. **汇总**：退出码和原因；请求数、各状态（`ok` / `error` / `inconclusive` / `unsupported`）的数量、各级别 finding 的数量、warning 数；本次的对比模式（`structure` 只比结构，`values` 比较值）。HTML 还有一张请求一览表，点名字跳到详情。
2. **每条请求**：
   - 标题：`名字  方法 路径  基线状态码 → 候选状态码`；没有拿到响应时状态码写 `-`。
   - 状态不是 `ok` 时给出原因，例如"候选返回 401，检查候选的 token 或限流"。
   - 两侧的完整 URL、耗时、重试次数（HTML）。
   - **curl 复现命令**：带上 suite 里给这一侧配的请求头名字，值一律写成 `<redacted>`，复现前自己填上。
   - **finding 表**：级别、类型（后面括号里是说明，例如 asserts 的 `message`）、具体路径、泛化路径（HTML）、两侧的值、命中的规则。按 breaking → suspicious → info 排序。
   - **body 对照**（只有 HTML）：两侧 body 并排，只显示有差异的行和上下 3 行，中间省略的部分用 `⋯` 表示。左侧红底是基线里删掉或改掉的行，右侧绿底是候选里新增或改后的行；改动的行左右对应时，行内具体变了的字符用深色标出（超过 200 字符的行只整行标色）。JSON 按原文的小数位重新缩进排版；body 很大时只对比前 1500 行 / 20 万个字符，并注明已截断。
3. **建议 ignore**：基线两次请求中值不同的字段，附带可以直接粘贴进 `rules` 的 YAML 片段。**不会自动生效**，确认是不稳定字段（时间戳、traceId 等）后再加。
4. **本次未命中的规则**：在本次请求的 body 里一个路径都没匹配到的规则。只表示这次没用上（可能那条请求这次没返回该字段），不一定是规则过期了。
5. **warning**：降级、解析问题、`success` 求值出错等提示，不影响退出码。各 code 的含义见 [compare.md](compare.md#提示warning) 和 [run.md](run.md#提示warning)。

## finding 表里的值

| 显示 | 含义 |
|---|---|
| `(缺失)` | 这一侧没有这个字段（和 `null` 不同） |
| `12.50` | 数字，按原文的小数位显示 |
| `"12.50"` | 字符串（带引号）。`12.50` → `"12.50"` 是 `type_changed` |
| `{number}`、`{null, object}` | schema 模式的数组：所有元素在这个路径上出现过的类型 |
| `小数位 {2}` → `小数位 {1, 2}` | schema 模式 + `decimal` 规则：所有元素的小数位集合。候选多出了 1 位小数 |
| `（示例下标 3）` | schema 模式的 finding 附带一个出问题的元素下标，方便到 body 里找 |
| 空 | 这类 finding 没有值，例如 `assert_failed`：看类型后面括号里的 `message` 和「命中的规则」一列里的表达式 |

非 body 的 finding 用伪路径：`$status`（状态码）、`$success`（业务成功条件）、`$header.X-Name`（响应头）、`$body`（一侧是 JSON 另一侧不是）、`$assert[0]`（第几个 assert）。

**命中的规则** 列出报出这条 finding 的 suite 配置，不用回头翻 suite：

| 写法 | 含义 |
|---|---|
| `defaults.rules[1] **.amount（decimal: {}）`、`rules[0] data.amount（on_value_change: breaking）` | defaults 或本请求的第几条规则、它的路径和内容，说明级别或比较方式是哪条规则决定的。命中多条时用 `；` 分开 |
| `rules_file rules/order-detail.yaml:3 data.amount（on_value_change: breaking）` | 规则来自请求的规则文件：文件、行号、展开后的路径和内容，见 [rules-file.md](rules-file.md) |
| `asserts[0] cand.body.total > 100` | 本请求第几个 assert 和它的表达式（`assert_failed`、`assert_error`）。配了 `message` 时，message 写在类型后面的括号里 |
| `defaults.success body.code == 0`、`success body.code == 0` | `business_error` 用的 `success` 表达式，以及它写在 defaults 还是本请求 |
| 空 | 按默认行为报出，没有规则参与 |

JUnit 里同样的内容写在每条 finding 末尾的 `[...]` 里。

**泛化路径**（HTML）把具体下标换成 `[*]`、keyed 元素换成 `[*]`，可以直接写进规则的 `path`。

## 例：字段改名

候选把 `data.coupon_amount` 改名成了 `data.discount_amount`。api-regress 不猜改名，报成一删一增：

```
### order-detail  GET /orders/10086  200 → 200

| 级别 | 类型 | 路径 | 基线 | 候选 | 命中的规则 |
|---|---|---|---|---|---|
| breaking | field_removed | data.coupon_amount | "5.00" | (缺失) |  |
| info | field_added | data.discount_amount | (缺失) | "5.00" |  |
```

看法：同一条请求里，同一层级一个 `field_removed` 加一个 `field_added`、值相同，基本就是改名。对调用方来说旧字段没了，所以是 breaking。

- **不是预期的**：找开发确认，改回来或者两个字段都返回一段时间。
- **是预期的**：`run` 下改 suite 或通知调用方；用快照时，确认后执行 `snapshot accept <run 目录> -d snap/ --only order-detail` 把候选收进快照，见 [snapshot.md](snapshot.md)。

## 例：金额精度变化

```
| breaking | scale_changed | data.list[*].price（示例下标 0） | 小数位 {2} | 小数位 {1, 2} | defaults.rules[0] **.price（decimal: {}） |
```

列表的 `price` 原来都是 2 位小数，候选里出现了 1 位（例如 `12.50` 变成 `12.5`），第一个出问题的元素是下标 0。命中的规则是 `defaults.rules[0]`（`**.price` 上的 `decimal` 规则）。在 HTML 的 body 对照里能看到具体是哪个值。

## 例：请求没比成

```
### expired  GET /expired  200 → 401

**inconclusive**：候选返回 401，检查候选的 token 或限流
```

`inconclusive` 和 `error` 的请求没有 finding，退出码为 4（没有触发 1 时）。按原因处理后重跑；可以用 `--only expired` 只跑这一条。

## JUnit

每条请求是一个 `testcase`（`classname` 是 suite 文件名去掉扩展名）：

| 情况 | 记为 |
|---|---|
| 有达到 `--fail-on` 级别的 finding | `failure`，内容是这些 finding |
| 否则，状态是 `error` 或 `inconclusive` | `error`，`message` 是原因 |
| 状态是 `unsupported`（两侧都不是 JSON） | `skipped` |
| 其他 | 通过 |

没达到 `--fail-on` 的 finding 和 warning 写在 `system-out` 里。`--fail-on never` 时不会有 `failure`。`time` 是候选那次请求的耗时。
