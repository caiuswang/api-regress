# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 本文件只写长期稳定的项目规则和背景，不写当前进度。
> 当前进度和下一步做什么，看当前版本的任务文件（`doc/spec/spec-v*-tasks.md`）。

## 项目背景

api-regress 是一个给 QA 做 API 回归用的 Python CLI 工具，开发也可以用。它把同一组 GET/HEAD 请求分别发给**基线（baseline）**和**候选（candidate）**两个目标，比较 JSON 响应的结构和字段，按 breaking / suspicious / info 分级，生成报告（HTML / Markdown / JUnit / `result.json`），并给出 CI 能用的退出码。

主要场景是**发版后接口被意外改坏**，比如删了字段、改了类型、金额精度变了、状态码变了。

**非目标**：不做 Postman / Apifox 那样的接口管理平台。登录流程、请求串联、前后置脚本、Mock、断言编辑器都不做。

## 文档体系

| 位置 | 内容 |
|---|---|
| `spec-01.md` | 最初的需求简述 |
| `doc/spec/spec-vN.md` | 行为规格，**行为以它为准**；内含验收项（AC）和决策记录 |
| `doc/spec/spec-vN-design.md` | 实现约束，以及实测过的技术结论；和 spec 冲突时以 spec 为准 |
| `doc/spec/spec-vN-tasks.md` | 任务拆分、依赖关系、**当前进度** |
| `doc/user/` | 面向使用者：命令、配置格式、报告怎么看 |
| `doc/dev/` | 面向维护者：模块职责、关键路径、主流程图、数据契约 |

**规则**
- 改行为之前，先改 spec。
- spec 里已经确认的决策不要推翻；确实需要变更时，先和用户确认。
- 文档里**不要引用代码行号**，因为行号容易和代码对不上。改为写文件名或函数名。

## 硬性约束

- Python 3.12；配置用 YAML；对外交互只走 HTTP/HTTPS。
- 每个函数的 radon 等级不能超过 B（CC ≤ 10）。单个文件不超过 1000 行。
  容易写出长 if 链的地方，改成数据驱动：有序规则表、分发表、`max(key=...)`。
- 注释只写在逻辑复杂、或需要特别说明的地方。
- 关键节点必须打日志。**任何级别的日志都不能输出请求头的值**；快照、运行输出目录、报告里也不能出现。

## 检查命令

依赖用 uv 管理：`uv sync --python 3.12` 安装，命令前加 `uv run`。CI 见 `.github/workflows/ci.yml`，必须通过以下检查：

```
radon cc -n C src/     # 有输出即失败
lint-imports           # import-linter：core 不能 import cli 或 report（契约在 pyproject.toml）
pytest                 # 跑单个测试：pytest tests/path/test_x.py::test_name
```

## 架构原则

- **分层**：
  - `core` 是纯库，输入输出都是 pydantic v2 模型，提供 async API 和同步包装。
  - `cli` 和 `report` 只调用 `core`。
  - 以后加界面时也直接调用 `core.service`。
  - `result.json` 是对外的数据契约，必须保持稳定，有 JSON Schema 约束。
- **默认只比较结构**：值只在显式配置（`on_value_change` 或 `compare: values`）时才比较。
- **finding 和 warning 要分开**：finding 参与退出码计算，warning 不参与。
- **对比引擎自己实现**：deepdiff 识别不了 `1.0` 和 `1.00` 的小数位差异，只在测试里用来做交叉校验。

## 已实测的坑

- **小数位**：用 `max(0, -d.as_tuple().exponent)` 计算。**不要调用 `Decimal.normalize()`**，它会抹掉末尾的 0。
- **simpleeval 表达式**：
  - body 必须包一层只读的 `JsonView`，否则 `body.x.items` 拿到的是 dict 的方法。
  - 只把浮点字面量转成 Decimal，整数保持 int。
  - AST 白名单必须限制函数调用的目标。
- **BOM**：`response.text` 会保留 BOM，解析 JSON 前要先去掉。
- **原子替换目录**：对非空的已存在目录执行 `os.replace` 会报 `ENOTEMPTY`。做法是先写到同级的临时目录，再交换，最后删除 `.old`。
- **重试**：httpx transport 自带的 `retries` 不会重试超时，所以重试要自己实现，只针对连接错误和超时。

## 文档同步（防止文档和代码脱节）

项目有任何改动，在完成之前都要检查文档是否已经跟着变了；没跟上的，在同一个分支里一起更新。

| 改了什么 | 需要同步的文档 |
|---|---|
| CLI 命令、参数、stdout 输出、退出码 | `doc/user/` 的命令说明、`README.md` |
| suite 配置字段、规则语义、路径语法 | **先改 spec**，再改 `doc/user/` 的格式和规则说明 |
| finding 类型、级别、请求状态、warning code | spec 中的对比语义、`doc/user/` 中的报告解读、`result.json` 的 JSON Schema、`doc/dev/` |
| `result.json` 的数据模型 | 重新导出 JSON Schema，更新 `doc/dev/` 中的数据契约 |
| 新增、删除、重命名模块，模块职责变化，主流程变化 | `doc/dev/` 中的模块职责、关键路径、主流程图 |
| 依赖、检查命令、开发命令 | 本文件中的"检查命令"一节、`README.md` |
| 某项实现约束已经落地 | 把它从 `spec-vN-design.md` 迁到 `doc/dev/` |
| 任务开始或完成 | `spec-vN-tasks.md` 中的状态列 |
| 项目规则或约定变化 | 本文件 |

**检测方法**（收尾前执行）
1. 列出这次改动涉及的标识符，包括命令名、参数名、配置字段、finding 类型、文件名、函数名，然后在 `doc/`、`README.md`、`CLAUDE.md` 里逐个 grep，确认描述仍然正确。
2. `doc/dev/` 里引用的文件路径和函数名，都要在代码里实际存在。改名或删除时，全局搜索一遍旧名字。
3. 文档里的示例（suite YAML、命令、报告片段）要和当前实现一致；能直接执行的示例要实际跑一遍。
4. 发现和本次改动无关的文档偏差时，在回复里告诉用户，不要悄悄忽略。

## Git 提交规则

- **提交时机**：只在用户要求时才提交。如果当前在 `main` 上做功能开发，先建分支，命名为 `feat/<任务号>-<简述>`，例如 `feat/t1-config`。
- **提交信息**：用 Conventional Commits 格式，`<type>(<scope>): <中文简述>`。
  - type：`feat`、`fix`、`refactor`、`test`、`docs`、`chore`。
  - scope：用模块名或任务号，例如 `feat(config): 支持 ${VAR:-default}`、`docs(spec): 调整数组长度变化的级别`。
  - 正文说明为什么改。涉及验收项时写上 AC 编号。
- **一次提交只做一件事**：spec 或文档的修改，和代码修改分开提交。
- **提交前先跑检查**：radon、lint-imports、pytest 都要通过，不能提交跑不通的代码。
- **不要提交**：运行输出（`api-regress-out/`）、快照数据、虚拟环境、含 token 的文件。
- 提交信息末尾加上 harness 要求的 Co-Authored-By 行。
