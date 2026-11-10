"""对比行为的快照：doc/dev/diff-combinations.md 必须和引擎当前的实际输出一致。

失败说明对比行为变了。确认变化符合 spec 后运行
    uv run python scripts/gen_diff_combinations.py
并在 review 时逐格检查 git diff。"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "gen_diff_combinations.py"


def _load():
    spec = importlib.util.spec_from_file_location("gen_diff_combinations", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_combination_doc_is_up_to_date():
    module = _load()
    expected = module.render()
    actual = module.OUTPUT.read_text(encoding="utf-8")
    assert actual == expected, "对比行为变了：确认符合 spec 后运行 uv run python scripts/gen_diff_combinations.py"
