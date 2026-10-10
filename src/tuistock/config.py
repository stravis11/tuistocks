"""Saved grid size and watchlist for the chart panels."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

INTERVALS: tuple[str, ...] = ("1m", "5m", "15m", "1h", "1d")
RANGES: tuple[str, ...] = ("1d", "5d", "1mo", "6mo", "1y")
GRID_SIZES: tuple[int, ...] = (1, 2, 3, 4)
MAX_PANELS = GRID_SIZES[-1] ** 2

DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("TSLA", "5m", "1d"),
    ("SPCX", "5m", "1d"),
    ("BTC", "5m", "1d"),
    ("SOL", "5m", "1d"),
    ("AAPL", "5m", "1d"),
    ("NVDA", "5m", "1d"),
    ("MSFT", "5m", "1d"),
    ("AMZN", "5m", "1d"),
    ("GOOG", "5m", "1d"),
    ("META", "5m", "1d"),
    ("SPY", "5m", "1d"),
    ("QQQ", "5m", "1d"),
    ("ETH", "5m", "1d"),
    ("DOGE", "5m", "1d"),
    ("AVAX", "5m", "1d"),
    ("XRP", "5m", "1d"),
)


@dataclass
class PanelState:
    symbol: str
    interval: str = "5m"
    range_key: str = "1d"


@dataclass
class LayoutState:
    panels: list[PanelState]
    grid_size: int = 2


def allowed_ranges(interval: str) -> tuple[str, ...]:
    if interval == "1m":
        return ("1d", "5d")
    if interval in {"5m", "15m"}:
        return ("1d", "5d", "1mo")
    return RANGES


def clamp_range(interval: str, range_key: str) -> str:
    options = allowed_ranges(interval)
    if range_key in options:
        return range_key
    return options[-1]


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "tuistock" / "layout.json"


def default_states() -> list[PanelState]:
    return [PanelState(symbol, interval, range_key) for symbol, interval, range_key in DEFAULTS]


def load_layout() -> LayoutState:
    path = config_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_panels = payload["panels"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return LayoutState(default_states())
    if not isinstance(raw_panels, list):
        return LayoutState(default_states())
    grid_size = payload.get("grid_size", 2)
    if type(grid_size) is not int or grid_size not in GRID_SIZES:
        grid_size = 2

    states: list[PanelState] = []
    for index, item in enumerate(raw_panels[:MAX_PANELS]):
        if not isinstance(item, dict) or "symbol" not in item:
            states.append(PanelState(*DEFAULTS[index]))
            continue
        interval = str(item.get("interval", "5m"))
        range_key = str(item.get("range", "1d"))
        if interval not in INTERVALS:
            interval = "5m"
        states.append(
            PanelState(
                symbol=str(item["symbol"]).upper(),
                interval=interval,
                range_key=clamp_range(interval, range_key),
            )
        )
    while len(states) < MAX_PANELS:
        symbol, interval, range_key = DEFAULTS[len(states)]
        states.append(PanelState(symbol, interval, range_key))
    return LayoutState(states, grid_size)


def save_layout(states: list[PanelState], grid_size: int = 2) -> None:
    if type(grid_size) is not int or grid_size not in GRID_SIZES:
        raise ValueError("Grid size must be 1, 2, 3, or 4")
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "grid_size": grid_size,
        "panels": [
            {"symbol": state.symbol, "interval": state.interval, "range": state.range_key}
            for state in states[:MAX_PANELS]
        ]
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
