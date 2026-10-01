"""Group positioned words into reading-order lines and column cells."""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from .documents import Word


@dataclass
class Cell:
    text: str
    word_ids: list[int]
    x0: float
    x1: float


@dataclass
class Line:
    page: int
    word_ids: list[int]
    y0: float
    y1: float
    cells: list[Cell] = field(default_factory=list)
    text: str = ""

    @property
    def lower(self) -> str:
        return self.text.lower()


def build_lines(words: Sequence["Word"]) -> list[Line]:
    lines: list[Line] = []
    pages = sorted({w.page for w in words})
    for page in pages:
        ids = [i for i, w in enumerate(words) if w.page == page]
        if not ids:
            continue
        heights = [words[i].y1 - words[i].y0 for i in ids]
        median_h = statistics.median(heights) or 0.01
        ids.sort(key=lambda i: ((words[i].y0 + words[i].y1) / 2, words[i].x0))

        current: list[int] = []
        current_center = None
        page_lines: list[list[int]] = []
        for i in ids:
            center = (words[i].y0 + words[i].y1) / 2
            if current and abs(center - current_center) > median_h * 0.55:
                page_lines.append(current)
                current = []
            current.append(i)
            centers = [(words[j].y0 + words[j].y1) / 2 for j in current]
            current_center = statistics.fmean(centers)
        if current:
            page_lines.append(current)

        for group in page_lines:
            group.sort(key=lambda i: words[i].x0)
            line = Line(page=page, word_ids=group,
                        y0=min(words[i].y0 for i in group), y1=max(words[i].y1 for i in group))
            # Split into cells wherever the horizontal gap is wider than ~2 character heights.
            gap_limit = median_h * 1.9
            cell_ids: list[int] = [group[0]]
            for prev, nxt in zip(group, group[1:]):
                if words[nxt].x0 - words[prev].x1 > gap_limit:
                    line.cells.append(_cell(words, cell_ids))
                    cell_ids = []
                cell_ids.append(nxt)
            line.cells.append(_cell(words, cell_ids))
            line.text = " ".join(words[i].text for i in group)
            lines.append(line)
    return lines


def _cell(words: Sequence["Word"], ids: list[int]) -> Cell:
    return Cell(text=" ".join(words[i].text for i in ids), word_ids=list(ids),
                x0=min(words[i].x0 for i in ids), x1=max(words[i].x1 for i in ids))


def bbox_of(words: Sequence["Word"], ids: Sequence[int]) -> dict | None:
    ids = [i for i in ids if 0 <= i < len(words)]
    if not ids:
        return None
    return {
        "page": words[ids[0]].page,
        "box": [round(min(words[i].x0 for i in ids), 4), round(min(words[i].y0 for i in ids), 4),
                round(max(words[i].x1 for i in ids), 4), round(max(words[i].y1 for i in ids), 4)],
    }


def conf_of(words: Sequence["Word"], ids: Sequence[int]) -> float:
    ids = [i for i in ids if 0 <= i < len(words)]
    if not ids:
        return 0.0
    return statistics.fmean(words[i].conf for i in ids)
