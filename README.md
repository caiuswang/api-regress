# api-regress

一个给 QA 做 API 回归用的命令行小工具，开发也可以用。

它把同一组请求分别发给**基线**和**候选**两个目标，对比两边 JSON 响应的结构和字段，及时发现发版后接口被意外改坏的情况：

- 字段被删除、被改名
- 字段类型被改（包括对象变成 `null`）
- 金额精度变化，比如 `12.50` 变成 `12.5`
- 状态码变化、业务错误码异常（HTTP 200，但 body 里 `code != 0`）

差异分为 **breaking / suspicious / info** 三级，结果输出成报告，同时给出 CI 可直接使用的退出码。

> 项目还在开发中，当前进度见 [doc/spec/spec-v1-tasks.md](doc/spec/spec-v1-tasks.md)。下面的用法和格式以 [v1 需求](doc/spec/spec-v1.md) 为准。

## 适用场景

| 场景 | 做法 |
|---|---|
| 预发和生产同时在线，发版前对比 | `run`：两边都实时请求 |
| 只有一个环境：发版前录基线，发版后对比 | `record` → 发版 → `compare` |
| 差异是预期内的变更，需要更新基线 | `accept` |
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
- **不稳定字段建议**：基线会请求两次，两次结果不一样的字段（时间戳、traceId 等）列为"建议 ignore"，但不会自动忽略。
- **报告**：HTML、Markdown、JUnit XML，以及机器可读的 `result.json`。
- **只读请求**：只发 GET / HEAD，不会改动线上数据。

**不做**：Postman / Apifox 那类接口管理平台的功能，包括登录流程、请求串联、前后置脚本、Mock、接口文档。需要鉴权的接口，先自己拿到 token，再通过 `${VAR}` 注入请求头。

## 快速上手

写一个 `suite.yaml`：

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

# 只有一个环境时：
api-regress record  suite.yaml --snapshot snap/   # 发版前
api-regress compare suite.yaml --snapshot snap/   # 发版后
api-regress accept  api-regress-out/<时间戳> --snapshot snap/ --only order-detail
```

报告输出到 `api-regress-out/<时间戳>/` 目录。

**退出码**

| 退出码 | 含义 |
|---|---|
| 0 | 通过 |
| 1 | 有达到 `--fail-on` 级别的差异 |
| 2 | 配置错误 |
| 3 | 工具自身错误 |
| 4 | 部分请求无法得出结论，比如网络错误、token 失效、基线不可用 |

## 文档

| 文档 | 内容 |
|---|---|
| [doc/spec/spec-v1.md](doc/spec/spec-v1.md) | v1 需求（行为以它为准） |
| [doc/spec/spec-v1-design.md](doc/spec/spec-v1-design.md) | 技术设计约束 |
| [doc/spec/spec-v1-tasks.md](doc/spec/spec-v1-tasks.md) | 任务拆分与进度 |
| `doc/user/` | 使用文档（随实现补充） |
| `doc/dev/` | 维护文档（随实现补充） |

## 开发

- 环境：Python 3.12。
- 代码约束：每个函数的 radon 复杂度不超过 B，单个文件不超过 1000 行。
- 提交前的检查：

```bash
radon cc -n C src/   # 有输出即失败
lint-imports         # core 不能依赖 cli 或 report
pytest
```

项目规则和提交约定见 [CLAUDE.md](CLAUDE.md)。
