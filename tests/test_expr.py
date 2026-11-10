import ast
import threading
import time
import warnings
from decimal import Decimal

import pytest

from api_regress.core.config import ConfigError, UnverifiedSuite, load_suite_text
from api_regress.core.expr import (
    ExprError,
    JsonView,
    SideView,
    compile_expr,
    eval_assert,
    eval_success,
)

BODY = {
    "code": 0,
    "data": {
        "items": [{"sku_id": "A1", "price": Decimal("12.50")}, {"sku_id": "B2", "price": Decimal("3.0")}],
        "x": {"items": 3, "get": "g", "keys": [1]},
        "_private": 1,
        "a.b": "dotted",
        "coupon": None,
    },
}
HEADERS = {"Content-Type": "application/json; charset=utf-8", "X-Trace": "t1"}


def side(body=BODY, status=200, headers=HEADERS):
    return SideView(status, headers, body)


def run_assert(source, base=None, cand=None):
    return eval_assert(compile_expr(source, "assert"), base or side(), cand or side())


def run_success(source, body=BODY, status=200, headers=HEADERS):
    return eval_success(compile_expr(source, "success"), status, headers, body)


def rejected(source, kind="assert"):
    with pytest.raises(ExprError) as info:
        compile_expr(source, kind)
    return str(info.value)


# ---------- AC-25：JsonView、字面量 ----------


@pytest.mark.parametrize(
    "source",
    [
        "cand.body.data.x.items == 3",  # 取到字段值而不是 dict.items
        "cand.body.data.x.get == 'g'",
        "cand.body.data.x['keys'] == [1]",
        "cand.body.data['items'][0].price == 12.5",  # int 下标 + Decimal 比较
        "cand.body.data['items'][-1].sku_id == 'B2'",
        "cand.body.data['items'][0].price + 0.1 == 12.6",
        "cand.body.data['items'][0].price * 2 == 25",
        "0.1 + 0.2 == 0.3",  # 浮点字面量是 Decimal
        "scale(cand.body.data['items'][0].price) == 2",
        "scale(cand.body.data['items'][1].price) == 1",
        "scale(1.50) == 2 and scale(3) == 0",
        "cand.body.data['a.b'] == 'dotted'",
        "cand.body.data['_private'] == 1",
        "cand.body.data.coupon is None",
        "len(cand.body.data['items']) == 2",
        "keys(cand.body.data.x) == ['items', 'get', 'keys']",
        "set(i.sku_id for i in cand.body.data['items']) == set(['A1', 'B2'])",
        "all(i.price > 0 for i in cand.body.data['items'])",
        "any(i.sku_id == 'B2' for i in cand.body.data['items'])",
        "[i.sku_id for i in cand.body.data['items'] if i.price > 5] == ['A1']",
        "max(i.price for i in cand.body.data['items']) == 12.5",
        "min(1, 2) == 1 and abs(-1.5) == 1.5",
        "'x' in cand.body.data and 'nope' not in cand.body.data",
        "cand.body == base.body",
        "cand.status == 200 and base.status == 200",
        "(cand.body.code if cand.status == 200 else 1) == 0",
        "len(cand.body.data['items'] + base.body.data['items']) == 4",
        "len(set(keys(cand.body.data)) - set(keys(base.body.data))) == 0",
    ],
)
def test_assert_true(source):
    out = run_assert(source)
    assert out.passed, out


def test_headers_case_insensitive():
    assert run_assert("cand.headers['CONTENT-TYPE'] == cand.headers['content-type']").passed
    assert run_assert("'x-trace' in cand.headers and cand.headers['X-TRACE'] == 't1'").passed
    assert run_success("get(headers, 'x-trace') == 't1'").passed
    assert run_success("keys(headers) == ['content-type', 'x-trace']").passed  # keys() 是小写


def test_items_is_field_not_method():
    view = JsonView({"items": 1, "get": 2})
    assert view.items == 1 and view.get == 2
    with pytest.raises(AttributeError):
        view._data2  # noqa: B018
    with pytest.raises(AttributeError):
        view.items = 3


def test_integer_literal_stays_int_and_decimal_keeps_scale():
    compiled = compile_expr("body.data['items'][0] == 1.50", "success")
    values = [n.value for n in ast.walk(compiled.tree) if isinstance(n, ast.Constant)]
    assert [v for v in values if type(v) is int] == [0]
    assert [str(v) for v in values if isinstance(v, Decimal)] == ["1.50"]


@pytest.mark.parametrize(
    "source",
    [
        "1 / 10 == 0.1",  # 整数相除也是精确的 Decimal
        "body.cents / 100 == 12.34",
        "body.cents / 2 + 0.5 == 617.5",
        "scale(1 / 4) == 2",
        "-7 // 2 == -3 and -7.0 // 2 == -3",  # // 和 % 统一向零取整
        "-7 % 2 == -1 and -7.0 % 2 == -1",
        "7 // 2 == 3 and is_number(7 // 2)",
        "123456789012345678901234567890.5 + 1 == 123456789012345678901234567891.5",  # 超过 28 位不丢精度
    ],
)
def test_exact_arithmetic(source):
    out = run_success(source, body={"cents": 1234})
    assert out.passed, out


def test_get_and_exists():
    assert run_assert("get(cand.body, 'data.items[0].price', 0) == 12.5").passed
    assert run_assert("get(cand.body, 'data.nope', 7) == 7").passed
    assert run_assert("get(cand.body, 'data.items[9].price') is None").passed
    assert run_assert("get(cand.body, 'code.deeper', 1) == 1").passed  # 走进标量也算缺失
    assert run_assert("get(cand.body, '$') == cand.body").passed
    assert run_assert("exists(cand.body, 'data[\"a.b\"]')").passed
    assert run_assert("exists(cand.body, 'data.coupon')").passed  # 值为 null 也算存在
    assert not run_assert("exists(cand.body, 'data.nope')").passed
    assert run_assert("not exists(cand.body, 'data.nope')").error is None


def test_get_and_exists_on_side():
    base, cand = side(body={"total": 100}), side(body={"total": 5})
    assert not run_assert("get(cand, 'body.total', 0) >= get(base, 'body.total', 0)", base, cand).passed
    assert run_assert("exists(cand, 'body.total') and get(cand, 'status') == 200", base, cand).passed
    assert run_assert("get(cand, 'headers.x-trace') == 't1'", base, cand).passed
    assert not run_assert("exists(cand, 'nope')", base, cand).passed


def test_get_on_concatenated_array():
    """拼接结果仍是 JsonView，. 访问和 get/exists 都正常。"""
    body = {"a": [{"id": 1, "price": 1}], "b": [{"id": 2, "price": 2}]}
    assert run_success("all(x.price > 0 for x in body.a + body.b)", body=body).passed
    assert run_success("exists(body.a + body.b, '[1].id')", body=body).passed
    assert "下标越界" in run_success("(body.a + body.b)[9] == 1", body=body).error


# ---------- success ----------


def test_success_variables():
    assert run_success("body.code == 0 and status == 200").passed
    assert not run_success("body.code == 0", body={"code": 500}).passed
    assert run_success("body.code == 0", body={"code": 500}).error is None


def test_success_error_is_not_satisfied():
    out = run_success("body.data.nope == 1")
    assert not out.passed and out.error == "字段不存在：data.nope"


def test_success_root_array_and_scalar_body():
    assert run_success("len(body) == 2", body=[1, 2]).passed
    assert run_success("body == 5", body=5).passed


@pytest.mark.parametrize(("source", "kind"), [("cand.status == 1", "success"), ("base", "success"), ("status", "assert")])
def test_variables_are_per_kind(source, kind):
    assert "未知的变量" in rejected(source, kind)


# ---------- 结果必须是布尔值 ----------


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("body.code == 0, status == 200", "不能是元组"),
        ("len(body) >= 1, 'msg'", "不能是元组"),
        ("[body.code == 0]", "不能是列表"),
        ("[x for x in body]", "不能是列表"),
        ("(x for x in body)", "不能是生成器"),
        ("1", "必须是 True 或 False"),
        ("'ok'", "必须是 True 或 False"),
    ],
)
def test_non_bool_result_rejected_at_compile(source, message):
    assert message in rejected(source, "success")


@pytest.mark.parametrize(
    ("source", "body", "message"),
    [
        ("body.code", {"code": 0}, "这里是数字"),
        ("body.data", {"data": {"a": 1}}, "这里是对象"),
        ("body.data and True", {"data": []}, "这里是数组"),
        ("body.msg", {"msg": "ok"}, "这里是字符串"),
        ("body.v", {"v": None}, "这里是null"),
    ],
)
def test_non_bool_result_is_error(source, body, message):
    out = run_success(source, body=body)
    assert not out.passed and message in out.error


def test_bool_field_is_fine():
    assert run_success("body.ok", body={"ok": True}).passed
    out = run_success("body.ok", body={"ok": False})
    assert not out.passed and out.error is None


# ---------- AC-24：assert 为假 / 抛异常 ----------


def test_assert_false_has_no_error():
    out = run_assert("len(cand.body.data['items']) > 5")
    assert not out.passed and out.error is None


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("cand.body.data.nope == 1", "字段不存在：data.nope"),
        ("cand.body.data['items'][5] == 1", "下标越界：data.items[5]，数组长度 2"),
        ("cand.body.data['items'].x == 1", "data.items 是数组，不能用 .x 访问"),
        ("cand.body.data['items']['x'] == 1", "下标必须是整数"),
        ("cand.body.data[0] == 1", "key 必须是字符串"),
        ("1 / 0 == 1", "除数为 0"),
        ("1 // 0 == 1", "除数为 0"),
        ("max([]) == 1", "max() 的参数为空"),
        ("scale('1.0') == 1", "scale() 只接受数字"),
        ("scale(True) == 1", "scale() 只接受数字"),
        ("keys(cand.body.data['items']) == []", "keys() 只接受对象"),
        ("cand.body.code < 'a'", "< 只能比较两个数字或两个字符串，这里是数字和字符串"),
        ("cand.body.data['items'] < base.body.data['items']", "这里是数组和数组"),
        ("cand.body.code.real == 1", "只能在对象上用 . 访问字段，这里是数字"),
        ("cand.body.data.coupon.amount == 1", "值为 null，不能用 .amount 访问"),
        ("cand.headers.nope == 1", "字段不存在：headers.nope"),
        ("get(cand.body, 1) == 1", "路径字符串"),
    ],
)
def test_assert_error(source, message):
    out = run_assert(source)
    assert not out.passed
    assert message in out.error, out.error


def test_errors_are_independent():
    """一条出错不影响下一条（各自独立求值，求值器不共享状态）。"""
    a = compile_expr("[x for x in cand.body.data['items']][9] == 1", "assert")
    b = compile_expr("len([x for x in cand.body.data['items']]) == 2", "assert")
    assert eval_assert(a, side(), side()).error
    assert eval_assert(b, side(), side()).passed


def test_scale_nan_is_error():
    out = run_success("scale(body.v) == 0", body={"v": Decimal("NaN")})
    assert "NaN" in out.error


# ---------- 推导式：惰性、作用域 ----------


def test_any_all_short_circuit():
    """生成器是惰性的：any/all 找到结果就停，后面缺字段的元素不会报错。"""
    body = {"items": [{"sku_id": "A1", "price": -1}, {"gift": True}]}
    out = run_success("any(x.sku_id == 'A1' for x in body['items'])", body=body)
    assert out.passed, out
    out = run_success("all(x.price > 0 for x in body['items'])", body=body)
    assert not out.passed and out.error is None
    big = {"items": [{"id": i} for i in range(20000)]}
    assert run_success("any(x.id == 0 for x in body['items'])", body=big).passed


def test_nested_comprehension_scope():
    body = {"rows": [[1, 2], [3]]}
    assert run_success("[y for x in body.rows for y in x] == [1, 2, 3]", body=body).passed
    assert run_success("all(any(y == 3 for y in x) or len(x) == 2 for x in body.rows)", body=body).passed


@pytest.mark.parametrize(
    "source",
    [
        "x > 0 and all(x > 0 for x in body.items)",
        "len([x for x in x]) == 0",
        "len([x for x in body.a]) == 0 and x",
        "len([y for x in body.a if y]) == 0",
    ],
)
def test_loop_variable_out_of_scope(source):
    assert "未知的变量" in rejected(source, "success")


# ---------- 静态拒绝（AC-25 + 恶意用例） ----------


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("__import__('os') == 1", "未知的函数 __import__"),
        ("cand._y == 1", "不能访问 _ 开头的属性"),
        ("cand.body.__class__ == 1", "不能访问 _ 开头的属性"),
        ("(1).__class__.__bases__ == 1", "不能访问 _ 开头的属性"),
        ("cand.body.__dict__ == 1", "不能访问 _ 开头的属性"),
        ("cand.body.x.keys() == 1", "不能调用方法 .keys()"),
        ("cand.headers['a'].upper() == 1", "不能调用方法 .upper()"),
        ("'{0.__class__}'.format(cand) == 1", "不能调用方法"),
        ("eval('1') == 1", "未知的函数 eval"),
        ("open('/etc/passwd') == 1", "未知的函数 open"),
        ("getattr(cand, 'body') == 1", "未知的函数 getattr"),
        ("type(cand) == 1", "未知的函数 type"),
        ("dict(a=1) == 1", "未知的函数 dict"),
        ("list(cand.body) == 1", "未知的函数 list"),
        ("(lambda: 1)() == 1", "Lambda"),
        ("len(*cand.body) == 1", "Starred"),
        ("len(x=1) == 1", "不支持关键字参数"),
        ("len == 1", "只能直接调用"),
        ("max(cand.body, key=len) == 1", "不支持关键字参数"),
        ("import os", "语法错误"),
        ("x = 1", "语法错误"),
        ("(x := 1) == 1", "NamedExpr"),
        ("2 ** 10 == 1", "不支持的运算符 Pow（第 1 列）"),
        ("1 << 100 == 1", "不支持的运算符 LShift"),
        ("1 | 2 == 1", "不支持的运算符 BitOr"),
        ("~1 == 1", "不支持的运算符 Invert"),
        ("f'{cand}' == 1", "JoinedStr"),
        ("{1: 2} == 1", "Dict"),
        ("{1, 2} == 1", "Set"),
        ("{k: 1 for k in cand.body} == 1", "DictComp"),
        ("cand.body[0:2] == 1", "Slice"),
        ("b'x' == 1", "不支持的字面量 bytes"),
        ("1j == 1", "不支持的字面量 complex"),
        ("... == 1", "不支持的字面量 ellipsis"),
        ("len([a for a, b in cand.body]) == 1", "循环变量只能是单个名字"),
        ("len([len for len in cand.body]) == 1", "循环变量不能叫 'len'"),
        ("len([cand for cand in base.body]) == 1", "循环变量不能叫 'cand'"),
        ("len([body for body in base.body]) == 1", "循环变量不能叫 'body'"),
        ("len([_x for _x in cand.body]) == 1", "循环变量不能叫 '_x'"),
        ("os.system('x') == 1", "不能调用方法"),
        ("unknown == 1", "未知的变量 'unknown'"),
        ("get(cand.body, 'a[*].b') == 1", "不能用 [*] 或 **"),
        ("exists(cand.body, 'a..b')", "路径写法有误"),
        ("", "表达式为空"),
        ("   ", "表达式为空"),
        ("1 +", "语法错误"),
        ("1\n2", "语法错误"),
        ("a" * 2001, "表达式过长"),
        ("cand.body.x < 1e9999999999999999999", "超出范围"),
        ("'\\d' == cand.body.x", "语法错误：invalid escape sequence"),
    ],
)
def test_rejected_at_compile(source, message):
    assert message in rejected(source)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("get(cand.body) == 1", "get() 需要 2~3 个参数，这里有 1 个"),
        ("exists(cand.body, 'items', False)", "exists() 需要 2 个参数，这里有 3 个"),
        ("len(cand.body, 1) == 1", "len() 需要 1 个参数"),
        ("scale(1.0, 2) == 1", "scale() 需要 1 个参数"),
        ("keys() == []", "keys() 需要 1 个参数，这里有 0 个"),
        ("abs() == 1", "abs() 需要 1 个参数"),
        ("max() == 1", "max() 需要 至少 1 个参数"),
        ("set(1, 2) == 1", "set() 需要 0~1 个参数"),
    ],
)
def test_arity_checked_at_compile(source, message):
    assert message in rejected(source)


@pytest.mark.parametrize(
    ("source", "kind", "message"),
    [
        ("cand.boby.code == 0", "assert", "cand 只有 status、headers、body 三个属性，没有 .boby"),
        ("cand.header['x'] == 't1'", "assert", "没有 .header"),
        ("base.status.x == 1", "assert", "status 是整数"),
        ("status.x == 1", "success", "status 是整数"),
    ],
)
def test_attribute_typos_rejected(source, kind, message):
    assert message in rejected(source, kind)


@pytest.mark.parametrize("source", ["status is 200", "body.a is body.b", "body.x is not 'a'", "1 is 1"])
def test_is_only_with_singletons(source):
    assert "is / is not 只能和 None、True、False 比较" in rejected(source, "success")


def test_is_with_singletons_allowed():
    body = {"v": None, "ok": True}
    assert run_success("body.v is None and body.ok is True and None is not body.ok", body=body).passed


def test_fullwidth_attribute_rejected():
    """Python 会把 .字段１ 规范化成 .字段1，读错字段。"""
    assert "全角" in rejected("body.字段１ == 1", "success")
    assert run_success("body['字段１'] == 1", body={"字段１": 1}).passed


def test_compile_reports_column_in_characters():
    assert "（第 5 列）" in rejected("1 + cand._x == 1")
    assert "（第 22 列）" in rejected("body.msg == '成功' and body._x == 1", "success")
    assert "（第 2 行第 1 列）" in rejected("(body.a == 1 and\nbody._x == 1)", "success")


# ---------- 超大计算（求值时拦截） ----------


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("'a' * 1000000000 == 1", "* 只能用于数字"),
        ("[0] * 1000000000 == 1", "* 只能用于数字"),
        ("'%999999999d' % 1 == 1", "% 只能用于数字"),
        ("len([1 for a in cand.body.big for b in cand.body.big]) == 1", "迭代次数超过 10000"),
        ("len(cand.body.big + cand.body.big) == 1", "超过 10000"),
    ],
)
def test_huge_computation_rejected(source, message):
    big = side(body={"big": list(range(9000))})
    out = eval_assert(compile_expr(source, "assert"), big, big)
    assert not out.passed
    assert message in out.error


def test_repeated_squaring_is_bounded():
    levels = " ".join(f"for v{i + 1} in [v{i} * v{i}]" for i in range(30))
    source = f"len([0 for v0 in [999999999] {levels}]) == 1"
    start = time.monotonic()
    out = run_success(source)
    assert time.monotonic() - start < 1
    assert out.error == "整数乘积过大"


@pytest.mark.parametrize(
    "source",
    [
        "len([body.b + body.b for x in body.a]) > 0",
        "len([set(body.a) for x in body.a]) > 0",
        "len([body.a == body.a for x in body.a]) > 0",
        "all(x in body.a for x in body.a)",
    ],
)
def test_work_budget(source):
    body = {"a": list(range(5000)), "b": list(range(4000))}
    start = time.monotonic()
    out = run_success(source, body=body)
    assert time.monotonic() - start < 5
    assert out.error == "计算量超出上限，请简化表达式"


def test_deep_nesting_does_not_crash():
    for source in ["-" * 1500 + "1 == 1", "not " * 450 + "True", "(" * 150 + "1" + ")" * 150 + " == 1"]:
        try:
            compiled = compile_expr(source, "success")
        except ExprError:
            continue
        out = eval_success(compiled, 200, {}, {})
        assert out.passed or out.error


def test_number_arithmetic_still_allowed():
    assert run_assert("cand.body.code * 3 % 2 == 0 and 7 // 2 == 3 and 1 - 2 == -1").passed


def test_concurrent_eval_leaves_warning_filters_alone():
    before = list(warnings.filters)
    compiled = compile_expr("body.code == 0", "success")

    def work():
        for _ in range(300):
            eval_success(compiled, 200, HEADERS, BODY)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert warnings.filters == before


# ---------- 接入配置加载（validate / run 前） ----------

SUITE = """version: 1
targets:
  baseline: {base_url: "http://a"}
  candidate: {base_url: "http://b"}
defaults:
  success: "%s"
requests:
  - name: r
    method: GET
    path: /r
    success: "%s"
    asserts:
      - expr: "%s"
"""


def test_config_rejects_bad_expressions_with_lines():
    text = SUITE % ("body.__class__ == 1", "status.x()", "import os")
    with pytest.raises(ConfigError) as info:
        load_suite_text(text, "s.yaml")
    got = [(i.location, i.line) for i in info.value.issues]
    assert got == [("defaults.success", 6), ("requests[0].success", 11), ("requests[0].asserts[0].expr", 13)]
    assert all(i.message.startswith("表达式不合法：") for i in info.value.issues)


def test_config_accepts_valid_expressions():
    text = SUITE % ("body.code == 0", "status == 200", "len(cand.body) >= len(base.body)")
    assert load_suite_text(text).suite.requests[0].asserts[0].level == "suspicious"


def test_success_variables_differ_from_assert_in_config():
    text = SUITE % ("cand.body.code == 0", "status == 200", "body.code == 0")
    with pytest.raises(ConfigError) as info:
        load_suite_text(text)
    assert [i.location for i in info.value.issues] == ["defaults.success", "requests[0].asserts[0].expr"]


def test_config_skips_expression_with_missing_var():
    text = SUITE % ("${EXPR}", "status == 200", "True")
    loaded = load_suite_text(text, env={})
    assert [m.name for m in loaded.missing_vars] == ["EXPR"]


def test_expressions_checked_even_when_suite_unverified():
    """有字段因缺变量无法校验类型时，表达式仍然要检查。"""
    good = SUITE % ("body.code == 0", "status == 200", "True")
    bad = SUITE % ("__import__('os').system('id') == 0", "status == 200", "True")
    timeout = 'defaults:\n  timeout: "${T}"\n'
    with pytest.raises(UnverifiedSuite):
        load_suite_text(good.replace("defaults:\n", timeout), env={})
    with pytest.raises(ConfigError) as info:
        load_suite_text(bad.replace("defaults:\n", timeout), env={})
    assert [i.location for i in info.value.issues] == ["defaults.success"]


def test_non_string_expression_left_to_pydantic():
    text = SUITE % ("body.code == 0", "status == 200", "True")
    text = text.replace('success: "status == 200"', "success: [1]")
    with pytest.raises(ConfigError) as info:
        load_suite_text(text)
    assert [i.location for i in info.value.issues] == ["requests[0].success"]


# ---------- 组合矩阵发现的问题（spec §7.7、D16） ----------

TYPED = {"t": True, "f": False, "z": 0, "i": 3, "s": "ab", "a": [1, 2], "o": {"k": 1}, "nan": Decimal("NaN"),
         "inf": Decimal("Infinity"), "ea": []}


@pytest.mark.parametrize(
    "source",
    [
        "not (body.f == body.z)",  # F1：false 不等于 0
        "body.t != 1 and body.f != 0",
        "not (body.t in body.a)",
        "not (body.t in [1, 2]) and True in [True]",
        "[0] != [False]",
        "body.a == (1, 2)",  # 数组和元组逐个元素比较
        "body.o == body.o",
        "body.nan != body.nan",  # NaN 不等于自身
        "'a' < 'b' and 1 < 2.5",
        "set([1, 2]) - set([2]) == set([1])",
        "'a' in 'ab' and 'k' in body.o and 1 in body.a",
    ],
)
def test_json_typed_semantics(source):
    out = run_success(source, body=TYPED)
    assert out.passed, out


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("body.t + body.i == 4", "+ 不能用于布尔值和数字"),
        ("body.t - 1 == 0", "- 只能用于数字"),
        ("body.t < 2", "< 只能比较两个数字或两个字符串，这里是布尔值和数字"),
        ("-body.t == -1", "- 只能用于数字，这里是布尔值"),
        ("abs(body.t) == 1", "abs() 只能用于数字，这里是布尔值"),
        ("max(body.z, body.t) == 1", "max() 只能比较全是数字或全是字符串的值"),
        ("len(set([1, body.t])) == 1", "set() 里不能同时有布尔值和数字"),
    ],
)
def test_bool_is_not_a_number(source, message):
    out = run_success(source, body=TYPED)
    assert not out.passed and message in out.error, out


def test_success_code_false_is_not_zero():
    """F1 的实际危害：code 为 false 时 body.code == 0 不能成立。"""
    assert not run_success("body.code == 0", body={"code": False}).passed


@pytest.mark.parametrize(
    "source",
    [
        "not (x for x in body.a)",
        "(x for x in body.a) == [1, 2]",
        "len((x for x in body.a)) == 2",
        "1 in (x for x in body.a)",
        "max((x for x in body.a), 3) == 3",
        "[y for y in (x for x in body.a)] == [1, 2]",
    ],
)
def test_generator_only_as_sole_argument(source):
    assert "生成器只能作为 all / any / set / min / max 的唯一参数" in rejected(source, "success")


def test_generator_allowed_positions():
    for source in [
        "any(x == 1 for x in body.a)",
        "all(x > 0 for x in body.a)",
        "max(x for x in body.a) == 2",
        "min(x for x in body.a) == 1",
        "set(x for x in body.a) == set([1, 2])",
    ]:
        assert run_success(source, body=TYPED).passed, source


@pytest.mark.parametrize(
    "source", ["cand == base", "len(cand) > 0", "set([cand]) == set()", "[c for c in [cand]] == []", "cand"]
)
def test_side_not_usable_as_value(source):
    assert "不能直接使用，请写" in rejected(source)


def test_side_allowed_positions():
    assert run_assert("cand.body == base.body and get(cand, 'status') == 200 and exists(base, 'body.code')").passed


def test_get_on_plain_lists():
    """F4：列表字面量和 keys() 的结果也按数组取值。"""
    assert run_success('get(keys(body.o), "[0]") == "k"', body=TYPED).passed
    assert run_success('exists([1, 2], "[0]") and get((1, 2), "[1]") == 2', body=TYPED).passed
    assert run_success('get([1], "[5]", 0) == 0', body=TYPED).passed


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("body.i + body.s == 1", "+ 不能用于数字和字符串"),
        ("body.o in body.s", "in 的右边是字符串时，左边也必须是字符串，这里是对象"),
        ("'k' in body.i", "in 的右边必须是对象、数组或字符串，这里是数字"),
        ("body.i < body.s", "< 只能比较两个数字或两个字符串，这里是数字和字符串"),
        ("min(body.ea) == 1", "min() 的参数为空"),
        ("len(body.i) == 1", "len() 只能用于对象、数组或字符串，这里是数字"),
        ("all(body.i)", "all() 需要数组或生成器，这里是数字"),
        ("len(set(body.a + [body.o])) == 1", "set() 的元素不能是对象或数组"),
        ("body.i['k'] == 1", "只能对对象、数组、字符串用 [...]，这里是数字"),
        ("body.s['k'] == 1", "字符串的下标必须是整数，这里是字符串"),
        ("body.s[5] == 'a'", "下标越界：[5]，长度 2"),
        ("len([x for x in body.i]) == 0", "for 只能遍历数组、对象或字符串，这里是数字"),
        ("body.a - body.a == []", "- 只能用于数字，这里是数组和数组"),
        ("body.s * 2 == 'abab'", "* 只能用于数字，这里是字符串和数字"),
    ],
)
def test_friendly_type_errors(source, message):
    """F5：类型不匹配时用 JSON 类型名描述，不出现 Python 内部名字。"""
    out = run_success(source, body=TYPED)
    assert not out.passed and message in out.error, out
    for word in ("TypeError", "ValueError", "IndexError", "JsonView", "SideView", "decimal.", "NoneType", "'int'"):
        assert word not in out.error


def test_bool_index_rejected_on_literals():
    """F6：字符串和列表字面量也不接受布尔值下标。"""
    for source in ["body.s[True] == 'b'", "[1, 2][True] == 2", "body.a[True] == 2"]:
        assert "下标必须是整数" in run_success(source, body=TYPED).error


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("body.nan < 1", "NaN 不能比较大小"),
        ("max(body.nan, 1) == 1", "NaN 不能比较大小"),
        ("body.inf - body.inf == 0", "数值运算没有意义（例如 Infinity - Infinity）"),
    ],
)
def test_nan_and_infinity_messages(source, message):
    out = run_success(source, body=TYPED)
    assert not out.passed and message in out.error, out
