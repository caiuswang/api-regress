"""表达式行为的快照与不变量：doc/dev/expr-combinations.md 必须和引擎当前的实际结果一致。

快照失败说明表达式行为变了。确认变化符合 spec §7.7 后运行
    uv run python scripts/gen_expr_combinations.py
并在 review 时逐格检查 git diff。
不变量失败说明引入了 bug，不要靠重新生成来"修"。"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "gen_expr_combinations.py"


def _load():
    spec = importlib.util.spec_from_file_location("gen_expr_combinations", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 需要能按模块名找到自己
    spec.loader.exec_module(module)
    return module


gen = _load()
TABLES = gen.collect()
CODES = gen.CODES
PAIRS = [(a, b) for a in CODES for b in CODES]


def _ok(table: str, a: str, b: str):
    cell = TABLES[table][(a, b)]
    return cell.value if cell.kind == "ok" else None


def test_combination_doc_is_up_to_date():
    actual = gen.OUTPUT.read_text(encoding="utf-8")
    assert actual == gen.render(), "表达式行为变了：确认符合 spec 后运行 uv run python scripts/gen_expr_combinations.py"


def test_no_raw_python_exceptions():
    raw = [(name, key, c.text) for name, t in TABLES.items() for key, c in t.items() if c.kind == "raw"]
    assert raw == []


# 错误信息只用 JSON 类型名描述，不出现 Python 内部类型名或异常名
_PY_NAMES = re.compile(r"\b(int|float|Decimal|list|tuple|dict|NoneType|bool|str|JsonView|HeadersView|SideView)\b|Error\b")


def test_messages_use_json_type_names():
    leaked = [
        (name, key, c.text)
        for name, t in TABLES.items()
        for key, c in t.items()
        if c.kind != "ok" and (_PY_NAMES.search(c.text) or "其他值" in c.text)
    ]
    assert leaked == []


def test_eq_is_symmetric_and_ne_is_its_complement():
    for a, b in PAIRS:
        eq, ne = TABLES["binop:=="][(a, b)], TABLES["binop:!="][(a, b)]
        assert (eq.kind, eq.value) == (TABLES["binop:=="][(b, a)].kind, TABLES["binop:=="][(b, a)].value), (a, b)
        assert eq.kind == ne.kind, (a, b)
        if eq.kind == "ok":
            assert eq.value is not ne.value, (a, b)


def test_in_and_not_in_are_complements():
    for a, b in PAIRS:
        inside, outside = TABLES["binop:in"][(a, b)], TABLES["binop:not in"][(a, b)]
        assert inside.kind == outside.kind, (a, b)
        if inside.kind == "ok":
            assert inside.value is not outside.value, (a, b)


@pytest.mark.parametrize(("op", "mirror"), [("<", ">"), ("<=", ">=")])
def test_ordering_is_antisymmetric(op, mirror):
    for a, b in PAIRS:
        left, right = TABLES[f"binop:{op}"][(a, b)], TABLES[f"binop:{mirror}"][(b, a)]
        assert left.kind == right.kind, (op, a, b)
        assert left.value == right.value, (op, a, b)


# 字面量和同值的字段在所有二元运算里结果一致（NaN 不参与：它不等于自己）
@pytest.mark.parametrize(("literal", "field"), [("3", "i"), ("1.50", "d"), ("[1,2]", "a")])
def test_literal_matches_field(literal, field):
    for op in gen.BINOPS:
        for other in CODES:
            if other in ("nan", "gen"):
                continue
            for lit_key, field_key in [((literal, other), (field, other)), ((other, literal), (other, field))]:
                lit, fld = TABLES[f"binop:{op}"][lit_key], TABLES[f"binop:{op}"][field_key]
                assert (lit.kind, lit.value) == (fld.kind, fld.value), (op, lit_key)


# 整数 3 和 Decimal 3.0 在比较类运算里结果一致
@pytest.mark.parametrize("op", ["==", "!=", "<", "<=", ">", ">=", "in", "not in"])
def test_int_and_integral_decimal_compare_the_same(op):
    for other in CODES:
        if other in ("i", "di"):
            continue
        for i_key, d_key in [(("i", other), ("di", other)), ((other, "i"), (other, "di"))]:
            assert _ok(f"binop:{op}", *i_key) == _ok(f"binop:{op}", *d_key), (op, i_key)


def test_variables_are_scoped_to_their_context():
    for source in gen.CONTEXT:
        uses_side = re.search(r"\b(base|cand)\b", source) is not None
        wrong = "success" if uses_side else "assert"
        assert TABLES["context"][(source, wrong)].kind == "rej", source
