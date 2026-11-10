"""success / asserts 表达式：AST 白名单编译 + simpleeval 求值。

安全边界由 compile_expr 的白名单保证，求值器再做一层兜底：
- 只允许白名单里的语法节点、变量名，函数调用的目标只能是内置函数名，参数个数也要对；
- 生成器只能作为 all/any/set/min/max 的唯一参数，base/cand 不能直接参与运算；
- 禁止访问 `_` 开头的属性，`.` 只能用在 JsonView / SideView 上；
- 每次求值有总计算量预算，防止超大计算。
值的运算语义（JSON 类型、友好的错误信息）在 expr_values.py。
"""

from __future__ import annotations

import ast
import decimal
import logging
import warnings
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import simpleeval

from api_regress.core import paths
from api_regress.core.expr_values import (
    FUNCTIONS,
    ITERABLE,
    OPERATOR_SYMBOLS,
    OPERATORS,
    SIDE_ATTRS,
    ExprEvalError,
    HeadersView,
    JsonView,
    SideView,
    literal_path,
    type_name,
    unwrap,
    wrap,
)

__all__ = [
    "CompiledExpr", "ExprError", "ExprEvalError", "ExprKind", "HeadersView", "JsonView", "Outcome", "SideView",
    "compile_expr", "eval_assert", "eval_success", "unwrap", "wrap",
]  # fmt: skip

log = logging.getLogger(__name__)

ExprKind = Literal["success", "assert"]

MAX_EXPR_LENGTH = 2000
MAX_ITERATIONS = 10000
WORK_BUDGET = 5_000_000
DECIMAL_PRECISION = 50
_ERROR_MESSAGE_LIMIT = 300

VARIABLES: dict[ExprKind, frozenset[str]] = {
    "success": frozenset({"status", "headers", "body"}),
    "assert": frozenset({"base", "cand"}),
}
_SIDE_NAMES = frozenset({"base", "cand"})


class ExprError(ValueError):
    """表达式在编译（静态检查）阶段被拒绝。"""


_GENERATOR_FUNCTIONS = frozenset({"all", "any", "set", "min", "max"})
# 参数个数：(最少, 最多)，None 表示不限
ARITY: dict[str, tuple[int, int | None]] = {
    "len": (1, 1),
    "scale": (1, 1),
    "get": (2, 3),
    "exists": (2, 2),
    "is_number": (1, 1),
    "is_string": (1, 1),
    "keys": (1, 1),
    "set": (0, 1),
    "all": (1, 1),
    "any": (1, 1),
    "abs": (1, 1),
    "min": (1, None),
    "max": (1, None),
}
_PATH_ARG_FUNCTIONS = frozenset({"get", "exists"})
_FUNCTION_LIST = "、".join(FUNCTIONS)
# 推导式循环变量不能用的名字：两种表达式的变量名都算，避免同一个名字在不同表达式里含义不同
_RESERVED = VARIABLES["success"] | VARIABLES["assert"] | FUNCTIONS.keys()


# ---------- 编译：AST 白名单 ----------

_STRUCTURE_NODES: frozenset[type] = frozenset(
    {
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Attribute,
        ast.Subscript,
        ast.Compare,
        ast.BoolOp,
        ast.UnaryOp,
        ast.BinOp,
        ast.IfExp,
        ast.Call,
        ast.List,
        ast.Tuple,
        ast.ListComp,
        ast.GeneratorExp,
        ast.comprehension,
    }
)
_OPERATOR_BASES = (ast.operator, ast.unaryop, ast.cmpop, ast.boolop)
_CONSTANT_TYPES = (int, float, str, bool, type(None))
_COMPREHENSIONS = (ast.ListComp, ast.GeneratorExp)
_NOT_BOOL_RESULTS = {ast.Tuple: "元组", ast.List: "列表", ast.ListComp: "列表", ast.GeneratorExp: "生成器"}


@dataclass(frozen=True)
class CompiledExpr:
    source: str
    kind: ExprKind
    tree: ast.expr


def _is_singleton(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and any(node.value is v for v in (None, True, False))


def _is_name(node: ast.expr, names: Iterable[str]) -> bool:
    return isinstance(node, ast.Name) and node.id in names


def _is_status(node: ast.expr) -> bool:
    """status 变量，或 base.status / cand.status：都是整数。"""
    if isinstance(node, ast.Attribute):
        return node.attr == "status" and _is_name(node.value, _SIDE_NAMES)
    return _is_name(node, ("status",))


class _Checker:
    """静态检查。逐节点的约束用分发表 checks；变量名按推导式作用域单独检查。"""

    def __init__(self, source: str, kind: ExprKind):
        self.source = source
        self.lines = source.splitlines() or [""]
        self.variables = VARIABLES[kind]
        self.checks: dict[type, Callable[[Any], None]] = {
            ast.Attribute: self._attribute,
            ast.Call: self._call,
            ast.Constant: self._constant,
            ast.BinOp: lambda n: self._operator(n, n.op),
            ast.UnaryOp: lambda n: self._operator(n, n.op),
            ast.Compare: self._compare,
            ast.GeneratorExp: self._generator,
            ast.Name: self._side_name,
        }
        self.generator_args: set[int] = set()
        self.side_uses: set[int] = set()

    def _allowed_positions(self, tree: ast.AST) -> None:
        """生成器只能作为 all/any/set/min/max 的唯一参数；base/cand 只能后接 .属性，或作为 get/exists 的第一个参数。"""
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.side_uses.add(id(node.value))
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args):
                continue
            if node.func.id in _GENERATOR_FUNCTIONS and len(node.args) == 1:
                self.generator_args.add(id(node.args[0]))
            if node.func.id in _PATH_ARG_FUNCTIONS:
                self.side_uses.add(id(node.args[0]))

    def run(self, tree: ast.Expression) -> None:
        self._result(tree.body)
        self._allowed_positions(tree)
        for node in ast.walk(tree):  # 循环变量名先查，提示比「不能直接使用」更准确
            if isinstance(node, ast.comprehension):
                self._comprehension(node)
        for node in ast.walk(tree):
            if isinstance(node, _OPERATOR_BASES):
                continue  # 运算符节点没有位置信息，由所在的表达式节点检查
            if type(node) not in _STRUCTURE_NODES:
                self.reject(node, f"不支持的语法 {type(node).__name__}")
            check = self.checks.get(type(node))
            if check:
                check(node)
        self._names(tree.body, frozenset())

    # ----- 位置与报错 -----

    def reject(self, node: ast.AST, message: str) -> None:
        raise ExprError(message + self._where(node))

    def _where(self, node: ast.AST) -> str:
        lineno, col = getattr(node, "lineno", None), getattr(node, "col_offset", None)
        if lineno is None or col is None:
            return ""
        text = self.lines[lineno - 1] if lineno <= len(self.lines) else ""
        column = len(text.encode("utf-8")[:col].decode("utf-8", errors="ignore")) + 1  # col_offset 是字节数
        return f"（第 {lineno} 行第 {column} 列）" if len(self.lines) > 1 else f"（第 {column} 列）"

    # ----- 逐节点检查 -----

    def _result(self, body: ast.expr) -> None:
        kind = _NOT_BOOL_RESULTS.get(type(body))
        if kind:
            self.reject(body, f"表达式的结果必须是 True 或 False，不能是{kind}；多个条件请用 and 连接")
        if isinstance(body, ast.Constant) and not isinstance(body.value, bool):
            self.reject(body, "表达式的结果必须是 True 或 False")

    def _generator(self, node: ast.GeneratorExp) -> None:
        if id(node) not in self.generator_args:
            self.reject(node, "生成器只能作为 all / any / set / min / max 的唯一参数；需要列表请写成 [...]")

    def _side_name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            return  # 循环变量名由 _comprehension 检查
        if node.id in _SIDE_NAMES and node.id in self.variables and id(node) not in self.side_uses:
            self.reject(
                node,
                f"{node.id} 不能直接使用，请写 {node.id}.status / {node.id}.headers / {node.id}.body，"
                f"或者 get({node.id}, \"body.字段\")",
            )

    def _operator(self, node: ast.AST, op: ast.AST) -> None:
        if type(op) not in OPERATORS:
            self.reject(node, f"不支持的运算符 {type(op).__name__}")

    def _compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        for i, op in enumerate(node.ops):
            self._operator(node, op)
            is_op = isinstance(op, (ast.Is, ast.IsNot))
            if is_op and not (_is_singleton(operands[i]) or _is_singleton(operands[i + 1])):
                self.reject(node, "is / is not 只能和 None、True、False 比较；比较值请用 == / !=")

    def _attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            self.reject(node, f"不能访问 _ 开头的属性 .{node.attr}；字段名以 _ 开头时用 [\"{node.attr}\"]")
        segment = ast.get_source_segment(self.source, node) or ""
        if not segment.endswith(node.attr):
            self.reject(node, f"字段名 .{node.attr} 含全角等会被改写的字符，请改用 [\"...\"]")
        if _is_name(node.value, _SIDE_NAMES) and node.attr not in SIDE_ATTRS:
            self.reject(node, f"{node.value.id} 只有 status、headers、body 三个属性，没有 .{node.attr}")  # type: ignore[attr-defined]
        if _is_status(node.value):
            self.reject(node, "status 是整数，不能用 . 访问")

    def _call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            self.reject(node, f"不能调用方法 .{func.attr}()；只能调用内置函数：{_FUNCTION_LIST}")
        if not isinstance(func, ast.Name) or func.id not in FUNCTIONS:
            name = func.id if isinstance(func, ast.Name) else type(func).__name__
            self.reject(node, f"未知的函数 {name}；只能调用内置函数：{_FUNCTION_LIST}")
        if node.keywords:
            self.reject(node, f"{func.id}() 不支持关键字参数")  # type: ignore[union-attr]
        self._arity(node, func.id)  # type: ignore[union-attr]
        if func.id in _PATH_ARG_FUNCTIONS:  # type: ignore[union-attr]
            self._path_arg(node.args[1])

    def _arity(self, node: ast.Call, name: str) -> None:
        low, high = ARITY[name]
        count = len(node.args)
        if count < low or (high is not None and count > high):
            want = f"至少 {low}" if high is None else (f"{low}" if low == high else f"{low}~{high}")
            self.reject(node, f"{name}() 需要 {want} 个参数，这里有 {count} 个")

    def _path_arg(self, arg: ast.expr) -> None:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            try:
                literal_path(arg.value)
            except (paths.PathSyntaxError, ExprEvalError) as exc:
                self.reject(arg, f"路径写法有误：{exc}")

    def _constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, _CONSTANT_TYPES):
            self.reject(node, f"不支持的字面量 {type(node.value).__name__}")

    def _comprehension(self, node: ast.comprehension) -> None:
        if not isinstance(node.target, ast.Name):
            self.reject(node.target, "推导式的循环变量只能是单个名字")
        elif node.target.id.startswith("_") or node.target.id in _RESERVED:
            self.reject(node.target, f"推导式的循环变量不能叫 {node.target.id!r}")
        if node.is_async:
            self.reject(node, "不支持 async 推导式")

    # ----- 变量名（按作用域） -----

    def _names(self, node: ast.AST, scope: frozenset[str]) -> None:
        if isinstance(node, _COMPREHENSIONS):
            self._comprehension_names(node, scope)
        elif isinstance(node, ast.Name):
            self._name(node, scope)
        elif isinstance(node, ast.Call):
            for arg in node.args:  # 函数名本身由 _call 检查
                self._names(arg, scope)
        else:
            for child in ast.iter_child_nodes(node):
                self._names(child, scope)

    def _comprehension_names(self, node: ast.ListComp | ast.GeneratorExp, scope: frozenset[str]) -> None:
        """和 Python 一致：第一个 for 的可迭代对象在外层作用域求值，循环变量只在推导式内部可见。"""
        inner = scope
        for gen in node.generators:
            self._names(gen.iter, inner)
            inner = inner | {gen.target.id}  # type: ignore[attr-defined]
            for cond in gen.ifs:
                self._names(cond, inner)
        self._names(node.elt, inner)

    def _name(self, node: ast.Name, scope: frozenset[str]) -> None:
        if node.id in self.variables or node.id in scope:
            return
        if node.id in FUNCTIONS:
            self.reject(node, f"函数 {node.id} 只能直接调用，例如 {node.id}(...)")
        allowed = "、".join(sorted(self.variables))
        self.reject(node, f"未知的变量 {node.id!r}，这里只能用 {allowed}")


class _DecimalLiterals(ast.NodeTransformer):
    """浮点字面量按源码原文转成 Decimal（保留小数位），整数保持 int。"""

    def __init__(self, checker: _Checker):
        self.checker = checker

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        if not isinstance(node.value, float):
            return node
        text = ast.get_source_segment(self.checker.source, node) or repr(node.value)
        try:
            value = Decimal(text)
        except (ArithmeticError, ValueError):
            self.checker.reject(node, f"小数字面量 {text[:40]} 超出范围")
        return ast.copy_location(ast.Constant(value), node)


def _parse(source: str) -> ast.Expression:
    if not source:
        raise ExprError("表达式为空")
    if len(source) > MAX_EXPR_LENGTH:
        raise ExprError(f"表达式过长（超过 {MAX_EXPR_LENGTH} 个字符）")
    try:
        # 只在加载 suite 时调用。SyntaxWarning（如无效的转义）一律当错误，结果不受 PYTHONWARNINGS 影响
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        where = f"（第 {exc.offset} 列）" if exc.offset else ""
        raise ExprError(f"语法错误：{exc.msg}{where}") from None
    except (ValueError, RecursionError, MemoryError) as exc:
        raise ExprError(f"无法解析：{type(exc).__name__}") from None
    for w in caught:
        if issubclass(w.category, (SyntaxWarning, DeprecationWarning)):
            raise ExprError(f"语法错误：{w.message}")
    return tree


def compile_expr(source: str, kind: ExprKind) -> CompiledExpr:
    """静态检查并编译表达式；不通过时抛 ExprError（加载 suite 时调用，validate 和每次运行前都会走到）。"""
    source = source.strip()
    tree = _parse(source)
    checker = _Checker(source, kind)
    try:
        checker.run(tree)
        tree = _DecimalLiterals(checker).visit(tree)
    except RecursionError:
        raise ExprError("表达式嵌套过深") from None
    return CompiledExpr(source=source, kind=kind, tree=tree.body)


# ---------- 求值 ----------


def _index_sequence(seq: str | list | tuple, index: Any) -> Any:
    """字符串和列表字面量的下标：和 JSON 数组一样，只接受整数（不接受布尔值）。"""
    if isinstance(index, bool) or not isinstance(index, int):
        raise ExprEvalError(f"{type_name(seq)}的下标必须是整数，这里是{type_name(index)}")
    if not -len(seq) <= index < len(seq):
        raise ExprEvalError(f"下标越界：[{index}]，长度 {len(seq)}")
    return seq[index]


class _Budget:
    """每次求值的计算量预算：每个节点记 1，函数调用和运算按参数规模（JSON 节点数、元素数）记。"""

    def __init__(self, limit: int):
        self.left = limit
        self.iterations = 0
        self._sizes: dict[int, int] = {}

    def charge(self, amount: int) -> None:
        self.left -= amount
        if self.left < 0:
            raise ExprEvalError("计算量超出上限，请简化表达式")

    def iterate(self) -> None:
        self.iterations += 1
        if self.iterations > MAX_ITERATIONS:
            raise ExprEvalError(f"推导式迭代次数超过 {MAX_ITERATIONS}")
        self.charge(1)

    def size(self, value: Any) -> int:
        if isinstance(value, JsonView):
            return self._json_size(value._data)
        if isinstance(value, (list, tuple, set, frozenset)):
            return 1 + sum(self.size(v) for v in value)
        if isinstance(value, str):
            return 1 + len(value) // 64
        return 1

    def _json_size(self, data: Any) -> int:
        if not isinstance(data, (dict, list)):
            return self.size(data)
        cached = self._sizes.get(id(data))
        if cached is None:
            children = data.values() if isinstance(data, dict) else data
            cached = 1 + sum(self._json_size(v) for v in children)
            self._sizes[id(data)] = cached
        return cached


class _Evaluator(simpleeval.SimpleEval):
    """复用 simpleeval 的运算、比较、调用等节点处理，自己负责分发、变量作用域、属性访问和推导式。

    不调用父类 __init__：它每次都会用 warnings.catch_warnings() 修改进程级的 warnings 过滤器，
    多线程并发求值时不安全；分发表放在类上（_HANDLERS），也避免了绑定方法造成的引用环。
    """

    def __init__(self, names: dict[str, Any], source: str):  # noqa: D107
        self.names = names
        self.expr = source
        self.budget = _Budget(WORK_BUDGET)
        self.scope: dict[str, Any] = {}
        self.functions = {name: self._metered(f"{name}()", fn) for name, fn in FUNCTIONS.items()}
        self.operators = {op: self._metered(OPERATOR_SYMBOLS[op], fn) for op, fn in OPERATORS.items()}

    def _metered(self, label: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        """记账，并把漏网的 Python 原生异常转成用 JSON 类型名描述的提示。"""

        def call(*args: Any) -> Any:
            self.budget.charge(sum(self.budget.size(a) for a in args))
            try:
                return fn(*args)
            except decimal.DecimalException:
                raise ExprEvalError(f"{label}：数值运算没有意义（例如 Infinity - Infinity）") from None
            except (TypeError, ValueError, IndexError, KeyError):
                kinds = "和".join(type_name(a) for a in args)
                raise ExprEvalError(f"{label} 不能用于{kinds}") from None

        return call

    def _eval_subscript(self, node: ast.Subscript) -> Any:
        container = self._eval(node.value)
        key = self._eval(node.slice)
        if isinstance(container, JsonView):
            return container[key]
        if isinstance(container, (str, list, tuple)):
            return _index_sequence(container, key)
        raise ExprEvalError(f"只能对对象、数组、字符串用 [...]，这里是{type_name(container)}")

    def _eval(self, node: ast.AST) -> Any:
        self.budget.charge(1)
        handler = _HANDLERS.get(type(node))
        if handler is None:
            raise ExprEvalError(f"不支持的语法 {type(node).__name__}")
        return handler(self, node)

    def _eval_in(self, node: ast.AST, scope: dict[str, Any]) -> Any:
        saved, self.scope = self.scope, scope
        try:
            return self._eval(node)
        finally:
            self.scope = saved

    def _eval_name(self, node: ast.Name) -> Any:
        if node.id in self.scope:
            return self.scope[node.id]
        if node.id in self.names:
            return self.names[node.id]
        raise ExprEvalError(f"变量 {node.id} 未定义")

    def _eval_attribute(self, node: ast.Attribute) -> Any:
        if node.attr.startswith("_"):
            raise ExprEvalError(f"不能访问 _ 开头的属性 .{node.attr}")
        value = self._eval(node.value)
        if isinstance(value, SideView) and node.attr in SIDE_ATTRS or isinstance(value, JsonView):
            return getattr(value, node.attr)
        if value is None:
            raise ExprEvalError(f"值为 null，不能用 .{node.attr} 访问")
        raise ExprEvalError(f".{node.attr}：只能在对象上用 . 访问字段，这里是{type_name(value)}")

    def _comprehension(self, node: ast.ListComp | ast.GeneratorExp) -> Iterator[Any]:
        """惰性生成：any/all 找到结果就停，和 Python 一致。循环变量按词法作用域绑定。"""
        return self._generate(node, 0, self.scope)

    def _generate(self, node: ast.ListComp | ast.GeneratorExp, index: int, scope: dict[str, Any]) -> Iterator[Any]:
        gen = node.generators[index]
        iterable = self._eval_in(gen.iter, scope)
        if not isinstance(iterable, ITERABLE):
            raise ExprEvalError(f"for 只能遍历数组、对象或字符串，这里是{type_name(iterable)}")
        for item in iterable:
            self.budget.iterate()
            inner = {**scope, gen.target.id: item}  # type: ignore[attr-defined]
            if not all(self._eval_in(cond, inner) for cond in gen.ifs):
                continue
            if index + 1 < len(node.generators):
                yield from self._generate(node, index + 1, inner)
            else:
                yield self._eval_in(node.elt, inner)

    def run(self, tree: ast.expr) -> Any:
        return self._eval(tree)


_HANDLERS: dict[type, Callable[[_Evaluator, Any], Any]] = {
    ast.Constant: lambda ev, n: n.value,
    ast.Name: _Evaluator._eval_name,
    ast.Attribute: _Evaluator._eval_attribute,
    ast.Subscript: _Evaluator._eval_subscript,
    ast.Compare: simpleeval.SimpleEval._eval_compare,
    ast.BoolOp: simpleeval.SimpleEval._eval_boolop,
    ast.UnaryOp: simpleeval.SimpleEval._eval_unaryop,
    ast.BinOp: simpleeval.SimpleEval._eval_binop,
    ast.IfExp: simpleeval.SimpleEval._eval_ifexp,
    ast.Call: simpleeval.SimpleEval._eval_call,
    ast.List: lambda ev, n: [ev._eval(e) for e in n.elts],
    ast.Tuple: lambda ev, n: tuple(ev._eval(e) for e in n.elts),
    ast.ListComp: lambda ev, n: list(ev._comprehension(n)),
    ast.GeneratorExp: _Evaluator._comprehension,
}


@dataclass(frozen=True)
class Outcome:
    """passed：结果为 True 且没有出错。error：求值出错时的简短原因（不含请求头的值）。"""

    passed: bool
    error: str | None = None


def _describe(exc: BaseException) -> str:
    if isinstance(exc, ExprEvalError):
        text = str(exc)
    elif isinstance(exc, decimal.DecimalException):
        text = "数值运算没有意义（例如 Infinity - Infinity）"
    else:
        text = f"{type(exc).__name__}: {exc}"
    return text[:_ERROR_MESSAGE_LIMIT]


def _evaluate(expr: CompiledExpr, names: dict[str, Any]) -> Outcome:
    try:
        with decimal.localcontext(prec=DECIMAL_PRECISION):
            value = _Evaluator(names, expr.source).run(expr.tree)
    except Exception as exc:  # noqa: BLE001 —— 任何异常（含 RecursionError）都只算这条表达式出错
        log.debug("表达式求值出错：%s -> %s", expr.source, type(exc).__name__)
        return Outcome(passed=False, error=_describe(exc))
    if not isinstance(value, bool):
        return Outcome(passed=False, error=f"表达式的结果必须是 True 或 False，这里是{type_name(value)}")
    return Outcome(passed=value)


def eval_success(expr: CompiledExpr, status: int, headers: Mapping[str, str], body: Any) -> Outcome:
    """success 表达式：变量 status、headers（不区分大小写）、body。"""
    return _evaluate(expr, {"status": status, "headers": HeadersView(headers), "body": wrap(body)})


def eval_assert(expr: CompiledExpr, base: SideView, cand: SideView) -> Outcome:
    """assert 表达式：变量 base、cand。原始响应，不受 ignore 影响。"""
    return _evaluate(expr, {"base": base, "cand": cand})
