"""Terminal chart builders for devtools visualization.

Portable visualization infrastructure: horizontal strip charts, plotext
scatter plots, 256-color palettes, and watch-mode utilities. Import these
builders from devtools commands that need visualization.
"""

import select
import statistics
import sys
import termios
import tty
from collections.abc import Callable
from datetime import datetime, timedelta


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


# ── types ─────────────────────────────────────────────────


ScatterPoint = tuple[float, float, str, int]
"""(x, y, category_label, item_id) for scatter charts."""


# ── strip chart ───────────────────────────────────────────


def build_strip_chart(
    by_group: dict[str, list[float]],
    group_labels: dict[str, str],
    term_width: int,
    *,
    color_map: dict[str, int] | None = None,
    group_totals: dict[str, int] | None = None,
    title: str = "Score by group (higher is better)",
    sort_key: Callable[[str], tuple[int, ...]] | None = None,
) -> str:
    """Build a horizontal scatter strip chart with summary statistics.

    Each group gets a row showing individual data points as dots, with
    mean/std/min/max statistics.  Uses IQR-based axis clipping to handle
    outliers and shows a zero reference line when in range.

    Args:
        by_group: Group name -> list of float values to plot.
        group_labels: Group name -> display suffix (e.g. date, description).
        term_width: Terminal width in columns.
        color_map: Pre-computed group -> 256-color code. Auto-assigned if None.
        group_totals: Total count per group (shown as n/total in labels).
        title: Bold header line above the chart.
        sort_key: Sort function for group ordering. Lexicographic if None.
    """
    key_fn: Callable[[str], tuple[int, ...]] = sort_key or (lambda _: (0,))

    all_groups = set(by_group.keys())
    if group_totals:
        all_groups |= group_totals.keys()
    groups_sorted = sorted(all_groups, key=key_fn)

    group_stats: list[tuple[str, int, float, float, float, float]] = []
    for g in groups_sorted:
        values = by_group.get(g, [])
        n = len(values)
        if n > 0:
            avg = statistics.mean(values)
            std = statistics.stdev(values) if n > 1 else 0.0
            group_stats.append((g, n, avg, std, min(values), max(values)))
        else:
            group_stats.append((g, 0, 0.0, 0.0, 0.0, 0.0))

    all_values = sorted(v for g in groups_sorted for v in by_group.get(g, []))
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

    def label_text(g: str, n: int) -> str:
        suffix = group_labels.get(g, "")
        total = group_totals.get(g) if group_totals else None
        if total is not None:
            return f"{g} {suffix} {n}/{total}".strip()
        return f"{g} {suffix} (n={n:>2})".strip()

    label_width = max(len(label_text(s[0], s[1])) for s in group_stats)
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

    if color_map is None:
        colors = pick_colors(len(groups_sorted))
        color_map = dict(zip(groups_sorted, colors))

    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    lines: list[str] = [f"{BOLD}{title}{RESET}", ""]

    for g, n, avg, std, mn, mx in group_stats:
        label = label_text(g, n).ljust(label_width)
        c = color_map.get(g)
        ansi = f"\033[38;5;{c}m" if c is not None else ""

        if n > 0:
            counts: dict[int, int] = {}
            for v in by_group.get(g, []):
                pos = to_pos(v)
                counts[pos] = counts.get(pos, 0) + 1

            parts: list[str] = []
            for col in range(chart_width):
                count = counts.get(col, 0)
                if count > 0:
                    char = "●" if count == 1 else str(min(count, 9))
                    parts.append(f"{ansi}{char}{RESET}")
                elif col == zero_pos:
                    parts.append(f"{DIM}│{RESET}")
                else:
                    parts.append(" ")
            row = "".join(parts)
            stats = f"{avg:>5.1f} ±{std:>2.0f}  {mn:>3.0f}‥{mx:.0f}"
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
    color_map: dict[str, int],
    epoch: datetime,
) -> str:
    """Build a scatter chart panel with IQR-based y-axis clipping.

    Uses plotext for terminal rendering.  Points are colored by category
    and the x-axis shows dates relative to *epoch*.

    Args:
        points: List of (x_day_offset, y_value, category, item_id).
        title_label: Chart title shown above the plot.
        ylabel: Y-axis label.
        term_width: Terminal width in columns.
        chart_height: Chart height in rows.
        color_map: Category -> 256-color code mapping.
        epoch: Reference datetime for x-axis date labels.
    """
    import plotext as plt

    x_all = [p[0] for p in points]
    y_all = [p[1] for p in points]

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

    by_category: dict[str, list[ScatterPoint]] = {}
    for p in points:
        by_category.setdefault(p[2], []).append(p)

    plt.clear_figure()
    plt.theme("dark")

    for cat, color in color_map.items():
        cat_points = by_category.get(cat)
        if not cat_points:
            continue
        plt.scatter(
            [p[0] for p in cat_points],
            [p[1] for p in cat_points],
            marker="dot",
            color=color,
        )

    n_yticks = max(3, chart_height // 3)
    y_step = (y_hi - y_lo) / max(1, n_yticks)
    yticks: list[float] = []
    v = 0.0
    while v >= y_lo:
        yticks.append(v)
        v -= y_step
    v = y_step
    while v <= y_hi:
        yticks.append(v)
        v += y_step
    yticks.sort()
    plt.yticks(yticks, [f"{t:.0f}" for t in yticks])
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
    categories: list[str],
    color_map: dict[str, int],
    counts: dict[str, int],
    totals: dict[str, int] | None,
    term_width: int,
    sort_key: Callable[[str], tuple[int, ...]] | None = None,
) -> str:
    """Build a colored inline legend for scatter charts.

    Args:
        categories: Category names to include.
        color_map: Category -> 256-color code.
        counts: Visible/resolved count per category.
        totals: Total count per category (shown as count/total).
        term_width: Terminal width for even spacing.
        sort_key: Sort function. Lexicographic if None.
    """
    key_fn: Callable[[str], tuple[int, ...]] = sort_key or (lambda _: (0,))

    entries: list[tuple[str, int]] = []
    for cat in sorted(categories, key=key_fn):
        color = color_map.get(cat, 7)
        total = totals.get(cat, 0) if totals else 0
        if not total:
            continue
        count = counts.get(cat, 0)
        ansi = f"\033[38;5;{color}m"
        text = f"{cat} {count}/{total}"
        entries.append((f"{ansi}{text}\033[0m", len(text)))

    if not entries:
        return ""

    visible_total = sum(vl for _, vl in entries)
    n = len(entries)
    remaining = max(0, term_width - visible_total)
    gap = remaining // max(1, n - 1) if n > 1 else 0
    return (" " * gap).join(entry for entry, _ in entries)


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
