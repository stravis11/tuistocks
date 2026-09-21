"""Braille price chart with SMA(50) and volume columns."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.text import Text

from tuistock.market import Candle

PERIOD = 50
PANEL_BG = "#10181c"
PRICE_STYLE = "#3ee07a"
SMA_STYLE = "#e070a0"
GUIDE_STYLE = "#2a3e48"
AXIS_STYLE = "#6d828c"
TIME_STYLE = "#80949c"
UP_VOL = "#1f8a4c"
DOWN_VOL = "#a33d3c"
BADGE_STYLE = "bold #fff8f3 on #e36b1f"
LABEL_KEY = f"#8ea3ac on {PANEL_BG}"
LABEL_SMA = f"#e7b0cb on {PANEL_BG}"
LABEL_UP = f"#46e08a on {PANEL_BG}"
LABEL_DOWN = f"#ff7d72 on {PANEL_BG}"

_DOTS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)
_BLOCKS = " ▁▂▃▄▅▆▇█"
_RANGE_SECONDS = {
    "1d": 86_400,
    "5d": 5 * 86_400,
    "1mo": 31 * 86_400,
    "6mo": 183 * 86_400,
    "1y": 366 * 86_400,
}

Cell = tuple[str, str]


def sma_series(values: list[float], period: int = PERIOD) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= period:
            total -= values[index - period]
        if index >= period - 1:
            out[index] = total / period
    return out


def fmt_price(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= 1000:
        return f"{value:,.2f}"
    if magnitude >= 1:
        return f"{value:,.2f}"
    if magnitude >= 0.01:
        return f"{value:.4f}"
    return f"{value:.6f}"


def fmt_axis(value: float, span: float) -> str:
    magnitude = abs(value)
    if span >= 50 and magnitude >= 100:
        return f"{value:,.0f}"
    if magnitude >= 1000:
        return f"{value:,.1f}"
    if span < 0.5:
        return f"{value:.3f}"
    return f"{value:.2f}"


def fmt_volume(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f} B"
    if magnitude >= 1_000_000:
        return f"{value / 1_000_000:.2f} M"
    if magnitude >= 10_000:
        return f"{value / 1_000:.2f} K"
    if magnitude >= 100:
        return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
    return f"{value:.2f}"


def fmt_change(price: float, previous: float) -> tuple[str, str]:
    delta = price - previous
    if previous:
        percent = abs(delta) / abs(previous) * 100
        pct = f" ({percent:.2f}%)"
    else:
        pct = ""
    if delta > 1e-8:
        return f"▲ ${fmt_price(delta)}{pct}", "#3ee07a"
    if delta < -1e-8:
        return f"▼ ${fmt_price(abs(delta))}{pct}", "#ff5d5d"
    return f"● ${fmt_price(0)}{pct}", "#8ea3ac"


def select_window(
    candles: list[Candle], range_key: str
) -> tuple[list[Candle], list[float | None]]:
    if not candles:
        return [], []
    sma = sma_series([candle.close for candle in candles])
    if range_key == "1d":
        local = datetime.now().astimezone().tzinfo or timezone.utc
        day = datetime.fromtimestamp(candles[-1].ts, local).date()
        indexes = [
            index
            for index, candle in enumerate(candles)
            if datetime.fromtimestamp(candle.ts, local).date() == day
        ]
        if len(indexes) >= 2:
            return (
                [candles[index] for index in indexes],
                [sma[index] for index in indexes],
            )
    span = _RANGE_SECONDS.get(range_key, _RANGE_SECONDS["1d"])
    cutoff = candles[-1].ts - span
    indexes = [index for index, candle in enumerate(candles) if candle.ts >= cutoff]
    if len(indexes) < 2:
        indexes = list(range(len(candles)))
    return [candles[index] for index in indexes], [sma[index] for index in indexes]


def render_chart(
    *,
    width: int,
    height: int,
    candles: list[Candle],
    sma: list[float | None],
    price: float | None,
    previous_close: float | None,
    session_volume: float = 0.0,
) -> Text:
    if width < 16 or height < 6 or len(candles) < 2:
        return _message(width, height, "waiting for bars", AXIS_STYLE)

    badge = fmt_price(price if price is not None else candles[-1].close)
    axis_w = min(12, max(7, len(badge)))
    gap = 1
    plot_w = width - axis_w - gap
    if plot_w < 10:
        return _message(width, height, "widen the terminal", AXIS_STYLE)

    has_volume = any(candle.volume > 0 for candle in candles)
    vol_h = max(2, min(4, height // 5)) if has_volume else 0
    price_h = height - vol_h - 1
    if has_volume and price_h < 4:
        vol_h = 2
        price_h = height - vol_h - 1
    if price_h < 3:
        return _message(width, height, "taller terminal needed", AXIS_STYLE)

    closes = [candle.close for candle in candles]
    if price is not None:
        closes[-1] = price
    domain = list(closes)
    domain.extend(value for value in sma if value is not None)
    low = min(domain)
    high = max(domain)
    pad = (high - low) * 0.08 if high != low else max(abs(high) * 0.01, 1.0)
    ymin = low - pad
    ymax = high + pad
    span = ymax - ymin

    cols = plot_w * 2
    rows = price_h * 4
    price_bits = [[0 for _ in range(plot_w)] for _ in range(price_h)]
    sma_bits = [[0 for _ in range(plot_w)] for _ in range(price_h)]

    def y_of(level: float) -> int:
        if span == 0:
            return rows // 2
        scaled = 1 - (level - ymin) / span
        return max(0, min(rows - 1, int(round(scaled * (rows - 1)))))

    _stroke(price_bits, _points(closes, cols, y_of), cols, rows, thick=True)
    _stroke(sma_bits, _points(sma, cols, y_of), cols, rows, thick=False)

    cells = [[(" ", "") for _ in range(width)] for _ in range(height)]
    for row in (price_h // 4, price_h // 2, (price_h * 3) // 4):
        if 0 <= row < price_h:
            for col in range(plot_w):
                cells[row][col] = ("┈", GUIDE_STYLE)

    for row in range(price_h):
        for col in range(plot_w):
            if price_bits[row][col]:
                cells[row][col] = (chr(0x2800 + price_bits[row][col]), PRICE_STYLE)
            elif sma_bits[row][col]:
                cells[row][col] = (chr(0x2800 + sma_bits[row][col]), SMA_STYLE)

    if previous_close is not None and ymin < previous_close < ymax:
        guide_row = min(price_h - 1, y_of(previous_close) // 4)
        for col in range(plot_w):
            if cells[guide_row][col][0] in {" ", "┈"}:
                cells[guide_row][col] = ("┈", GUIDE_STYLE)

    _draw_volume(cells, candles, price_h, vol_h, plot_w)
    _stamp_labels(cells, plot_w, candles, sma, session_volume)
    _draw_axis(cells, price_h, plot_w, gap, axis_w, ymin, span, y_of, badge)
    _draw_times(cells, candles, plot_w)
    return _to_text(cells)


def _points(
    values: list[float | None], cols: int, y_of
) -> list[tuple[int, int] | None]:
    count = len(values)
    points: list[tuple[int, int] | None] = []
    for index, value in enumerate(values):
        if value is None:
            points.append(None)
            continue
        x = 0 if count == 1 else round(index * (cols - 1) / (count - 1))
        points.append((x, y_of(value)))
    return points


def _stroke(
    layer: list[list[int]],
    points: list[tuple[int, int] | None],
    cols: int,
    rows: int,
    *,
    thick: bool,
) -> None:
    previous: tuple[int, int] | None = None
    for point in points:
        if point is None:
            previous = None
            continue
        if previous is not None:
            _line(layer, previous[0], previous[1], point[0], point[1], cols, rows, thick)
        else:
            _plot(layer, point[0], point[1], cols, rows, thick)
        previous = point


def _line(
    layer: list[list[int]],
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    cols: int,
    rows: int,
    thick: bool,
) -> None:
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    while True:
        _plot(layer, x0, y0, cols, rows, thick)
        if x0 == x1 and y0 == y1:
            break
        doubled = 2 * err
        if doubled > -dy:
            err -= dy
            x0 += sx
        if doubled < dx:
            err += dx
            y0 += sy


def _plot(
    layer: list[list[int]], x: int, y: int, cols: int, rows: int, thick: bool
) -> None:
    _dot(layer, x, y, cols, rows)
    if thick:
        _dot(layer, x, y + 1, cols, rows)


def _dot(layer: list[list[int]], x: int, y: int, cols: int, rows: int) -> None:
    if x < 0 or y < 0 or x >= cols or y >= rows:
        return
    layer[y // 4][x // 2] |= _DOTS[y % 4][x % 2]


def _draw_volume(
    cells: list[list[Cell]],
    candles: list[Candle],
    price_h: int,
    vol_h: int,
    plot_w: int,
) -> None:
    buckets: list[list[Candle]] = [[] for _ in range(plot_w)]
    last_index = len(candles) - 1
    for index, candle in enumerate(candles):
        x = 0 if last_index == 0 else round(index * (plot_w * 2 - 1) / last_index)
        buckets[min(plot_w - 1, x // 2)].append(candle)
    amounts = [
        max((item.volume for item in bucket), default=0.0) for bucket in buckets
    ]
    peak = max(amounts) or 1.0
    for col, (amount, bucket) in enumerate(zip(amounts, buckets)):
        if not bucket or amount <= 0:
            continue
        last = bucket[-1]
        style = UP_VOL if last.close >= last.open else DOWN_VOL
        filled = (amount / peak) * vol_h
        for step in range(vol_h):
            from_bottom = vol_h - step
            row = price_h + step
            if filled >= from_bottom:
                cells[row][col] = ("█", style)
            elif filled > from_bottom - 1:
                frac = filled - (from_bottom - 1)
                glyph = _BLOCKS[max(1, min(8, round(frac * 8)))]
                cells[row][col] = (glyph, style)


def _draw_axis(
    cells: list[list[Cell]],
    price_h: int,
    plot_w: int,
    gap: int,
    axis_w: int,
    ymin: float,
    span: float,
    y_of,
    badge: str,
) -> None:
    width = len(cells[0])
    rows = price_h * 4
    ticks = [0, price_h // 3, (price_h * 2) // 3, price_h - 1]
    for row in ticks:
        braille_y = min(rows - 1, row * 4 + 2)
        level = ymin + (1 - braille_y / (rows - 1)) * span
        label = fmt_axis(level, span)
        start = plot_w + gap + axis_w - len(label)
        for offset, char in enumerate(label):
            x = start + offset
            if plot_w + gap <= x < width:
                cells[row][x] = (char, AXIS_STYLE)

    # Place the badge on the row nearest the last price. y_of is in braille dots.
    # The last price is encoded by the caller through the highest-priority row
    # we compute from the badge's numeric value via the axis scale endpoints.
    last_level = _parse_price(badge)
    if last_level is None:
        badge_row = price_h // 2
    else:
        badge_row = min(price_h - 1, y_of(last_level) // 4)
    start = max(plot_w + gap, width - len(badge))
    for offset, char in enumerate(badge):
        x = start + offset
        if 0 <= x < width:
            cells[badge_row][x] = (char, BADGE_STYLE)


def _parse_price(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _stamp_labels(
    cells: list[list[Cell]],
    plot_w: int,
    candles: list[Candle],
    sma: list[float | None],
    session_volume: float,
) -> None:
    latest_sma = next((value for value in reversed(sma) if value is not None), None)
    last = candles[-1]
    volume = last.volume if last.volume > 0 else session_volume
    vol_style = LABEL_UP if last.close >= last.open else LABEL_DOWN
    sma_text = fmt_price(latest_sma) if latest_sma is not None else "—"
    key = "SMA (50)" if plot_w >= 28 else "SMA"
    vol_key = "Volume" if plot_w >= 28 else "Vol"
    _write(cells, 0, 0, f"{key}  ", LABEL_KEY)
    _write(cells, 0, len(key) + 2, sma_text, LABEL_SMA)
    _write(cells, 1, 0, f"{vol_key}  ", LABEL_KEY)
    _write(cells, 1, len(vol_key) + 2, fmt_volume(volume), vol_style)


def _draw_times(cells: list[list[Cell]], candles: list[Candle], plot_w: int) -> None:
    row = len(cells) - 1
    span = candles[-1].ts - candles[0].ts
    local = datetime.now().astimezone().tzinfo or timezone.utc
    labels = [
        (0.0, _fmt_time(candles[0].ts, local, span)),
        (0.5, _fmt_time(candles[len(candles) // 2].ts, local, span)),
        (1.0, _fmt_time(candles[-1].ts, local, span)),
    ]
    placed: list[tuple[int, int]] = []
    for anchor, text in labels:
        if anchor <= 0:
            start = 0
        elif anchor >= 1:
            start = max(0, plot_w - len(text))
        else:
            start = max(0, int((plot_w - len(text)) * anchor))
        end = start + len(text)
        if any(not (end <= left or start >= right) for left, right in placed):
            continue
        placed.append((start, end))
        _write(cells, row, start, text, TIME_STYLE)


def _fmt_time(ts: int, tz, span: float) -> str:
    moment = datetime.fromtimestamp(ts, tz)
    if span >= 2 * 86_400:
        return f"{moment.strftime('%b')} {moment.day}"
    return moment.strftime("%H:%M")


def _write(cells: list[list[Cell]], row: int, col: int, text: str, style: str) -> None:
    if row < 0 or row >= len(cells):
        return
    width = len(cells[row])
    for offset, char in enumerate(text):
        x = col + offset
        if 0 <= x < width:
            cells[row][x] = (char, style)


def _to_text(cells: list[list[Cell]]) -> Text:
    text = Text(no_wrap=True, overflow="crop")
    for index, row in enumerate(cells):
        if index:
            text.append("\n")
        for char, style in row:
            text.append(char, style=style)
    return text


def _message(width: int, height: int, message: str, style: str) -> Text:
    if width <= 0 or height <= 0:
        return Text("")
    text = Text(no_wrap=True, overflow="crop")
    blank = " " * width
    mid = max(0, height // 2)
    for row in range(height):
        if row:
            text.append("\n")
        if row == mid:
            clipped = message[:width].center(width)
            text.append(clipped, style=style)
        else:
            text.append(blank)
    return text
