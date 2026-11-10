"""AC-36 中能在 pytest 里检查的部分；radon 和 lint-imports 在 CI 里直接执行。"""

from pathlib import Path

SRC = Path(__file__).parent.parent / "src"


def test_no_source_file_over_1000_lines():
    too_long = {
        str(f.relative_to(SRC)): n
        for f in SRC.rglob("*.py")
        if (n := len(f.read_text(encoding="utf-8").splitlines())) > 1000
    }
    assert too_long == {}
