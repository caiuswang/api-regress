"""表达式里的值怎么运算：只读视图、JSON 语义的相等与包含、内置函数、运算符（spec §7.7、D16）。

这里不做语法检查，只负责「值」的语义；语法白名单和求值流程在 expr.py。
所有类型不匹配都抛 ExprEvalError，错误信息用 JSON 类型名描述，不暴露 Python 内部类型。
"""

from __future__ import annotations

import ast
import operator
import types
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal
from functools import lru_cache
from typing import Any

from api_regress.core import paths

MAX_SEQUENCE_LENGTH = 10000
MAX_INT_BITS = 8192
SIDE_ATTRS = frozenset({"status", "headers", "body"})


class ExprEvalError(Exception):
    """表达式求值时的业务错误，例如字段不存在。"""


_TYPE_NAMES: tuple[tuple[type | tuple[type, ...], str], ...] = (
    (bool, "布尔值"),
    ((int, Decimal, float), "数字"),
    (str, "字符串"),
    (type(None), "null"),
    ((list, tuple), "数组"),
    (dict, "对象"),
    ((set, frozenset), "集合"),
    (types.GeneratorType, "生成器"),
)


def type_name(value: Any) -> str:
    """错误信息里用 JSON 的类型名，不暴露 Python 内部类型。"""
    if isinstance(value, JsonView):
        return "对象" if isinstance(value._data, dict) else "数组"
    if isinstance(value, SideView):
        return "base/cand"
    return next((name for kinds, name in _TYPE_NAMES if isinstance(value, kinds)), "其他值")


# ---------- 只读视图 ----------


class JsonView:
    """只读的 JSON 容器视图。`.x` 和 `["x"]` 都只映射到 JSON 的 key，不暴露 dict/list 的方法。

    类上除了 dunder 和 `_` 开头的内部属性外没有任何属性，所以 getattr 一定落到 __getattr__。
    """

    __slots__ = ("_data", "_path")
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, data: dict | list, path: paths.Path = ()):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_path", path)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("JsonView 是只读的")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if not isinstance(self._data, dict):
            raise ExprEvalError(f"{self._where()} 是数组，不能用 .{name} 访问")
        return self[name]

    def __getitem__(self, key: Any) -> Any:
        if isinstance(self._data, dict):
            return self._dict_item(key)
        return self._list_item(key)

    def _dict_item(self, key: Any) -> Any:
        if not isinstance(key, str):
            raise ExprEvalError(f"{self._where()} 是对象，key 必须是字符串")
        norm = self._norm(key)
        if norm not in self._data:
            raise ExprEvalError(f"字段不存在：{paths.format_path((*self._path, paths.Key(key)))}")
        return wrap(self._data[norm], (*self._path, paths.Key(key)))

    def _list_item(self, index: Any) -> Any:
        if isinstance(index, bool) or not isinstance(index, int):
            raise ExprEvalError(f"{self._where()} 是数组，下标必须是整数")
        if not -len(self._data) <= index < len(self._data):
            raise ExprEvalError(f"下标越界：{self._where()}[{index}]，数组长度 {len(self._data)}")
        return wrap(self._data[index], (*self._path, paths.Index(index)))

    @staticmethod
    def _norm(key: str) -> str:
        return key

    def _where(self) -> str:
        return paths.format_path(self._path)

    def _keys(self) -> list[str]:
        if not isinstance(self._data, dict):
            raise ExprEvalError(f"keys() 只接受对象，{self._where()} 是数组")
        return list(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self) -> Iterator[Any]:
        if isinstance(self._data, dict):
            return iter(list(self._data))
        return (wrap(v, (*self._path, paths.Index(i))) for i, v in enumerate(self._data))

    def __contains__(self, item: Any) -> bool:
        return contains(item, self)

    def __eq__(self, other: object) -> bool:
        return json_equal(self, other)

    def _no_order(self, other: object) -> bool:
        raise ExprEvalError(f"{self._where()} 是{type_name(self)}，不能比较大小")

    __lt__ = __le__ = __gt__ = __ge__ = _no_order

    def __repr__(self) -> str:
        return f"<JSON {type_name(self)} {self._where()}>"


class HeadersView(JsonView):
    """响应头视图，key 不区分大小写；keys() 和迭代得到小写的名字。"""

    __slots__ = ()

    def __init__(self, headers: Mapping[str, str]):
        super().__init__({k.lower(): v for k, v in headers.items()}, (paths.Key("headers"),))

    @staticmethod
    def _norm(key: str) -> str:
        return key.lower()


class SideView:
    """asserts 里的 base / cand：只有 status、headers、body 三个属性。"""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: Mapping[str, str], body: Any):
        self.status = status
        self.headers = HeadersView(headers)
        self.body = wrap(body)


def wrap(value: Any, path: paths.Path = ()) -> Any:
    return JsonView(value, path) if isinstance(value, (dict, list)) else value


def unwrap(value: Any) -> Any:
    return value._data if isinstance(value, JsonView) else value


# ---------- 按 JSON 类型的相等与包含 ----------


def _is_array(x: Any) -> bool:
    return isinstance(x, (list, tuple)) or (isinstance(x, JsonView) and isinstance(x._data, list))


def _is_nan(x: Any) -> bool:
    return isinstance(x, Decimal) and x.is_nan()


def _sequence_equal(a: list | tuple, b: list | tuple) -> bool:
    return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))


def _object_equal(a: dict, b: dict) -> bool:
    return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)


_CONTAINER_EQUAL: tuple[tuple[type | tuple[type, ...], Callable[[Any, Any], bool]], ...] = (
    ((list, tuple), _sequence_equal),
    (dict, _object_equal),
)


def json_equal(a: Any, b: Any) -> bool:
    """JSON 语义的相等：布尔值不是数字（false != 0）；数组（含元组）逐个元素比较；对象按 key 比较。"""
    a, b = unwrap(a), unwrap(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    for kinds, equal in _CONTAINER_EQUAL:
        if isinstance(a, kinds) and isinstance(b, kinds):
            return equal(a, b)
    return a == b


def contains(item: Any, container: Any) -> bool:
    """`in`：对象判断 key，数组和集合判断元素（JSON 相等），字符串判断子串。"""
    if isinstance(container, JsonView) and isinstance(container._data, dict):
        return isinstance(item, str) and container._norm(item) in container._data
    if isinstance(container, str):
        if not isinstance(item, str):
            raise ExprEvalError(f"in 的右边是字符串时，左边也必须是字符串，这里是{type_name(item)}")
        return item in container
    if _is_array(container) or isinstance(container, (set, frozenset)):
        return any(json_equal(item, e) for e in unwrap(container))
    raise ExprEvalError(f"in 的右边必须是对象、数组或字符串，这里是{type_name(container)}")


# ---------- 内置函数 ----------


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float, Decimal)) and not isinstance(x, bool)


def _is_string(x: Any) -> bool:
    return isinstance(x, str)


def _scale(x: Any) -> int:
    if not _is_number(x):
        raise ExprEvalError("scale() 只接受数字")
    if not isinstance(x, Decimal):
        return 0
    exponent = x.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ExprEvalError("scale() 不接受 NaN / Infinity")
    return max(0, -exponent)


@lru_cache(maxsize=1024)
def literal_path(text: str) -> tuple[str | int, ...]:
    """get / exists 的路径：规则路径语法里的 key 和字面下标，不能有通配。"""
    tokens = paths.parse(text)
    out: list[str | int] = []
    for tok in tokens:
        if isinstance(tok, paths.Key):
            out.append(tok.name)
        elif isinstance(tok, paths.Index):
            out.append(tok.value)
        else:
            raise ExprEvalError(f"get/exists 的路径不能用 [*] 或 **：{text!r}")
    return tuple(out)


_MISSING = object()


def _step(obj: Any, step: str | int) -> Any:
    if isinstance(obj, SideView):
        return getattr(obj, step) if step in SIDE_ATTRS else _MISSING
    if isinstance(obj, (list, tuple)):
        obj = JsonView(list(obj))  # 列表字面量、keys() 的结果也按数组取值
    if not isinstance(obj, JsonView):
        return _MISSING
    try:
        return obj[step]
    except ExprEvalError:
        return _MISSING


def _lookup(obj: Any, path: str) -> Any:
    if not isinstance(path, str):
        raise ExprEvalError("get/exists 的第二个参数必须是路径字符串")
    for step in literal_path(path):
        obj = _step(obj, step)
        if obj is _MISSING:
            break
    return obj


def _get(obj: Any, path: str, default: Any = None) -> Any:
    found = _lookup(obj, path)
    return default if found is _MISSING else found


def _exists(obj: Any, path: str) -> bool:
    return _lookup(obj, path) is not _MISSING


def _keys(obj: Any) -> list[str]:
    if not isinstance(obj, JsonView):
        raise ExprEvalError(f"keys() 只接受对象，这里是{type_name(obj)}")
    return obj._keys()


_SIZED = (JsonView, str, list, tuple, set, frozenset)
ITERABLE = (*_SIZED, types.GeneratorType)


def _len(x: Any) -> int:
    if not isinstance(x, _SIZED):
        raise ExprEvalError(f"len() 只能用于对象、数组或字符串，这里是{type_name(x)}")
    return len(x)


def _items(name: str, x: Any) -> Any:
    if not isinstance(x, ITERABLE):
        raise ExprEvalError(f"{name}() 需要数组或生成器，这里是{type_name(x)}")
    return x


def _all(x: Any) -> bool:
    return all(_items("all", x))


def _any(x: Any) -> bool:
    return any(_items("any", x))


def _set(x: Any = ()) -> set:
    items = list(_items("set", x))
    if any(isinstance(i, (JsonView, list, tuple, dict)) for i in items):
        raise ExprEvalError("set() 的元素不能是对象或数组")
    if any(isinstance(i, bool) for i in items) and any(_is_number(i) for i in items):
        raise ExprEvalError("set() 里不能同时有布尔值和数字")
    return set(items)


def _abs(x: Any) -> Any:
    if not _is_number(x):
        raise ExprEvalError(f"abs() 只能用于数字，这里是{type_name(x)}")
    return abs(x)


def _extreme(name: str, pick: Callable[[list], Any]) -> Callable[..., Any]:
    """min / max：一个参数时取其中的元素；元素必须全是数字或全是字符串，不能为空、不能有 NaN。"""

    def run(*args: Any) -> Any:
        items = list(_items(name, args[0])) if len(args) == 1 else list(args)
        if not items:
            raise ExprEvalError(f"{name}() 的参数为空")
        if not (all(_is_number(i) for i in items) or all(isinstance(i, str) for i in items)):
            raise ExprEvalError(f"{name}() 只能比较全是数字或全是字符串的值")
        if any(_is_nan(i) for i in items):
            raise ExprEvalError("NaN 不能比较大小")
        return pick(items)

    return run


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "len": _len,
    "scale": _scale,
    "get": _get,
    "exists": _exists,
    "is_number": _is_number,
    "is_string": _is_string,
    "keys": _keys,
    "set": _set,
    "all": _all,
    "any": _any,
    "abs": _abs,
    "min": _extreme("min", min),
    "max": _extreme("max", max),
}
# ---------- 运算符 ----------


def _require_numbers(symbol: str, a: Any, b: Any) -> None:
    if not (_is_number(a) and _is_number(b)):
        raise ExprEvalError(f"{symbol} 只能用于数字，这里是{type_name(a)}和{type_name(b)}")


def _require_nonzero(b: Any) -> None:
    if b == 0:
        raise ExprEvalError("除数为 0")


def _mul(a: Any, b: Any) -> Any:
    _require_numbers("*", a, b)
    if isinstance(a, int) and isinstance(b, int) and a.bit_length() + b.bit_length() > MAX_INT_BITS:
        raise ExprEvalError("整数乘积过大")
    return a * b


def _truediv(a: Any, b: Any) -> Decimal:
    """整数相除也按 Decimal 精确计算：1 / 10 == 0.1。"""
    _require_numbers("/", a, b)
    _require_nonzero(b)
    return Decimal(a) / Decimal(b)


def _decimal_op(symbol: str, fn: Callable[[Decimal, Decimal], Decimal]) -> Callable[[Any, Any], Any]:
    """// 和 % 统一按 Decimal 语义（向零取整），两边都是整数时结果仍是整数。"""

    def op(a: Any, b: Any) -> Any:
        _require_numbers(symbol, a, b)
        _require_nonzero(b)
        result = fn(Decimal(a), Decimal(b))
        return int(result) if isinstance(a, int) and isinstance(b, int) else result

    return op


def _add(a: Any, b: Any) -> Any:
    """数字相加、字符串拼接、数组拼接；布尔值不算数字。"""
    if _is_number(a) and _is_number(b):
        return a + b
    both_strings = isinstance(a, str) and isinstance(b, str)
    if not (both_strings or (_is_array(a) and _is_array(b))):
        raise ExprEvalError(f"+ 不能用于{type_name(a)}和{type_name(b)}")
    if len(a) + len(b) > MAX_SEQUENCE_LENGTH:
        raise ExprEvalError(f"拼接结果超过 {MAX_SEQUENCE_LENGTH} 个元素")
    return a + b if both_strings else wrap(list(unwrap(a)) + list(unwrap(b)))


def _sub(a: Any, b: Any) -> Any:
    """数字相减，或集合求差。"""
    if isinstance(a, (set, frozenset)) and isinstance(b, (set, frozenset)):
        return a - b
    _require_numbers("-", a, b)
    return a - b


def _unary(symbol: str, fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    def op(a: Any) -> Any:
        if not _is_number(a):
            raise ExprEvalError(f"{symbol} 只能用于数字，这里是{type_name(a)}")
        return fn(a)

    return op


def _ordering(symbol: str, fn: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
    """大小比较只在两个数字或两个字符串之间进行；布尔值、NaN 都不行。"""

    def op(a: Any, b: Any) -> bool:
        if not (isinstance(a, str) and isinstance(b, str)):
            if not (_is_number(a) and _is_number(b)):
                raise ExprEvalError(f"{symbol} 只能比较两个数字或两个字符串，这里是{type_name(a)}和{type_name(b)}")
            if _is_nan(a) or _is_nan(b):
                raise ExprEvalError("NaN 不能比较大小")
        return fn(a, b)

    return op


OPERATORS: dict[type, Callable[..., Any]] = {
    ast.Add: _add,
    ast.Sub: _sub,
    ast.Mult: _mul,
    ast.Div: _truediv,
    ast.FloorDiv: _decimal_op("//", operator.floordiv),
    ast.Mod: _decimal_op("%", operator.mod),
    ast.USub: _unary("-", operator.neg),
    ast.UAdd: _unary("+", operator.pos),
    ast.Not: operator.not_,
    ast.Eq: json_equal,
    ast.NotEq: lambda a, b: not json_equal(a, b),
    ast.Lt: _ordering("<", operator.lt),
    ast.LtE: _ordering("<=", operator.le),
    ast.Gt: _ordering(">", operator.gt),
    ast.GtE: _ordering(">=", operator.ge),
    ast.In: contains,
    ast.NotIn: lambda a, b: not contains(a, b),
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}
OPERATOR_SYMBOLS: dict[type, str] = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//", ast.Mod: "%",
    ast.USub: "-", ast.UAdd: "+", ast.Not: "not", ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<",
    ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.In: "in", ast.NotIn: "not in", ast.Is: "is",
    ast.IsNot: "is not",
}  # fmt: skip
