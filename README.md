# tuistock

Realtime stock and crypto charts in the terminal. Four panels, defaulting to TSLA, SPCX, BTC, and SOL.

```bash
uv run tuistock
```

`1`–`4` or the arrow keys focus a panel. `e` changes its symbol (`AAPL`, `ETH`, `BTC-USD`). `i` cycles the interval, `r` cycles the range, and `q` quits. The layout is saved in `~/.config/tuistock/layout.json`.

```bash
uv run tuistock --symbols AAPL,ETH,NVDA,SPY
uv run tuistock --reset
```

Stocks use Nasdaq's realtime quotes, and Yahoo candles when that feed is available so the volume bars can draw. Crypto uses Coinbase candles and the live trade feed. Buy and Sell match the chart layout; this app does not send orders. Multi-day stock charts use daily bars.
