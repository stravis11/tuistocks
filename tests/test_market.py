"""Feed fallback behaviour when providers refuse or rate-limit the client."""

import time
import unittest
from unittest.mock import patch

import httpx

from tuistock import market

NOW = int(time.time())
ROWS = [[NOW - i * 300, 99, 101, 100, 100 + i, 5] for i in range(20)]
COINBASE_PRODUCTS = {"ETH-USD": "2000", "META-USD": "3.78"}


def feeds(seen: list[str], nasdaq: str):
    """Yahoo 429; Nasdaq per `nasdaq`; Coinbase knows ETH-USD and META-USD only."""

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        seen.append(f"{host}{path}")
        if "yahoo" in host:
            return httpx.Response(429, text="Too Many Requests")
        if host == "api.nasdaq.com":
            if nasdaq == "blocked":
                return httpx.Response(403, text="Access Denied")
            return httpx.Response(200, json={"status": {"rCode": 400}, "data": None})
        product = path.split("/")[2]
        if product not in COINBASE_PRODUCTS:
            return httpx.Response(404, json={"message": "NotFound"})
        if path.endswith("/candles"):
            return httpx.Response(200, json=ROWS)
        return httpx.Response(200, json={"last": COINBASE_PRODUCTS[product], "open": "1", "volume": "7"})

    return httpx.MockTransport(handler)


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

    async def test_blocked_stock_reports_reason_and_stops_retrying(self):
        seen: list[str] = []
        async with httpx.AsyncClient(transport=feeds(seen, nasdaq="blocked")) as client:
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
        async with httpx.AsyncClient(transport=feeds(seen, nasdaq="blocked")) as client:
            with self.assertRaises(market.Blocked):
                await market.load_quote(client, "META", "5m", "1d")
            self.assertFalse(any("coinbase" in url for url in seen))

            quote = await market.load_quote(client, "ETH", "5m", "1d")
            self.assertEqual((quote.venue, quote.price), ("coinbase", 2000.0))

    async def test_unknown_ticker_coinbase_miss_is_remembered(self):
        seen: list[str] = []
        async with httpx.AsyncClient(transport=feeds(seen, nasdaq="not found")) as client:
            for _ in range(3):
                with self.assertRaisesRegex(market.MarketError, "No data for ZZZZ"):
                    await market.load_quote(client, "ZZZZ", "5m", "1d")
            coinbase_hits = [url for url in seen if "coinbase" in url]
            # One candles + one stats miss on the first poll, then nothing.
            self.assertLessEqual(len(coinbase_hits), 2)


if __name__ == "__main__":
    unittest.main()
