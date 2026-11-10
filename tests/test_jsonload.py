from decimal import Decimal

import pytest

from api_regress.core.diff.values import scale
from api_regress.core.jsonload import MAX_DEPTH, charset_of, is_json_media, load_body, parse_json


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1.10", 2), ("1.1", 1), ("1", 0), ("1.50E1", 1), ("1E2", 0), ("-0.00", 2), ("0.5e-3", 4), ("-7", 0)],
)
def test_scale_from_raw_text(text, expected):
    """spec §7.5 的小数位表（AC-07）。"""
    value, _ = parse_json(text)
    assert scale(value) == expected


def test_numbers_keep_int_and_decimal():
    value, _ = parse_json('{"i": 1, "f": 1.0}')
    assert type(value["i"]) is int
    assert value["f"] == Decimal("1.0") and str(value["f"]) == "1.0"


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("application/json", True),
        ("Application/JSON; charset=utf-8", True),
        ("application/problem+json", True),
        ("application/vnd.api+json;v=2", True),
        ("text/html", False),
        ("text/json+html", False),
        ("model/gltf+json", True),
        ("text/vnd.foo+json", True),
        ("application/+json", False),
        ("+json", False),
        (None, None),
        ("", None),
    ],
)
def test_is_json_media(content_type, expected):
    assert is_json_media(content_type) is expected


def test_charset():
    assert charset_of("application/json; charset=GBK") == "gbk"
    assert charset_of('application/json; charset="latin-1"') == "iso8859-1"
    assert charset_of("application/json; charset=nope") == "utf-8"
    assert charset_of(None) == "utf-8"


def test_decode_by_charset_and_strip_bom():
    body = '{"名": "值"}'.encode("gbk")
    assert load_body(body, "application/json; charset=gbk").value == {"名": "值"}
    assert load_body(b'\xef\xbb\xbf{"a": 1}', "application/json").value == {"a": 1}
    assert load_body('﻿{"a": 1}', "application/json").value == {"a": 1}


def test_duplicate_key_takes_last_and_warns():
    loaded = load_body('{"a": 1, "b": {"a": 0, "a": 3}, "a": 2}', "application/json", request="r", side="candidate")
    assert loaded.value == {"a": 2, "b": {"a": 3}}
    assert [w.code for w in loaded.warnings] == ["duplicate_key"]
    assert loaded.warnings[0].request == "r" and "candidate" in loaded.warnings[0].message


def test_nan_and_infinity_warn():
    loaded = load_body('[NaN, Infinity, -Infinity, NaN]', "application/json")
    assert loaded.is_json
    assert [str(v) for v in loaded.value] == ["NaN", "Infinity", "-Infinity", "NaN"]
    assert [w.code for w in loaded.warnings] == ["nan_or_infinity"]


@pytest.mark.parametrize(
    ("body", "content_type", "is_json", "codes"),
    [
        ("{bad", "application/json", False, ["json_parse_failed"]),
        (b"\xff\xfe{", "application/json", False, ["json_parse_failed"]),
        ("", "application/json", False, ["json_parse_failed"]),
        ("{bad", None, False, []),  # 没有 Content-Type：尝试一次，失败不提示
        ('{"a": 1}', None, True, []),
        ('{"a": 1}', "text/html", False, []),  # 声明为其他类型时不解析
    ],
)
def test_json_detection(body, content_type, is_json, codes):
    loaded = load_body(body, content_type)
    assert loaded.is_json is is_json
    assert [w.code for w in loaded.warnings] == codes


def test_too_deep_is_parse_failure():
    deep = "[" * (MAX_DEPTH + 1) + "]" * (MAX_DEPTH + 1)
    loaded = load_body(deep, "application/json")
    assert not loaded.is_json and loaded.warnings[0].code == "json_parse_failed"
    assert load_body("[" * MAX_DEPTH + "]" * MAX_DEPTH, "application/json").is_json
    # 只数对象和数组，最里面的标量不算一层
    assert load_body("[" * MAX_DEPTH + "1" + "]" * MAX_DEPTH, "application/json").is_json
    assert load_body('{"a":' * MAX_DEPTH + "1" + "}" * MAX_DEPTH, "application/json").is_json


@pytest.mark.parametrize("charset", ["hex", "base64", "rot13", "zlib"])
def test_non_text_charset_falls_back_to_utf8(charset):
    assert load_body(b'{"a": 1}', f"application/json; charset={charset}").value == {"a": 1}


@pytest.mark.parametrize("content_type", ["application/json", None])
def test_number_out_of_range(content_type):
    loaded = load_body('{"a": 1e9999999999999999999}', content_type)
    assert not loaded.is_json
    if content_type:
        assert "数字超出范围" in loaded.warnings[0].message


def test_very_long_integer():
    value, _ = parse_json("1" * 5000)
    assert value == Decimal("1" * 5000) and scale(value) == 0


def test_lone_surrogates_replaced():
    loaded = load_body('{"\\ud800": "x\\ud83d", "ok": "\\ud83d\\ude00"}', "application/json")
    assert loaded.value == {"\ufffd": "x\ufffd", "ok": "\U0001f600"}
    from api_regress.core.models import Finding

    Finding(type="value_changed", level="info", path="$", generalized_path="$", base_value=loaded.value).model_dump_json()


def test_parse_failure_message_does_not_echo_body():
    loaded = load_body('{"token": "SENTINEL-123", bad}', "application/json")
    assert "SENTINEL" not in loaded.warnings[0].message
