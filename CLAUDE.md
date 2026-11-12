# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 本文件只写长期稳定的项目规则和背景，不写当前进度。
> 当前进度和下一步做什么，看最新一个 spec 目录里的任务文件（`doc/spec/v*-*/tasks.md`）。

## 项目背景

api-regress 是一个给 QA 做 API 回归用的 Python CLI 工具，开发也可以用。它把同一组 GET/HEAD 请求分别发给**基线（baseline）**和**候选（candidate）**两个目标，比较 JSON 响应的结构和字段，按 breaking / suspicious / info 分级，生成报告（HTML / Markdown / JUnit / `result.json`），并给出 CI 能用的退出码。

主要场景是**发版后接口被意外改坏**，比如删了字段、改了类型、金额精度变了、状态码变了。

**非目标**：不做 Postman / Apifox 那样的接口管理平台。登录流程、请求串联、前后置脚本、Mock、断言编辑器都不做。
内置的 `demo` 服务只提供固定的演示数据（不联网试用、自测），不要把它扩展成可配置的 Mock。

## 文档体系

| 位置 | 内容 |
|---|---|
| `TODO.md` | 已知缺陷和后续计划（HAR 导入等） |
| `doc/spec/vN-<slug>/spec.md` | 行为规格，**行为以它为准**；内含验收项（AC）和决策记录。每个需求一个目录，v2 起只写相对上一版的增量 |
| `doc/spec/vN-<slug>/design.md` | 实现约束，以及实测过的技术结论；和 spec 冲突时以 spec 为准 |
| `doc/spec/vN-<slug>/tasks.md` | 任务拆分、依赖关系、**当前进度** |
| `doc/user/` | 面向使用者：命令、配置格式（`suite.md`）、对比规则、表达式、报告怎么看 |
| `doc/dev/` | 面向维护者：模块职责、关键路径、主流程图、数据契约 |

**规则**
- 走 Spec Driven 流程（细则见 `.claude/skills/spec-driven/SKILL.md`）：
  1. 新需求或行为变更，先在 `doc/spec/` 下新建 spec 目录，写好 `spec.md`、`design.md`、`tasks.md`。
  2. 用户明确同意 spec 之前，不改代码（`doc/spec/` 以外的文件都不动）。
  3. 只有在实现阶段，才改代码。
- Task 拆分：简单需求 1–2 个，复杂需求 4–6 个，超过 6 个先和用户确认。
- D / AC / T / S / L / K 编号在每个 spec 内独立，从 1 开始，不接着上一个 spec 往下排（避免多人并行写 spec 时撞号）；引用其他 spec 的编号时带目录名，如"v2-rules-tree 的 D27"。v1、v2 保持原有编号。
- 改行为之前，先改 spec。
- spec 里已经确认的决策不要推翻；确实需要变更时，先和用户确认。
- 文档里**不要引用代码行号**，因为行号容易和代码对不上。改为写文件名或函数名。
- **spec 只用于项目管理，随时可能删除**。`doc/user/`、`doc/dev/`、`README.md`、代码、注释、测试、生成脚本都**不能引用 `doc/spec/`**：不链接 spec 文件，不写章节号（§）、验收项编号（AC-xx）、决策编号（Dxx）、任务号（Tx）、场景和限制编号（Sx、Lx）。需要的内容直接写进 `doc/user/` 或 `doc/dev/`，测试名和注释用描述性文字。spec 内部可以互相引用，也可以引用其他文档。

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

对比行为变化后重新生成组合表：`uv run python scripts/gen_diff_combinations.py`（输出 `doc/dev/diff-combinations.md`）。
表达式行为变化后同理：`uv run python scripts/gen_expr_combinations.py`（输出 `doc/dev/expr-combinations.md`）。
报告格式变化后重新生成快照：`UPDATE_GOLDEN=1 uv run pytest tests/test_report.py`（输出 `tests/golden/`），逐行检查 diff。
`result.json` 的数据模型变化后重新导出 JSON Schema：`uv run python scripts/export_result_schema.py`（输出 `schema/result.schema.json`，`tests/test_result_schema.py` 拦住没更新的情况）。
测试里发往本地 mock 服务的请求不能走代理，`tests/conftest.py` 会清掉代理环境变量；mock 两侧服务的工具在 `tests/mock_api.py`。

## 架构原则

- **分层**：
  - `core` 是纯库，输入输出都是 pydantic v2 模型，提供 async API 和同步包装。
  - `cli` 和 `report` 只调用 `core`。
  - 以后加界面时也直接调用 `core.service`。
  - `result.json` 是对外的数据契约，必须保持稳定，有 JSON Schema 约束。
  - `demo` 是独立的演示服务，不依赖 `core` / `cli` / `report`，`core` 也不依赖它。改了 `demo/data.py` 后同步 `doc/user/demo.md` 的接口表和 `tests/test_demo.py` 的预期 finding。
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
| suite 配置字段、规则语义、路径语法 | **先改 spec**，再改 `doc/user/suite.md` 和相关的规则说明 |
| finding 类型、级别、请求状态、warning code | spec 中的对比语义、`doc/user/` 中的报告解读、`result.json` 的 JSON Schema、`doc/dev/` |
| 对比语义（`core/diff/`、`core/jsonload.py` 的行为） | 先改 spec；在 `scripts/gen_diff_combinations.py` 里补上新的维度或场景，重新生成 `doc/dev/diff-combinations.md`，逐格检查 diff（`tests/test_diff_combinations.py` 会拦住没更新的情况） |
| 表达式语义（`core/expr.py`、`core/expr_values.py` 的行为） | 先改 spec；同步 `doc/user/expressions.md`；在 `scripts/gen_expr_combinations.py` 里补上新的维度，重新生成 `doc/dev/expr-combinations.md`，逐格检查 diff（`tests/test_expr_combinations.py` 拦住没更新的情况，并检查不变量） |
| 报告内容或格式（`report/`、`core/reportdata.py`） | spec 中的输出一节；`doc/user/report.md`；`doc/dev/` 的「报告」；重新生成 `tests/golden/` |
| `result.json` 的数据模型 | 运行 `scripts/export_result_schema.py` 重新导出 JSON Schema，更新 `doc/dev/` 中的数据契约和 `doc/user/run.md` 的字段说明 |
| 新增、删除、重命名模块，模块职责变化，主流程变化 | `doc/dev/` 中的模块职责、关键路径、主流程图 |
| 依赖、检查命令、开发命令 | 本文件中的"检查命令"一节、`README.md` |
| 某项实现约束已经落地 | 把它从 spec 目录的 `design.md` 迁到 `doc/dev/` |
| 任务开始或完成 | spec 目录的 `tasks.md` 中的状态列 |
| 项目规则或约定变化 | 本文件 |

**检测方法**（收尾前执行）
1. 列出这次改动涉及的标识符，包括命令名、参数名、配置字段、finding 类型、文件名、函数名，然后在 `doc/`、`README.md`、`CLAUDE.md` 里逐个 grep，确认描述仍然正确。
2. `doc/dev/` 里引用的文件路径和函数名，都要在代码里实际存在。改名或删除时，全局搜索一遍旧名字。
   spec 以外不能出现对 spec 的引用：`grep -rnE 'doc/spec|spec-v|§|AC-[0-9]|\bD[0-9]+\b|\bT[0-9]\b' README.md doc/user doc/dev src scripts tests` 应当没有结果（`D107` 这类 lint 代码、mermaid 节点名除外）。
3. 文档里的示例（suite YAML、命令、报告片段）要和当前实现一致；能直接执行的示例要实际跑一遍。
4. 发现和本次改动无关的文档偏差时，在回复里告诉用户，不要悄悄忽略。

## Git 提交规则

- **提交时机**：只在用户要求时才提交。
- **分支**：一个 spec 一个集成分支，从 `main` 拉出，和 spec 目录同名：`feat/vN-<slug>`，例如 `feat/v3-har-import`。这个 spec 的所有 Task 都在这个分支上提交，**不要每个 Task 建一个分支**，除非用户主动要求。（v1、v2 早期的 `feat/t1-config` 这类按任务的分支是旧做法。）
- **提交信息**：用 Conventional Commits 格式，`<type>(<scope>): <中文简述>`。
  - type：`feat`、`fix`、`refactor`、`test`、`docs`、`chore`。
  - scope：用模块名或「版本-任务号」，例如 `feat(config): 支持 ${VAR:-default}`、`feat(v3-t1): 解析 HAR 文件`、`docs(spec): 调整数组长度变化的级别`。
  - 正文说明为什么改。涉及验收项时写上 AC 编号。
- **按功能提交，一次提交只做一件事**：一个功能（或一个修复）的 spec、代码、测试、文档放在同一个提交里，不要按"文档 / 代码 / 测试"拆开。
  - 例如实现 T1 时，T1 涉及的 spec 补充、代码、测试、`doc/` 更新整体作为一个提交。
  - 和这个功能无关的改动（例如顺手修正项目规则、无关的文档偏差）单独提交。
- **提交前先跑检查**：radon、lint-imports、pytest 都要通过，不能提交跑不通的代码。
- **不要提交**：运行输出（`api-regress-out/`）、`demo --write-suites` 写到根目录 `demo/` 的示例 suite、快照数据、虚拟环境、含 token 的文件。
- 提交信息末尾加上 harness 要求的 Co-Authored-By 行。
