import pytest

from api_regress.core import paths
from api_regress.core.paths import ANY, DEEP, Index, Key, KeyedElem, PathSyntaxError


def p(text):
    return paths.parse(text)


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("data.items", (Key("data"), Key("items"))),
        ("data.items[0]", (Key("data"), Key("items"), Index(0))),
        ("data.items[*].price", (Key("data"), Key("items"), ANY, Key("price"))),
        ('meta["x.y"]', (Key("meta"), Key("x.y"))),
        ('meta["a[0]"]', (Key("meta"), Key("a[0]"))),
        ('meta["say \\"hi\\""]', (Key("meta"), Key('say "hi"'))),
        ('meta["back\\\\slash"]', (Key("meta"), Key("back\\slash"))),
        ('["x.y"].z', (Key("x.y"), Key("z"))),
        ("[*].id", (ANY, Key("id"))),
        ("**.updated_at", (DEEP, Key("updated_at"))),
        ("data.**.id", (Key("data"), DEEP, Key("id"))),
        ("**[*].id", (DEEP, ANY, Key("id"))),
        ("a-b.c_d.123", (Key("a-b"), Key("c_d"), Key("123"))),
        ('a[""]', (Key("a"), Key(""))),
    ],
)
def test_parse(text, tokens):
    assert p(text) == tokens


@pytest.mark.parametrize(
    "text",
    ["", ".a", "a.", "a..b", "a[", "a[x]", "a[-1]", 'a["x]', "a b", "**", "a.**", "**.**.a", "a*", "a]b", "$.a"],
)
def test_parse_errors(text):
    with pytest.raises(PathSyntaxError):
        p(text)


@pytest.mark.parametrize(
    "text",
    ["data.items[*].price", 'meta["x.y"].z', '["a\\"b"]', "**.x", "a[3].b", 'meta["**"]', 'k[""]'],
)
def test_format_round_trip(text):
    assert paths.format_path(p(text)) == text
    assert p(paths.format_path(p(text))) == p(text)


def test_format_keyed_and_root():
    assert paths.format_path((Key("data"), Key("items"), KeyedElem("sku_id=A12"), Key("price"))) == (
        "data.items[sku_id=A12].price"
    )
    assert paths.format_path(()) == "$"


def test_generalize():
    concrete = (Key("data"), Key("items"), Index(3), Key("tags"), KeyedElem("id=1"), Key("x"))
    assert paths.format_path(paths.generalize(concrete)) == "data.items[*].tags[*].x"


@pytest.mark.parametrize(
    ("pattern", "concrete", "expected"),
    [
        ("**.updated_at", (Key("updated_at"),), True),  # ** 包括 0 层
        ("**.updated_at", (Key("a"), Index(1), Key("updated_at")), True),
        ("**.updated_at", (Key("updated_at"), Key("x")), False),
        ("data.items[*].price", (Key("data"), Key("items"), Index(2), Key("price")), True),
        ("data.items[*].price", (Key("data"), Key("items"), ANY, Key("price")), True),
        ("data.items[*].price", (Key("data"), Key("items"), KeyedElem("id=1"), Key("price")), True),
        ("data.items[0].price", (Key("data"), Key("items"), Index(0), Key("price")), True),
        ("data.items[0].price", (Key("data"), Key("items"), Index(1), Key("price")), False),
        # 字面下标匹配不到 schema / keyed 数组的元素
        ("data.items[0].price", (Key("data"), Key("items"), ANY, Key("price")), False),
        ("data.items[0].price", (Key("data"), Key("items"), KeyedElem("id=0"), Key("price")), False),
        ('meta["x.y"]', (Key("meta"), Key("x.y")), True),
        ('meta["x.y"]', (Key("meta"), Key("x"), Key("y")), False),
        ("data.items", (Key("data"), Key("items"), Index(0)), False),
        ("data.**.id", (Key("data"), Key("id")), True),
        ("data.**.id", (Key("x"), Key("data"), Key("id")), False),
        ("[*]", (Key("a"),), False),
        ("a", (), False),
    ],
)
def test_match(pattern, concrete, expected):
    assert paths.match(p(pattern), concrete) is expected


@pytest.mark.parametrize(
    ("general", "specific", "expected"),
    [
        ("**.x", "data.x", True),
        ("**.x", "**.x", True),
        ("data.x", "**.x", False),
        ("a[*]", "a[0]", True),
        ("a[0]", "a[*]", False),
        ("**.b", "a.**.b", True),
        ("a.**.b", "**.b", False),
    ],
)
def test_covers(general, specific, expected):
    assert paths.covers(p(general), p(specific)) is expected


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("**.x", "data.x", True),
        ("**.x", "**.y", False),
        ("a[0]", "a[*]", True),
        ("a[0]", "a[1]", False),
        ("a.b", "a.c", False),
        ("**.items", "data.**.items", True),
        ("a.**.c", "**.b.c", True),
        ("a[*]", "a.b", False),
    ],
)
def test_overlaps(a, b, expected):
    assert paths.overlaps(p(a), p(b)) is expected
    assert paths.overlaps(p(b), p(a)) is expected


def test_specificity_order():
    exact, star, deep = (paths.specificity(p(t)) for t in ("data.amount", "data[*].amount", "**.amount"))
    assert exact > star > deep
    assert paths.specificity(p("a.b.c")) > paths.specificity(p("a.b"))
    assert paths.specificity(p("**.a.b")) > paths.specificity(p("**.b"))
    # 同时含 [*] 和 ** 按 ** 算
    assert paths.specificity(p("a[*].b.c.d")) > paths.specificity(p("**.a[*].b.c.d"))


def test_root_path():
    assert p("$") == ()
    assert paths.match(p("$"), ())
    assert not paths.match(p("$"), (Key("a"),))
    assert paths.format_path(paths.generalize(())) == "$"


@pytest.mark.parametrize("name", ["$schema", "$", "$id"])
def test_dollar_keys_round_trip(name):
    for tokens in [(Key(name),), (Key(name), Key("x")), (Key("a"), Key(name))]:
        text = paths.format_path(tokens)
        assert p(text) == tokens
    assert paths.format_path((Key(name),)).startswith('["$')


def test_parse_is_cached():
    assert p("data.items[*].price") is p("data.items[*].price")
