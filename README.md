# api-regress

一个给 QA 做 API 回归用的命令行小工具，开发也可以用。

它把同一组请求分别发给**基线**和**候选**两个目标，对比两边 JSON 响应的结构和字段，及时发现发版后接口被意外改坏的情况：

- 字段被删除、被改名
- 字段类型被改（包括对象变成 `null`）
- 金额精度变化，比如 `12.50` 变成 `12.5`
- 状态码变化、业务错误码异常（HTTP 200，但 body 里 `code != 0`）

差异分为 **breaking / suspicious / info** 三级，结果输出成报告，同时给出 CI 可直接使用的退出码。

## 适用场景

| 场景 | 做法 |
|---|---|
| 预发和生产同时在线，发版前对比 | `run`：两边都实时请求 |
| 只有一个环境：发版前录基线，发版后对比 | `snapshot record` → 发版 → `snapshot compare` |
| 差异是预期内的变更，需要更新基线 | `snapshot accept` |
| 接入 CI，出现 breaking 就阻断 | `--fail-on breaking`，配合 JUnit 报告 |

## 功能

- **零配置发现结构破坏**：默认只比较结构（字段增删、类型、状态码）。只有显式配置了规则，才会比较值。
- **按字段配置规则**：
  - `ignore`：忽略无关字段
  - `decimal`：检查金额的小数位
  - `on_value_change`：字段值变了算哪一级
  - `array`：数组怎么对齐，例如按 id
  - `nullable`：字段允许为 null
- **业务成功条件**：`success: "body.code == 0"`，用来识别"HTTP 200 包着业务错误"的情况。
- **asserts 表达式**（高级用法）：在一行表达式里同时访问基线和候选。
- **字段树**：`shape` 从录好的样本里列出每个字段的类型、示例值和最终生效的规则，一眼看出哪些字段没配规则；还能写出规则文件骨架。
- **不稳定字段建议**：基线会请求两次，两次结果不一样的字段（时间戳、traceId 等）列为"建议 ignore"，但不会自动忽略。
- **报告**：HTML、Markdown、JUnit XML，以及机器可读的 `result.json`。
- **只读请求**：只发 GET / HEAD，不会改动线上数据。

**不做**：Postman / Apifox 那类接口管理平台的功能，包括登录流程、请求串联、前后置脚本、Mock、接口文档。（内置的 `demo` 服务只提供固定的演示数据，不是 Mock 功能。）需要鉴权的接口，先自己拿到 token，再通过 `${VAR}` 注入请求头。

## 安装

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。在仓库根目录执行：

```bash
uv tool install --python 3.12 .   # 安装 api-regress 命令到 uv 的工具目录
api-regress --help                # 确认可用
```

如果提示找不到 `api-regress`，运行 `uv tool update-shell` 把工具目录加进 `PATH`，然后重开终端。代码更新后用 `uv tool install --python 3.12 --reinstall .` 重新安装。

不想全局安装的话，也可以 `uv sync --python 3.12` 后在下文每条命令前加 `uv run`（见[开发](#开发)）。

## 快速上手

### 不联网先试一下

内置一个本地演示服务：`/v1` 是基线，`/v2` 是故意改坏了几处的候选（见 [doc/user/demo.md](doc/user/demo.md)）。

```bash
api-regress demo --write-suites demo/   # 写出示例 suite
api-regress demo                        # 另开一个终端启动服务，Ctrl+C 停止
api-regress run demo/pass.yaml          # 两侧都是 /v1：全部通过，退出码 0
api-regress run demo/regress.yaml       # /v1 对 /v2：报出 breaking，退出码 1
```

然后用浏览器打开 stdout 里 `reports:` 行列出的 `report.html`，看每处改动报成了什么。

### 对比自己的接口

写一个 `suite.yaml`（完整字段见 [doc/user/suite.md](doc/user/suite.md)）：

```yaml
version: 1

targets:
  baseline:
    base_url: https://api.shop.example.com
    headers:
      Authorization: "Bearer ${BASE_TOKEN}"
  candidate:
    base_url: https://staging.shop.example.com
    headers:
      Authorization: "Bearer ${CAND_TOKEN}"

defaults:
  success: "body.code == 0"
  rules:
    - path: "**.updated_at"
      ignore: true
    - path: "**.amount"      # 金额精度需要显式开启
      decimal: {}

requests:
  - name: order-detail
    method: GET
    path: /v1/orders/10086
    rules:
      - path: data.items
        array: { keys: [sku_id] }   # 数组按 sku_id 对齐
```

运行：

```bash
api-regress validate suite.yaml               # 只校验配置，不发请求
api-regress run suite.yaml --fail-on breaking # 两边实时请求并对比
api-regress demo                              # 启动本地演示服务

# 只有一个环境时：
api-regress snapshot record  suite.yaml -d snap/   # 发版前
api-regress snapshot compare suite.yaml -d snap/   # 发版后
api-regress snapshot accept  api-regress-out/<时间戳> -d snap/ --only order-detail

# review 规则：每个字段最后按什么规则比较、哪些字段没配（只读，不发请求）
api-regress shape suite.yaml --from snap/ --only order-detail
api-regress shape suite.yaml --from snap/ --write rules/          # 写出规则文件骨架
```

结果输出到 `api-regress-out/<时间戳>/` 目录：`report.html`（浏览器打开排查）、`report.md`（贴到 PR）、`junit.xml`（给 CI）、`result.json` 和两侧的响应，stdout 打印摘要。用 `--format md,junit` 只要其中几种报告。命令见 [doc/user/run.md](doc/user/run.md)，报告怎么看见 [doc/user/report.md](doc/user/report.md)。快照的录制、对比和更新见 [doc/user/snapshot.md](doc/user/snapshot.md)：`snapshot record` 只需要基线的环境变量，`snapshot compare` 只需要候选的；快照缺失或指纹不一致的请求是 `inconclusive`，`snapshot accept` 后即可正常对比。

**退出码**

| 退出码 | 含义 |
|---|---|
| 0 | 通过 |
| 1 | 有达到 `--fail-on` 级别的差异 |
| 2 | 配置错误（含命令行参数写错、快照版本不兼容） |
| 3 | 工具自身错误（含快照损坏、无法写入快照、run 目录或报告） |
| 4 | 部分请求无法得出结论，比如网络错误、token 失效、基线不可用、快照里没有；`snapshot record` / `accept` 有请求没写入快照；`shape` 有请求没有样本 |

## 文档

| 文档 | 内容 |
|---|---|
| [doc/user/suite.md](doc/user/suite.md) | suite 配置格式：完整示例、字段、路径写法、规则怎么合并 |
| [doc/user/shape.md](doc/user/shape.md) | `shape` 命令：字段树怎么看、生效的规则、过长的对象、写出规则文件骨架、退出码 |
| [doc/user/rules-file.md](doc/user/rules-file.md) | 规则文件：按响应结构写规则（`rules_file`）、格式、优先级、YAML 的坑 |
| [doc/user/validate.md](doc/user/validate.md) | `validate` 命令、warning、环境变量、YAML 常见坑 |
| [doc/user/run.md](doc/user/run.md) | `run` 命令：请求怎么发、每条请求的流程与状态、非 body 的差异、不稳定字段建议、`--format`、stdout、退出码、run 目录与 `result.json` |
| [doc/user/snapshot.md](doc/user/snapshot.md) | `snapshot record` / `compare` / `accept`：快照怎么录、怎么对比、accept 接受和拒绝的规则、请求指纹、快照目录格式与原子写入、退出码 |
| [doc/user/report.md](doc/user/report.md) | 怎么看报告：各板块、finding 的值怎么读、字段改名等示例、JUnit 的判定 |
| [doc/user/compare.md](doc/user/compare.md) | 对比规则：差异类型与级别、规则属性、数组模式怎么选、已知限制 |
| [doc/user/expressions.md](doc/user/expressions.md) | `success` 与 `asserts` 表达式的写法、内置函数、不支持的语法 |
| [doc/user/demo.md](doc/user/demo.md) | `demo` 本地演示服务：接口、v2 改了什么、两个示例 suite |
| [doc/dev/architecture.md](doc/dev/architecture.md) | 模块职责、关键路径、主流程、`result.json` 契约（随实现补充） |
| [schema/result.schema.json](schema/result.schema.json) | `result.json` 的 JSON Schema（自动导出） |
| [doc/dev/diff-combinations.md](doc/dev/diff-combinations.md) | 对比引擎在各种组合下的实际输出（自动生成） |
| [doc/dev/expr-combinations.md](doc/dev/expr-combinations.md) | 表达式引擎在各种组合下的实际结果（自动生成） |

## 开发

- 环境：Python 3.12，用 [uv](https://docs.astral.sh/uv/) 管理依赖。
- 代码约束：每个函数的 radon 复杂度不超过 B，单个文件不超过 1000 行。

```bash
uv sync --python 3.12                   # 建虚拟环境并安装依赖（含开发依赖）
uv run api-regress validate suite.yaml  # 在开发环境里运行命令
```

提交前的检查（CI 同样执行，见 `.github/workflows/ci.yml`）：

```bash
uv run radon cc -n C src/   # 有输出即失败
uv run lint-imports         # core 不能依赖 cli 或 report
uv run pytest
```

改了对比行为时，`tests/test_diff_combinations.py` 会失败：确认变化符合预期后运行 `uv run python scripts/gen_diff_combinations.py` 重新生成 [doc/dev/diff-combinations.md](doc/dev/diff-combinations.md)。
改了表达式行为时同理：`tests/test_expr_combinations.py` 的快照测试会失败，确认后运行 `uv run python scripts/gen_expr_combinations.py` 重新生成 [doc/dev/expr-combinations.md](doc/dev/expr-combinations.md)；同一文件里的不变量测试失败则说明是 bug。

改了报告格式时，`tests/test_report.py` 的快照测试会失败：确认变化符合预期后运行 `UPDATE_GOLDEN=1 uv run pytest tests/test_report.py` 重新生成 `tests/golden/` 下的快照，逐行检查 diff。

改了 `result.json` 的数据模型时，`tests/test_result_schema.py` 会失败：确认后运行 `uv run python scripts/export_result_schema.py` 重新导出 [schema/result.schema.json](schema/result.schema.json)。

项目规则和提交约定见 [CLAUDE.md](CLAUDE.md)。
