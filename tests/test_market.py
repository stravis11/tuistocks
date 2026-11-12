"""Feed fallback behaviour when providers refuse or rate-limit the client."""

import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from tuistock import market

NOW = int(time.time())
ROWS = [[NOW - i * 300, 99, 101, 100, 100 + i, 5] for i in range(20)]
COINBASE_PRODUCTS = {"ETH-USD": "2000", "META-USD": "3.78"}


class FakeResponse:
    def __init__(self, status_code: int, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no JSON payload")
        return self._json


class FakeClient:
    """Duck-typed stand-in for market.MarketClient."""

    def __init__(self, handler):
        self._handler = handler

    async def get(self, url, params=None, headers=None, timeout=None):
        split = urlsplit(url)
        query = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        return self._handler(split.hostname, split.path, query)

    async def aclose(self) -> None:
        pass


def feeds(seen: list[str], nasdaq: str):
    """Yahoo 429; Nasdaq per `nasdaq`; Coinbase knows ETH-USD and META-USD only."""

    def handler(host: str, path: str, query: str = "") -> FakeResponse:
        seen.append(f"{host}{path}")
        if "yahoo" in host:
            return FakeResponse(429, text="Too Many Requests")
        if host == "api.nasdaq.com":
            if nasdaq == "blocked":
                return FakeResponse(403, text="Access Denied")
            return FakeResponse(200, json_data={"status": {"rCode": 400}, "data": None})
        product = path.split("/")[2]
        if product not in COINBASE_PRODUCTS:
            return FakeResponse(404, json_data={"message": "NotFound"})
        if path.endswith("/candles"):
            return FakeResponse(200, json_data=ROWS)
        return FakeResponse(200, json_data={"last": COINBASE_PRODUCTS[product], "open": "1", "volume": "7"})

    return FakeClient(handler)


def yahoo_payload(symbol: str) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "regularMarketPrice": 42.0,
                        "regularMarketTime": NOW,
                        "instrumentType": "EQUITY",
                        "exchangeTimezoneName": "America/New_York",
                    },
                    "timestamp": [NOW - 600, NOW - 300],
                    "indicators": {
                        "quote": [
                            {
                                "open": [41.0, 41.5],
                                "high": [43.0, 43.0],
                                "low": [40.0, 41.0],
                                "close": [42.0, 41.8],
                                "volume": [10, 10],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def yahoo_feeds(seen: list[str], known: set[str]):
    """Yahoo answers 200 only for symbols in `known`; everything else 404s."""

    def handler(host: str, path: str, query: str = "") -> FakeResponse:
        seen.append(f"{host}{path}")
        if "yahoo" in host:
            symbol = path.rsplit("/", 1)[-1]
            if symbol in known:
                return FakeResponse(200, json_data=yahoo_payload(symbol))
            return FakeResponse(404, json_data={"chart": {"error": {"code": "Not Found"}}})
        return FakeResponse(403, text="Access Denied")

    return FakeClient(handler)


class FeedFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in (
            ("_yahoo_blocked_until", 0.0),
            ("_nasdaq_blocked_until", 0.0),
            ("_COINBASE_MISSING", {}),
            ("_ASSET_CLASS", {}),
        ):
            patcher = patch.object(market, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        market.configure_alpaca(None)
        market._ALPACA_SNAPSHOTS.clear()

    async def test_blocked_stock_reports_reason_and_stops_retrying(self):
        seen: list[str] = []
        client = feeds(seen, nasdaq="blocked")
        with self.assertRaisesRegex(market.Blocked, r"Nasdaq blocked \(403\)"):
            await market.load_quote(client, "TSLA", "5m", "1d")
        self.assertEqual(sum("nasdaq" in url for url in seen), 1)
        self.assertFalse(any("coinbase" in url for url in seen))

        seen.clear()
        with self.assertRaises(market.Blocked):
            await market.load_quote(client, "TSLA", "5m", "1d")
        self.assertEqual(seen, [], "a blocked stock should not hit any feed on the next poll")

    async def test_blocked_stock_never_shows_same_ticker_crypto(self):
        seen: list[str] = []
        client = feeds(seen, nasdaq="blocked")
        with self.assertRaises(market.Blocked):
            await market.load_quote(client, "META", "5m", "1d")
        self.assertFalse(any("coinbase" in url for url in seen))

        quote = await market.load_quote(client, "ETH", "5m", "1d")
        self.assertEqual((quote.venue, quote.price), ("coinbase", 2000.0))

    async def test_unknown_ticker_coinbase_miss_is_remembered(self):
        seen: list[str] = []
        client = feeds(seen, nasdaq="not found")
        for _ in range(3):
            with self.assertRaisesRegex(market.MarketError, "No data for ZZZZ"):
                await market.load_quote(client, "ZZZZ", "5m", "1d")
        coinbase_hits = [url for url in seen if "coinbase" in url]
        # One candles + one stats miss on the first poll, then nothing.
        self.assertLessEqual(len(coinbase_hits), 2)

    async def test_international_ticker_loads_dotted_without_rewrite(self):
        seen: list[str] = []
        client = yahoo_feeds(seen, known={"SHOP.TO", "VOD.L", "7203.T"})
        for symbol in ("SHOP.TO", "VOD.L", "7203.T"):
            quote = await market.load_quote(client, symbol, "5m", "1d")
            self.assertEqual(quote.price, 42.0)
        # Every dotted ticker is sent as typed; the dashed spelling is only
        # tried after the dotted one fails.
        requested = [url.rsplit("/", 1)[-1] for url in seen if "yahoo" in url]
        self.assertEqual(requested, ["SHOP.TO", "VOD.L", "7203.T"])

    async def test_share_class_falls_back_to_dashed_spelling(self):
        seen: list[str] = []
        client = yahoo_feeds(seen, known={"BRK-B"})
        quote = await market.load_quote(client, "BRK.B", "5m", "1d")
        self.assertEqual((quote.yahoo, quote.price), ("BRK-B", 42.0))
        requested = [url.rsplit("/", 1)[-1] for url in seen if "yahoo" in url]
        # Dotted is tried on both mirrors before the dashed spelling.
        self.assertEqual(requested, ["BRK.B", "BRK.B", "BRK-B"])


ALPACA_BARS = [
    {"t": "2026-11-11T14:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 10},
    {"t": "2026-11-11T14:05:00Z", "o": 100.5, "h": 102, "l": 100, "c": 101, "v": 12},
]
ALPACA_SNAPSHOT = {
    "latestTrade": {"p": 123.45, "t": "2026-11-11T14:06:30Z"},
    "prevDailyBar": {"c": 120.0},
    "dailyBar": {"v": 5400},
}


def alpaca_feeds(
    seen: list[str],
    *,
    bars: dict | None = None,
    snapshots: dict | None = None,
    bars_status: int = 200,
    snapshot_status: int = 200,
    yahoo_known: set[str] | None = None,
):
    """Alpaca serves bars/snapshots per arguments; Yahoo per `yahoo_known`."""

    def handler(host: str, path: str, query: str = "") -> FakeResponse:
        seen.append(f"{host}{path}")
        if host == "data.alpaca.markets":
            if path.endswith("/bars"):
                if bars_status != 200:
                    return FakeResponse(bars_status, text="denied")
                return FakeResponse(200, json_data={"bars": bars or {}, "next_page_token": None})
            if path.endswith("/snapshots"):
                if snapshot_status != 200:
                    return FakeResponse(snapshot_status, text="denied")
                return FakeResponse(200, json_data=snapshots or {})
        if "yahoo" in host:
            symbol = path.rsplit("/", 1)[-1]
            if symbol in (yahoo_known or set()):
                return FakeResponse(200, json_data=yahoo_payload(symbol))
            return FakeResponse(404, json_data={"chart": {"error": {"code": "Not Found"}}})
        return FakeResponse(403, text="Access Denied")

    return FakeClient(handler)


class AlpacaFeedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        session = patch.object(market, "prefer_extended_feed", return_value=False)
        session.start()
        self.addCleanup(session.stop)
        market.configure_alpaca(("key-id", "key-secret"))
        market._ALPACA_SNAPSHOTS.clear()

    async def test_configured_alpaca_serves_stocks_first(self):
        seen: list[str] = []
        client = alpaca_feeds(
            seen,
            bars={"AAPL": ALPACA_BARS},
            snapshots={"AAPL": ALPACA_SNAPSHOT},
        )
        quote = await market.load_quote(client, "AAPL", "5m", "1d")
        self.assertEqual(quote.venue, "alpaca")
        self.assertEqual(quote.price, 123.45)
        self.assertEqual(quote.previous_close, 120.0)
        self.assertEqual(quote.session_volume, 5400)
        self.assertEqual(len(quote.candles), 2)

    async def test_network_failure_falls_back_to_yahoo(self):
        """An unreachable Alpaca must not escape load_quote as a raw error."""
        seen: list[str] = []

        def handler(host: str, path: str, query: str = "") -> FakeResponse:
            seen.append(f"{host}{path}")
            if host == "data.alpaca.markets":
                raise market.RequestException("connection refused (simulated offline)")
            symbol = path.rsplit("/", 1)[-1]
            if "yahoo" in host and symbol in {"TSLA"}:
                return FakeResponse(200, json_data=yahoo_payload(symbol))
            return FakeResponse(403, text="Access Denied")

        client = FakeClient(handler)
        quote = await market.load_quote(client, "TSLA", "5m", "1d")
        self.assertEqual(quote.venue, "yahoo")
        self.assertTrue(any("alpaca" in url for url in seen))

    async def test_prefetch_converts_network_failure_to_market_error(self):
        """The poll tick must see MarketError, not a network exception."""

        def handler(host: str, path: str, query: str = "") -> FakeResponse:
            raise market.RequestException("connection refused (simulated offline)")

        client = FakeClient(handler)
        with self.assertRaises(market.MarketError):
            await market.prefetch_snapshots(client, ["AAPL", "MSFT"])

    async def test_network_failure_cooldowns_alpaca_temporarily(self):
        """After a network failure, fallback is immediate until the cooldown ends."""
        seen: list[str] = []

        def handler(host: str, path: str, query: str = "") -> FakeResponse:
            seen.append(f"{host}{path}")
            if host == "data.alpaca.markets":
                raise market.RequestException("connection refused (simulated offline)")
            symbol = path.rsplit("/", 1)[-1]
            if "yahoo" in host and symbol == "TSLA":
                return FakeResponse(200, json_data=yahoo_payload(symbol))
            return FakeResponse(403, text="Access Denied")

        client = FakeClient(handler)
        quote = await market.load_quote(client, "TSLA", "5m", "1d")
        self.assertEqual(quote.venue, "yahoo")
        alpaca_hits = sum("alpaca" in url for url in seen)
        seen.clear()
        # Still inside the cooldown: no Alpaca attempt at all.
        self.assertFalse(market.alpaca_enabled())
        quote = await market.load_quote(client, "TSLA", "5m", "1d")
        self.assertEqual(quote.venue, "yahoo")
        self.assertFalse(any("alpaca" in url for url in seen))
        self.assertEqual(sum("alpaca" in url for url in seen), 0)
        self.assertEqual(
            alpaca_hits, 1, "only the first poll should pay the network timeout"
        )
        # Cooldown expired: Alpaca is retried.
        market._alpaca_down_until = 0.0
        seen.clear()
        await market.load_quote(client, "TSLA", "5m", "1d")
        self.assertTrue(any("alpaca" in url for url in seen))

    async def test_auth_failure_disables_alpaca_and_falls_back_to_yahoo(self):
        seen: list[str] = []
        client = alpaca_feeds(
            seen,
            bars_status=401,
            yahoo_known={"AAPL"},
        )
        quote = await market.load_quote(client, "AAPL", "5m", "1d")
        self.assertEqual(quote.venue, "yahoo")
        alpaca_hits = [url for url in seen if "alpaca" in url]
        seen.clear()
        await market.load_quote(client, "AAPL", "5m", "1d")
        self.assertFalse(
            any("alpaca" in url for url in seen),
            "rejected keys should not be retried on the next poll",
        )
        self.assertEqual(alpaca_hits, ["data.alpaca.markets/v2/stocks/bars"])

    async def test_prefetch_is_one_batched_request(self):
        seen: list[str] = []
        client = alpaca_feeds(seen, snapshots={"AAPL": ALPACA_SNAPSHOT})
        await market.prefetch_snapshots(client, ["NVDA", "AAPL", "MSFT"])
        alpaca_urls = [url for url in seen if "alpaca" in url]
        self.assertEqual(alpaca_urls, ["data.alpaca.markets/v2/stocks/snapshots"])

    async def test_patch_price_reads_prefetched_snapshot_without_request(self):
        seen: list[str] = []
        client = alpaca_feeds(seen, snapshots={"AAPL": ALPACA_SNAPSHOT})
        await market.prefetch_snapshots(client, ["AAPL"])
        seen.clear()
        quote = market.Quote(
            display="AAPL", yahoo="AAPL", is_crypto=False, interval="5m", range_key="1d",
            candles=[], price=100.0, previous_close=120.0,
            tz="America/New_York", market_time=0, updated_at=0.0, venue="alpaca",
        )
        await market.patch_price(client, quote)
        self.assertEqual(quote.price, 123.45)
        self.assertEqual(quote.session_volume, 5400)
        self.assertEqual(seen, [], "a fresh prefetched snapshot should serve the patch")


class ExtendedHoursTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        market.configure_alpaca(("key", "secret"))
        market._yahoo_blocked_until = 0

    def payload(self):
        payload = yahoo_payload("AAPL")
        block = payload["chart"]["result"][0]
        # November uses EST; 17:00 and 17:05 are after-hours.
        block["timestamp"] = [market._epoch("2026-11-11T22:00:00Z"), market._epoch("2026-11-11T22:05:00Z")]
        block["meta"]["regularMarketTime"] = market._epoch("2026-11-11T21:00:00Z")
        return payload

    def test_newer_after_hours_candle_wins_over_regular_price(self):
        quote = market._parse_yahoo(self.payload(), "AAPL", "5m", "1d")
        self.assertEqual(quote.price, 41.8)
        self.assertEqual(quote.market_time, quote.candles[-1].ts)
        self.assertEqual(quote.session, "after-hours")
        self.assertEqual(quote.candles[-1].session, "after-hours")

    def test_session_boundaries_and_early_close(self):
        for stamp, expected in (("13:00", "premarket"), ("14:30", "regular"), ("21:00", "after-hours")):
            self.assertEqual(market.stock_session(market._epoch(f"2026-11-11T{stamp}:00Z")), expected)
        ts = market._epoch("2026-11-27T18:30:00Z")
        self.assertEqual(market.stock_session(ts, periods={"post": {"start": ts-1800, "end": ts+3600}}), "after-hours")

    async def test_extended_load_and_patch_use_same_broader_feed(self):
        seen = []
        def handler(host, path, query):
            seen.append(host)
            self.assertIn("includePrePost=true", query)
            return FakeResponse(200, self.payload())
        with patch.object(market, "prefer_extended_feed", return_value=True):
            quote = await market.load_quote(FakeClient(handler), "AAPL", "5m", "1d")
            await market.patch_price(FakeClient(handler), quote)
        self.assertEqual(quote.venue, "yahoo")
        self.assertTrue(all("yahoo" in host for host in seen))
        self.assertEqual(len(quote.candles), 2)

    def test_stale_trade_does_not_create_current_candle(self):
        quote = market._parse_yahoo(self.payload(), "AAPL", "5m", "1d")
        before = (quote.price, quote.market_time, len(quote.candles))
        market.apply_trade(quote, 10, timestamp=quote.market_time-60)
        self.assertEqual(before, (quote.price, quote.market_time, len(quote.candles)))

    async def test_daily_chart_gets_extended_price(self):
        client = FakeClient(lambda *args: FakeResponse(200, self.payload()))
        quote = await market._yahoo_quote(client, "AAPL", "1d", "1mo")
        self.assertEqual((quote.price, quote.session), (41.8, "after-hours"))

    def test_chart_marks_extended_columns(self):
        from tuistock.chart import render_chart
        quote = market._parse_yahoo(self.payload(), "AAPL", "5m", "1d")
        chart = render_chart(width=60, height=14, candles=quote.candles, sma=[None, None], price=quote.price, previous_close=42, extended_sessions=True)
        self.assertIn("EXT: shaded", chart.plain)
        self.assertTrue(any("#30251c" in str(span.style) for span in chart.spans))


if __name__ == "__main__":
    unittest.main()
