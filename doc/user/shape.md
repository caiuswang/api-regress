# shape：查看字段树和生效的规则

`shape` 从已有的基线样本（快照或 run 目录）里列出每条请求的完整字段树。每个字段标出类型、小数位、示例值，以及**最终生效的规则和它来自哪条规则**。用来：

- review 一条请求的规则：每个字段最后按什么规则比较、哪些字段没配规则（例如某个金额字段漏了 `decimal`）；
- 给字段很多的新接口配规则：用 `--write` 写出[规则文件](rules-file.md)骨架，在骨架上直接标注。

`shape` 只读，**不发任何请求**，也不需要设置任何环境变量。

## 命令

```
api-regress shape <suite.yaml> --from DIR [--only NAME ...] [--max-keys N] [--write DIR]
```

| 参数 | 说明 |
|---|---|
| `--from DIR` | 必填。样本从哪里读：**快照目录**（有 `manifest.json`）或 **run 目录**（有 `result.json`，读其中基线的响应）。两个文件都有时按快照处理 |
| `--only NAME` | 可以重复，只处理指定的请求 |
| `--max-keys N` | 一个对象最多列出多少个 key，默认 50，至少 1（见[过长的对象](#过长的对象)） |
| `--write DIR` | 不打印字段树，改为给每条请求写出规则文件骨架 `DIR/<name>.yaml`（见[写出骨架](#写出规则文件骨架---write)） |
| `--log-level`、`--log-file` | 同其他命令 |

- **样本**：快照里这条请求的全部 sample（1–2 个），或 run 目录里这条请求的全部基线 sample。网络错误、body 为空、不是 JSON 的 sample 跳过。
- **没有 body 的请求**：HEAD 请求，或者 body 全为空的请求（例如 204），本来就没有字段可列，写一行 `# health: 没有 body（HEAD 请求，跳过）`（不是 HEAD 时括号里是"响应 body 为空，跳过"）。这不算"没有样本"，不影响退出码，`--write` 也不给它写骨架。
- **环境变量**：`targets` 里的变量缺了不影响。规则本身（`rules` 的路径、`rules_file` 的路径）用了没设置的变量时报 suite 错误，因为规则没法确定。
- **规则**：和 `run` 完全一样，按 [suite.md](suite.md#规则怎么合并) 的合并方式（含 `rules_file` 里的规则）算出每个字段最终生效的属性。

典型用法：

```bash
api-regress snapshot record suite.yaml -d snap/            # 先有基线样本
api-regress shape suite.yaml --from snap/ --only order-detail
api-regress shape suite.yaml --from api-regress-out/<时间戳>/  # 也可以直接用某次 run 的结果
```

## 字段树怎么看

用 [demo](demo.md) 的 `regress.yaml` 录一个快照，再运行 `shape`（`request_id`、`server_time` 每次都不一样）：

```
$ api-regress shape demo/regress.yaml --from snap/ --only order-detail --only users
# order-detail  GET /orders/10086  样本 2 个（snap）
code:          # number  例 0
msg:           # string  例 "ok"
request_id:    # string  例 "af59b1af799546f8b71c8587e366a5c2"
server_time:   # string  例 "2026-11-12T06:19:18.028936+00:00"
data:          # object
  order_id:    # number  例 10086
  amount:      # number  小数位 {2}  例 12.50  | decimal ← defaults.rules[1] **.amount; on_value_change: breaking ← rules[0] data.amount
  currency:    # string  例 "CNY"
  status:      # string  例 "PAID"
  items:       # array  长度 {2}  | array: keyed [sku_id] ← rules[1] data.items
  items[*]:    # object
    sku_id:    # string  例 "A01"
    price:     # number  小数位 {2}  例 1.00  | decimal ← defaults.rules[0] **.price
    qty:       # number  例 2
  coupon:      # null  | nullable ← rules[2] data.coupon
  updated_at:  # string  例 "2026-11-12T06:19:18.028893+00:00"

# users  GET /users  样本 2 个（snap）
# 根节点 $：array  长度 {3}  | array: keyed [id] ← rules[0] $
"[*]":    # object
  id:     # number  例 1
  name:   # string  例 "张三"
  email:  # string  例 "zhangsan@example.com"
  vip:    # boolean  例 true
# 未命中的规则：
#   defaults.rules[0] **.price
#   defaults.rules[1] **.amount
exit 0: 全部输出
```

字段树按响应的嵌套结构缩进，每个字段一行，key 的写法和[规则文件](rules-file.md)相同，可以直接对照着写规则。

| 内容 | 说明 |
|---|---|
| 标题 | `# 请求名  方法 路径  样本 N 个（--from 的目录）`。N 是参与合并的 JSON 样本数 |
| 字段顺序 | 按 key 在样本里第一次出现的顺序 |
| 数组 | 数组本身一行（数组级规则写在这里，如 `items:`），非空时下面再列一行 `items[*]:`，元素结构是所有元素、所有 sample 合并后的结果。空数组只有一行，写 `长度 {0}`。响应体本身是数组时，元素行写 `"[*]":` |
| 根节点 | 响应体不是对象（数组、字符串等），或者根节点 `$` 上有生效的规则时，第一行是 `# 根节点 $：…` |
| 类型 | 所有 sample、所有元素里出现过的 JSON 类型，用 `|` 分隔，`null` 放在最后。`object|null` 表示有时是对象、有时是 `null` |
| 小数位 | 只对数字列出，所有出现过的小数位（算法和 `decimal` 规则相同，`12.50` 是 2）。只有整数时不列 |
| 长度 | 只对数组列出，出现过的所有长度 |
| 示例值 | 只对字符串、数字、布尔值列出，取第一个不是 `null` 的值。数字按原文（`12.50`）；字符串加引号，超过 40 个字符截断并加 `…` |
| 生效的规则 | `|` 后面列出每个**最终生效**的属性和它的来源：`属性 ← 来源 规则路径`，多个用 `;` 分隔。来源的写法和报告里"命中的规则"一致：`defaults.rules[i]`、`rules[i]`（请求级）、`rules_file <文件>:<行号>`。没有 `|` 就是没有任何规则作用在这个字段上，按默认方式比较结构 |
| ignore | 字段最终 `ignore: true` 时注明"子树不对比"，**不再列出它下面的字段** |

- 示例值来自你录下的真实响应，**不要把 `shape` 的输出贴到公开的地方**（日志里不会写示例值）。
- 字段树按 `[*]` 算生效的规则，写了字面下标的规则（`items[0].x`）不会出现在树上，在这条请求的末尾列出"写了下标的规则（树上不展示）"。
- 每条请求的末尾列出"未命中的规则"：在这条请求的样本里一个路径都没匹配到的规则（判定方式和 `run` 的[本次未命中的规则](run.md)相同，范围只是这条请求）。上例中 `users` 没有 `price`、`amount` 字段，所以 defaults 里的两条规则对它来说没命中；规则写错了路径时也会出现在这里。
- 没有样本的请求写一行 `# <name>: 没有样本（原因）`，例如快照里没有这条请求，或者样本都是网络错误，或者 body 都不是 JSON（例如返回了 HTML 错误页，多半是地址配错了）。
- 请求之间空一行，最后一行是 `exit N: 原因`。

### 过长的对象

一个对象的 key 超过 `--max-keys` 个时（例如 key 是 id 的对象 `{"10086": {...}, "10087": {...}}`），只列出前 N 个，然后写一行：

```
  # … 还有 1234 个 key 没有列出（key 可能是 id 之类的动态值）
```

截断只影响展示：生效的规则和"未命中的规则"仍然按全部 key 计算。这种对象每次运行都会报字段增删，通常要整个 `ignore`。

字段名里有 `.`、`[`、空白等特殊字符，或者会被 YAML 当成别的类型（`on`、`null`、`1`）时，key 会加引号，例如 `'["x.y"]':`、`"0":`，和规则文件里的写法一致。

## 写出规则文件骨架（`--write`）

```
$ api-regress shape demo/regress.yaml --from snap/ --only order-detail --write rules/
wrote rules/order-detail.yaml
在 suite 的请求里加上 rules_file（例如 rules_file: ../rules/order-detail.yaml，相对 suite 所在目录）才会生效
exit 0: 全部输出
```

`rules/order-detail.yaml`：

```yaml
# api-regress 规则文件：order-detail
# 每个 key 是相对路径；以 $ 开头的 key 是规则属性（$ignore、$decimal、$scale_level、$on_value_change、$array、$nullable）。
# 只列出字段、不写属性的节点不产生规则。写法见 doc/user/rules-file.md。
code:          # number
msg:           # string
request_id:    # string
server_time:   # string
data:          # object
  order_id:    # number
  amount:      # number  小数位 {2}
  currency:    # string
  status:      # string
  items:       # array
  items[*]:    # object
    sku_id:    # string
    price:     # number  小数位 {2}
    qty:       # number
  coupon:      # null
  updated_at:  # string
```

- 给 `--only` 范围内、有样本的每条请求写一个 `DIR/<name>.yaml`。`DIR` 不存在时创建。
- 骨架只有字段和类型、小数位注释，**没有示例值**（规则文件通常会提交到仓库，示例值可能是用户数据或 token），也**不写入现有的规则**（规则一改注释就过时了；要看生效的规则用不带 `--write` 的 `shape`）。
- 被 ignore 的子树不展开，注释写"已 ignore，子树不展开"。
- **key 超过 `--max-keys` 的对象，骨架里一个 key 都不写**，只留一行 `# … 有 N 个 key，超过 --max-keys，一个都不列出（…）`：这种 key 多半是 id、邮箱、手机号之类的数据，不应该进仓库。字段树（不带 `--write`）里仍然列出前 N 个。
- 骨架不加任何属性就是一个合法的规则文件，不产生任何规则。在需要的字段上加属性即可，例如把 `amount:` 改成 `amount: {$on_value_change: breaking}`，写法见 [rules-file.md](rules-file.md)。
- **只要有一个目标文件已经存在，一个都不写**（退出码 2），列出已存在的文件，不会覆盖你改过的规则文件。写到一半失败时删掉本次写出的文件（退出码 3）。
- 写完后要在 suite 的请求里加上 `rules_file: <路径>` 才会生效（路径相对 suite 文件所在的目录；suite 是符号链接时，相对链接所在的目录）。stdout 最后一行给出的例子已经按这个规则算好；算不出相对路径时（例如 Windows 上不在同一个盘）给绝对路径。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 所有请求都输出了（没有 body 而跳过的请求也算） |
| 2 | suite 有问题（含规则文件的错误、规则用了没设置的环境变量）；`--only` 的 name 不存在；`--max-keys` 不合法；`--write` 的目标文件已存在 |
| 3 | `--from` 不存在、既不是快照也不是 run 目录、损坏（含快照版本不兼容、body 文件对不上）；写文件失败；内部错误 |
| 4 | 至少有一条请求没有样本：快照或 run 目录里没有这条请求，或者样本都是网络错误，或者有非空的 body 但都不是 JSON |

同时满足多个时按 2 > 3 > 4 > 0 取值。

## 日志

INFO 记录样本来源（快照 / run 目录、请求数）、每条请求的样本数和节点数（或者没有样本、没有 body 的原因）、写出的文件路径、退出码及原因。**示例值不写日志**。
