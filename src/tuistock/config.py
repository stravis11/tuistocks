"""Saved watchlist for the four chart panels."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

INTERVALS: tuple[str, ...] = ("1m", "5m", "15m", "1h", "1d")
RANGES: tuple[str, ...] = ("1d", "5d", "1mo", "6mo", "1y")

DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("TSLA", "5m", "1d"),
    ("SPCX", "5m", "1d"),
    ("BTC", "5m", "1d"),
    ("SOL", "5m", "1d"),
)


@dataclass
class PanelState:
    symbol: str
    interval: str = "5m"
    range_key: str = "1d"


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


def load_layout() -> list[PanelState]:
    path = config_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_panels = payload["panels"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return default_states()

    states: list[PanelState] = []
    for item in raw_panels[:4]:
        if not isinstance(item, dict) or "symbol" not in item:
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
    if not states:
        return default_states()
    while len(states) < 4:
        symbol, interval, range_key = DEFAULTS[len(states)]
        states.append(PanelState(symbol, interval, range_key))
    return states


def save_layout(states: list[PanelState]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "panels": [
            {"symbol": state.symbol, "interval": state.interval, "range": state.range_key}
            for state in states[:4]
        ]
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
