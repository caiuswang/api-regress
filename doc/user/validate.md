# validate：检查 suite 配置

```
api-regress validate <suite.yaml> [--log-level LEVEL] [--log-file FILE]
```

只做静态检查，**不发任何请求**。检查内容：

- YAML 语法、重复的 key、文件规模（嵌套超过 64 层、锚点展开后过大、锚点引用自身都会报错）
- 字段拼写（未知字段直接报错）、字段类型
- `version` 只能是 `1`
- 请求 `name`：只能包含 `A-Z a-z 0-9 _ . -`，并且不能重复。**区分大小写**：`order-list` 和 `Order-List` 是两条请求，但在 macOS、Windows 上它们的快照文件会互相覆盖，不要这样起名
- `method`：只能是 `GET` 或 `HEAD`，写方法（POST/PUT/PATCH/DELETE）一律报错
- `base_url`：必须是 `http://` 或 `https://` 开头的完整地址（值来自缺失变量的那一侧除外）
- 规则：路径语法；至少写一个属性；`array` 的 `mode` 与 `keys` 是否一致
- 环境变量：写法无法识别时报错；缺少的变量**不报错**，按 target 列成 warning

## 输出与退出码

通过时（退出码 0）：

```
OK, 3 requests
warnings (3):
  [env_var_missing] suite.yaml:7: targets.baseline.headers.Authorization: baseline 缺少环境变量 BASE_TOKEN
  [env_var_missing] suite.yaml:11: targets.candidate.headers.Authorization: candidate 缺少环境变量 CAND_TOKEN
  [index_rule_ineffective] suite.yaml:51: requests[2].rules[3]: 规则 'data.items[0].sku_id' 用了字面下标，但数组 data.items 不是 positional 模式，规则不会生效
```

不通过时（退出码 2），每个错误都带文件名和行号：

```
INVALID, 2 error(s)
  bad.yaml:11: requests[0].query.page: 应为字符串（YAML 里的数字、布尔值、日期要加引号）
  bad.yaml:13: requests[1].method: v1 只发 GET/HEAD，不支持写方法 DELETE
```

- warning 不影响退出码。
- 命令行参数写错（缺少 suite 参数、`--log-level` 取值不对、子命令不存在）时，打印用法说明，退出码同样为 2。
- 工具自身出错（包括 `--log-file` 无法打开）时退出码为 3。
- YAML 语法错误只报出错位置和原因，**不回显出错那一行的原文**，以免把硬编码在 suite 里的 token 打到终端或 CI 日志里。

## warning 一览

| code | 含义 | 怎么处理 |
|---|---|---|
| `env_var_missing` | 某个 target（或两侧共用的部分）引用的环境变量没有设置 | 跑 `run` / `record` / `compare` 前设置好；只影响对应命令需要的那一侧 |
| `rule_under_ignored` | 规则写在一个已经被 `ignore: true` 的节点下面；或者规则所在的节点本身被更宽泛的 ignore 覆盖，而它自己没写 `ignore` | 删掉它；或者把父节点的 ignore 改得更精确；节点本身被覆盖时，在这条规则上加 `ignore: false` |
| `index_rule_ineffective` | 规则用了字面下标（如 `items[0].x`），但该数组不是 positional 模式 | 改用 `items[*].x`，或者给数组配 `array: {mode: positional}` |
| `rule_ineffective_in_schema` | `on_value_change` 写在 schema 模式数组的元素里，schema 模式不比较元素的值 | 给数组配 `array: {keys: [...]}` 改用 keyed 模式 |

有字段的值来自缺失的环境变量、而且这个字段不是字符串（比如 `timeout: "${T}"`）时，它的类型要等变量设置后才能确定。此时 `validate` 仍然通过，但会列出这些字段，并**跳过规则检查**；设置好变量再跑一次，就能得到完整结果。

## 环境变量

- 只替换**字符串值**里的 `${VAR}`，支持 `${VAR:-默认值}`：变量未设置**或为空**时取默认值，和 shell 一致。默认值可以为空：`${VAR:-}`。
- 默认值里不能出现 `$` 和 `}`，不支持嵌套。`${A:-${B}}`、`${A-x}`、`${ A }` 这类写法直接报错，不会被悄悄当成普通文本。
- 数值字段也可以写成字符串形式，例如 `timeout: "${TIMEOUT:-10}"`，会自动转换成数字。
- 每个命令只要求它用到的那一侧：`targets.baseline` 下的变量只有 baseline 需要，`targets.candidate` 同理；`defaults` 和 `requests` 里的变量两侧都需要。`validate` 不要求任何一侧。

## 规则的几个约定

- 每条规则至少要写一个属性（`ignore`、`decimal`、`scale_level`、`on_value_change`、`array`、`nullable`），只写 `path` 会报错。
- 请求级的 `success` 覆盖 defaults 里的；写成 `success: null` 表示这条请求不判定 success（例如 `/health` 这类 body 里没有业务码的接口）。

## YAML 常见坑

| 写法 | YAML 实际解析成 | 正确写法 |
|---|---|---|
| `page: 007` | 数字 7 | `page: "007"` |
| `amount: 1.10` | 数字 1.1 | `amount: "1.10"` |
| `flag: on` / `no` | 布尔值 | `"on"` / `"no"` |
| `start: 2024-02-01` | 日期（非法日期如 `2024-02-30` 直接报错） | `start: "2024-02-01"` |

`query` 和 `headers` 的值必须是字符串，没加引号的数字、布尔值、日期都会被 `validate` 报出来，而不会悄悄变成别的值。

## 路径写法速查

| 写法 | 含义 |
|---|---|
| `data.items` | 逐级访问对象 key，开头不写 `$` |
| `data.items[0]` | 数组下标，只在 positional 模式下生效 |
| `data.items[*].price` | 任意数组元素 |
| `meta["x.y"]` | key 里有 `.`、`[`、`]`、`"`、`*`、空白，或以 `$` 开头时用引号括起来；引号内用 `\"`、`\\` 转义 |
| `**.updated_at` | 任意深度（包括 0 层）。`**` 不能放在末尾，也不能连续写两个 |
| `$` | 根节点本身，只能单独写。响应体本身是数组时，用 `path: "$"` 配 `array`，元素写 `[*].price` |

规则的优先级和合并方式见 [spec §6.3](../spec/spec-v1.md)。
