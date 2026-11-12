# api-regress v2 技术设计约束

> 配套 [spec.md](spec.md)。v1 的约束（[v1 design](../v1-mvp/design.md)、`doc/dev/`）全部继续有效，本文只写 v2 新增的部分。行为以 spec 为准，两者冲突时按 spec 执行。
> 实现完成后，相应内容迁移到 `doc/dev/`。

## 1. 模块划分

| 模块 | 职责 |
|---|---|
| `core/rulesfile.py`（新，已实现） | 把树展开成「路径 + 属性」的原始规则并做结构检查。只依赖 `paths`、`models`；YAML 加载和 `Rule` 校验在 `config` 里做，避免 config ↔ rulesfile 循环引用 |
| `core/config.py`（已实现） | `Request` 新增 `rules_file`；`load_suite` 在 pydantic 校验通过后按 suite 文件所在目录读规则文件，展开结果存在 `Request` 的私有属性 `file_rules` 上，不改 `Request` 的字段 |
| `core/rules.py`（已实现） | `compile_rules` 从 `request.file_rules` 取规则文件的规则，`_SCOPE_RANK` 为 `defaults 0 < rules_file 1 < request 2`；规则文件里的规则 `RuleRef` 带文件和行号 |
| `core/shape.py`（新） | 从若干 body 合并出字段树（节点：类型集合、小数位集合、长度集合、示例值、子节点、被截断的 key 数），用 `RuleSet.resolve` 标注生效规则；输出 pydantic 模型。另有两个纯函数把树渲染成文本行：带示例和规则的展示版、只有类型的骨架版 |
| `core/service.py` | 新增 `shape` / `shape_sync`：加载 suite（不需要任何 target）、读 `--from`、调 `shape`、`--write` 时写文件。`service.py` 已有 655 行，`shape` 的流程放进单独的 `core/shapeflow.py`，`service` 只做转发，避免超过 1000 行 |
| `cli.py` | 新增 `shape` 命令，只解析参数、打印 `service` 返回的文本行 |
| `report/`（已实现） | `rule_where` 统一规则位置的写法，支持 `rules_file` 来源；`reportdata` 用 `compile_rules` 收集规则 |
| `core/samples.py`（已实现） | 请求文件格式改为引用 body 文件（spec §6.3）：`to_record` / `from_record` 不再处理 body 本身，改为产出和接收 `body_file`、`body_size`、`body_sha256`；新增按约定算 body 目录、写 body 文件、读并校验 body 文件的函数，快照和 run 目录共用 |
| `core/snapshot.py`、`core/rundir.py`（已实现） | 写请求文件时同时写 body 文件；原样复制时连 `samples/<name>/` 一起复制；`SCHEMA_VERSION` 保持 1；run 目录的清理和残留判断加上 `samples/` |
| `core/accept.py`（无需改动） | 候选的 body 文件在 `service._judge_accept` 读 run 目录时由 `parse_side_document` 校验；被接受的 sample 按 `NewEntry` 重新写进快照，格式化幂等，body 文件逐字节相同 |
| `core/jsonfmt.py`（新，已实现） | `pretty_json` / `compact_json` 从 `report/common.py` 挪到 core，报告和落盘共用 |

import-linter 契约不变：`rulesfile`、`shape`、`shapeflow` 都在 `core` 里，不能 import `cli` / `report`。

## 2. 关键实现约束

> 规则文件（T8）的展开、重复路径、行号已经实现，约束迁到 `doc/dev/architecture.md` 的「配置加载的关键点」。
> body 文件（T10）的格式化、扩展名、读取校验、原样复制、accept 的校验已经实现，迁到 `doc/dev/architecture.md` 的「落盘格式」「快照」。

- **shape 不需要环境变量**：加载 suite 时 `sides=()`，但 validate 的 `UnverifiedSuite` 拿不到 `Suite`。做法：缺失的变量只要都在 `targets` 下，就用占位值替换后再校验（占位值不进任何输出）；`defaults` / `requests` 里有缺失变量时按 suite 错误。
- **合并样本**：和 `diff/schema.py` 的 schema 表思路一致，但 shape 需要保留嵌套和顺序，单独实现，不要复用 `build_table`（它只产出扁平的泛化路径，而且会受 keyed 等数组规则影响）。所有数组一律按元素合并，和数组规则无关；数组规则只用来标注。
- **复杂度**：渲染一行的各部分（类型、小数位、长度、示例、规则）用有序的格式化函数表拼接，避免长 if 链；CC ≤ 10。
- **示例值**：只在展示版渲染里出现，骨架渲染函数拿不到示例值字段（两个渲染函数各自接收需要的字段），从结构上保证骨架和日志不会带出示例值。
- **截断**：`--max-keys` 只在渲染时截断，树本身保留全部 key（未命中规则要按全部 key 算）。大对象（例如 28 万条路径）下树的构建要线性：合并时用 dict 按 key 归并，不要对每个 key 线性查找。
- **`--write` 的原子性**：先检查所有目标文件都不存在，再逐个写；写到一半失败时删掉本次已写出的文件，退出码 3。

## 3. 数据契约变化

- `models.RuleRef`：`scope` 改为 `Literal["defaults", "rules_file", "request"]`，新增 `file: str | None = None`、`line: int | None = None`。`RuleRef` 是 frozen 的，被 `reportdata` 当作 dict key，新增字段后仍可 hash。
- `models.WarningItem`：新增 `file: str | None = None`。
- 序列化时 `None` 的可选字段是否省略，要和 v1 现有字段的做法一致，保证不使用 `rules_file` 的 suite 输出逐字节不变（AC-47，用 `tests/golden/` 验证）。
- 改完运行 `scripts/export_result_schema.py`。
- 快照的请求文件和 run 目录的两侧文件不在 `result.json` 的 JSON Schema 里，格式变化不用重新导出；但 `doc/user/snapshot.md`、`doc/user/run.md`、`doc/dev/architecture.md` 里的格式描述要一起改。

## 4. 依赖

不新增第三方依赖。
