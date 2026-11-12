# 规则文件：按响应结构写规则

suite 里的 `rules` 是一串路径（`data.amount`、`data.items[*].price`），字段一多、嵌套一深就很难看出每个字段配了什么。规则文件把规则写成和响应同形的 YAML 树，一个接口的规则集中在一个文件里，方便整体 review。

规则文件只是另一种写法：加载时展开成和 `rules` 完全相同的路径规则，对比结果不变。

不想从头手写时，先录一个快照，用 [`shape --write`](shape.md#写出规则文件骨架---write) 写出列好了所有字段的骨架，再在需要的字段上加属性；写好后用不带 `--write` 的 `shape` 看每个字段最终生效的规则。

## 引用

在请求上写 `rules_file`：

```yaml
requests:
  - name: order-detail
    method: GET
    path: /v1/orders/10086
    rules_file: rules/order-detail.yaml   # 相对 suite 文件所在的目录
    rules:                                 # 可以同时写，具体程度相同时 rules 优先
      - path: data.amount
        on_value_change: breaking
```

- 相对路径相对于 **suite 文件所在的目录**，不是当前目录；也可以写绝对路径。
- 只能写在请求上，defaults 里不能写。多条请求可以引用同一个文件。
- `validate`、`run`、`snapshot record`、`snapshot compare`、`shape` 都会读规则文件；`snapshot accept` 不读 suite，不受影响。
- 规则文件不参与[请求指纹](snapshot.md#请求指纹)，改规则不会让快照失效。
- 规则文件里不替换 `${VAR}`。

## 格式

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

每个 key 有两种含义：

| key | 含义 |
|---|---|
| `$ignore`、`$decimal`、`$scale_level`、`$on_value_change`、`$array`、`$nullable` | 规则属性，作用在**所在的节点**上。值的写法和 [suite.md](suite.md) 里同名属性完全相同 |
| 其他 | **相对路径**，按 [suite.md](suite.md) 的「路径写法」解析，拼在父节点后面。可以是一个字段名（`amount`），也可以是多级（`data.amount`、`items[*].price`、`"**.updated_at"`） |

- **数组**：数组本身的规则（`$array`）写在 `items:` 上，元素里的字段写在 `items[*]:` 下面。
- **根节点**：写在文件最顶层的属性作用在根节点 `$` 上。响应体本身是数组时：

  ```yaml
  $array: {keys: [id]}
  "[*]":
    price: {$decimal: {}}
  ```

- **只列字段、不配规则**：值留空（`code:`），不产生任何规则。可以把整个响应的字段都列出来，只给需要的字段加属性，review 时一眼能看出哪些字段没配。
- **字段名以 `$` 开头**：写成 `'["$x"]':`（`$` 开头的 key 都当作属性）。
- **同一个字段的规则只能写在一处**：既写了 `data: {amount: {$decimal: {}}}`，又写了 `data.amount: {$ignore: true}`，会报错。把属性合到一起写。
- 一个节点有属性时展开成一条规则；它的完整路径必须是合法的规则路径，例如 `"**": {$ignore: true}` 展开后以 `**` 结尾，会报错。
- 空文件（或只有注释）表示没有规则。

## YAML 的坑

| 写法 | 问题 | 正确写法 |
|---|---|---|
| `**.updated_at:` | `*` 开头在 YAML 里是锚点引用 | `"**.updated_at":` |
| `[*]:`、`["$x"]:` | `[` 开头在 YAML 里是列表 | `"[*]":`、`'["$x"]':` |
| `200:`、`on:`、`no:` | 被解析成数字、布尔值，报错 | `"200":`、`"on":` |
| `items[*]:`、`meta["x.y"]:` | 没问题，不用加引号 | |

和 suite 一样，重复的 key 会报错，文件规模的限制也相同（见 [validate.md](validate.md)）。

## 优先级

规则文件里的规则算作请求级规则，按 [suite.md](suite.md) 的「规则怎么合并」参与比较。具体程度相同时：

**请求的 `rules` > `rules_file` > defaults 的 `rules`**

同一个文件里具体程度相同的两条规则（例如 `"**.b.x"` 和 `"a.**.x"` 都匹配 `a.b.x`），写在后面的优先。

所以通常的用法是：规则文件写"这个接口每个字段怎么比"，个别要临时覆盖的写在 `rules` 里。

## 报错和提示

规则文件的错误按 suite 错误处理（退出码 2），位置写的是规则文件自己的文件名、行号和节点路径：

```
INVALID, 2 error(s)
  rules/order-detail.yaml:3: data: 未知的规则属性 '$nullabel'，只能是 $ignore、$decimal、$scale_level、$on_value_change、$array、$nullable；字段名本身以 $ 开头时写成 '["$nullabel"]':
  rules/order-detail.yaml:5: data.items $array: keyed 模式必须写 keys
```

规则文件不存在时，错误报在 suite 里 `rules_file` 那一行。

`validate` 对规则文件里的规则做和 suite 里一样的检查（`rule_under_ignored` 等，见 [validate.md](validate.md)），提示的位置同样是规则文件：

```
  [rule_under_ignored] rules/order-detail.yaml:2: data.amount: 规则 'data.amount' 位于已被 ignore 的 'data' 之下，整棵子树不对比，这条规则不会生效
```

## 报告和 result.json 里怎么显示

- 报告「命中的规则」一列：`rules_file rules/order-detail.yaml:3 data.amount（on_value_change: breaking）`。
- 本次未命中的规则：`order-detail: rules_file rules/order-detail.yaml:9 data.legacy`。
- `result.json`：`rule_refs` 和 `unmatched_rules` 里来自规则文件的规则是 `{"scope": "rules_file", "index": 0, "file": "rules/order-detail.yaml", "line": 3}`，`index` 是展开后的第几条（从 0 开始，按文件里从上到下的顺序）。见 [run.md](run.md) 的「result.json」。

文件路径是按 suite 所在目录拼好的路径：suite 和规则文件目录都在当前目录下时，就是 `rules_file` 的原文。
