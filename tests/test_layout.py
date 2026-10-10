"""Layout persistence, CLI, and headless keyboard/rendering regressions."""

import json
import math
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from textual.containers import Grid

from tuistock.app import ChartBody, ChartPanel, StatusBar, SymbolInput, TrackerApp, main
from tuistock.config import (
    GRID_SIZES,
    MAX_PANELS,
    PanelState,
    config_path,
    default_states,
    load_layout,
    save_layout,
)
from tuistock.market import Candle, Quote


def sample_quote(panel):
    """Deterministic price/volume data without an external market request."""
    now = int(time.time())
    candles = []
    for index in range(60):
        price = 100 + index * 0.2 + math.sin(index / 4) * 3
        candles.append(Candle(now - (59 - index) * 300, price - 0.2,
                              price + 0.5, price - 0.5, price, 1000 + index * 10))
    return Quote(panel.symbol, f"{panel.symbol}-USD" if panel.is_crypto else panel.symbol,
                 panel.is_crypto, panel.interval, panel.range_key, candles,
                 candles[-1].close, 100, "America/New_York", now, time.time(),
                 "coinbase" if panel.is_crypto else "nasdaq")


class TemporaryConfig:
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)

    def write_config(self, payload):
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


class ConfigTests(TemporaryConfig, unittest.TestCase):
    def test_missing_config_defaults_to_2x2(self):
        layout = load_layout()
        self.assertEqual(layout.grid_size, 2)
        self.assertEqual(len(layout.panels), MAX_PANELS)
        self.assertEqual([panel.symbol for panel in layout.panels[:4]],
                         ["TSLA", "SPCX", "BTC", "SOL"])

    def test_all_layouts_round_trip_every_panel_including_hidden_settings(self):
        states = default_states()
        states[-1] = PanelState("NVDA", "1h", "1mo")
        for size in GRID_SIZES:
            with self.subTest(size=size):
                save_layout(states, size)
                layout = load_layout()
                self.assertEqual(layout.grid_size, size)
                self.assertEqual(layout.panels, states)

    def test_legacy_watchlist_keeps_symbols_and_defaults_to_2x2(self):
        self.write_config({"panels": [
            {"symbol": symbol, "interval": "1h", "range": "5d"}
            for symbol in ("aapl", "eth", "nvda", "spy")
        ]})
        layout = load_layout()
        self.assertEqual(layout.grid_size, 2)
        self.assertEqual(layout.panels[:4], [
            PanelState(symbol, "1h", "5d")
            for symbol in ("AAPL", "ETH", "NVDA", "SPY")
        ])
        self.assertEqual(len(layout.panels), MAX_PANELS)

    def test_malformed_configs_fall_back_to_defaults(self):
        for payload in (None, [], {}, {"panels": None}, {"panels": {}},
                        {"panels": "bad"}):
            with self.subTest(payload=payload):
                self.write_config(payload)
                self.assertEqual(load_layout().panels, default_states())
                self.assertEqual(load_layout().grid_size, 2)
        config_path().write_text("{", encoding="utf-8")
        self.assertEqual(load_layout().panels, default_states())

    def test_invalid_grid_sizes_use_default(self):
        for size in (0, 5, "3", True, None, [], {}):
            with self.subTest(size=size):
                self.write_config({"grid_size": size, "panels": []})
                self.assertEqual(load_layout().grid_size, 2)

    def test_bad_panel_does_not_move_later_panels(self):
        self.write_config({"grid_size": 3, "panels": [
            None, {"symbol": "nvda", "interval": "invalid", "range": "1y"}
        ]})
        layout = load_layout()
        self.assertEqual(layout.grid_size, 3)
        self.assertEqual(layout.panels[0], default_states()[0])
        self.assertEqual(layout.panels[1], PanelState("NVDA", "5m", "1mo"))


class CLITests(TemporaryConfig, unittest.TestCase):
    def test_each_layout_can_be_selected_and_saved(self):
        for size in GRID_SIZES:
            with self.subTest(size=size), patch("tuistock.app.run") as run:
                main(["--layout", f"{size}x{size}"])
                self.assertEqual(run.call_args.args[1], size)
                self.assertEqual(load_layout().grid_size, size)

    def test_saved_layout_is_used_without_flags(self):
        save_layout(default_states(), 4)
        with patch("tuistock.app.run") as run:
            main([])
        self.assertEqual(run.call_args.args[1], 4)

    def test_all_sixteen_symbols_and_common_settings(self):
        symbols = [f"TEST{index}" for index in range(MAX_PANELS)]
        with patch("tuistock.app.run") as run:
            main(["--layout", "4x4", "--symbols", ",".join(symbols),
                  "--interval", "1h", "--range", "5d"])
        states, size = run.call_args.args
        self.assertEqual(size, 4)
        self.assertEqual(states, [PanelState(symbol, "1h", "5d") for symbol in symbols])
        self.assertEqual(load_layout().panels, states)

    def test_invalid_layout_and_too_many_symbols_are_rejected(self):
        for arguments in (["--layout", "5x5"],
                          ["--symbols", ",".join(["AAPL"] * 17)]):
            with self.subTest(arguments=arguments), patch("sys.stderr"), \
                    patch("tuistock.app.run") as run:
                with self.assertRaises(SystemExit) as error:
                    main(arguments)
                self.assertEqual(error.exception.code, 2)
                run.assert_not_called()

    def test_reset_restores_default_grid_and_supports_layout_override(self):
        states = default_states()
        states[0].symbol = "NVDA"
        save_layout(states, 4)
        with patch("tuistock.app.run"):
            main(["--reset"])
            self.assertEqual(load_layout().grid_size, 2)
            self.assertEqual(load_layout().panels, default_states())
            main(["--reset", "--layout", "1x1"])
            self.assertEqual(load_layout().grid_size, 1)


class AppTests(TemporaryConfig, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        # Keep tests independent of external market services.
        for name in ("_poll_loop", "_crypto_loop"):
            mocked = patch.object(TrackerApp, name, new_callable=AsyncMock)
            mocked.start()
            self.addCleanup(mocked.stop)
        refresh = patch.object(TrackerApp, "request_refresh")
        self.refresh = refresh.start()
        self.addCleanup(refresh.stop)

    async def test_cycle_renders_all_four_grids_and_saves_size(self):
        app = TrackerApp(default_states(), 1)
        async with app.run_test(size=(160, 60)) as pilot:
            for size in GRID_SIZES:
                self.assertEqual(app.grid_size, size)
                panels = app.active_panels()
                self.assertEqual(len(panels), size ** 2)
                board = app.query_one("#board", Grid)
                self.assertEqual(board.styles.grid_size_columns, size)
                self.assertEqual(board.styles.grid_size_rows, size)
                for index, panel in enumerate(panels):
                    row, column = divmod(index, size)
                    self.assertEqual(panel.region.x, panels[column].region.x)
                    self.assertEqual(panel.region.y, panels[row * size].region.y)
                    self.assertGreater(panel.region.width, 0)
                    self.assertGreater(panel.region.height, 0)
                    self.assertLessEqual(panel.region.right, board.region.right)
                    self.assertLessEqual(panel.region.bottom, board.region.bottom)
                    if column:
                        self.assertGreater(panel.region.x, panels[index - 1].region.right)
                    if row:
                        self.assertGreater(panel.region.y, panels[index - size].region.bottom)
                self.assertIn(f"g {size}x{size}", app.query_one(StatusBar).render().plain)
                if size == 4:
                    app.action_focus_index(15)
                    await pilot.pause()
                    self.assertEqual(app.focused_panel().index, 15)
                await pilot.press("g")
                self.assertEqual(load_layout().grid_size, app.grid_size)
            self.assertEqual(app.grid_size, 1)
            self.assertEqual(app.focused_panel().index, 0)
            await pilot.press("4", "9")
            self.assertEqual(app.focused_panel().index, 0)

    async def test_arrows_use_current_grid_and_stop_at_edges(self):
        app = TrackerApp(default_states(), 1)
        async with app.run_test(size=(120, 48)) as pilot:
            for size in GRID_SIZES:
                app.action_focus_index(0)
                await pilot.press("left", "up")
                self.assertEqual(app.focused_panel().index, 0)
                for _ in range(size + 1):
                    await pilot.press("right")
                self.assertEqual(app.focused_panel().index, size - 1)
                for _ in range(size + 1):
                    await pilot.press("down")
                self.assertEqual(app.focused_panel().index, size ** 2 - 1)
                for _ in range(size + 1):
                    await pilot.press("left")
                self.assertEqual(app.focused_panel().index, size * (size - 1))
                await pilot.press("left")
                self.assertEqual(app.focused_panel().index, size * (size - 1))
                await pilot.press("g")

    async def test_numbers_and_tab_reach_panels_in_4x4(self):
        app = TrackerApp(default_states(), 4)
        async with app.run_test() as pilot:
            await pilot.press("9")
            self.assertEqual(app.focused_panel().index, 8)
            for _ in range(7):
                await pilot.press("tab")
            self.assertEqual(app.focused_panel().index, 15)
            await pilot.press("tab")
            self.assertEqual(app.focused_panel().index, 0)

    async def test_hidden_settings_survive_cycle_and_restart(self):
        app = TrackerApp(default_states(), 4)
        async with app.run_test() as pilot:
            panel = app.query_one("#p15", ChartPanel)
            panel.set_symbol("NVDA")
            panel.interval = "1h"
            panel.range_key = "1mo"
            app.persist()
            await pilot.press("g")
            self.assertFalse(panel.display)
            saved = load_layout()
            self.assertEqual(saved.grid_size, 1)
            self.assertEqual(saved.panels[15], PanelState("NVDA", "1h", "1mo"))
            await pilot.press("g", "g", "g")
            self.assertTrue(panel.display)
            self.assertEqual((panel.symbol, panel.interval, panel.range_key),
                             ("NVDA", "1h", "1mo"))
        restarted = TrackerApp(saved.panels, saved.grid_size)
        async with restarted.run_test() as pilot:
            await pilot.press("g", "g", "g")
            panel = restarted.query_one("#p15", ChartPanel)
            self.assertTrue(panel.display)
            self.assertEqual(panel.symbol, "NVDA")

    async def test_symbol_editor_keeps_layout_and_accepts_g_in_symbol(self):
        app = TrackerApp(default_states())
        async with app.run_test() as pilot:
            await pilot.press("e")
            editor = app.query_one(SymbolInput)
            await pilot.press("g")
            self.assertEqual(editor.value, "g")
            app.action_cycle_layout()
            self.assertEqual(app.grid_size, 2)
            await pilot.press("o", "o", "g", "enter")
            self.assertEqual(app.focused_panel().symbol, "GOOG")
            self.assertEqual(load_layout().panels[0].symbol, "GOOG")

    async def test_market_work_is_limited_to_visible_panels(self):
        app = TrackerApp(default_states(), 1)
        async with app.run_test() as pilot:
            for size in GRID_SIZES:
                with patch.object(app, "_refresh_panel", new_callable=AsyncMock,
                                  return_value="ok") as refresh:
                    await app._refresh_all()
                    self.assertEqual(refresh.await_count, size ** 2)
                    self.assertEqual([call.args[0].index for call in refresh.await_args_list],
                                     list(range(size ** 2)))
                with patch("tuistock.app.load_quote", new_callable=AsyncMock) as load:
                    if size < 4:
                        await app._refresh_panel(app.query_one("#p15", ChartPanel))
                        load.assert_not_awaited()
                expected_products = [f"{panel.symbol}-USD" for panel in app.active_panels()
                                     if panel.is_crypto]
                self.assertEqual(app._coinbase_products(), expected_products)
                self.refresh.reset_mock()
                await pilot.press("g")
                expected_new = list(range(size ** 2, app.grid_size ** 2))
                self.assertEqual([call.args[0].index for call in self.refresh.call_args_list],
                                 expected_new)

    async def test_all_layouts_render_in_small_terminal(self):
        app = TrackerApp(default_states(), 1)
        async with app.run_test(size=(40, 12)) as pilot:
            for size in GRID_SIZES:
                self.assertEqual(len(app.active_panels()), size ** 2)
                self.assertIn(f"g {size}x{size}", app.query_one(StatusBar).render().plain)
                await pilot.press("g")

    async def test_populated_charts_render_after_switching_layouts_and_resizing(self):
        app = TrackerApp(default_states(), 1)
        async with app.run_test(size=(160, 60)) as pilot:
            for panel in app.query(ChartPanel):
                panel.quote = sample_quote(panel)
                panel.paint()
            for size in GRID_SIZES:
                await pilot.pause()
                for panel in app.active_panels():
                    body = panel.query_one(ChartBody)
                    chart = body.render().plain
                    self.assertTrue(any("\u2801" <= char <= "\u28ff" for char in chart))
                    self.assertEqual(len(chart.splitlines()), body.size.height)
                await pilot.press("g")
            await pilot.resize_terminal(40, 12)
            await pilot.press("g", "g", "g")
            self.assertEqual(len(app.active_panels()), 16)
            for panel in app.active_panels():
                panel.query_one(ChartBody).render()


if __name__ == "__main__":
    unittest.main()
