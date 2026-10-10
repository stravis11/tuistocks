# tuistock

Realtime stock and crypto charts in the terminal. Choose a 1×1, 2×2, 3×3, or 4×4 grid. The default 2×2 grid shows TSLA, SPCX, BTC, and SOL.

```bash
uv run tuistock
```

`g` cycles the grid size (1×1 → 2×2 → 3×3 → 4×4 → 1×1). The status bar shows the current size. `1`–`9` focus the first nine panels; arrow keys (or `h`, `j`, `k`, `l`) and Tab reach every panel. `e` changes the focused panel's symbol (`AAPL`, `ETH`, `BTC-USD`). `i` cycles the interval, `r` cycles the range, and `q` quits.

The grid size and all 16 panel settings are saved in `~/.config/tuistock/layout.json` (or under `$XDG_CONFIG_HOME` when set). Smaller grids keep hidden panels' settings for when you expand again. Hidden panels are excluded from polling and live subscriptions. Existing four-panel configuration files open as 2×2 grids.

```bash
uv run tuistock --symbols AAPL,ETH,NVDA,SPY
uv run tuistock --layout 1x1 --symbols BTC
uv run tuistock --layout 3x3 --symbols TSLA,SPCX,BTC,SOL,AAPL,NVDA,MSFT,AMZN,GOOG
uv run tuistock --layout 4x4
uv run tuistock --reset
```

`--symbols` accepts up to 16 tickers in row order. `--reset` restores the default symbols and 2×2 grid; combine it with `--layout` to start fresh at another size.

Stocks use Nasdaq's realtime quotes, and Yahoo candles when that feed is available so the volume bars can draw. Crypto uses Coinbase candles and the live trade feed. Multi-day stock charts use daily bars.

Run the layout regression tests with `uv run python -m unittest discover -s tests -v`. They use Textual's headless test runner and do not contact market services.
