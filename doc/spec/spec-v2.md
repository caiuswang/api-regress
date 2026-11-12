# api-regress 产品需求 v2

> 状态：草案 r1（2026-11-12）
> 来源：用户反馈（2026-11-12）：规则写成一串散开的路径，嵌套多的 JSON 很难整体 review，看不出每个字段最后生效的是哪些规则、哪些字段没有规则；快照里的 body 是一个转义过的字符串，没法直接查看返回内容。
> 基于 [spec-v1.md](spec-v1.md)：本文只写**新增和改动**的部分，没提到的行为一律沿用 v1。
> 配套文档：实现约束见 [spec-v2-design.md](spec-v2-design.md)，任务拆分见 [spec-v2-tasks.md](spec-v2-tasks.md)。
> 决策记录见 §8，编号接着 v1 往下排（D25 起），验收项从 AC-38 开始，任务从 T8 开始。

## 1. 背景与目标

v1 默认就会比较响应里**所有字段的结构**（删字段、改类型都能报出来），规则只用来调整个别字段的比较方式。问题在于规则的写法：

- 规则是一串路径（`**.amount`、`data.items`、`data.coupon`），和响应的嵌套结构对不上，要在脑子里把路径还原成树，才能 review。
- 同一个字段可能同时被 defaults 的 `**` 规则和请求级规则命中，每个属性单独决定用哪条，光看 suite 很难判断最后生效的是什么。
- 看不出哪些字段**没有**规则，也就不知道有没有漏配（例如某个金额字段没配 `decimal`）。

v2 的目标：

1. **字段树视图**（`shape` 命令）：从已有的基线样本里列出每条请求的完整字段树，每个字段标出类型、小数位、示例值，以及**最终生效的规则和它来自哪条规则**。只读，不发请求。
2. **树形规则文件**（`rules_file`）：规则可以写在一个和响应同形的 YAML 文件里，可以从字段树直接生成骨架。加载时展开成 v1 的路径规则，**对比语义不变**。
3. **响应体单独成文件**：快照和 run 目录里，每个 sample 的 body 按原样存成一个独立的文件（JSON 响应就是一个完整的 `.json` 文件），放在单独的 `samples/` 目录下，请求文件里只引用文件名。

### 1.1 典型场景

| # | 场景 | 用法 |
|---|---|---|
| S5 | review 某条请求的规则：每个字段最后按什么规则比较、有没有漏配 | `shape suite.yaml --from snap/ --only order-detail` |
| S6 | 给一个字段很多的新接口配规则 | `snapshot record` → `shape --write rules/` 生成骨架 → 在骨架上标注 → suite 里写 `rules_file` |
| S7 | 查看快照或 run 目录里某次的具体返回 | 直接用编辑器打开 `snap/samples/order-detail/1.json` |

## 2. 范围

### 2.1 v2 包含
1. `api-regress shape` 命令：输出字段树，可以写出规则文件骨架（§3）。
2. 请求级 `rules_file` 字段，以及树形规则文件的格式（§4）。
3. `result.json`、报告、`validate` 里能看到规则来自规则文件（§5）。
4. 快照、run 目录的落盘格式改版：body 单独存文件（§6）。

### 2.2 本期不做
- **绝对校验**（必填、枚举、取值范围、正则等只校验一侧、和基线无关的约束），以及 **JSON Schema / OpenAPI 导入**。这类约束离 v1 的非目标"不做单接口断言平台"很近，等 v2 用一段时间再决定（D25）。
- **动态 key 对象**（`{"10086": {...}}` 这种 key 是 id 的对象）的折叠和比较，例如路径语法 `{*}`。v2 的 `shape` 只截断过长的对象（§3.3），对比行为和 v1 一样（D30）。
- defaults 级的规则文件：规则文件只能挂在请求上。
- 规则文件里的 `${VAR}` 替换。
- 自动把 suite 里已有的 `rules` 迁移进规则文件。
- 旧格式快照的自动迁移：用 `snapshot record --clean` 重录（§6.4）。
- 落盘时格式化（缩进）响应体：body 按原样保存（D33）。

## 3. shape：字段树

### 3.1 命令

```
api-regress shape <suite.yaml> --from DIR [--only NAME ...] [--max-keys N] [--write DIR]
```

| 参数 | 说明 |
|---|---|
| `--from DIR` | 必填。样本从哪里读：**快照目录**（有 `manifest.json`）或 **run 目录**（有 `result.json`，读其中的 `baseline/`）。两个文件都有时按快照处理 |
| `--only NAME` | 可以重复，只处理指定的请求 |
| `--max-keys N` | 一个对象最多列出多少个 key，默认 50，至少 1（§3.3） |
| `--write DIR` | 不打印字段树，改为给每条请求写出规则文件骨架 `DIR/<name>.yaml`（§3.4） |

另外支持通用的 `--log-level`、`--log-file`。

- **不发任何请求**，不需要任何环境变量（`targets` 里缺的变量不影响）。规则本身（`rules`、`rules_file` 的路径）依赖缺失的环境变量时，按 suite 错误处理。
- **样本**：快照里这条请求的全部 sample（1–2 个），或 run 目录 `baseline/<name>.json` 里的全部 sample。网络错误的 sample 和非 JSON 的 sample 跳过。
- **规则**：和 `run` 一样，按 v1 的合并规则，对每个节点算出最终生效的属性（含 `rules_file` 里的规则）。

### 3.2 字段树的内容

字段树按响应的嵌套结构缩进，每个节点一行。写法和规则文件（§4）相同，所以可以直接对照着写规则：

```
# order-detail  GET /v1/orders/10086  样本 2 个（snap/）
code:                    # number  例 0
data:                    # object
  amount:                # number  小数位 {2}  例 12.50  | decimal ← defaults.rules[1] **.amount; on_value_change: breaking ← rules[0] data.amount
  coupon:                # object|null  | nullable ← rules_file order-detail.yaml:9 data.coupon
    code:                # string  例 "NEW10"
  items:                 # array  长度 {2, 3}  | array: keyed [sku_id] ← rules[1] data.items
  items[*]:              # object
    sku_id:              # string  例 "A12"
    price:               # number  小数位 {2}  例 99.00  | decimal ← defaults.rules[2] **.price
  updated_at:            # string  | ignore ← defaults.rules[0] **.updated_at（子树不对比）
```

| 内容 | 规则 |
|---|---|
| 节点顺序 | 按 key 在样本里第一次出现的顺序 |
| 数组 | 数组本身一行（写数组级规则的地方）；非空时下面再列一行 `name[*]:`，元素结构按 schema 模式的方式合并所有元素、所有 sample。空数组写 `长度 {0}`，没有 `[*]` 行。根节点是数组时，元素行写 `"[*]":` |
| 类型 | 所有 sample、所有元素里出现过的 JSON 类型的并集，`|` 分隔（`object|null`）。`null` 放在最后 |
| 小数位 | 只对数字列出，所有出现过的小数位的集合，算法同 v1 的 decimal 规则 |
| 长度 | 只对数组列出，各 sample（schema 模式下合并后）出现过的长度的集合 |
| 示例值 | 只对字符串、数字、布尔值列出，取第一个非 null 的值。数字按原文；字符串加引号，超过 40 个字符截断加 `…` |
| 生效的规则 | `|` 后面列出每个**最终生效**的属性和它的来源。来源写法和报告里"命中的规则"一致：`defaults.rules[i]`、`rules[i]`（请求级）、`rules_file <文件名>:<行号>`，后面跟规则的路径 |
| ignore | 节点最终 `ignore: true` 时注明"子树不对比"，**不再展开子节点** |

- 字段树按泛化路径（`[*]`）算生效的规则。写了字面下标的规则（`items[0].x`）不会出现在树上，在这条请求的末尾单独列出"写了下标的规则（树上不展示）"。
- 每条请求的末尾列出"未命中的规则"：在这些样本里一个路径都没匹配到的规则。判定方式和 v1 的"本次未命中的规则"相同，范围只是这条请求的样本。
- 请求之间空一行。

### 3.3 过长的对象

一个对象（schema 数组里合并后的对象也算）的 key 超过 `--max-keys` 个时，只列出前 N 个，然后写一行：

```
  # … 还有 1234 个 key 没有列出（key 可能是 id 之类的动态值）
```

截断只影响展示，生效规则和"未命中的规则"仍按全部 key 计算。

### 3.4 `--write`：写出规则文件骨架

给每条（`--only` 范围内、有样本的）请求写出 `DIR/<name>.yaml`：

```yaml
# api-regress 规则文件：order-detail
# 每个 key 是相对路径；以 $ 开头的 key 是规则属性（$ignore、$decimal、$scale_level、$on_value_change、$array、$nullable）。
# 只列出字段、不写属性的节点不产生规则。写法见 doc/user/rules-file.md。
code:            # number
data:            # object
  amount:        # number  小数位 {2}
  coupon:        # object|null
    code:        # string
  items:         # array
  items[*]:      # object
    sku_id:      # string
    price:       # number  小数位 {2}
```

- 骨架只有字段和类型注释，**没有示例值**：规则文件通常会提交到仓库，示例值可能是用户数据或 token（D29）。
- 骨架**不写入现有的规则**：生效规则写进注释，规则一改注释就过时了。要看生效规则用不带 `--write` 的 `shape`。
- 被 ignore 的子树、超过 `--max-keys` 的 key 同样不展开，写法同 §3.2、§3.3。
- `DIR` 不存在时创建。**只要有一个目标文件已存在，一个都不写**，按参数错误处理（退出码 2），列出已存在的文件。
- 写完在 stdout 每个文件一行 `wrote <path>`，最后提示：在 suite 的请求里加上 `rules_file: <path>` 才会生效。

### 3.5 stdout 与退出码

不带 `--write` 时，stdout 是字段树；没有样本的请求写一行 `# <name>: 没有样本（原因）`。最后一行和其他命令一样是 `exit N: 原因`。

| 码 | 含义 |
|---|---|
| 0 | 所有请求都输出了 |
| 2 | suite 有问题（含规则文件的错误）；`--only` 的 name 不存在；`--max-keys` 不合法；`--write` 的目标文件已存在 |
| 3 | `--from` 不存在、既不是快照也不是 run 目录、损坏；写文件失败；内部错误 |
| 4 | 至少有一条请求没有样本：快照或 run 目录里没有这条请求，或者全部 sample 都是网络错误或非 JSON |

同时满足多个时按 **2 > 3 > 4 > 0** 取值。

### 3.6 日志

INFO 记录：样本来源（快照 / run 目录、请求数）、每条请求的节点数和样本数、写出的文件路径、退出码及原因。**示例值不写日志**。

## 4. 树形规则文件

### 4.1 引用

```yaml
requests:
  - name: order-detail
    method: GET
    path: /v1/orders/10086
    rules_file: rules/order-detail.yaml   # 相对 suite 文件所在的目录
    rules:                                 # 仍然可以同时写
      - path: data.amount
        on_value_change: breaking
```

- `rules_file` 只能写在请求上，值是文件路径：相对路径相对于 **suite 文件所在的目录**，也可以写绝对路径。
- 多条请求可以引用同一个文件。
- 读取规则文件的命令：`validate`、`run`、`snapshot record`、`snapshot compare`、`shape`。`snapshot accept` 不读 suite，不受影响。
- 规则文件**不参与请求指纹**（v1 的规则本来就不参与）。

### 4.2 格式

```yaml
code:
data:
  amount: {$decimal: {}, $on_value_change: breaking}
  coupon: {$nullable: true}
  items: {$array: {keys: [sku_id]}}
  items[*]:
    price: {$decimal: {}}
  meta["x.y"]: {$ignore: true}
"**.updated_at": {$ignore: true}
```

等价于：

```yaml
rules:
  - {path: data.amount, decimal: {}, on_value_change: breaking}
  - {path: data.coupon, nullable: true}
  - {path: data.items, array: {keys: [sku_id]}}
  - {path: "data.items[*].price", decimal: {}}
  - {path: 'data.meta["x.y"]', ignore: true}
  - {path: "**.updated_at", ignore: true}
```

**key 的两种含义**

| key | 含义 |
|---|---|
| `$ignore`、`$decimal`、`$scale_level`、`$on_value_change`、`$array`、`$nullable` | 规则属性，作用在**所在的节点**上。值的写法和 v1 规则里同名属性完全相同 |
| 其他 | **相对路径**，按 v1 的路径语法解析，拼在父节点的路径后面。可以是单个字段名（`amount`），也可以是多级（`data.amount`、`items[*].price`、`"**.updated_at"`） |

- 写在文件最顶层的属性作用在根节点 `$` 上（例如响应体本身是数组时，顶层写 `$array: {keys: [id]}`，元素写在 `"[*]":` 下面）。
- 以 `$` 开头但不是上面 6 个之一的 key 报错。JSON 字段名本身以 `$` 开头时，按 v1 的路径语法写成 `["$x"]`。
- **值**只能是映射或空（`amount:`）。空表示只列出字段、不配规则，不产生任何规则；骨架里的节点都是这种写法。写成字符串、数字、列表报错。
- 一个节点有至少一个属性时，展开成**一条**规则，路径是从根到这个节点的完整路径。完整路径必须是合法的 v1 规则路径（例如不能以 `**` 结尾），否则报错；只有子节点、没有属性的节点不检查。
- **同一个完整路径在文件里出现两次且都带属性时报错**（例如既写了 `data: {amount: {$decimal: {}}}` 又写了 `data.amount: {$ignore: true}`），让每个字段的规则只写在一处，方便 review。
- 空文件（或只有注释）表示没有规则。顶层不是映射时报错。
- YAML 的限制和 suite 相同：重复的 key 报错，展开锚点后不超过 100,000 个节点，嵌套不超过 64 层。
- YAML 的坑：`**` 和 `[` 开头的 key 要加引号（`"**.updated_at":`、`"[*]":`）；`items[*]:`、`meta["x.y"]:` 不用。

### 4.3 优先级

规则文件里的规则是**请求级**规则，按 v1 的合并方式参与比较，只是在"具体程度相同"时的顺序变为：

1. 越具体越优先（不变）。
2. 具体程度相同：**请求的 `rules` > `rules_file` > defaults 的 `rules`**（D27）。
3. 还相同（只会出现在同一来源内部）：后写的优先。规则文件里"后写"指在文件里出现得更靠后。

所以规则文件适合写"这个接口每个字段怎么比"，个别要临时覆盖的再写在 `rules` 里。

### 4.4 报错

规则文件的错误一律按 suite 错误处理（退出码 2），位置写成规则文件自己的 `<文件路径>:<行号>: <节点路径>:`：

- 文件不存在或读不出来：报在 suite 里 `rules_file` 所在的行。
- YAML 语法错误、重复 key、超出大小限制。
- 未知的 `$` 属性、值不是映射、路径语法错误、展开后的路径不合法、属性值不合法（例如 `$array: {mode: keyed}` 没写 `keys`）、同一路径重复。

`validate` 对规则文件里的规则做和 v1 相同的静态检查（`rule_under_ignored`、`index_rule_ineffective`、`rule_ineffective_in_schema`），warning 的位置同样写规则文件的文件名和行号。

## 5. 对已有输出的影响

### 5.1 result.json

只做**新增**，已有字段的含义不变（D31）：

- 规则引用 `rule_refs[]` 的 `scope` 新增取值 `rules_file`，`index` 是这条规则在规则文件展开后的规则列表里的序号（按文件里出现的顺序，从 0 开始）；同时新增可选字段 `file`（规则文件路径：相对路径按 suite 文件所在目录拼好，suite 在当前目录时就是 `rules_file` 的原文）、`line`（行号）。`scope` 为 `defaults` / `request` 时这两个字段不出现。
- warning 新增可选字段 `file`：warning 来自规则文件时为规则文件路径，此时 `line` 是规则文件里的行号。
- 本次未命中的规则 `unmatched_rules[]`：规则文件里的规则按请求级规则统计，`scope` 为 `rules_file`，带 `file`、`line`。
- 重新导出 JSON Schema。

### 5.2 报告

- "命中的规则"一列：来自规则文件的规则写成 `rules_file order-detail.yaml:9 data.coupon（nullable: true）`，其余不变。
- 本次未命中的规则同样写规则文件和行号。（报告的 warning 列表只有运行时的 warning，不带位置；静态 warning 只在 `validate` 里出现，见 §5.3。）

### 5.3 validate

stdout 的 `OK, N requests` 不变；规则文件的错误和 warning 按 §4.4 的位置格式列出。

## 6. 快照与 run 目录的落盘格式

### 6.1 目录结构

v1 把 body 原文作为字符串存在请求文件的 `samples[].body` 里（非 UTF-8 时存 `body_b64`），JSON 响应被转义成一整行字符串，没法直接看。v2 把每个 sample 的 body 单独存成文件，放在 `samples/` 目录下：

```
snap/                                   # 快照
  manifest.json
  requests/
    order-detail.json                   # 请求文件：元数据，samples[] 引用 body 文件
  samples/
    order-detail/
      1.json                            # 第 1 个 sample 的 body 原文
      2.json

api-regress-out/<时间戳>/               # run 目录
  result.json  report.html  report.md  junit.xml
  baseline/order-detail.json
  candidate/order-detail.json
  samples/
    baseline/order-detail/1.json
    baseline/order-detail/2.json
    candidate/order-detail/1.json
```

- 快照里一条请求的 body 放在 `samples/<name>/`，run 目录里放在 `samples/<side>/<name>/`（`side` 是 `baseline` 或 `candidate`）。
- 文件名是 sample 的序号（从 1 开始）加扩展名。

### 6.2 body 文件

- **JSON body 缩进格式化后保存**（D33）：按对比时的方式解析（数字用 Decimal），再按缩进 2 格输出，和 HTML 报告里 body 对照的排版相同。数字按原文的小数位输出（`12.50`、`-0.00` 不变），所以对比结果和存原文完全一样。会变的只有写法：
  - 指数写法规范化：`1.50E1` → `15.0`、`1E2` → `1E+2`、`0.0000001` → `1E-7`（小数位不变）；
  - `\u4e2d` 这类转义写成字符本身，BOM 去掉，末尾加换行。
- **以下情况存原始字节，不格式化**（现实中很少见）：
  - 不是 JSON（Content-Type 声明为其他类型），或者解析失败；
  - 有重复的 key（格式化会只剩最后一个，`duplicate_key` 提示就没了）；
  - Content-Type 的 charset 不是 UTF-8（格式化输出的是 UTF-8，和存下的 Content-Type 对不上）；
  - 自检不通过：格式化结果重新解析后，值或 warning 和原文的不一致。
- **扩展名**：Content-Type 是 JSON（判断方式和对比时相同）时为 `.json`，不论是否格式化；否则 body 能按 UTF-8 解码时为 `.txt`；否则为 `.bin`。扩展名只是为了方便打开，读取时不看扩展名。
- **不写文件的情况**：body 为空（包括 HEAD、204）的 sample，和网络错误的 sample。

### 6.3 请求文件

`requests/<name>.json`（快照）、`baseline/<name>.json`、`candidate/<name>.json`（run 目录）的格式：

```json
{
  "name": "order-detail",
  "fingerprint": "e1e0b46e…",
  "fingerprint_version": 1,
  "samples": [
    {
      "url": "https://api.shop.example.com/v1/orders/10086",
      "status": 200,
      "headers": {"content-type": "application/json"},
      "body_file": "1.json",
      "body_formatted": true,
      "body_size": 1834,
      "body_sha256": "9f2c…",
      "elapsed_ms": 42.1,
      "retries": 0
    }
  ]
}
```

- 和 v1 相比：去掉 `body`、`body_b64`，新增 `body_file`、`body_formatted`（body 文件是格式化过的为 `true`，是原始字节为 `false`）、`body_size`（body 文件的字节数）、`body_sha256`（body 文件内容的 SHA-256，十六进制小写）。其余字段不变。
- `body_file` **只是文件名**，所在目录由 6.1 的约定决定，必须符合 `<序号>.json|txt|bin` 的写法，序号等于这个 sample 在 `samples` 里的位置；不符合时按损坏处理。这样请求文件里写不出指向别处的路径。
- body 为空时没有 `body_file`、`body_formatted`、`body_sha256`，`body_size` 为 0；网络错误的 sample 和 v1 一样只有 `url`、`status`（null）、`error`、`retries`。
- **读取时校验**：body 文件不存在、大小或 SHA-256 和请求文件对不上，都按损坏处理。手动改了 body 文件也会被发现。

### 6.4 版本与兼容

- 快照 `manifest.json` 的 `schema_version` **仍然是 1**，不升级（D34）：v1 还没有用户，不需要区分新旧格式。
- 旧格式的请求文件（sample 里有 `body` / `body_b64`、没有 `body_file`）一律按格式不对处理：`compare` 读到时快照损坏（退出码 3），`accept` 读到 run 目录里的旧格式文件时 run 目录损坏（退出码 3）。提示里写明是旧格式，快照用 `snapshot record --clean` 重录，run 目录重新运行 `run` 或 `snapshot compare`。
- `result.json` 的内容不受影响，`schema_version` 不变。

### 6.5 对已有行为的影响

v1 关于快照和 run 目录的规则都保留，只是"一条请求的文件"从一个文件变成"请求文件 + 它的 body 目录"：

- **record / accept 原样复制没变的请求**：请求文件和 `samples/<name>/` 目录都按字节原样复制，不解析、不校验 body（和 v1 一样，大快照上 `--only` 不用读全部 body）。`samples/<name>/` 不存在时照样复制请求文件，到 `compare` 时再按损坏报出来，用 `snapshot record --only NAME` 重录即可修好。
- **accept**：读出 run 目录里的 `candidate/<name>.json` 和 `samples/candidate/<name>/`（同时按 6.3 校验），再写成快照的 `requests/<name>.json` 和 `samples/<name>/`。格式化是幂等的，写出的 body 文件和 run 目录里的逐字节相同。校验不通过时和 v1 的候选请求文件损坏一样：拒绝这条请求（原因写"run 目录里的候选响应损坏：…"），run 目录按损坏处理，退出码 3。
- **快照损坏的判定**：v1 的条件不变，`compare` 另外检查要用到的 body 文件（6.3）。
- **run 目录的复用**：`samples/` 和两侧目录一样，复用前整个删掉；判断"上次中断留下的 run 目录"时，`samples/` 下允许任意文件。
- **原子写入**：`samples/` 和 `requests/` 一起写在同级的临时目录里，整体交换，和 v1 相同。
- **不保存请求头**、响应体原样保存、不要把快照提交到公开的地方：都和 v1 相同。body 单独成文件后更容易被误提交，`doc/user/snapshot.md` 要写明。
- **请求 name 只差大小写**：在 macOS、Windows 上 `samples/` 下的目录也会冲突，和 v1 的请求文件冲突是同一个问题。

## 7. 验收标准

所有用例都用本地 mock 服务或固定的快照 / run 目录自动化执行。

**shape**

| ID | 验收项 |
|---|---|
| AC-38 | 从快照和从 run 目录各输出一次字段树：类型是所有 sample、所有数组元素的并集；数字列出小数位集合，数组列出长度集合；示例值取第一个非 null 值，长字符串截断；节点顺序是第一次出现的顺序 |
| AC-39 | 字段树上每个属性显示的是最终生效的规则及其来源，覆盖 defaults 的 `**` 规则、请求级规则、规则文件三种来源；被 ignore 的节点注明"子树不对比"且不展开；写了字面下标的规则和未命中的规则在请求末尾列出 |
| AC-40 | 对象的 key 超过 `--max-keys` 时只列前 N 个并提示剩余个数；未命中规则的判定不受截断影响 |
| AC-41 | `shape` 不发请求，在没有设置任何环境变量时也能运行；`--from` 里没有某条请求时这条写"没有样本"，退出码 4；`--from` 不是快照也不是 run 目录时退出码 3 |
| AC-42 | `--write` 写出的骨架不含示例值；任一目标文件已存在时一个都不写，退出码 2；骨架不加任何属性直接作为 `rules_file` 时 `validate` 通过且不产生规则 |

**规则文件**

| ID | 验收项 |
|---|---|
| AC-43 | §4.2 的规则文件与它的等价 `rules` 分别跑同一组 mock 响应，finding（类型、级别、路径）和 warning 完全相同，只有 `rule_refs` 的来源不同 |
| AC-44 | 具体程度相同时，请求的 `rules` 覆盖 `rules_file`，`rules_file` 覆盖 defaults；更具体的规则不论来源都优先；同一文件里后出现的优先 |
| AC-45 | 未知 `$` 属性、值是标量或列表、路径语法错误、展开后以 `**` 结尾、属性值不合法、同一路径重复、重复 YAML key、文件不存在：都是退出码 2，并指出规则文件（或 suite 中 `rules_file`）的文件名和行号 |
| AC-46 | `result.json` 里来自规则文件的 `rule_refs` 带 `scope: rules_file`、`file`、`line`，能通过重新导出的 JSON Schema 校验；报告的"命中的规则"、未命中的规则、`validate` 的静态 warning 都显示规则文件和行号 |
| AC-47 | v1 的全部验收项仍然通过（涉及快照、run 目录格式的用例按 §6 改写）；不使用 `rules_file` 的 suite，`result.json` 和报告与 v1 逐字节相同（golden 不变） |

**落盘格式**

| ID | 验收项 |
|---|---|
| AC-48 | 合法的 UTF-8 JSON（含 BOM、`\u` 转义、指数写法、`NaN`）写出缩进格式化的 `.json`，`body_formatted: true`，重新读回后的值、finding、warning 和用原文对比完全相同；重复 key、GBK 编码、声明为 JSON 但解析失败的 body 按原始字节写出，`body_formatted: false`；HTML 写出 `.txt`、二进制写出 `.bin`，都和原始字节相同；HEAD、204、网络错误不写 body 文件；请求文件里没有 `body`、`body_b64` |
| AC-49 | 对 v1 的 AC-26 ~ AC-29 按新格式重跑，结果相同；`record --only A` 时其他请求的请求文件和 `samples/` 目录逐字节不变；`accept` 写进快照的 body 文件和 run 目录里候选的逐字节相同 |
| AC-50 | body 文件缺失、大小或 SHA-256 不符、`body_file` 不是约定的文件名（如 `../x.json`、`2.json` 写在第 1 个 sample 上）时，`compare` 退出码 3；`accept` 遇到这样的候选拒绝这条请求并按 run 目录损坏处理，退出码 3（和 v1 候选请求文件损坏的处理相同） |
| AC-51 | 旧格式（sample 内嵌 `body` / `body_b64`）的快照：`compare` 退出码 3，提示用 `snapshot record --clean` 重录，`record --clean` 能正常重录；`accept` 读旧格式的 run 目录退出码 3。新快照的 `schema_version` 仍是 1 |
| AC-52 | 复用 run 目录时清掉上次的 `samples/`；中断留下的、含 `samples/` 的 run 目录可以复用 |

## 8. 决策记录与待定问题

| # | 问题 | 状态 |
|---|---|---|
| D25 | v2 范围：只做字段树视图（`shape`）和树形规则文件（`rules_file`），规则文件用 YAML。绝对校验、JSON Schema / OpenAPI 导入不做，等用一段时间再定（用户提出，2026-11-12） | 已确认（2026-11-12） |
| D26 | 规则文件的格式：和响应同形的 YAML；key 是相对路径（沿用 v1 路径语法，可以多级）；规则属性用 `$` 前缀，和字段名分开；值只能是映射或空；同一路径重复带属性时报错。否决：JSON Schema 加 `x-` 扩展字段（每层字段要多写 `properties` / `items` 两三层，难以通读）；属性不加前缀（字段名叫 `array`、`ignore` 时会混淆）；数组元素写成 `items: {"[*]": ...}`（多一层缩进，`[*]` 必须加引号） | 已确认（2026-11-12） |
| D27 | 具体程度相同时的来源顺序：请求 `rules` > `rules_file` > defaults | 已确认（2026-11-12） |
| D28 | `shape` 的样本只来自快照或 run 目录，不实时请求：命令只读、不需要 token，结果可重复 | 已确认（2026-11-12） |
| D29 | `--write` 写出的骨架不含示例值、不含现有规则（可能提交到仓库；注释里的规则会过时）；目标文件已存在时一个都不写 | 已确认（2026-11-12） |
| D30 | 动态 key 对象（`{*}`）本期不做：v1 下这种对象每次都会报字段增删，只能 ignore；`shape` 先用 `--max-keys` 截断展示。实测：models.dev 的 `/api.json` 有 28.3 万条不同的字段路径，把两层 id 对象折叠后只剩 120 条 | 已确认（2026-11-12） |
| D31 | `result.json` 只做新增：`rule_refs[].scope` 新增 `rules_file`，新增可选的 `file`、`line`；warning 新增可选的 `file` | 已确认（2026-11-12） |
| D32 | 快照和 run 目录的 body 单独存文件（用户提出，2026-11-12）：v1 把 body 存成请求文件里的转义字符串，没法直接查看。改为快照 `samples/<name>/<序号>.<扩展名>`、run 目录 `samples/<side>/<name>/…`，请求文件的 sample 用 `body_file` 引用，只写文件名，目录按约定推出（请求文件里写不出路径穿越，accept 也能按字节原样复制请求文件）；附 `body_size`、`body_sha256` 用来发现文件缺失或被改动（§6）。否决：body 文件放在请求文件旁边、和请求同名的目录里（`requests/order-detail/1.json`；用户要求放在单独的目录下）；`body_file` 写相对路径（accept 时要改写请求文件，还要防路径穿越） | 已确认（2026-11-12） |
| D33 | JSON body 用和报告相同的缩进排版保存（解析后按 Decimal 原文输出，小数位不变，对比结果和存原文相同）；重复 key、charset 不是 UTF-8、解析失败、自检不通过时存原始字节，用 `body_formatted` 标明。否决：一律存原始字节（压缩成一行的 JSON 没法直接看）；不解析、只调整空白的格式化（要多写一个扫描器，收益只是保住几种罕见情况）（用户确认，2026-11-12） | 已确认（2026-11-12） |
| D34 | 快照 `schema_version` 保持 1，不升级、不写迁移：v1 还没有用户。旧格式的请求文件按损坏处理（退出码 3），提示用 `snapshot record --clean` 重录（用户决定，2026-11-12） | 已确认（2026-11-12） |
