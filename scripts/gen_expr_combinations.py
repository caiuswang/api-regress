"""生成 doc/dev/expr-combinations.md：把表达式引擎在各种组合下的实际结果列成矩阵。

每个格子都是真正跑一遍 compile_expr 和求值的结果，不是手写的预期。
tests/test_expr_combinations.py 会重新生成并和仓库里的文件比较，行为一变测试就失败；
同一个测试文件还会在这些结果上检查一组不变量（== 对称、in 与 not in 互补、不漏出 Python 原生异常等）。

加新的语法、函数、运算符或类型时：
1. 在下面的 OPERANDS / BINOPS / FUNCS1 / ACCESS / TRUTHY / CONTEXT 里加上对应的维度；
2. 运行 `uv run python scripts/gen_expr_combinations.py`；
3. 用 `git diff doc/dev/expr-combinations.md` 逐格检查变化是否符合 spec §7.7。
"""

from __future__ import annotations

import decimal
import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

from api_regress.core import expr
from api_regress.core.expr import ExprError, ExprEvalError, SideView, compile_expr, eval_success, unwrap
from api_regress.core.jsonload import parse_json

OUTPUT = Path(__file__).resolve().parent.parent / "doc" / "dev" / "expr-combinations.md"

BODY_TEXT = (
    '{"i": 3, "z": 0, "di": 3.0, "d": 1.50, "t": true, "f": false, "s": "ab", "es": "", "ns": "3", "n": null,'
    ' "o": {"k": 1}, "eo": {}, "a": [1, 2], "ea": [], "nan": NaN, "inf": Infinity}'
)
HEADERS = {"Content-Type": "application/json", "X-K": "v"}
STATUS = 200

# ---------- 维度定义：加新分支时改这里 ----------

# (列头代号, 表达式, 说明)
OPERANDS = [
    ("i", "body.i", "整数 3"),
    ("z", "body.z", "整数 0"),
    ("3", "3", "整数字面量"),
    ("d", "body.d", "Decimal 1.50"),
    ("di", "body.di", "Decimal 整值 3.0"),
    ("1.50", "1.50", "小数字面量（转成 Decimal）"),
    ("nan", "body.nan", "NaN"),
    ("inf", "body.inf", "Infinity"),
    ("status", "status", "状态码 200"),
    ("t", "body.t", "true"),
    ("f", "body.f", "false"),
    ("s", "body.s", '字符串 "ab"'),
    ("es", "body.es", "空字符串"),
    ("ns", "body.ns", '数字样子的字符串 "3"'),
    ("n", "body.n", "null"),
    ("o", "body.o", '对象 {"k": 1}'),
    ("eo", "body.eo", "空对象"),
    ("headers", "headers", "响应头"),
    ("a", "body.a", "数组 [1, 2]"),
    ("ea", "body.ea", "空数组"),
    ("[1,2]", "[1, 2]", "列表字面量"),
    ("(1,2)", "(1, 2)", "元组字面量"),
    ("set", "set(body.a)", "集合"),
    ("keys", "keys(body.o)", "keys() 的结果"),
    ("gen", "(x for x in body.a)", "生成器"),
]

BINOPS = ["+", "-", "*", "/", "//", "%", "==", "!=", "<", "<=", ">", ">=", "in", "not in"]

FUNCS1 = ["len", "scale", "is_number", "is_string", "keys", "set", "all", "any", "abs", "min", "max"]
FUNCS2 = ["min", "max"]

ACCESS = [
    ("X.k", "{x}.k"),
    ('X["k"]', '{x}["k"]'),
    ("X[0]", "{x}[0]"),
    ("X[-1]", "{x}[-1]"),
    ("X[5]", "{x}[5]"),
    ("X[True]", "{x}[True]"),
    ("X[1.0]", "{x}[1.0]"),
    ('X["0"]', '{x}["0"]'),
    ("X.items", "{x}.items"),
    ("遍历", "len([v for v in {x}]) >= 0"),
    ('"k" in X', '"k" in {x}'),
    ("1 in X", "1 in {x}"),
    ('get(X,"k")', 'get({x}, "k")'),
    ('get(X,"[0]")', 'get({x}, "[0]")'),
    ('exists(X,"k")', 'exists({x}, "k")'),
]

PUBLIC = "eval_success"
TRUTHY = [
    ("not X", "not {x}"),
    ("X and True", "{x} and True"),
    ("X or True", "{x} or True"),
    ("X if .. else", "(True if {x} else False)"),
    ("推导式 if X", "[1 for v in [0] if {x}] == [1]"),
    ("顶层结果 X", "{x}"),
    (PUBLIC, "{x}"),
]

CONTEXT = [
    "status == 200",
    "len(headers) > 0",
    "body.i == 3",
    "base.status == 200",
    "cand.status == 200",
    "len(cand.headers) > 0",
    "cand.body.i == 3",
    "cand.body == base.body",
    "len(cand) > 0",
    "cand == base",
    "cand.status.real == 1",
    "len(cand.body) > 0",
    "exists(cand, 'body.i')",
    "get(base, 'status') == 200",
    "[c for c in [cand]] == [cand]",
    "set([cand]) == set()",
]
KINDS = ["success", "assert"]

# ---------- 运行 ----------

_ATOMIC = re.compile(r"[A-Za-z_][\w.]*(\(.*\))?|\(.*\)|\[.*\]")
_WHERE = re.compile(r"（第 (\d+ 行第 )?\d+ 列）$")


@dataclass(frozen=True)
class Cell:
    """kind：ok 求值成功 / err 求值出错（友好提示）/ rej 编译阶段拒绝 / raw 漏出了 Python 原生异常。"""

    kind: str
    text: str
    value: Any = None


def _names(kind: str) -> dict[str, Any]:
    body = parse_json(BODY_TEXT)[0]
    if kind == "assert":
        side = SideView(STATUS, HEADERS, body)
        return {"base": side, "cand": SideView(STATUS, HEADERS, body)}
    return {"status": STATUS, "headers": expr.HeadersView(HEADERS), "body": expr.wrap(body)}


def _p(operand: str) -> str:
    return operand if _ATOMIC.fullmatch(operand) else f"({operand})"


def _compile(source: str, kind: str) -> Any:
    try:
        return compile_expr(source, kind)  # type: ignore[arg-type]
    except ExprError as exc:
        return Cell("rej", _WHERE.sub("", str(exc)))
    except Exception as exc:  # noqa: BLE001 —— 编译期漏出的任何异常都要在表里显出来
        return Cell("raw", f"{type(exc).__name__}: {exc}")


def evaluate(source: str, kind: str = "success") -> Cell:
    """求出表达式的原始值（不要求是布尔值），以便在表里看到运算结果。"""
    compiled = _compile(source, kind)
    if isinstance(compiled, Cell):
        return compiled
    try:
        with decimal.localcontext(prec=expr.DECIMAL_PRECISION):
            # 用内部求值器拿到非布尔的结果；公开接口的行为见 TRUTHY 里的 eval_success 一列
            value = expr._Evaluator(_names(kind), source).run(compiled.tree)
    except ExprEvalError as exc:
        return Cell("err", str(exc))
    except Exception as exc:  # noqa: BLE001
        return Cell("raw", expr._describe(exc))
    return Cell("ok", show(value), unwrap(value))


def evaluate_public(source: str) -> Cell:
    compiled = _compile(source, "success")
    if isinstance(compiled, Cell):
        return compiled
    names = _names("success")
    outcome = eval_success(compiled, STATUS, HEADERS, unwrap(names["body"]))
    if outcome.error is not None:
        return Cell("err", outcome.error)
    return Cell("ok", show(outcome.passed), outcome.passed)


def show(value: Any, limit: int = 24) -> str:
    text = _show(unwrap(value))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _show_dict(v: dict) -> str:
    return "{" + ", ".join(f"{json.dumps(k, ensure_ascii=False)}: {_show(unwrap(x))}" for k, x in v.items()) + "}"


# 有序：bool 必须在 int 之前
_SHOWERS: tuple[tuple[type | tuple[type, ...], Any], ...] = (
    (bool, lambda v: "T" if v else "F"),
    (type(None), lambda v: "null"),
    ((int, Decimal), str),
    (str, lambda v: json.dumps(v, ensure_ascii=False)),
    ((set, frozenset), lambda v: "{" + ", ".join(sorted(_show(x) for x in v)) + "}"),
    ((list, tuple), lambda v: "[" + ", ".join(_show(unwrap(x)) for x in v) + "]"),
    (dict, _show_dict),
)


def _show(v: Any) -> str:
    fn = next((fn for kinds, fn in _SHOWERS if isinstance(v, kinds)), expr.type_name)
    return fn(v)


Table = dict[tuple[str, str], Cell]


def _pairs(template: str) -> Table:
    return {(a, b): evaluate(template.format(a=_p(ea), b=_p(eb))) for a, ea, _ in OPERANDS for b, eb, _ in OPERANDS}


def _per_operand(columns: list[tuple[str, str]]) -> Table:
    table: Table = {}
    for a, ea, _ in OPERANDS:
        for name, template in columns:
            run = evaluate_public if name == PUBLIC else evaluate
            table[(a, name)] = run(template.format(x=_p(ea)))
    return table


@cache
def collect() -> dict[str, Table]:
    """表名 → {(行, 列): 结果}。测试里的不变量也基于它。"""
    tables: dict[str, Table] = {f"binop:{op}": _pairs(f"{{a}} {op} {{b}}") for op in BINOPS}
    tables["func1"] = _per_operand([(fn, f"{fn}({{x}})") for fn in FUNCS1])
    tables |= {f"func2:{fn}": _pairs(f"{fn}({{a}}, {{b}})") for fn in FUNCS2}
    tables["access"] = _per_operand(ACCESS)
    tables["truthy"] = _per_operand(TRUTHY)
    tables["context"] = {(src, kind): evaluate(src, kind) for src in CONTEXT for kind in KINDS}
    return tables


# ---------- 渲染 ----------

_PREFIX = {"err": "E", "rej": "R", "raw": "X"}


class _Legend:
    """一张表内的错误信息编号：E 求值出错，R 编译拒绝，X 原生异常（应该为 0）。"""

    def __init__(self) -> None:
        self.ids: dict[tuple[str, str], str] = {}

    def ref(self, cell: Cell, subs: list[tuple[str, str]]) -> str:
        """subs：把信息里本格操作数的类型名换成占位符，同一模板的信息只编一个号。"""
        if cell.kind == "ok":
            return cell.text
        text = cell.text
        for old, new in subs:
            text = text.replace(old, new)
        key = (cell.kind, text)
        if key not in self.ids:
            count = sum(1 for k in self.ids if k[0] == cell.kind)
            self.ids[key] = f"{_PREFIX[cell.kind]}{count + 1}"
        return f"`{self.ids[key]}`"

    def render(self) -> str:
        order = sorted(self.ids.items(), key=lambda kv: ("ERX".index(kv[1][0]), int(kv[1][1:])))
        return "\n".join(f"- `{code}`：{text}" for (_, text), code in order)


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_escape(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def _pair_subs(r: str, c: str) -> list[tuple[str, str]]:
    return [(f"{operand_type(r)}和{operand_type(c)}", "‹左›和‹右›")]


def _row_subs(r: str, c: str) -> list[tuple[str, str]]:
    return [(f"这里是{operand_type(r)}", "这里是‹X›")]


def _grid(table: Table, rows: list[str], cols: list[str], corner: str, subs: Any = None, row_label: Any = None) -> str:
    legend = _Legend()
    label = row_label or (lambda r: f"**{r}**")
    body = [[label(r), *(legend.ref(table[(r, c)], subs(r, c) if subs else []) for c in cols)] for r in rows]
    header = [corner, *(f"`{c}`" for c in cols)]
    notes = legend.render()
    return _table(header, body) + ("\n\n" + notes if notes else "") + "\n"


CODES = [code for code, _, _ in OPERANDS]


@cache
def operand_type(code: str) -> str:
    source = dict((c, e) for c, e, _ in OPERANDS)[code]
    return expr.type_name(evaluate(f"[{source}][0]").value) if code != "gen" else "生成器"


def _sections(tables: dict[str, Table]) -> list[str]:
    out = ["## 二元运算\n", "表达式是 `行 运算符 列`，例如 `body.i + body.s`。\n"]
    for op in BINOPS:
        out.append(f"### `{op}`\n\n" + _grid(tables[f"binop:{op}"], CODES, CODES, "左 ↓ ／ 右 →", _pair_subs))
    out.append("## 单参数函数\n\n表达式是 `函数(行)`。\n\n" + _grid(tables["func1"], CODES, FUNCS1, "参数 ↓ ／ 函数 →", _row_subs))
    out.append("## 两个参数的 min / max\n\n表达式是 `min(行, 列)`。\n")
    for fn in FUNCS2:
        out.append(f"### `{fn}`\n\n" + _grid(tables[f"func2:{fn}"], CODES, CODES, "第一个 ↓ ／ 第二个 →", _pair_subs))
    access_cols = [name for name, _ in ACCESS]
    out.append("## 访问方式\n\n`X` 换成行里的操作数。\n\n" + _grid(tables["access"], CODES, access_cols, "X ↓"))
    truthy_cols = [name for name, _ in TRUTHY]
    out.append(
        "## 真值上下文\n\n`X` 换成行里的操作数。`顶层结果 X` 是求值得到的原始值；"
        f"`{PUBLIC}` 是公开接口的结果（不是布尔值就算出错）。\n\n" + _grid(tables["truthy"], CODES, truthy_cols, "X ↓", _row_subs)
    )
    out.append(
        "## 变量与上下文\n\n同一条表达式分别作为 `success` 和 assert 编译、求值；assert 里 base 和 cand 是同一份响应。\n\n"
        + _grid(tables["context"], CONTEXT, KINDS, "表达式", row_label=lambda r: f"`{r}`")
    )
    return out


def _operand_table() -> str:
    return _table(["代号", "表达式", "说明"], [[f"`{c}`", f"`{e}`", d] for c, e, d in OPERANDS])


HEADER = """# 表达式组合表

> **本文件由 `scripts/gen_expr_combinations.py` 生成，不要手改。**
> 每个格子都是真正运行 `compile_expr` 和求值的结果。`tests/test_expr_combinations.py` 会重新生成并比较：
> 表达式行为一变，测试就失败。确认变化符合 spec §7.7 后，运行 `uv run python scripts/gen_expr_combinations.py`
> 更新本文件，并在 review 时逐格检查 `git diff`。加新的语法、函数或运算符时，先在脚本里加上对应的维度。
> 同一个测试还会检查一组不变量：`==` 对称、`==` 与 `!=` 互补、`in` 与 `not in` 互补、大小比较反对称、
> 字面量和同值字段结果一致、错误信息里没有 Python 原生异常和类型名。

## 怎么读

| 格子 | 含义 |
|---|---|
| `T` / `F` | 结果是 True / False |
| 其他文本 | 求值成功，结果的值（超过 24 个字符截断）；字符串带引号，集合写成 `{...}` |
| `E1`、`E2`… | 求值出错，具体信息见表下方。信息里本格操作数的类型名换成了占位符：`‹左›`、`‹右›` 是左右两边的类型，`‹X›` 是行里操作数的类型（访问方式一表不替换，因为那里的类型指下标）；所以同一个编号在不同格子里代表同一个模板 |
| `R1`、`R2`… | 编译（validate）阶段就拒绝，具体信息见表下方；位置信息（第几列）已去掉 |
| `X1`、`X2`… | 漏出了 Python 原生异常——**不应出现**，出现即是 bug |

编号只在一张表内有效。

## 组合维度

```mermaid
flowchart LR
  OP["25 种操作数：数字 / 布尔 / 字符串 / null / 对象 / 数组 / 字面量 / set / keys / 生成器"] --> B["× 14 个二元运算符"]
  OP --> F1["× 11 个单参数函数"]
  OP --> F2["× 操作数 → min / max 两个参数"]
  OP --> AC["× 15 种访问方式"]
  OP --> TR["× 7 种真值上下文"]
  VAR["16 条变量用法"] --> CTX["× success / assert"]
```

## 判定流程

```mermaid
flowchart TD
  S["表达式原文"] --> C{"compile_expr：语法白名单、变量、函数、参数个数、生成器位置、顶层结果"}
  C -->|不通过| R["R：validate 报错"]
  C -->|通过| E{"求值：JSON 语义的运算、访问、内置函数，计算量预算"}
  E -->|ExprEvalError| ER["E：求值出错（友好提示）"]
  E -->|其他异常| X["X：原生异常，属于 bug"]
  E -->|得到值| V{"是布尔值?"}
  V -->|是| OK["T / F"]
  V -->|否| NB["公开接口：E「结果必须是 True 或 False」"]
```

## 操作数

"""


def render() -> str:
    body = parse_json(BODY_TEXT)[0]
    context = (
        f"响应体 `{BODY_TEXT}`；响应头 `{', '.join(HEADERS)}`；status 为 {STATUS}。"
        f"（解析后 `nan`、`inf`、`d`、`di` 是 Decimal：{', '.join(f'{k}={body[k]}' for k in ('d', 'di', 'nan', 'inf'))}）\n"
    )
    parts = [HEADER + _operand_table() + "\n\n" + context]
    parts += _sections(collect())
    return "\n".join(parts)


def main() -> int:
    OUTPUT.write_text(render(), encoding="utf-8")
    print(f"已写入 {OUTPUT.relative_to(Path.cwd()) if OUTPUT.is_relative_to(Path.cwd()) else OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
