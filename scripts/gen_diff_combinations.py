"""生成 doc/dev/diff-combinations.md：把对比引擎在各种组合下的实际输出列成矩阵。

每个格子都是真正跑一遍 diff_json / load_body 的结果，不是手写的预期。
tests/test_diff_combinations.py 会重新生成并和仓库里的文件比较，行为一变测试就失败。

加新的条件分支（新的规则属性、数组模式、finding 类型等）时：
1. 在下面的 RULE_SETS / CONTEXTS / ARRAY_SCENARIOS / BODY_CASES 里加上对应的维度或场景；
2. 运行 `uv run python scripts/gen_diff_combinations.py`；
3. 用 `git diff doc/dev/diff-combinations.md` 逐格检查变化是否符合 spec。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from api_regress.core.config import Suite
from api_regress.core.diff import diff_json
from api_regress.core.jsonload import load_body, parse_json
from api_regress.core.rules import RuleSet

OUTPUT = Path(__file__).resolve().parent.parent / "doc" / "dev" / "diff-combinations.md"

TARGETS = {"baseline": {"base_url": "http://b"}, "candidate": {"base_url": "http://c"}}

FINDING_CODES = {
    "type_changed": "T",
    "nullability_changed": "N",
    "scale_changed": "Sc",
    "value_changed": "V",
    "field_removed": "FR",
    "field_added": "FA",
    "array_length_changed": "L",
    "element_missing": "EM",
    "element_added": "EA",
}
LEVEL_CODES = {"breaking": "B", "suspicious": "S", "info": "I"}
WARNING_CODES = {
    "array_empty": "⚠empty",
    "array_degraded_to_schema": "⚠degrade",
    "rule_ineffective_in_schema": "⚠ineffective",
}

# ---------- 维度定义：加新分支时改这里 ----------

TYPES = ["object", "array", "string", "number", "boolean", "null"]
# 类型不同时用的样本值
SAMPLE = {"object": '{"a": 1}', "array": "[1]", "string": '"x"', "number": "1.10", "boolean": "true", "null": "null"}
# 类型相同时用的一对样本：值不同；number 的小数位也不同
SAME_PAIR = {
    "object": ('{"a": 1}', '{"b": 1}'),
    "array": ("[1]", "[1, 2]"),
    "string": ('"x"', '"y"'),
    "number": ("1.10", "2.0"),
    "boolean": ("true", "false"),
    "null": ("null", "null"),
}

# 规则写在被比较的节点上（path 由 CONTEXTS 决定）
RULE_SETS: dict[str, dict[str, Any]] = {
    "无规则": {"rule": None},
    "nullable: true": {"rule": {"nullable": True}},
    "on_value_change: breaking": {"rule": {"on_value_change": "breaking"}},
    "decimal: {}": {"rule": {"decimal": {}}},
    "decimal: {allow_string: true}": {"rule": {"decimal": {"allow_string": True}}},
    "compare: values": {"rule": None, "compare": "values"},
}

# (标题, 包装模板, 规则路径, 节点路径, 额外规则)
CONTEXTS = [
    ("普通字段", '{{"v": {}}}', "v", "v", []),
    ("schema 数组的一列", '{{"l": [{{"v": {}}}]}}', "l[*].v", "l[*].v", []),
    ("unordered 数组的一列", '{{"l": [{{"v": {}}}]}}', "l[*].v", "l[*].v", [{"path": "l", "array": {"mode": "unordered"}}]),
]

ARRAY_MODES = {
    "schema": None,
    "keyed": {"keys": ["id"]},
    "positional": {"mode": "positional"},
    "unordered": {"mode": "unordered"},
}
ELEMENT_RULES: dict[str, list[dict[str, Any]]] = {
    "无元素规则": [],
    "l[*].p: decimal": [{"path": "l[*].p", "decimal": {}}],
    "l[*].p: on_value_change: breaking": [{"path": "l[*].p", "on_value_change": "breaking"}],
}
ARRAY_SCENARIOS = [
    ("完全相同", '[{"id":1,"p":1.10},{"id":2,"p":2.00}]', '[{"id":1,"p":1.10},{"id":2,"p":2.00}]'),
    ("顺序打乱", '[{"id":1,"p":1.10},{"id":2,"p":2.00}]', '[{"id":2,"p":2.00},{"id":1,"p":1.10}]'),
    ("多 1 个元素", '[{"id":1,"p":1.10}]', '[{"id":1,"p":1.10},{"id":2,"p":2.00}]'),
    ("少 1 个元素", '[{"id":1,"p":1.10},{"id":2,"p":2.00}]', '[{"id":1,"p":1.10}]'),
    ("空 → 非空", "[]", '[{"id":1,"p":1.10}]'),
    ("非空 → 空", '[{"id":1,"p":1.10}]', "[]"),
    ("两侧都空", "[]", "[]"),
    ("元素删字段", '[{"id":1,"p":1.10,"x":1}]', '[{"id":1,"p":1.10}]'),
    ("元素字段改类型", '[{"id":1,"p":1.10}]', '[{"id":1,"p":"1.10"}]'),
    ("元素值变（小数位相同）", '[{"id":1,"p":1.10}]', '[{"id":1,"p":2.20}]'),
    ("元素小数位变", '[{"id":1,"p":1.10}]', '[{"id":1,"p":1.1}]'),
    ("元素字段变 null", '[{"id":1,"p":1.10}]', '[{"id":1,"p":null}]'),
    ("元素对象字段变 null", '[{"id":1,"o":{"a":1}}]', '[{"id":1,"o":null}]'),
    ("key 重复", '[{"id":1,"p":1.10},{"id":1,"p":2.00}]', '[{"id":1,"p":1.10},{"id":1,"p":2.00}]'),
    ("key 缺失", '[{"id":1,"p":1.10}]', '[{"p":1.10}]'),
    ("元素不是对象", "[1,2]", "[2,1]"),
    ("内层数组变空", '[{"id":1,"sub":[{"a":1}]}]', '[{"id":1,"sub":[]}]'),
]

PRESENCE_CASES = [
    ("字段被删除", '{"v": 1}', "{}", []),
    ("字段新增", "{}", '{"v": 1}', []),
    ("值为 null 的字段被删除", '{"v": null}', "{}", []),
    ("字段被删除，但 ignore", '{"v": 1}', "{}", [{"path": "v", "ignore": True}]),
    ("父节点 ignore，子节点 ignore: false", '{"m": {"v": 1}}', '{"m": {}}', [{"path": "m", "ignore": True}, {"path": "m.v", "ignore": False}]),
    ("全局 **.v ignore，请求级 v ignore: false", '{"v": 1}', "{}", [{"path": "v", "ignore": False}]),
]

BODY_CASES = [
    ("application/json", '{"a": 1}', "application/json"),
    ("application/json，解析失败", "{bad", "application/json"),
    ("application/problem+json", '{"a": 1}', "application/problem+json"),
    ("model/gltf+json", '{"a": 1}', "model/gltf+json"),
    ("没有 Content-Type", '{"a": 1}', None),
    ("没有 Content-Type，解析失败", "{bad", None),
    ("text/html", '{"a": 1}', "text/html"),
    ("charset=gbk", '{"名": 1}'.encode("gbk"), "application/json; charset=gbk"),
    ("charset=hex（非文本编码）", b'{"a": 1}', "application/json; charset=hex"),
    ("BOM", b'\xef\xbb\xbf{"a": 1}', "application/json"),
    ("重复 key", '{"a": 1, "a": 2}', "application/json"),
    ("NaN", '{"a": NaN}', "application/json"),
    ("数字超出范围", '{"a": 1e9999999999999999999}', "application/json"),
    ("对象/数组嵌套 200 层", "[" * 200 + "1" + "]" * 200, "application/json"),
    ("对象/数组嵌套 201 层", "[" * 201 + "]" * 201, "application/json"),
    ("孤立代理字符", '{"a": "\\ud800"}', "application/json"),
]

# ---------- 运行 ----------


def _ruleset(rules: list[dict[str, Any]], defaults: list[dict[str, Any]], compare: str) -> RuleSet:
    suite = Suite.model_validate(
        {
            "version": 1,
            "targets": TARGETS,
            "defaults": {"compare": compare, "rules": defaults},
            "requests": [{"name": "r", "method": "GET", "path": "/", "rules": rules}],
        }
    )
    return RuleSet.for_request(suite, suite.requests[0], 0)


def _relative(path: str, node: str) -> str:
    if path == node:
        return ""
    if path.startswith(node):
        return "@" + path[len(node) :]
    return "@" + path


def _summary(result: Any, node: str) -> str:
    parts = [
        f"{FINDING_CODES[f.type]}·{LEVEL_CODES[f.level]}{_relative(f.path, node)}" for f in result.findings
    ]
    parts += [WARNING_CODES.get(w.code, "⚠" + w.code) for w in result.warnings]
    return " + ".join(parts) or "—"


def _diff(base: str, cand: str, rules: list, defaults: list | None = None, compare: str = "structure") -> Any:
    ruleset = _ruleset(rules, defaults or [], compare)
    return diff_json(parse_json(base)[0], parse_json(cand)[0], ruleset, compare)  # type: ignore[arg-type]


def _type_cell(context: tuple, rule_set: dict[str, Any], base_type: str, cand_type: str) -> str:
    _, wrap, rule_path, node, extra = context
    base, cand = SAME_PAIR[base_type] if base_type == cand_type else (SAMPLE[base_type], SAMPLE[cand_type])
    rules = list(extra)
    if rule_set["rule"] is not None:
        rules.append({"path": rule_path, **rule_set["rule"]})
    result = _diff(wrap.format(base), wrap.format(cand), rules, compare=rule_set.get("compare", "structure"))
    return _summary(result, node)


# ---------- 渲染 ----------


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def _type_matrices() -> list[str]:
    out = []
    for context in CONTEXTS:
        out.append(f"### {context[0]}\n")
        for name, rule_set in RULE_SETS.items():
            rows = [[f"**{b}**", *(_type_cell(context, rule_set, b, c) for c in TYPES)] for b in TYPES]
            out.append(f"**{name}**\n\n" + _table(["基线 ↓ ／ 候选 →", *TYPES], rows) + "\n")
    return out


def _array_matrices() -> list[str]:
    out = []
    for name, element_rules in ELEMENT_RULES.items():
        rows = []
        for title, base, cand in ARRAY_SCENARIOS:
            cells = []
            for mode in ARRAY_MODES.values():
                rules = ([{"path": "l", "array": mode}] if mode else []) + element_rules
                cells.append(_summary(_diff(f'{{"l": {base}}}', f'{{"l": {cand}}}', rules), "l"))
            rows.append([title, *cells])
        out.append(f"**{name}**\n\n" + _table(["场景 ↓ ／ 模式 →", *ARRAY_MODES], rows) + "\n")
    return out


def _presence_table() -> str:
    rows = []
    for title, base, cand, rules in PRESENCE_CASES:
        defaults = [{"path": "**.v", "ignore": True}] if "全局" in title else []
        rows.append([title, _summary(_diff(base, cand, rules, defaults), "")])
    return _table(["情况", "结果"], rows)


def _body_table() -> str:
    rows = []
    for title, body, content_type in BODY_CASES:
        loaded = load_body(body, content_type)
        warnings = ", ".join(w.code for w in loaded.warnings) or "—"
        rows.append([title, "JSON" if loaded.is_json else "非 JSON", warnings])
    return _table(["输入", "结果", "warning"], rows)


HEADER = """# 对比引擎组合表

> **本文件由 `scripts/gen_diff_combinations.py` 生成，不要手改。**
> 每个格子都是真正运行 `diff_json` / `load_body` 的结果。`tests/test_diff_combinations.py` 会重新生成并比较：
> 对比行为一变，测试就失败。确认变化符合 spec 后，运行 `uv run python scripts/gen_diff_combinations.py` 更新本文件，
> 并在 review 时逐格检查 `git diff`。加新的条件分支时，先在脚本里加上对应的维度或场景。

## 怎么读

格子里是 `类型·级别@相对路径`，多条用 `+` 连接；路径和被比较的节点相同时省略 `@`。

| 缩写 | finding | | 缩写 | 含义 |
|---|---|---|---|---|
| `T` | type_changed | | `·B` | breaking |
| `N` | nullability_changed | | `·S` | suspicious |
| `Sc` | scale_changed | | `·I` | info |
| `V` | value_changed | | `⚠empty` | array_empty |
| `FR` / `FA` | field_removed / field_added | | `⚠degrade` | array_degraded_to_schema |
| `L` | array_length_changed | | `⚠ineffective` | rule_ineffective_in_schema |
| `EM` / `EA` | element_missing / element_added | | `—` | 没有 finding 也没有 warning |

## 组合维度

```mermaid
flowchart LR
  BODY["body 层：Content-Type × 能否解析"] --> NODE["每个节点"]
  NODE --> D1["存在性：两侧都有 / 只有基线 / 只有候选"]
  NODE --> D2["类型对：6 × 6"]
  NODE --> D3["所在位置：普通字段 / schema 列 / unordered 列"]
  NODE --> D4["规则：ignore · nullable · decimal · allow_string · scale_level · on_value_change · compare"]
  NODE --> D5["数组模式 × 场景"]
```

## 判定流程

```mermaid
flowchart TD
  A["visit(路径, 基线, 候选)"] --> R{"ignore?"}
  R -->|是| X["整棵子树跳过"]
  R -->|否| P{"有一侧缺失?"}
  P -->|字段| PF["FR·B / FA·I"]
  P -->|数组元素| PE["EM·S / EA·I"]
  P -->|否| T{"JSON 类型相同?"}
  T -->|"否：标量 ↔ null"| N["N（变 null ·S，从 null 变值 ·I；nullable 时不报）<br/>配了值比较另报 V"]
  T -->|"否：其他"| TC["T·B，不再展开<br/>容器 ↔ null 且 nullable 时不报"]
  T -->|object| O["逐个 key 再 visit"]
  T -->|标量| S["decimal → Sc；值规则 → V"]
  T -->|array| M{"array.mode"}
  M --> MS["schema：L + compare_elements"]
  M --> MK["keyed：按 key 配对再 visit<br/>失败 → ⚠degrade → schema"]
  M --> MP["positional：L + 按下标 visit"]
  M --> MU["unordered：compare_elements + 多重集合 → V"]
  MS --> CE["compare_elements：两侧建表逐列比较<br/>类型集合、null、小数位、父节点决定子字段是否报"]
  MU --> CE
  O --> A
  MK --> A
  MP --> A
```

"""


def render() -> str:
    parts = [HEADER, "## 类型 × 位置 × 规则\n"]
    parts.append(
        "类型相同时用两份不同的值（number 的小数位也不同：`1.10` → `2.0`），"
        "类型不同时用 `" + "`、`".join(SAMPLE.values()) + "`。\n"
    )
    parts += _type_matrices()
    parts.append("## 数组模式 × 场景\n")
    parts.append("数组 `l` 的元素形如 `{\"id\": 1, \"p\": 1.10}`，keyed 模式用 `keys: [id]`。路径相对于 `l`。\n")
    parts += _array_matrices()
    parts.append("## 缺失与 ignore\n\n" + _presence_table() + "\n")
    parts.append("## body 层\n\n" + _body_table() + "\n")
    return "\n".join(parts)


def main() -> int:
    OUTPUT.write_text(render(), encoding="utf-8")
    print(f"已写入 {OUTPUT.relative_to(Path.cwd()) if OUTPUT.is_relative_to(Path.cwd()) else OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
