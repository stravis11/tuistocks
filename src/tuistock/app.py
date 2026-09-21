"""Four-panel realtime stock and crypto chart board."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime

import httpx
import websockets
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid
from textual.css.query import NoMatches
from textual.events import Click
from textual.widget import Widget
from textual.widgets import Input

from tuistock.chart import fmt_change, fmt_price, render_chart, select_window
from tuistock.config import (
    INTERVALS,
    PanelState,
    allowed_ranges,
    clamp_range,
    default_states,
    load_layout,
    save_layout,
)
from tuistock.market import (
    HEADERS,
    INTERVAL_SECONDS,
    MarketError,
    Quote,
    RateLimit,
    apply_trade,
    load_quote,
    normalize_symbol,
    patch_price,
    resolve_symbol,
)

_MOVES = {
    "left": (0, 1, 2, 3),
    "right": (1, 1, 3, 3),
    "up": (0, 1, 0, 1),
    "down": (2, 3, 2, 3),
}


class SymbolInput(Input):
    """Ticker editor docked over the focused panel."""

    def on_mount(self) -> None:
        self.select_all()


class PanelHead(Widget):
    """Symbol, change, and the latest open/close."""

    def on_click(self, event: Click) -> None:
        panel = self.panel
        panel.focus()
        self.run_worker(
            self.app.action_edit_symbol(),
            name="edit",
            group="edit",
            exclusive=True,
            exit_on_error=False,
        )
        event.stop()

    def render(self) -> Text:
        panel = self.panel
        width = self.size.width
        if width <= 0:
            return Text("")
        quote = panel.shown_quote
        symbol = quote.display if quote else panel.symbol
        mark_style = "#4c9aff" if panel.has_focus or self._child_focused(panel) else "#5c7078"
        mark = "▣" if mark_style == "#4c9aff" else "☐"
        left = Text()
        left.append(f"{mark} ", style=mark_style)
        left.append("⌕ ", style="#5c7078")
        left.append(symbol, style="bold #f4f8f8")
        if quote is not None:
            change, change_style = fmt_change(quote.price, quote.previous_close)
            left.append("   ")
            left.append(change, style=change_style)
        else:
            left.append("  ·  loading", style="#5c7078")

        right = Text()
        if quote is not None and quote.candles:
            opened = quote.candles[-1].open
            right.append("O ", style="#8aa4ae")
            right.append(fmt_price(opened), style="#d5e0e6")
            right.append("  C ", style="#8aa4ae")
            right.append(fmt_price(quote.price), style="bold #ffb067")
        return _fit(left, right, width)

    @property
    def panel(self) -> ChartPanel:
        parent = self.parent
        assert isinstance(parent, ChartPanel)
        return parent

    def _child_focused(self, panel: ChartPanel) -> bool:
        focused = self.app.focused
        return focused is not None and focused is panel or (
            focused is not None and focused.parent is panel
        )


class ChartBody(Widget):
    """Price line, SMA, and volume."""

    def render(self) -> Text:
        panel = self.panel
        quote = panel.shown_quote
        width = self.size.width
        height = self.size.height
        if quote is None:
            detail = panel.error or "loading live prices"
            return _center(width, height, panel.symbol, detail, "#8ea3ac" if not panel.error else "#ff7d72")
        candles, sma = select_window(quote.candles, quote.range_key)
        if panel.error and len(candles) < 2:
            return _center(width, height, panel.symbol, panel.error, "#ff7d72")
        return render_chart(
            width=width,
            height=height,
            candles=candles,
            sma=sma,
            price=quote.price,
            previous_close=quote.previous_close,
            session_volume=quote.session_volume,
        )

    @property
    def panel(self) -> ChartPanel:
        parent = self.parent
        assert isinstance(parent, ChartPanel)
        return parent


class PanelFoot(Widget):
    """Range and interval controls for one panel."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._interval_at = 0

    def on_click(self, event: Click) -> None:
        panel = self.panel
        panel.focus()
        offset = event.get_content_offset(self)
        if offset is not None and offset.x >= self._interval_at:
            panel.cycle_interval()
        else:
            panel.cycle_range()
        event.stop()

    def render(self) -> Text:
        panel = self.panel
        width = self.size.width
        left = Text()
        left.append("Range ", style="#7d919a")
        left.append(panel.range_key, style="bold #d5e2e8")
        left.append(" ▾", style="#7d919a")
        left.append("    ")
        self._interval_at = left.cell_len
        left.append("Interval: ", style="#7d919a")
        left.append(panel.interval, style="bold #d5e2e8")
        left.append(" ▾", style="#7d919a")
        quote = panel.shown_quote
        if panel.error and panel.quote is not None:
            left.append("  ")
            left.append(panel.error[:24], style="#ff7d72")
        elif quote is not None and quote.note:
            left.append("  ")
            left.append(quote.note, style="#7d919a")
        right = Text()
        if quote is None:
            right.append("○", style="#5c7078")
        elif time.time() - quote.updated_at < 12:
            right.append("●", style="#3ee07a")
        else:
            right.append("●", style="#e0a050")
        return _fit(left, right, width)

    @property
    def panel(self) -> ChartPanel:
        parent = self.parent
        assert isinstance(parent, ChartPanel)
        return parent


class ChartPanel(Widget):
    """One symbol on the board."""

    can_focus = True

    def __init__(self, state: PanelState, index: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.symbol = state.symbol
        self.interval = state.interval
        self.range_key = state.range_key
        self.index = index
        self.quote: Quote | None = None
        self.error: str | None = None
        self.is_crypto = _guess_crypto(state.symbol)
        self._painted = 0.0
        self._paint_timer = None

    def compose(self) -> ComposeResult:
        yield PanelHead(classes="head")
        yield ChartBody(classes="chart")
        yield PanelFoot(classes="foot")

    def on_click(self, event: Click) -> None:
        if isinstance(event.widget, SymbolInput):
            return
        self.focus()

    @property
    def shown_quote(self) -> Quote | None:
        quote = self.quote
        if quote is None:
            return None
        if (
            quote.display != self.symbol
            or quote.interval != self.interval
            or quote.range_key != self.range_key
        ):
            return None
        return quote

    def cycle_interval(self) -> None:
        current = INTERVALS.index(self.interval) if self.interval in INTERVALS else 0
        self.interval = INTERVALS[(current + 1) % len(INTERVALS)]
        if self.interval == "1d" and self.range_key in {"1d", "5d"}:
            self.range_key = "6mo"
        elif self.interval == "1h" and self.range_key == "1d":
            self.range_key = "5d"
        self.range_key = clamp_range(self.interval, self.range_key)
        self._changed()

    def cycle_range(self) -> None:
        options = allowed_ranges(self.interval)
        current = options.index(self.range_key) if self.range_key in options else 0
        self.range_key = options[(current + 1) % len(options)]
        self._changed()

    def set_symbol(self, symbol: str) -> None:
        self.symbol = symbol
        self.is_crypto = _guess_crypto(symbol)
        self.error = None
        self._changed()

    def paint(self) -> None:
        if not self.is_mounted:
            return
        wait = 0.2 - (time.monotonic() - self._painted)
        if wait <= 0:
            self._flush_paint()
            return
        if self._paint_timer is None:
            self._paint_timer = self.set_timer(wait, self._flush_paint)

    def _flush_paint(self) -> None:
        self._paint_timer = None
        if not self.is_mounted:
            return
        self._painted = time.monotonic()
        try:
            self.query_one(PanelHead).refresh()
            self.query_one(ChartBody).refresh()
            self.query_one(PanelFoot).refresh()
        except NoMatches:
            return

    def _changed(self) -> None:
        tracker = self.app
        assert isinstance(tracker, TrackerApp)
        tracker.persist()
        tracker.bump_stream()
        tracker.request_refresh(self)
        self.paint()


class StatusBar(Widget):
    """Key hints and the live clock."""

    def render(self) -> Text:
        app = self.app
        assert isinstance(app, TrackerApp)
        width = max(0, self.size.width)
        now = datetime.now().astimezone().strftime("%H:%M:%S")
        ages = [
            time.time() - panel.quote.updated_at
            for panel in app.query(ChartPanel)
            if panel.quote is not None
        ]
        if not ages:
            state, style = "LOADING", "#8ea3ac"
        elif app.crypto_connected or min(ages) < 8:
            state, style = "LIVE", "#3ee07a"
        elif min(ages) < 30:
            state, style = "DELAYED", "#e0a050"
        else:
            state, style = "STALE", "#ff7d72"
        hints = "1-4 focus   e symbol   i interval   r range   q quit"
        if width < 78:
            hints = "e symbol   i interval   r range   q quit"
        left = Text()
        left.append(" tuistock ", style="bold #06281c on #3ee07a")
        left.append("  ")
        left.append(hints, style="#7d919a")
        right = Text()
        right.append(state, style=style)
        right.append(f"  {now}", style="#8ea3ac")
        return _fit(left, right, width)


class TrackerApp(App):
    """2×2 live chart board."""

    TITLE = "tuistock"
    ENABLE_COMMAND_PALETTE = False
    ALLOW_SELECT = False
    CSS = """
    Screen {
        background: #0c1214;
        overflow: hidden;
    }
    #board {
        layout: grid;
        grid-size: 2 2;
        grid-gutter: 1 1;
        grid-columns: 1fr 1fr;
        grid-rows: 1fr 1fr;
        height: 1fr;
        width: 1fr;
        background: #0c1214;
        padding: 0 1;
    }
    ChartPanel {
        layout: vertical;
        background: #10181c;
        border: solid #243038;
        height: 1fr;
        width: 1fr;
    }
    ChartPanel:hover {
        border: solid #2c4652;
    }
    ChartPanel:focus, ChartPanel:focus-within {
        border: solid #3ee07a;
    }
    .head {
        height: 1;
        padding: 0 1;
        background: #10181c;
        overflow-x: hidden;
        overflow-y: hidden;
    }
    .chart {
        height: 1fr;
        padding: 0 1;
        background: #10181c;
        overflow-x: hidden;
        overflow-y: hidden;
    }
    .foot {
        height: 1;
        padding: 0 1;
        background: #10181c;
        overflow-x: hidden;
        overflow-y: hidden;
    }
    #symbol-edit {
        dock: top;
        margin: 0 1;
        background: #1a2830;
        color: #f4f8f8;
    }
    StatusBar {
        height: 1;
        background: #0c1214;
        padding: 0 1;
    }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("e", "edit_symbol", "Symbol"),
        Binding("i", "cycle_interval", "Interval"),
        Binding("r", "cycle_range", "Range"),
        Binding("escape", "cancel_edit", show=False),
        Binding("up,k", "focus_move('up')", show=False),
        Binding("down,j", "focus_move('down')", show=False),
        Binding("left,h", "focus_move('left')", show=False),
        Binding("right,l", "focus_move('right')", show=False),
        Binding("1", "focus_index(0)", show=False),
        Binding("2", "focus_index(1)", show=False),
        Binding("3", "focus_index(2)", show=False),
        Binding("4", "focus_index(3)", show=False),
    ]

    def __init__(self, states: list[PanelState]) -> None:
        super().__init__()
        self.states = states
        self._client: httpx.AsyncClient | None = None
        self._poll_seconds = 4.0
        self._poll_tick = 0
        self._stream_gen = 0
        self.crypto_connected = False

    def compose(self) -> ComposeResult:
        with Grid(id="board"):
            for index, state in enumerate(self.states):
                yield ChartPanel(state, index, id=f"p{index}")
        yield StatusBar()

    def on_mount(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=5.0),
            headers=HEADERS,
            follow_redirects=True,
        )
        self.query_one("#p0", ChartPanel).focus()
        self.run_worker(
            self._poll_loop(),
            name="poll",
            group="poll",
            exclusive=True,
            exit_on_error=False,
        )
        self.run_worker(
            self._crypto_loop(),
            name="crypto",
            group="crypto",
            exclusive=True,
            exit_on_error=False,
        )
        self.set_interval(1.0, self._refresh_status)

    async def on_unmount(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not isinstance(event.input, SymbolInput):
            return
        panel = event.input.parent
        if not isinstance(panel, ChartPanel):
            return
        try:
            symbol = normalize_symbol(event.value)
        except ValueError as exc:
            self.notify(str(exc), severity="error", timeout=4)
            return
        event.input.remove()
        panel.set_symbol(symbol)
        panel.focus()

    def action_cancel_edit(self) -> None:
        editors = list(self.query(SymbolInput))
        if not editors:
            return
        for editor in editors:
            editor.remove()
        panel = self.focused_panel()
        if panel is not None:
            panel.focus()

    async def action_edit_symbol(self) -> None:
        if self.query(SymbolInput):
            return
        panel = self.focused_panel()
        if panel is None:
            return
        editor = SymbolInput(
            value=panel.symbol,
            placeholder="AAPL, ETH, BTC-USD",
            compact=True,
            id="symbol-edit",
        )
        await panel.mount(editor)
        editor.focus()
        editor.select_all()

    def action_cycle_interval(self) -> None:
        panel = self.focused_panel()
        if panel is not None and not self.query(SymbolInput):
            panel.cycle_interval()

    def action_cycle_range(self) -> None:
        panel = self.focused_panel()
        if panel is not None and not self.query(SymbolInput):
            panel.cycle_range()

    def action_focus_move(self, direction: str) -> None:
        if isinstance(self.focused, SymbolInput):
            return
        panel = self.focused_panel()
        if panel is None:
            self.action_focus_index(0)
            return
        target = _MOVES[direction][panel.index]
        self.action_focus_index(target)

    def action_focus_index(self, index: int) -> None:
        if isinstance(self.focused, SymbolInput):
            return
        try:
            self.query_one(f"#p{index}", ChartPanel).focus()
        except NoMatches:
            return

    def focused_panel(self) -> ChartPanel | None:
        node = self.focused
        while node is not None and not isinstance(node, ChartPanel):
            node = getattr(node, "parent", None)
        return node if isinstance(node, ChartPanel) else None

    def persist(self) -> None:
        save_layout(
            [
                PanelState(panel.symbol, panel.interval, panel.range_key)
                for panel in self.query(ChartPanel)
            ]
        )

    def bump_stream(self) -> None:
        self._stream_gen += 1

    def request_refresh(self, panel: ChartPanel) -> None:
        self.run_worker(
            self._refresh_panel(panel),
            name=f"refresh-{panel.index}",
            group="refresh",
            exclusive=False,
            exit_on_error=False,
        )

    async def _poll_loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self._refresh_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._poll_seconds = min(30.0, self._poll_seconds * 1.5)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.2, self._poll_seconds - elapsed))

    async def _refresh_all(self) -> None:
        full = self._poll_tick % 4 == 0
        self._poll_tick += 1
        panels = list(self.query(ChartPanel))
        results = await asyncio.gather(
            *(self._refresh_panel(panel, full=full) for panel in panels)
        )
        if any(result == "rate" for result in results):
            self._poll_seconds = min(30.0, max(8.0, self._poll_seconds * 2))
        elif results and all(result == "ok" for result in results):
            self._poll_seconds = 4.0
        self._refresh_status()

    async def _refresh_panel(self, panel: ChartPanel, full: bool = True) -> str:
        client = self._client
        if client is None or not panel.is_mounted:
            return "error"
        if not full and panel.quote is not None and panel.shown_quote is not None:
            try:
                await patch_price(client, panel.quote)
            except RateLimit as exc:
                panel.error = str(exc)
                panel.paint()
                return "rate"
            except MarketError:
                full = True
            else:
                panel.error = None
                panel.paint()
                return "ok"
        try:
            quote = await load_quote(client, panel.symbol, panel.interval, panel.range_key)
        except RateLimit as exc:
            panel.error = str(exc)
            panel.paint()
            return "rate"
        except MarketError as exc:
            panel.error = str(exc)
            panel.paint()
            return "error"
        except Exception:
            panel.error = "data unavailable"
            panel.paint()
            return "error"
        _keep_newer_tick(panel.quote, quote)
        panel.quote = quote
        panel.symbol = quote.display
        panel.is_crypto = quote.is_crypto
        panel.error = None
        panel.paint()
        return "ok"

    async def _crypto_loop(self) -> None:
        failures = 0
        while True:
            if failures >= 4:
                await asyncio.sleep(60)
                failures = 0
            generation = self._stream_gen
            products = self._coinbase_products()
            if not products:
                self.crypto_connected = False
                await asyncio.sleep(0.5)
                continue
            try:
                async with websockets.connect(
                    "wss://ws-feed.exchange.coinbase.com",
                    open_timeout=8,
                    ping_interval=20,
                    ping_timeout=20,
                ) as socket:
                    await socket.send(
                        json.dumps(
                            {
                                "type": "subscribe",
                                "product_ids": products,
                                "channels": ["ticker"],
                            }
                        )
                    )
                    failures = 0
                    self.crypto_connected = True
                    while generation == self._stream_gen:
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=1.0)
                        except TimeoutError:
                            continue
                        self._apply_coinbase(raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.crypto_connected = False
                failures += 1
                await asyncio.sleep(min(15, 2 * failures))

    def _coinbase_products(self) -> list[str]:
        products: list[str] = []
        seen: set[str] = set()
        for panel in self.query(ChartPanel):
            if not panel.is_crypto:
                continue
            if panel.quote is not None and panel.quote.venue == "coinbase":
                product = panel.quote.yahoo
            else:
                product = f"{panel.symbol.split('-')[0]}-USD"
            if product not in seen:
                seen.add(product)
                products.append(product)
        return products

    def _apply_coinbase(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        if message.get("type") != "ticker":
            return
        product = str(message.get("product_id") or "")
        try:
            price = float(message["price"])
        except (KeyError, TypeError, ValueError):
            return
        base = product.split("-")[0]
        for panel in self.query(ChartPanel):
            quote = panel.shown_quote
            if quote is None or quote.venue != "coinbase" or quote.display != base:
                continue
            apply_trade(quote, price)
            panel.paint()

    def _refresh_status(self) -> None:
        try:
            self.query_one(StatusBar).refresh()
        except NoMatches:
            return


def run(states: list[PanelState] | None = None) -> None:
    TrackerApp(states or load_layout()).run()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tuistock",
        description="Realtime stock and crypto charts in the terminal.",
    )
    parser.add_argument(
        "--symbols",
        help="Comma-separated tickers for the four panels, e.g. TSLA,SPCX,BTC,SOL",
    )
    parser.add_argument(
        "--interval",
        choices=INTERVALS,
        help="Interval applied to every panel",
    )
    parser.add_argument(
        "--range",
        dest="range_key",
        choices=("1d", "5d", "1mo", "6mo", "1y"),
        help="Range applied to every panel",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Restore TSLA, SPCX, BTC, and SOL",
    )
    args = parser.parse_args(argv)
    states = default_states() if args.reset else load_layout()
    if args.symbols:
        symbols = [part.strip() for part in args.symbols.split(",") if part.strip()]
        if not symbols:
            parser.error("--symbols needs at least one ticker")
        for index, symbol in enumerate(symbols[:4]):
            if index >= len(states):
                break
            states[index].symbol = symbol.upper()
    if args.interval:
        for state in states:
            state.interval = args.interval
    if args.range_key:
        for state in states:
            state.range_key = clamp_range(state.interval, args.range_key)
    else:
        for state in states:
            state.range_key = clamp_range(state.interval, state.range_key)
    if args.reset or args.symbols or args.interval or args.range_key:
        save_layout(states)
    run(states)


def _guess_crypto(symbol: str) -> bool:
    try:
        _display, _yahoo, is_crypto = resolve_symbol(symbol)
    except ValueError:
        return False
    return is_crypto


def _keep_newer_tick(old: Quote | None, new: Quote) -> None:
    if (
        old is None
        or old.display != new.display
        or old.interval != new.interval
        or not old.candles
        or not new.candles
        or old.market_time < new.market_time
    ):
        return
    tick = old.price
    last = new.candles[-1]
    tolerance = INTERVAL_SECONDS.get(new.interval, 60)
    if abs(last.ts - old.candles[-1].ts) > tolerance:
        return
    last.high = max(last.high, tick)
    last.low = min(last.low, tick)
    last.close = tick
    new.price = tick
    new.market_time = old.market_time
    new.updated_at = old.updated_at


def _fit(left: Text, right: Text, width: int) -> Text:
    if width <= 0:
        return Text("")
    gap = width - left.cell_len - right.cell_len
    out = Text(no_wrap=True, overflow="crop")
    if gap >= 1:
        out.append(left)
        out.append(" " * gap)
        out.append(right)
        return out
    if right.cell_len and right.cell_len < width and left.cell_len > width // 2:
        room = width - right.cell_len - 1
        clipped = left.copy()
        clipped.truncate(max(0, room))
        out.append(clipped)
        out.append(" ")
        out.append(right)
        return out
    clipped = left.copy()
    clipped.truncate(width)
    out.append(clipped)
    return out


def _center(width: int, height: int, title: str, detail: str, style: str) -> Text:
    if width <= 0 or height <= 0:
        return Text("")
    text = Text(no_wrap=True, overflow="crop")
    mid = max(0, height // 2)
    for row in range(height):
        if row:
            text.append("\n")
        if row == mid - 1:
            text.append(title[:width].center(width), style="bold #f4f8f8")
        elif row == mid:
            text.append(detail[:width].center(width), style=style)
        else:
            text.append(" " * width)
    return text
