"""用 deepdiff 交叉校验：positional 数组 + compare: values 下，两者报出的路径集合应该一致。

deepdiff 识别不了 1.0 和 1.00 的小数位差异，所以这里只用整数、字符串、布尔和 null。
deepdiff 把「非空容器变成空容器」整体记成一次值变化，和逐个字段的报法不可比，所以生成的容器都不为空。"""

import json
import random

import pytest
from deepdiff import DeepDiff

from api_regress.core import paths
from api_regress.core.config import Suite
from api_regress.core.diff import diff_json
from api_regress.core.rules import RuleSet

TARGETS = {"baseline": {"base_url": "http://b"}, "candidate": {"base_url": "http://c"}}
KEYS = "abcd"
SCALARS = [0, 1, 2, "x", "y", True, False, None]
DEEPDIFF_KINDS = (
    "values_changed",
    "type_changes",
    "dictionary_item_added",
    "dictionary_item_removed",
    "iterable_item_added",
    "iterable_item_removed",
)


def gen(rng, depth):
    roll = rng.random()
    if depth <= 0 or roll < 0.4:
        return rng.choice(SCALARS)
    if roll < 0.7:
        return {k: gen(rng, depth - 1) for k in rng.sample(KEYS, rng.randint(1, 3))}
    return [gen(rng, depth - 1) for _ in range(rng.randint(1, 3))]


def mutate(rng, value, depth=0):
    if rng.random() < 0.15:
        return gen(rng, 2 - min(depth, 2))
    if isinstance(value, dict):
        out = {k: mutate(rng, v, depth + 1) for k, v in value.items() if rng.random() > 0.15}
        if not out or rng.random() < 0.2:
            out[rng.choice(KEYS)] = gen(rng, 1)
        return out
    if isinstance(value, list):
        out = [mutate(rng, v, depth + 1) for v in value if rng.random() > 0.1]
        if not out or rng.random() < 0.2:
            out.append(gen(rng, 1))
        return out
    return value


def array_patterns(value, path=()):
    """把两侧出现过的所有数组都设成 positional。"""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from array_patterns(v, (*path, paths.Key(k)))
    elif isinstance(value, list):
        yield paths.format_path(paths.generalize(path))
        for v in value:
            yield from array_patterns(v, (*path, paths.ANY))


def to_deepdiff_path(tokens):
    parts = []
    for tok in tokens:
        parts.append(f"['{tok.name}']" if isinstance(tok, paths.Key) else f"[{tok.value}]")
    return "root" + "".join(parts)


def ours(base, cand):
    patterns = sorted(set(array_patterns(base)) | set(array_patterns(cand)))
    rules = [{"path": p, "array": {"mode": "positional"}} for p in patterns]
    suite = Suite.model_validate(
        {
            "version": 1,
            "targets": TARGETS,
            "defaults": {"compare": "values"},
            "requests": [{"name": "r", "method": "GET", "path": "/", "rules": rules}],
        }
    )
    result = diff_json(base, cand, RuleSet.for_request(suite, suite.requests[0], 0), "values")
    return {
        to_deepdiff_path(paths.parse(f.path) if f.path != "$" else ())
        for f in result.findings
        if f.type != "array_length_changed"
    }


def theirs(base, cand):
    diff = DeepDiff(base, cand, zip_ordered_iterables=True, threshold_to_diff_deeper=0)
    return {p for kind in DEEPDIFF_KINDS for p in diff.get(kind, {})}


@pytest.mark.parametrize("seed", range(300))
def test_matches_deepdiff(seed):
    rng = random.Random(seed)
    base = gen(rng, 4)
    cand = mutate(rng, base)
    assert ours(base, cand) == theirs(base, cand), json.dumps([base, cand])
