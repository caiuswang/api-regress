"""两侧文本的并排对照，输出 HTML 表格（内容已转义）。

行级对齐用 `difflib.SequenceMatcher`，只保留有差异的行和上下文。行内高亮只在被替换的一段里
按顺序一对一配对（git、diff2html 的做法），不像 `difflib.HtmlDiff` 那样在整段里两两比较找最相似的行：
那一步是 O(n·m)，上千行都变了时要好几秒。超过 `INTRALINE_MAX_CHARS` 的行不做行内高亮，整行标色。"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from markupsafe import Markup, escape

CONTEXT = 3
INTRALINE_MAX_CHARS = 200
_EMPTY = Markup("")


@dataclass(frozen=True)
class Cell:
    lineno: int | None  # None 表示这一侧没有对应的行
    html: Markup
    kind: str  # "" 未变；"sub" 删除或被替换；"add" 新增或替换后


Row = tuple[Cell, Cell]
_BLANK = Cell(None, _EMPTY, "")


def _highlight(text: str, spans: list[tuple[int, int]]) -> Markup:
    parts, pos = [], 0
    for start, end in spans:
        if start == end:  # 另一侧的插入或删除，这一侧没有字符
            continue
        parts += [escape(text[pos:start]), Markup('<span class="hl">'), escape(text[start:end]), Markup("</span>")]
        pos = end
    parts.append(escape(text[pos:]))
    return Markup("").join(parts)


def _intraline(base: str, cand: str) -> tuple[Markup, Markup]:
    if len(base) > INTRALINE_MAX_CHARS or len(cand) > INTRALINE_MAX_CHARS:
        return escape(base), escape(cand)
    ops = [op for op in difflib.SequenceMatcher(None, base, cand, autojunk=False).get_opcodes() if op[0] != "equal"]
    return _highlight(base, [(i1, i2) for _, i1, i2, _, _ in ops]), _highlight(cand, [(j1, j2) for *_, j1, j2 in ops])


def _equal(base: list[str], cand: list[str], i1: int, i2: int, j1: int, j2: int) -> Iterator[Row]:
    for i, j in zip(range(i1, i2), range(j1, j2), strict=True):
        yield Cell(i + 1, escape(base[i]), ""), Cell(j + 1, escape(cand[j]), "")


def _delete(base: list[str], cand: list[str], i1: int, i2: int, j1: int, j2: int) -> Iterator[Row]:
    for i in range(i1, i2):
        yield Cell(i + 1, escape(base[i]), "sub"), _BLANK


def _insert(base: list[str], cand: list[str], i1: int, i2: int, j1: int, j2: int) -> Iterator[Row]:
    for j in range(j1, j2):
        yield _BLANK, Cell(j + 1, escape(cand[j]), "add")


def _replace(base: list[str], cand: list[str], i1: int, i2: int, j1: int, j2: int) -> Iterator[Row]:
    paired = min(i2 - i1, j2 - j1)
    for k in range(paired):
        left, right = _intraline(base[i1 + k], cand[j1 + k])
        yield Cell(i1 + k + 1, left, "sub"), Cell(j1 + k + 1, right, "add")
    yield from _delete(base, cand, i1 + paired, i2, j1, j2)
    yield from _insert(base, cand, i1, i2, j1 + paired, j2)


_OPS: dict[str, Callable[[list[str], list[str], int, int, int, int], Iterator[Row]]] = {
    "equal": _equal, "delete": _delete, "insert": _insert, "replace": _replace,
}  # fmt: skip


def row_groups(base: list[str], cand: list[str]) -> list[list[Row]]:
    """有差异的各段（含上下 `CONTEXT` 行）；两侧完全相同时返回空列表。"""
    if base == cand:
        return []
    matcher = difflib.SequenceMatcher(None, base, cand)
    return [
        [row for tag, *span in group for row in _OPS[tag](base, cand, *span)]
        for group in matcher.get_grouped_opcodes(CONTEXT)
    ]


def _cells(cell: Cell) -> Markup:
    lineno = "" if cell.lineno is None else cell.lineno
    cls = f' class="{cell.kind}"' if cell.kind else ""
    return Markup('<td class="no">{}</td><td{}>{}</td>').format(lineno, Markup(cls), cell.html)


def table(base: list[str], cand: list[str], base_title: str, cand_title: str) -> Markup:
    """并排表格：左基线、右候选；各段之间用一行省略号隔开。"""
    head = Markup('<tr><th colspan="2">{}</th><th colspan="2">{}</th></tr>').format(base_title, cand_title)
    rows = [head]
    for index, group in enumerate(row_groups(base, cand)):
        if index:
            rows.append(Markup('<tr class="skip"><td colspan="4">⋯</td></tr>'))
        rows += [Markup("<tr>{}{}</tr>").format(_cells(left), _cells(right)) for left, right in group]
    return Markup('<table class="diff">\n{}\n</table>').format(Markup("\n").join(rows))
