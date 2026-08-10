"""Terminal chart builders for devtools visualization.

Portable visualization infrastructure: horizontal strip charts, plotext
scatter plots, 256-color palettes, and watch-mode utilities. Import these
builders from devtools commands that need visualization.
"""

import math
import select
import statistics
import sys
import termios
import tty
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import BaseModel, Field


# ── color palette ─────────────────────────────────────────


SATURATED_RING: list[int] = [
    196,
    202,
    208,
    214,
    220,
    226,
    190,
    154,
    118,
    82,
    46,
    47,
    48,
    49,
    50,
    51,
    45,
    39,
    33,
    27,
    21,
    57,
    93,
    129,
    165,
    201,
    200,
    199,
    198,
    197,
]
"""30-entry ring of maximally-spaced 256-color codes for category coloring."""


def pick_colors(n: int) -> list[int]:
    """Pick *n* maximally-spaced colors from the saturated 256-color ring."""
    if n <= 0:
        return []
    if n >= len(SATURATED_RING):
        return list(SATURATED_RING[:n])
    step = len(SATURATED_RING) / n
    return [SATURATED_RING[int(i * step) % len(SATURATED_RING)] for i in range(n)]


# ── chart vocabulary ──────────────────────────────────────


type GroupOrder = Callable[[str], tuple[int, ...]]
"""Orders one group or category by name; builders sort lexicographically without."""


class ScatterPoint(BaseModel):
    """One plotted point: where it sits, what it belongs to, what it came from."""

    x: float = Field(description="Days after the chart epoch")
    y: float = Field(description="The plotted value")
    category: str = Field(description="Category whose color this point takes")
    item_id: int = Field(description="Identity of the record behind the point")


class ChartCategory(BaseModel):
    """One category of a chart panel: how it draws, and how much of it shows."""

    name: str = Field(description="Category name, as the legend spells it")
    color: int = Field(default=7, description="256-color code drawing this category")
    count: int = Field(default=0, description="Points visible for this category")
    total: int = Field(default=0, description="Points the category has in all")


class StripGroup(BaseModel):
    """One row of a strip chart: a group's values, and how the row is labelled.

    The row answers its own statistics, so a caller assembles what it measured
    and never a second list running parallel to it.
    """

    name: str = Field(description="Group name, leading the row label")
    values: list[float] = Field(
        default_factory=list, description="Values plotted as dots along the row"
    )
    label: str = Field(default="", description="Display suffix — a date, a description")
    color: int | None = Field(
        default=None,
        description="256-color code; a position in the saturated ring when None",
    )
    total: int | None = Field(
        default=None,
        description="Population the values sample, labelling the row as n/total",
    )

    @property
    def count(self) -> int:
        """How many values the row plots."""
        return len(self.values)

    @property
    def mean(self) -> float:
        """Average of the plotted values, zero for an empty row."""
        return statistics.mean(self.values) if self.values else 0.0

    @property
    def stdev(self) -> float:
        """Sample deviation of the plotted values, zero below two of them."""
        return statistics.stdev(self.values) if len(self.values) > 1 else 0.0

    @property
    def lowest(self) -> float:
        """Smallest plotted value, zero for an empty row."""
        return min(self.values, default=0.0)

    @property
    def highest(self) -> float:
        """Largest plotted value, zero for an empty row."""
        return max(self.values, default=0.0)

    @property
    def caption(self) -> str:
        """The row's left-hand label: name, suffix, and how many of how many."""
        if self.total is not None:
            return f"{self.name} {self.label} {self.count}/{self.total}".strip()
        return f"{self.name} {self.label} (n={self.count:>2})".strip()


# ── strip chart ───────────────────────────────────────────


def build_strip_chart(
    groups: list[StripGroup],
    term_width: int,
    *,
    title: str = "Score by group (higher is better)",
    sort_key: GroupOrder | None = None,
) -> str:
    """Build a horizontal scatter strip chart with summary statistics.

    Each group gets a row showing individual data points as dots, with
    mean/std/min/max statistics.  Uses IQR-based axis clipping to handle
    outliers and shows a zero reference line when in range.

    Args:
        groups: One row per group, in any order. A group that carries a total
            but no values still gets a row, drawn empty.
        term_width: Terminal width in columns.
        title: Bold header line above the chart.
        sort_key: Row ordering over group names. Lexicographic if None.
    """
    key_fn: GroupOrder = sort_key or (lambda _: (0,))
    ordered = sorted(groups, key=lambda group: key_fn(group.name))
    ring = pick_colors(len(ordered))

    all_values = sorted(value for group in ordered for value in group.values)
    if not all_values:
        all_values = [0.0]
    n_values = len(all_values)
    q1 = all_values[n_values // 4]
    q3 = all_values[3 * n_values // 4]
    iqr = q3 - q1
    clip_lo = min(q1 - 2.0 * iqr, 0.0)
    clip_hi = max(q3 + 2.0 * iqr, 0.0)
    val_min = max(min(all_values), clip_lo)
    val_max = min(max(all_values), clip_hi)
    margin = max((val_max - val_min) * 0.03, 1.0)
    range_min = val_min - margin
    range_max = val_max + margin
    val_range = range_max - range_min

    label_width = max(len(group.caption) for group in ordered)
    value_width = 22
    chart_width = max(20, term_width - label_width - value_width - 4)

    def to_pos(value: float) -> int:
        return max(
            0,
            min(
                chart_width - 1,
                round((value - range_min) / val_range * (chart_width - 1)),
            ),
        )

    if range_max < 0:
        zero_pos = chart_width
    elif range_min > 0:
        zero_pos = -1
    else:
        zero_pos = to_pos(0.0)

    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    lines: list[str] = [f"{BOLD}{title}{RESET}", ""]

    for index, group in enumerate(ordered):
        label = group.caption.ljust(label_width)
        color = group.color if group.color is not None else ring[index % len(ring)]
        ansi = f"\033[38;5;{color}m"

        if group.count:
            counts = Counter(to_pos(value) for value in group.values)

            parts: list[str] = []
            for col in range(chart_width):
                count = counts[col]
                if count > 0:
                    char = "●" if count == 1 else str(min(count, 9))
                    parts.append(f"{ansi}{char}{RESET}")
                elif col == zero_pos:
                    parts.append(f"{DIM}│{RESET}")
                else:
                    parts.append(" ")
            row = "".join(parts)
            stats = (
                f"{group.mean:>5.1f} ±{group.stdev:>2.0f}  "
                f"{group.lowest:>3.0f}‥{group.highest:.0f}"
            )
            lines.append(f"  {ansi}{label}{RESET} {row}  {stats}")
        else:
            empty = list(" " * chart_width)
            if 0 <= zero_pos < chart_width:
                empty[zero_pos] = f"{DIM}│{RESET}"
            lines.append(f"  {ansi}{label}{RESET} {''.join(empty)}")

    scale_pad = " " * (label_width + 3)
    ruler = list("─" * chart_width)
    if 0 <= zero_pos < chart_width:
        ruler[zero_pos] = "┼"
    labels_row = [" "] * chart_width
    lo = f"{val_min:.0f}"
    for i, ch in enumerate(lo):
        if i < chart_width:
            labels_row[i] = ch
    hi = f"{val_max:.0f}"
    hi_start = chart_width - len(hi)
    for i, ch in enumerate(hi):
        if hi_start + i >= 0:
            labels_row[hi_start + i] = ch
    if 0 <= zero_pos < chart_width:
        z_start = max(0, zero_pos - 1)
        z_end = min(chart_width, zero_pos + 2)
        overlap = any(labels_row[j] != " " for j in range(z_start, z_end))
        if not overlap:
            labels_row[zero_pos] = "0"
    lines.append(f"{scale_pad}{DIM}{''.join(ruler)}{RESET}")
    lines.append(f"{scale_pad}{DIM}{''.join(labels_row)}{RESET}")

    lines.append(f"\n{DIM}● individual  2-9 overlapping  avg ±std  min‥max{RESET}")

    return "\n".join(lines)


# ── scatter chart ─────────────────────────────────────────


def build_scatter(
    points: list[ScatterPoint],
    title_label: str,
    ylabel: str,
    term_width: int,
    chart_height: int,
    categories: list[ChartCategory],
    epoch: datetime,
) -> str:
    """Build a scatter chart panel with IQR-based y-axis clipping.

    Uses plotext for terminal rendering.  Points are colored by category
    and the x-axis shows dates relative to *epoch*.

    Args:
        points: Every point to plot, in any order.
        title_label: Chart title shown above the plot.
        ylabel: Y-axis label.
        term_width: Terminal width in columns.
        chart_height: Chart height in rows.
        categories: Categories to draw, in drawing order, with their colors.
        epoch: Reference datetime for x-axis date labels.
    """
    import plotext as plt

    x_all = [point.x for point in points]
    y_all = [point.y for point in points]

    sorted_y = sorted(y_all)
    n = len(sorted_y)
    q1 = sorted_y[n // 4]
    q3 = sorted_y[3 * n // 4]
    iqr = q3 - q1
    y_lo = q1 - 2.0 * iqr
    y_hi = q3 + 2.0 * iqr
    clipped = sum(1 for y in y_all if y < y_lo or y > y_hi)
    y_lo = min(y_lo, 0.0)
    y_hi = max(y_hi, 0.0)

    plt.clear_figure()
    plt.theme("dark")

    for category in categories:
        drawn = [point for point in points if point.category == category.name]
        if not drawn:
            continue
        plt.scatter(
            [point.x for point in drawn],
            [point.y for point in drawn],
            marker="dot",
            color=category.color,
        )

    n_yticks = max(3, chart_height // 3)
    y_step = (y_hi - y_lo) / max(1, n_yticks)
    below = math.floor(-y_lo / y_step) if y_step else 0
    above = math.floor(y_hi / y_step) if y_step else 0
    yticks = [step * y_step for step in range(-below, above + 1)]
    plt.yticks(yticks, [f"{tick:.0f}" for tick in yticks])
    plt.ylim(yticks[0], yticks[-1])

    x_min, x_max = min(x_all), max(x_all)
    n_ticks = min(6, max(3, term_width // 20))
    x_step = (x_max - x_min) / max(1, n_ticks - 1)
    tick_pos = [x_min + i * x_step for i in range(n_ticks)]
    tick_labels = [(epoch + timedelta(days=d)).strftime("%d/%m") for d in tick_pos]
    plt.xticks(tick_pos, tick_labels)

    plt.hline(0, color="gray+")

    avg = statistics.mean(y_all)
    clip_note = f"  ({clipped} clipped)" if clipped else ""
    plt.title(f"{title_label}  ·  n={len(points)}  avg={avg:.1f}{clip_note}")
    plt.ylabel(ylabel)
    plt.plotsize(max(40, term_width - 10), max(8, chart_height))

    return plt.build()


# ── legend ────────────────────────────────────────────────


def build_legend(
    categories: list[ChartCategory],
    term_width: int,
    sort_key: GroupOrder | None = None,
) -> str:
    """Build a colored inline legend for scatter charts.

    A category with no total is left out: nothing was collected under it.

    Args:
        categories: Categories to describe, in any order.
        term_width: Terminal width for even spacing.
        sort_key: Ordering over category names. Lexicographic if None.
    """
    key_fn: GroupOrder = sort_key or (lambda _: (0,))
    shown = [
        category
        for category in sorted(categories, key=lambda item: key_fn(item.name))
        if category.total
    ]
    if not shown:
        return ""

    labels = [f"{item.name} {item.count}/{item.total}" for item in shown]
    visible_total = sum(len(label) for label in labels)
    remaining = max(0, term_width - visible_total)
    gap = remaining // max(1, len(shown) - 1) if len(shown) > 1 else 0
    return (" " * gap).join(
        f"\033[38;5;{category.color}m{label}\033[0m"
        for category, label in zip(shown, labels)
    )


# ── terminal utility ──────────────────────────────────────


def sleep_or_keypress(seconds: int) -> str | None:
    """Sleep for *seconds*, returning the key pressed or None on timeout.

    Puts the terminal in cbreak mode to detect single keypresses
    without requiring Enter.  Restores terminal state on exit.
    """
    old = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        elapsed = 0.0
        while elapsed < seconds:
            ready, _, _ = select.select([sys.stdin], [], [], 0.2)
            if ready:
                return sys.stdin.read(1)
            elapsed += 0.2
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
    return None
