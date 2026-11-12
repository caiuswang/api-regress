# 对比规则：会报出哪些差异

> 本页说明 body 怎么比较、各种差异的含义和级别，以及规则怎么影响结果。
> 状态码、响应头、业务错误这些 body 以外的差异见 [run.md](run.md)。各种组合下的实际输出见 [diff-combinations.md](../dev/diff-combinations.md)。

## 默认只比结构

不写任何规则时，只会报出**结构**变化：字段删了、加了，类型变了，数组长度变了。字段的值变了**不报**；要比较值，需要显式配置 `on_value_change`，或者在 defaults 里写 `compare: values`。

## 差异类型（finding）

| 类型 | 什么时候出现 | 级别 |
|---|---|---|
| `field_removed` | 基线有、候选没有的字段 | breaking |
| `field_added` | 候选新增的字段 | info |
| `type_changed` | JSON 类型变了，例如数字变成字符串、对象变成 `null` | breaking |
| `nullability_changed` | 字符串、数字、布尔值在 `null` 和非 `null` 之间变化 | 变成 null：suspicious；从 null 变成有值：info |
| `scale_changed` | 配了 `decimal` 的字段，小数位变了 | breaking，可以用 `scale_level` 修改 |
| `array_length_changed` | schema / positional 模式下数组长度不同 | 空 ↔ 非空：suspicious；其他：info |
| `element_missing` / `element_added` | keyed 模式下某个 key 只在一侧出现；positional 模式下超出另一侧长度的下标 | suspicious / info |
| `value_changed` | 配了值比较且值不同（包括变成 `null`、从 `null` 变成有值）；unordered 模式下元素集合不同 | `on_value_change` 指定的级别；`compare: values` 时为 suspicious |

几点说明：

- **数字类型**：`1` 和 `1.0` 都是数字，不算类型变化；值比较时也相等。`NaN` 和 `NaN` 在值比较时视为相等。
- **只报最高层**：一个对象被删除，或者变成了别的类型（包括 `null`），只在这个对象上报一条，不再列出它下面的每个字段。
- **变成 null 又配了值比较**：比如 `data.amount` 配了 `on_value_change: breaking`，`12.50` → `null` 会报两条：`nullability_changed`（suspicious）和 `value_changed`（breaking）。所以 `--fail-on breaking` 能拦住它。
- **两侧值看起来一样的 `type_changed`**：`12.50` → `"12.50"`。HTML / Markdown 报告里字符串带引号（`12.50` → `"12.50"`）；`result.json` 里两侧的值都是 `"12.50"`，看 finding 的 `base_type` / `cand_type`（`number` → `string`）区分。
- **字段改名**会报成两条：

  ```
  基线：{"data": {"coupon_amount": "5.00"}}
  候选：{"data": {"discount_amount": "5.00"}}

  breaking  field_removed  data.coupon_amount
  info      field_added    data.discount_amount
  ```

## 规则

规则写在 `defaults.rules`（所有请求）、请求自己的 `rules`，或请求引用的规则文件（[rules-file.md](rules-file.md)）里，路径写法和多条规则怎么合并见 [suite.md](suite.md)。

| 属性 | 作用 |
|---|---|
| `ignore: true` | 这个节点和它下面的整棵子树都不比较。被 ignore 的字段被删掉也不报 |
| `decimal: {}` | 比较小数位：`12.50` → `12.5` 报 `scale_changed`。`allow_string: true` 时 `"12.50"` 这种字符串也算（必须是标准 JSON 数字写法：`"007"`、`"１２.５"`、`"1_000"` 不算），值比较时 `"12.50"` 等于 `"12.5"`；但字符串和数字之间仍然是 `type_changed` |
| `scale_level` | `scale_changed` 的级别，默认 breaking |
| `on_value_change` | 值变了算哪一级 |
| `nullable: true` | 声明这个字段可以是 `null`：标量与 `null` 之间不再报 `nullability_changed`，对象、数组与 `null` 之间也不再报 `type_changed`。两个非 null 类型之间（如对象变字符串）照常报。配了值比较时，`value_changed` 照常报 |
| `array` | 数组怎么对齐，见下一节 |

小数位按 JSON 原文计算：`1.10` 是 2 位，`1` 是 0 位，`1.50E1` 是 1 位，`1E2` 是 0 位，`-0.00` 是 2 位。只配 `decimal` 不比较数值本身，要比值还得配 `on_value_change`。

后端如果用浮点数输出金额、不保留末尾的 0（`12.50` 输出成 `12.5`），原本就是 2 位的值也会显示成 1 位，decimal 规则就判断不了精度是否变了。decimal 规则要求后端稳定地输出固定的小数位。

## 数组怎么选模式

```yaml
- path: data.items
  array: { keys: [sku_id] }          # keyed
- path: data.steps
  array: { mode: positional }
- path: data.tags
  array: { mode: unordered }
```

| 模式 | 怎么比 | 适合 | 不适合 |
|---|---|---|---|
| `schema`（默认） | 把所有元素合并成一张表，只比较每个字段出现过的类型、小数位，以及数组长度。不逐个比较元素 | 分页列表、实时数据 | 需要比较具体元素的值 |
| `keyed` | 按 `keys` 配对元素，配上的逐个比较；路径显示为 `items[sku_id=A12].price` | 数据固定、元素有 id | 分页列表：数据一移动就会持续报 `element_missing` |
| `positional` | 按下标配对，路径显示为 `items[3].price` | 顺序有意义的固定数组 | 顺序会变的数组 |
| `unordered` | 元素结构按 schema 方式比较（删字段、改类型仍是 breaking）；再把元素当作多重集合比较值，不同时在数组上报一条 `value_changed` | 顺序不定的小型固定数组（如标签） | 大数组、数据会变的数组 |

**schema 模式的细节**

- 某个字段在基线的任一元素里出现过，而候选的所有元素里都没有，才算 `field_removed`。
- 类型比较先去掉 `null`：候选出现了基线没有的类型才算 `type_changed`；候选只用了基线已有类型的一部分，不报。
- `null` 新出现时：字段是对象或数组，记 `type_changed`（和普通字段一样）；是标量，记 `nullability_changed`。`nullable: true` 时都不报。
- 父字段在候选里已经不是对象（比如全变成了 `null`），它下面的字段不再一个个报 `field_removed`，只在父字段上报一条。
- 配了 `decimal` 时，候选出现了基线没有的小数位才算 `scale_changed`。报告里给出两侧的小数位集合，和一个出问题元素的下标（`example_index`）。
- 一侧为空、另一侧非空时只比较长度，并给出 `array_empty` 提示；两侧都为空时什么都不报。
- 元素里的内层数组也合并进同一张表比较，**不比较内层数组的长度**。内层数组在一侧全部为空时只给 `array_empty` 提示。内层数组上写 `keys` 等配对规则不起作用（外层没有配对），会给出 `rule_ineffective_in_schema` 提示。
- `on_value_change` 和 `compare: values` 在 schema 数组内部不起作用，会给出 `rule_ineffective_in_schema` 提示；要比较值，改用 keyed 模式。
- 写了字面下标的规则（如 `items[0].x`）对 schema 和 keyed 数组不生效。

**keyed 模式的细节**

- 可以用多个字段组合成 key：`keys: [id, color]`，路径显示为 `items[id=1,color=red]`。
- key 按类型和值比较：`1` 和 `1.0` 是同一个 key，`1` 和 `"1"` 不是。
- 有元素不是对象、缺少 key 字段（或者 key 的值是 null、对象、数组）、key 重复时，整个数组**降级为 schema 模式**，并给出 `array_degraded_to_schema` 提示。
- 路径里数字 key 不加引号、按数值显示（`1.0` 显示成 `1`）；会和数字混淆的字符串 key 加引号：`items[id=1.5]` 和 `items[id="1.5"]` 是两个不同的元素。

**unordered 模式的细节**

- 元素的结构按 schema 模式比较，规则和上面一样：元素里删字段、改类型照常报 breaking，配了 `decimal` 的字段比较小数位。
- 值：两侧元素作为多重集合不相等时，在数组路径上报一条 `value_changed`，级别取数组本身的 `on_value_change`，没配时为 suspicious。被 ignore 的字段不参与比较；元素里的内层数组也不看顺序，除非给它配了 `mode: positional`。
- 写在元素字段上的 `on_value_change` 不起作用，会给出 `rule_ineffective_in_schema` 提示。

## 提示（warning）

提示不是差异，不影响退出码。和 body 解析、对比有关的提示：

| code | 含义 |
|---|---|
| `json_parse_failed` | Content-Type 声明是 JSON，但解析失败（包括对象和数组嵌套超过 200 层、数字超出范围），这一侧按非 JSON 处理 |
| `duplicate_key` | 同一个对象里有重复的 key，取最后一个值 |
| `nan_or_infinity` | body 里出现了 `NaN` / `Infinity`（不是标准 JSON） |
| `array_degraded_to_schema` | keyed 数组对齐失败，降级为 schema 模式 |
| `array_empty` | schema / unordered 数组（或 schema 元素里的内层数组）一侧为空、另一侧非空，没有比较元素结构 |
| `rule_ineffective_in_schema` | 值比较落在 schema 或 unordered 数组的元素内部，或者配对规则写在 schema 元素的内层数组上，不生效 |

## 已知限制

| 限制 | 应对 |
|---|---|
| 基线和候选的数据源不同（比如预发和生产）时，值比较几乎全是差异，可空字段和数组长度也会频繁变化 | 默认只比结构；可空性和长度变化的级别已经调低 |
| 基线和候选不是同时请求的，中间数据可能变了 | 以结构对比为主 |
| schema 模式不比较单个元素的值 | 改用 keyed 模式，或者写 [asserts](expressions.md) |
| key 是动态值的对象（例如 `{"10086": {...}}`）每次都会报字段增删 | 用 `ignore` 忽略这个对象 |
| 分页列表、实时列表用 keyed 模式，数据一移动就持续报 `element_missing` | 这类数组用 schema 模式 |
| 后端用浮点数输出金额、不保留末尾的 0 | decimal 规则判断不了精度，见上文「规则」 |
