"""Live candles from Nasdaq (stocks) and Coinbase (crypto)."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from curl_cffi import CurlError
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from tuistock.config import clamp_range

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

# Common crypto bases. Anything else is tried as a stock, then as BASE-USD.
CRYPTO_BASES = {
    "AAVE", "ADA", "ALGO", "APE", "APT", "ARB", "ATOM", "AVAX", "AXS", "BCH",
    "BNB", "BONK", "BTC", "CHZ", "COMP", "CRO", "CRV", "DASH", "DOGE", "DOT",
    "EGLD", "EOS", "ETC", "ETH", "FET", "FIL", "FLOW", "GRT", "HBAR", "ICP",
    "IMX", "INJ", "JUP", "KAS", "LDO", "LINK", "LTC", "MANA", "MKR", "NEAR",
    "OP", "PEPE", "PENDLE", "POL", "PYTH", "RENDER", "RUNE", "SAND", "SEI",
    "SHIB", "SNX", "SOL", "STX", "SUI", "TAO", "TIA", "TON", "TRX", "UNI",
    "WIF", "WLD", "XLM", "XMR", "XRP", "XTZ", "ZEC",
}

INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}

_SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,14}")


class MarketError(Exception):
    """The feed could not return candles for this request."""


class RateLimit(MarketError):
    """The feed asked us to slow down."""


class Blocked(MarketError):
    """The feed refused this client outright, regardless of symbol."""


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Quote:
    display: str
    yahoo: str
    is_crypto: bool
    interval: str
    range_key: str
    candles: list[Candle]
    price: float
    previous_close: float
    tz: str
    market_time: int
    updated_at: float
    venue: str
    session_volume: float = 0.0
    note: str | None = None


def normalize_symbol(raw: str) -> str:
    text = raw.strip().upper().replace(" ", "").replace("/", "-").lstrip("$")
    if text.endswith("-USDT"):
        text = text[:-5] + "-USD"
    if not _SYMBOL_RE.fullmatch(text):
        raise ValueError("Use a ticker like TSLA, BTC, or ETH-USD")
    return text


def resolve_symbol(raw: str) -> tuple[str, str, bool]:
    """Return (display, yahoo symbol, is_crypto)."""
    text = normalize_symbol(raw)
    if text.endswith("USDT") and "-" not in text and len(text) > 4:
        base = text[:-4]
        return base, f"{base}-USD", True
    if "-" in text:
        base, quote = text.split("-", 1)
        if quote in {"USD", "USDT"}:
            return base, f"{base}-USD", True
        return text, text, False
    if text in CRYPTO_BASES:
        return text, f"{text}-USD", True
    return text, text, False


_ET = ZoneInfo("America/New_York")

# Both network-level curl failures and HTTP-layer failures surface to the
# quote loaders as "data unavailable"; keep them in one tuple.
_HTTP_ERRORS: tuple[type[Exception], ...] = (CurlError, RequestException)


class MarketClient:
    """Async HTTP client that presents a Chrome TLS fingerprint.

    Yahoo and Nasdaq both block generic Python HTTP stacks by TLS fingerprint
    (Yahoo answers 429 and Nasdaq's Akamai edge 403 even when the headers
    match a browser), so requests go through curl_cffi impersonating Chrome.
    """

    def __init__(self, timeout: float = 8.0) -> None:
        self._session = AsyncSession(impersonate="chrome", timeout=timeout)

    async def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
    ):
        kwargs: dict = {}
        if params:
            kwargs["params"] = params
        if headers:
            kwargs["headers"] = headers
        if timeout is not None:
            kwargs["timeout"] = timeout
        return await self._session.get(url, **kwargs)

    async def aclose(self) -> None:
        await self._session.close()


class MarketClientProtocol(Protocol):
    """The GET/Close surface the feed loaders need; tests fake this."""

    async def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
    ) -> Any: ...

    async def aclose(self) -> None: ...


_NASDAQ_HEADERS = {
    **HEADERS,
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
_ASSET_CLASS: dict[str, str] = {}
_yahoo_blocked_until = 0.0
# A Nasdaq 403 is Akamai refusing the client, not the symbol, so retrying it on
# every poll only adds load. Wait before asking again.
_NASDAQ_BLOCK_SECONDS = 600
_nasdaq_blocked_until = 0.0
# Stocks fall back to BASE-USD on Coinbase. Remember the misses so a stock
# watchlist does not drain Coinbase's per-IP limit that crypto panels rely on.
_COINBASE_MISSING_SECONDS = 3600
_COINBASE_MISSING: dict[str, float] = {}
_YAHOO_URLS = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
    "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
)
_RANGE_SECONDS = {"1d": 86_400, "5d": 5 * 86_400, "1mo": 31 * 86_400, "6mo": 183 * 86_400, "1y": 366 * 86_400}

# Alpaca is the primary stock feed when API keys are configured: it is an
# official paid/entitled API, so no bot protection to outsmart, and the free
# plan's 200 req/min comfortably fits a batched-snapshot poll per tick.
_ALPACA_BASE = "https://data.alpaca.markets"
_ALPACA_TIMEFRAMES = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour", "1d": "1Day"}
_ALPACA_RANGE_DAYS = {"1d": 5, "5d": 7, "1mo": 35, "6mo": 190, "1y": 370}
_ALPACA_MAX_PAGES = 5
_alpaca_keys: tuple[str, str] | None = None
# Bad keys (401/403) will not fix themselves mid-session; stop asking.
_alpaca_disabled = False
_ALPACA_SNAPSHOT_TTL = 8.0
_ALPACA_SNAPSHOTS: dict[str, tuple[float, dict]] = {}


def configure_alpaca(keys: tuple[str, str] | None) -> None:
    global _alpaca_keys, _alpaca_disabled
    _alpaca_keys = keys
    _alpaca_disabled = False


def alpaca_enabled() -> bool:
    return _alpaca_keys is not None and not _alpaca_disabled


def _alpaca_headers() -> dict:
    assert _alpaca_keys is not None
    key_id, secret = _alpaca_keys
    return {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}


def _reject_alpaca_auth(response) -> None:
    global _alpaca_disabled
    if response.status_code in (401, 403):
        _alpaca_disabled = True


async def prefetch_snapshots(client: MarketClientProtocol, symbols: list[str]) -> None:
    """Fetch latest trades for many tickers in one request and cache them.

    Called once per poll tick so per-panel price patches read the cache
    instead of sending one request per panel.
    """
    if not symbols:
        return
    try:
        response = await client.get(
            f"{_ALPACA_BASE}/v2/stocks/snapshots",
            params={"symbols": ",".join(sorted(set(symbols))), "feed": "iex"},
            headers=_alpaca_headers(),
        )
    except _HTTP_ERRORS as exc:
        raise MarketError("data unavailable") from exc
    _reject_alpaca_auth(response)
    if response.status_code == 429:
        raise RateLimit("rate limited")
    if response.status_code >= 400:
        raise MarketError("data unavailable")
    try:
        payload = response.json()
    except ValueError as exc:
        raise MarketError("data unavailable") from exc
    if not isinstance(payload, dict):
        raise MarketError("data unavailable")
    now = time.time()
    for symbol, snapshot in payload.items():
        if isinstance(snapshot, dict):
            _ALPACA_SNAPSHOTS[str(symbol).upper()] = (now, snapshot)


def _cached_alpaca_snapshot(display: str) -> dict | None:
    entry = _ALPACA_SNAPSHOTS.get(display.upper())
    if entry is None or time.time() - entry[0] > _ALPACA_SNAPSHOT_TTL:
        return None
    return entry[1]


async def _alpaca_snapshot(client: MarketClientProtocol, display: str) -> dict:
    cached = _cached_alpaca_snapshot(display)
    if cached is not None:
        return cached
    try:
        response = await client.get(
            f"{_ALPACA_BASE}/v2/stocks/snapshots",
            params={"symbols": display.upper(), "feed": "iex"},
            headers=_alpaca_headers(),
        )
    except _HTTP_ERRORS as exc:
        raise MarketError("data unavailable") from exc
    _reject_alpaca_auth(response)
    if response.status_code == 429:
        raise RateLimit("rate limited")
    if response.status_code >= 400:
        raise MarketError("data unavailable")
    try:
        payload = response.json()
    except ValueError as exc:
        raise MarketError("data unavailable") from exc
    snapshot = (payload or {}).get(display.upper())
    if not isinstance(snapshot, dict):
        raise MarketError(f"No data for {display}")
    _ALPACA_SNAPSHOTS[display.upper()] = (time.time(), snapshot)
    return snapshot


def _epoch(ts_text: str) -> int:
    try:
        return int(datetime.strptime(ts_text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC")).timestamp())
    except (TypeError, ValueError):
        return int(time.time())


async def _alpaca_quote(
    client: MarketClientProtocol,
    display: str,
    interval: str,
    range_key: str,
) -> Quote:
    timeframe = _ALPACA_TIMEFRAMES[interval]
    days = _ALPACA_RANGE_DAYS.get(range_key, 370)
    end = datetime.now(ZoneInfo("UTC"))
    start = end - timedelta(days=days)
    candles: list[Candle] = []
    page_token: str | None = None
    for _ in range(_ALPACA_MAX_PAGES):
        params: dict = {
            "symbols": display.upper(),
            "timeframe": timeframe,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": "10000",
            "feed": "iex",
        }
        if page_token:
            params["page_token"] = page_token
        try:
            response = await client.get(
                f"{_ALPACA_BASE}/v2/stocks/bars", params=params, headers=_alpaca_headers()
            )
        except _HTTP_ERRORS as exc:
            raise MarketError("data unavailable") from exc
        _reject_alpaca_auth(response)
        if response.status_code == 429:
            raise RateLimit("rate limited")
        if response.status_code >= 400:
            raise MarketError(f"No data for {display}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketError("data unavailable") from exc
        rows = ((payload.get("bars") or {}).get(display.upper())) or []
        for row in rows:
            if not isinstance(row, dict) or row.get("t") is None:
                continue
            close = row.get("c")
            if close is None:
                continue
            candles.append(
                Candle(
                    ts=_epoch(str(row["t"])),
                    open=float(row.get("o") or close),
                    high=float(row.get("h") or close),
                    low=float(row.get("l") or close),
                    close=float(close),
                    volume=float(row.get("v") or 0),
                )
            )
        page_token = payload.get("next_page_token")
        if not page_token:
            break
    if len(candles) < 2:
        raise MarketError(f"No data for {display}")
    candles.sort(key=lambda candle: candle.ts)
    snapshot = await _alpaca_snapshot(client, display)
    trade = snapshot.get("latestTrade") or {}
    price = float(trade["p"]) if trade.get("p") is not None else candles[-1].close
    previous_bar = snapshot.get("prevDailyBar") or {}
    previous = float(previous_bar["c"]) if previous_bar.get("c") is not None else candles[0].open
    daily = snapshot.get("dailyBar") or {}
    session_volume = float(daily["v"]) if daily.get("v") is not None else 0.0
    candles[-1].close = price
    candles[-1].high = max(candles[-1].high, price)
    candles[-1].low = min(candles[-1].low, price)
    return Quote(
        display=display,
        yahoo=display,
        is_crypto=False,
        interval=interval,
        range_key=range_key,
        candles=candles,
        price=price,
        previous_close=previous,
        tz="America/New_York",
        market_time=_epoch(str(trade.get("t"))) if trade.get("t") else candles[-1].ts,
        updated_at=time.time(),
        venue="alpaca",
        session_volume=session_volume,
    )


def apply_trade(quote: Quote, price: float, volume: float | None = None) -> None:
    """Move the live price onto the current candle."""
    now = int(time.time())
    quote.price = price
    quote.market_time = now
    quote.updated_at = time.time()
    if volume is not None and volume > 0:
        quote.session_volume = volume
    bucket = INTERVAL_SECONDS.get(quote.interval, 60)
    if not quote.candles:
        quote.candles.append(Candle(now, price, price, price, price, 0.0))
        return
    last = quote.candles[-1]
    if quote.interval == "1d":
        last.close = price
        last.high = max(last.high, price)
        last.low = min(last.low, price)
        return
    open_ts = now - (now % bucket)
    if last.ts >= open_ts - bucket // 2:
        last.close = price
        last.high = max(last.high, price)
        last.low = min(last.low, price)
        return
    quote.candles.append(Candle(open_ts, price, price, price, price, 0.0))
    if len(quote.candles) > 4000:
        del quote.candles[:-3000]


async def load_quote(
    client: MarketClientProtocol,
    raw_symbol: str,
    interval: str,
    range_key: str,
) -> Quote:
    display, product, is_crypto = resolve_symbol(raw_symbol)
    range_key = clamp_range(interval, range_key)
    if is_crypto:
        try:
            return await _coinbase_quote(client, display, product, interval, range_key)
        except RateLimit:
            raise
        except MarketError as first:
            try:
                return await _nasdaq_quote(client, display, interval, range_key)
            except MarketError:
                raise first from None
    try:
        if alpaca_enabled():
            try:
                return await _alpaca_quote(client, display, interval, range_key)
            except RateLimit:
                pass
            except MarketError:
                pass
        return await _yahoo_quote(client, display, interval, range_key)
    except RateLimit:
        pass
    except MarketError:
        pass
    try:
        return await _nasdaq_quote(client, display, interval, range_key)
    except (Blocked, RateLimit):
        # Nasdaq could not say whether this is a stock. Falling back to Coinbase
        # here would chart an unrelated token that shares the ticker (e.g. META).
        raise
    except MarketError as first:
        try:
            return await _coinbase_quote(client, display, f"{display}-USD", interval, range_key)
        except MarketError:
            raise first from None


async def patch_price(client: MarketClientProtocol, quote: Quote) -> None:
    if quote.venue == "coinbase":
        response = await client.get(
            f"https://api.exchange.coinbase.com/products/{quote.yahoo}/ticker"
        )
        if response.status_code == 429:
            raise RateLimit("rate limited")
        if response.status_code >= 400:
            raise MarketError("data unavailable")
        payload = response.json()
        apply_trade(quote, float(payload["price"]))
        return
    if alpaca_enabled() and quote.venue in {"alpaca", "yahoo"}:
        # Price patches come from the batched snapshot prefetch; Nasdaq is
        # only hit if Alpaca has nothing for this ticker.
        try:
            snapshot = await _alpaca_snapshot(client, quote.display)
            trade = snapshot.get("latestTrade") or {}
            if trade.get("p") is not None:
                daily = snapshot.get("dailyBar") or {}
                apply_trade(
                    quote,
                    float(trade["p"]),
                    float(daily["v"]) if daily.get("v") is not None else None,
                )
                return
        except RateLimit:
            raise
        except MarketError:
            pass
    asset = quote.venue.split(":", 1)[1] if quote.venue.startswith("nasdaq:") else None
    if asset not in {"stocks", "etf"}:
        asset = await _asset_class(client, quote.display)
    info = await _nasdaq_json(
        client, f"https://api.nasdaq.com/api/quote/{quote.display}/info?assetclass={asset}"
    )
    primary = (info.get("data") or {}).get("primaryData") or {}
    price = _money(primary.get("lastSalePrice"))
    volume = _money(primary.get("volume")) if primary.get("volume") else None
    if price is None:
        raise MarketError("data unavailable")
    apply_trade(quote, price, volume)


async def _coinbase_quote(
    client: MarketClientProtocol,
    display: str,
    product: str,
    interval: str,
    range_key: str,
) -> Quote:
    if _COINBASE_MISSING.get(product, 0.0) > time.monotonic():
        raise MarketError(f"No data for {display}")
    granularity = INTERVAL_SECONDS[interval]
    span = _RANGE_SECONDS.get(range_key, 86_400)
    now = int(time.time())
    start = now - span - granularity * 60
    candles, stats = await asyncio.gather(
        _coinbase_candles(client, product, granularity, start, now),
        _coinbase_stats(client, product),
    )
    if len(candles) < 2:
        raise MarketError(f"No data for {display}")
    price = float(stats.get("last") or candles[-1].close)
    last = candles[-1]
    last.close = price
    last.high = max(last.high, price)
    last.low = min(last.low, price)
    previous = float(stats.get("open") or candles[0].open)
    return Quote(
        display=display,
        yahoo=product,
        is_crypto=True,
        interval=interval,
        range_key=range_key,
        candles=candles,
        price=price,
        previous_close=previous,
        tz="UTC",
        market_time=now,
        updated_at=time.time(),
        venue="coinbase",
        session_volume=float(stats.get("volume") or 0),
    )


async def _coinbase_stats(client: MarketClientProtocol, product: str) -> dict:
    response = await client.get(f"https://api.exchange.coinbase.com/products/{product}/stats")
    if response.status_code == 429:
        raise RateLimit("rate limited")
    if response.status_code == 404:
        _COINBASE_MISSING[product] = time.monotonic() + _COINBASE_MISSING_SECONDS
        raise MarketError(f"No data for {product.split('-')[0]}")
    if response.status_code >= 400:
        raise MarketError("data unavailable")
    payload = response.json()
    if not isinstance(payload, dict) or "last" not in payload:
        raise MarketError(f"No data for {product.split('-')[0]}")
    return payload


async def _coinbase_candles(
    client: MarketClientProtocol,
    product: str,
    granularity: int,
    start: int,
    end: int,
) -> list[Candle]:
    candles: list[Candle] = []
    cursor = end
    pages = 0
    while cursor > start and pages < 8:
        chunk_start = max(start, cursor - granularity * 290)
        response = await client.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            params={
                "granularity": str(granularity),
                "start": datetime.fromtimestamp(chunk_start, tz=ZoneInfo("UTC")).isoformat(),
                "end": datetime.fromtimestamp(cursor, tz=ZoneInfo("UTC")).isoformat(),
            },
        )
        pages += 1
        if response.status_code == 429:
            raise RateLimit("rate limited")
        if response.status_code == 404:
            _COINBASE_MISSING[product] = time.monotonic() + _COINBASE_MISSING_SECONDS
            raise MarketError(f"No data for {product.split('-')[0]}")
        if response.status_code >= 400:
            raise MarketError("data unavailable")
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            ts, low, high, open_, close, volume = row[:6]
            candles.append(
                Candle(int(ts), float(open_), float(high), float(low), float(close), float(volume))
            )
        cursor = chunk_start
    candles.sort(key=lambda candle: candle.ts)
    merged: list[Candle] = []
    for candle in candles:
        if merged and merged[-1].ts == candle.ts:
            merged[-1] = candle
        else:
            merged.append(candle)
    return merged


def _yahoo_span(interval: str, range_key: str) -> str:
    if interval == "1m":
        return "5d"
    if interval == "1d":
        return {"1d": "6mo", "5d": "6mo", "1mo": "1y", "6mo": "2y", "1y": "2y"}.get(range_key, "1y")
    if range_key == "1d":
        return "5d"
    return range_key


async def _yahoo_quote(
    client: MarketClientProtocol,
    display: str,
    interval: str,
    range_key: str,
) -> Quote:
    global _yahoo_blocked_until
    if time.monotonic() < _yahoo_blocked_until:
        raise RateLimit("rate limited")
    params = {
        "interval": interval,
        "range": _yahoo_span(interval, range_key),
        "includePrePost": "true",
    }
    last_error: MarketError = MarketError(f"No data for {display}")
    # US share classes are dashed on Yahoo (BRK-B) while every other dotted
    # ticker keeps its dot (SHOP.TO, VOD.L, 7203.T). Try the ticker as typed,
    # then the dashed spelling.
    symbols = [display]
    dashed = display.replace(".", "-")
    if dashed != display:
        symbols.append(dashed)
    for symbol in symbols:
        for template in _YAHOO_URLS:
            try:
                response = await client.get(template.format(symbol=symbol), params=params)
            except _HTTP_ERRORS as exc:
                last_error = MarketError("data unavailable")
                continue
            if response.status_code == 429:
                _yahoo_blocked_until = time.monotonic() + 120
                raise RateLimit("rate limited")
            if response.status_code >= 400:
                last_error = MarketError(f"No data for {display}")
                continue
            try:
                payload = response.json()
            except ValueError:
                last_error = MarketError("data unavailable")
                continue
            try:
                return _parse_yahoo(payload, display, interval, range_key)
            except MarketError as exc:
                last_error = exc
    raise last_error


def _parse_yahoo(payload: dict, display: str, interval: str, range_key: str) -> Quote:
    chart = payload.get("chart") or {}
    result = chart.get("result") or []
    error = chart.get("error")
    if error or not result:
        raise MarketError(f"No data for {display}")
    block = result[0]
    meta = block.get("meta") or {}
    if meta.get("instrumentType") == "CRYPTOCURRENCY":
        raise MarketError(f"No data for {display}")
    timestamps = block.get("timestamp") or []
    quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    candles: list[Candle] = []
    for index, ts in enumerate(timestamps):
        close = _at(closes, index)
        if ts is None or close is None:
            continue
        price = float(close)
        candles.append(
            Candle(
                ts=int(ts),
                open=float(_at(opens, index) if _at(opens, index) is not None else price),
                high=float(_at(highs, index) if _at(highs, index) is not None else price),
                low=float(_at(lows, index) if _at(lows, index) is not None else price),
                close=price,
                volume=float(_at(volumes, index) or 0),
            )
        )
    if len(candles) < 2:
        raise MarketError(f"No data for {display}")
    price = float(meta.get("regularMarketPrice") or candles[-1].close)
    previous = meta.get("chartPreviousClose") or meta.get("previousClose") or candles[0].open
    candles[-1].close = price
    candles[-1].high = max(candles[-1].high, price)
    candles[-1].low = min(candles[-1].low, price)
    session = float(meta.get("regularMarketVolume") or 0)
    return Quote(
        display=display,
        yahoo=str(meta.get("symbol") or display),
        is_crypto=False,
        interval=interval,
        range_key=range_key,
        candles=candles,
        price=price,
        previous_close=float(previous),
        tz=str(meta.get("exchangeTimezoneName") or "America/New_York"),
        market_time=int(meta.get("regularMarketTime") or candles[-1].ts),
        updated_at=time.time(),
        venue="yahoo",
        session_volume=session,
    )


def _at(values: list, index: int) -> float | None:
    if index >= len(values) or values[index] is None:
        return None
    return float(values[index])


async def _nasdaq_quote(
    client: MarketClientProtocol,
    display: str,
    interval: str,
    range_key: str,
) -> Quote:
    asset = await _asset_class(client, display)
    intraday = interval != "1d" and range_key == "1d"
    if intraday:
        info, chart = await asyncio.gather(
            _nasdaq_json(client, f"https://api.nasdaq.com/api/quote/{display}/info?assetclass={asset}"),
            _nasdaq_json(client, f"https://api.nasdaq.com/api/quote/{display}/chart?assetclass={asset}"),
        )
        candles = _aggregate_chart((chart.get("data") or {}).get("chart") or [], INTERVAL_SECONDS[interval])
        note = None
    else:
        start, end = _history_dates(range_key)
        info, history = await asyncio.gather(
            _nasdaq_json(client, f"https://api.nasdaq.com/api/quote/{display}/info?assetclass={asset}"),
            _nasdaq_json(
                client,
                "https://api.nasdaq.com/api/quote/"
                f"{display}/historical?assetclass={asset}&fromdate={start}&todate={end}&limit=1000",
            ),
        )
        candles = _daily_rows(history)
        note = None if interval == "1d" else "daily bars"
    primary = ((info.get("data") or {}).get("primaryData") or {})
    price = _money(primary.get("lastSalePrice"))
    change = _money(primary.get("netChange"))
    if price is not None and change is not None:
        previous = price - change
    else:
        previous = candles[0].open if candles else None
    volume = _money(primary.get("volume")) or 0.0
    if price is None or previous is None or len(candles) < 1:
        raise MarketError(f"No data for {display}")
    _attach_live_bar(candles, price, volume)
    if len(candles) < 2:
        last = candles[-1]
        candles.insert(0, Candle(last.ts - 60, last.open, last.high, last.low, last.open, 0.0))
    now = int(time.time())
    return Quote(
        display=display,
        yahoo=display,
        is_crypto=False,
        interval=interval,
        range_key=range_key,
        candles=candles,
        price=price,
        previous_close=previous,
        tz="America/New_York",
        market_time=now,
        updated_at=time.time(),
        venue=f"nasdaq:{asset}",
        session_volume=volume,
        note=note,
    )


async def _asset_class(client: MarketClientProtocol, symbol: str) -> str:
    cached = _ASSET_CLASS.get(symbol)
    if cached:
        return cached
    last_error = MarketError(f"No data for {symbol}")
    for asset in ("stocks", "etf"):
        try:
            payload = await _nasdaq_json(
                client, f"https://api.nasdaq.com/api/quote/{symbol}/info?assetclass={asset}"
            )
        except (Blocked, RateLimit):
            raise
        except MarketError as exc:
            last_error = exc
            continue
        status = (payload.get("status") or {}).get("rCode")
        data = payload.get("data") or {}
        primary = data.get("primaryData") or {}
        if status == 200 and data.get("symbol") and primary.get("lastSalePrice"):
            _ASSET_CLASS[symbol] = asset
            return asset
    raise last_error


async def _nasdaq_json(client: MarketClientProtocol, url: str) -> dict:
    global _nasdaq_blocked_until
    if time.monotonic() < _nasdaq_blocked_until:
        raise Blocked("Nasdaq blocked (403)")
    try:
        response = await client.get(url, headers=_NASDAQ_HEADERS)
    except _HTTP_ERRORS as exc:
        raise MarketError("data unavailable") from exc
    if response.status_code == 429:
        raise RateLimit("rate limited")
    if response.status_code == 403:
        _nasdaq_blocked_until = time.monotonic() + _NASDAQ_BLOCK_SECONDS
        raise Blocked("Nasdaq blocked (403)")
    if response.status_code >= 400:
        raise MarketError("data unavailable")
    try:
        payload = response.json()
    except ValueError as exc:
        raise MarketError("data unavailable") from exc
    if not isinstance(payload, dict):
        raise MarketError("data unavailable")
    return payload


def _exchange_epoch(ts: int) -> int:
    """Nasdaq chart times are the exchange clock stored as if it were UTC."""
    wall = datetime.fromtimestamp(ts, tz=ZoneInfo("UTC"))
    return int(wall.replace(tzinfo=_ET).timestamp())


def _aggregate_chart(points: list, bucket: int) -> list[Candle]:
    grouped: dict[int, list[float]] = {}
    for point in points:
        if not isinstance(point, dict):
            continue
        price = point.get("y")
        stamp = point.get("x")
        if price is None or stamp is None:
            continue
        ts = _exchange_epoch(int(stamp) // 1000)
        key = ts - (ts % bucket)
        grouped.setdefault(key, []).append(float(price))
    candles = [
        Candle(key, prices[0], max(prices), min(prices), prices[-1], 0.0)
        for key, prices in sorted(grouped.items())
    ]
    return candles


def _daily_rows(payload: dict) -> list[Candle]:
    rows = (((payload.get("data") or {}).get("tradesTable") or {}).get("rows") or [])
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            day = datetime.strptime(str(row.get("date")), "%m/%d/%Y")
        except ValueError:
            continue
        close = _money(row.get("close"))
        if close is None:
            continue
        open_ = _money(row.get("open")) or close
        high = _money(row.get("high")) or close
        low = _money(row.get("low")) or close
        volume = _money(row.get("volume")) or 0.0
        stamp = int(day.replace(tzinfo=_ET, hour=16).timestamp())
        candles.append(Candle(stamp, open_, high, low, close, volume))
    candles.sort(key=lambda candle: candle.ts)
    return candles


def _history_dates(range_key: str) -> tuple[str, str]:
    days = {"5d": 14, "1mo": 70, "6mo": 240, "1y": 420, "1d": 10}.get(range_key, 420)
    end = datetime.now().astimezone(_ET).date()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _attach_live_bar(candles: list[Candle], price: float, volume: float) -> None:
    today = datetime.now().astimezone(_ET).date()
    today_ts = int(datetime(today.year, today.month, today.day, 16, tzinfo=_ET).timestamp())
    if candles and datetime.fromtimestamp(candles[-1].ts, _ET).date() == today:
        last = candles[-1]
        last.close = price
        last.high = max(last.high, price)
        last.low = min(last.low, price)
        return
    candles.append(Candle(today_ts, price, price, price, price, volume))


def _money(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("$", "").replace(",", "").replace("+", "")
    if not text or text in {"N/A", "--", "UNCH"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number
