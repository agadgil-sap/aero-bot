"""Pin the live underlying-equity reference feed's honest provenance contract."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from json import dumps

import httpx
import pytest

from aero_bot.stock_reference import (
    FINNHUB_QUOTE_DOCS_URL,
    UNDERLYING_BY_B20_SYMBOL,
    FinnhubQuoteStockReferenceBackend,
    StockReferenceFeed,
    StockReferenceFeedResult,
    StockReferenceQuote,
    StockReferenceSession,
    StockReferenceUnavailableError,
    YahooChartStockReferenceBackend,
    verified_underlying_for,
)

# The chart meta below was captured from the live public endpoint on
# 2026-10-03 (a Friday evening UTC): the regular session closed at
# 16:00:01 New York (20:00:01 UTC) and the pre/regular/post windows are
# that day's, so a fetch at 23:58 UTC sits inside the post-market window.
NVDA_CHART_META = {
    "currency": "USD",
    "symbol": "NVDA",
    "exchangeName": "NMS",
    "fullExchangeName": "NasdaqGS",
    "instrumentType": "EQUITY",
    "regularMarketTime": 1790971201,
    "regularMarketPrice": 233.95,
    "currentTradingPeriod": {
        "pre": {"timezone": "EDT", "start": 1790928000, "end": 1790947800, "gmtoffset": -14400},
        "regular": {
            "timezone": "EDT",
            "start": 1790947800,
            "end": 1790971200,
            "gmtoffset": -14400,
        },
        "post": {"timezone": "EDT", "start": 1790971200, "end": 1790985600, "gmtoffset": -14400},
    },
    "gmtoffset": -14400,
    "exchangeTimezoneName": "America/New_York",
}
# 2026-10-02 23:58:00 UTC: inside the post-market window above.
POST_MARKET_FETCH_AT = datetime(2026, 10, 2, 23, 58, tzinfo=UTC)
# The provider's as-of for the captured close print.
NVDA_CLOSE_AS_OF = datetime(2026, 10, 2, 20, 0, 1, tzinfo=UTC)
# Finnhub's own documented sample response, carrying the "t" timestamp
# their formal schema omits.
FINNHUB_SAMPLE_BODY = {"c": 261.74, "h": 263.31, "l": 260.68, "o": 261.07, "pc": 259.45}


def yahoo_chart_response(meta: object, status: int = 200) -> httpx.Response:
    """Build one chart-endpoint response around the given meta."""
    return httpx.Response(status, json={"chart": {"result": [{"meta": meta}], "error": None}})


def yahoo_chart_raw_response(meta: object) -> httpx.Response:
    """Build one chart response whose raw body may carry Infinity or NaN."""
    body = dumps({"chart": {"result": [{"meta": meta}], "error": None}})
    return httpx.Response(200, content=body.encode())


def fixed_yahoo_backend(
    response: httpx.Response, *, fetched_at: datetime, sleeps: list[float] | None = None
) -> YahooChartStockReferenceBackend:
    """Create the Yahoo backend over one scripted transport and clock."""

    def handler(request: httpx.Request) -> httpx.Response:
        return response

    return YahooChartStockReferenceBackend(
        now=lambda: fetched_at,
        sleep=(lambda seconds: sleeps.append(seconds)) if sleeps is not None else None,
        transport=httpx.MockTransport(handler),
    )


class StubBackend:
    """Serve deterministic quotes or failures without any network."""

    provider_id = "stub"

    def __init__(
        self,
        *,
        quotes: dict[str, StockReferenceQuote] | None = None,
        failures: dict[str, Exception] | None = None,
    ) -> None:
        """Configure the scripted quotes and per-symbol failures."""
        self._quotes = quotes or {}
        self._failures = failures or {}
        self.calls: list[str] = []

    def fetch_underlying_quote(
        self, b20_symbol: str, underlying_symbol: str
    ) -> StockReferenceQuote:
        """Serve one scripted quote or raise its scripted failure."""
        self.calls.append(underlying_symbol)
        failure = self._failures.get(b20_symbol)
        if failure is not None:
            raise failure
        quote = self._quotes.get(b20_symbol)
        if quote is None:
            raise StockReferenceUnavailableError(f"no scripted quote for {b20_symbol}")
        return quote


def make_quote(
    b20_symbol: str,
    *,
    as_of: datetime,
    fetched_at: datetime,
    price: str = "100",
    session: StockReferenceSession = StockReferenceSession.REGULAR,
) -> StockReferenceQuote:
    """Build one valid quote for feed-level tests."""
    return StockReferenceQuote(
        provider_id="stub",
        b20_symbol=b20_symbol,
        underlying_symbol=b20_symbol.removesuffix("c"),
        price_usd=Decimal(price),
        currency="USD",
        as_of=as_of,
        fetched_at=fetched_at,
        session=session,
        delay_label="stub delay",
        exchange_name="Stub Exchange",
        source_url="https://stub.invalid/quote",
    )


class TestQuoteModel:
    """The immutable quote provenance contract."""

    def test_rejects_non_usd_currency(self) -> None:
        """A foreign-currency quote can never feed USDC-per-share math."""
        with pytest.raises(ValueError, match="currency must be USD"):
            StockReferenceQuote(
                provider_id="stub",
                b20_symbol="AAPLc",
                underlying_symbol="AAPL",
                price_usd=Decimal("100"),
                currency="EUR",
                as_of=POST_MARKET_FETCH_AT,
                fetched_at=POST_MARKET_FETCH_AT,
                session=StockReferenceSession.REGULAR,
                delay_label="",
                exchange_name="",
                source_url="",
            )

    def test_rejects_future_as_of_beyond_skew(self) -> None:
        """An observation dated after its own fetch is a fault, not fresh."""
        with pytest.raises(ValueError, match="later than its fetch"):
            make_quote(
                "AAPLc",
                as_of=POST_MARKET_FETCH_AT + timedelta(seconds=31),
                fetched_at=POST_MARKET_FETCH_AT,
            )

    def test_age_is_measured_from_the_provider_as_of(self) -> None:
        """Age rides the provider observation time, never the fetch."""
        quote = make_quote("AAPLc", as_of=NVDA_CLOSE_AS_OF, fetched_at=POST_MARKET_FETCH_AT)
        # 23:58 minus 20:00:01 is 14,279 seconds: a closed-market close
        # honestly reads hours old at the consumption instant.
        assert quote.age_seconds(POST_MARKET_FETCH_AT) == 14_279
        assert quote.provenance_line(POST_MARKET_FETCH_AT).startswith(
            "reference AAPLc=100 USD via stub as-of"
        )


class TestYahooBackend:
    """The keyless chart backend against live-captured payloads."""

    def test_parses_the_captured_close_with_post_market_session(self) -> None:
        """The captured meta yields price, as-of, session, and provenance."""
        backend = fixed_yahoo_backend(
            yahoo_chart_response(NVDA_CHART_META), fetched_at=POST_MARKET_FETCH_AT
        )
        quote = backend.fetch_underlying_quote("NVDAc", "NVDA")
        assert quote.provider_id == "yahoo-chart"
        assert quote.price_usd == Decimal("233.95")
        assert quote.as_of == NVDA_CLOSE_AS_OF
        assert quote.session is StockReferenceSession.POST_MARKET
        assert quote.delay_label == "unlabeled by provider"
        assert quote.exchange_name == "NasdaqGS"
        assert quote.currency == "USD"

    def test_regular_session_window_selects_regular(self) -> None:
        """A fetch inside the regular window reads as the regular session."""
        meta = dict(NVDA_CHART_META)
        # An in-session tick: 17:29:59 UTC sits inside the regular window
        # and its as-of trails the 17:30 fetch by one second.
        meta["regularMarketTime"] = 1790962199
        meta["regularMarketPrice"] = 233.7
        backend = fixed_yahoo_backend(
            yahoo_chart_response(meta),
            fetched_at=datetime(2026, 10, 2, 17, 30, tzinfo=UTC),
        )
        quote = backend.fetch_underlying_quote("NVDAc", "NVDA")
        assert quote.session is StockReferenceSession.REGULAR
        assert quote.as_of == datetime(2026, 10, 2, 17, 29, 59, tzinfo=UTC)

    def test_fetch_outside_every_window_reads_closed(self) -> None:
        """A weekend fetch with stale windows reads as closed, not fresh."""
        backend = fixed_yahoo_backend(
            yahoo_chart_response(NVDA_CHART_META),
            fetched_at=datetime(2026, 10, 4, 12, 0, tzinfo=UTC),
        )
        quote = backend.fetch_underlying_quote("NVDAc", "NVDA")
        assert quote.session is StockReferenceSession.CLOSED

    def test_missing_trading_period_reads_unknown(self) -> None:
        """No published windows leave only the as-of time speaking."""
        meta = dict(NVDA_CHART_META)
        meta.pop("currentTradingPeriod")
        backend = fixed_yahoo_backend(yahoo_chart_response(meta), fetched_at=POST_MARKET_FETCH_AT)
        quote = backend.fetch_underlying_quote("NVDAc", "NVDA")
        assert quote.session is StockReferenceSession.UNKNOWN

    def test_non_usd_currency_is_refused(self) -> None:
        """A foreign listing never feeds the USDC reference math."""
        meta = dict(NVDA_CHART_META)
        meta["currency"] = "EUR"
        backend = fixed_yahoo_backend(yahoo_chart_response(meta), fetched_at=POST_MARKET_FETCH_AT)
        with pytest.raises(StockReferenceUnavailableError, match="not USD"):
            backend.fetch_underlying_quote("NVDAc", "NVDA")

    def test_missing_price_or_as_of_is_refused(self) -> None:
        """No price, or no provider timestamp, is never relabeled fresh."""
        no_price = dict(NVDA_CHART_META)
        no_price.pop("regularMarketPrice")
        no_time = dict(NVDA_CHART_META)
        no_time.pop("regularMarketTime")
        for meta in (no_price, no_time):
            backend = fixed_yahoo_backend(
                yahoo_chart_response(meta), fetched_at=POST_MARKET_FETCH_AT
            )
            with pytest.raises(StockReferenceUnavailableError):
                backend.fetch_underlying_quote("NVDAc", "NVDA")

    def test_corrupt_timestamps_fail_the_symbol_closed(self) -> None:
        """A non-finite or out-of-range stamp is per-symbol evidence."""
        for corrupt_time in (float("inf"), float("nan"), 10**20):
            meta = dict(NVDA_CHART_META)
            meta["regularMarketTime"] = corrupt_time
            backend = fixed_yahoo_backend(
                yahoo_chart_raw_response(meta), fetched_at=POST_MARKET_FETCH_AT
            )
            with pytest.raises(StockReferenceUnavailableError, match="failed validation"):
                backend.fetch_underlying_quote("NVDAc", "NVDA")

    def test_nonfinite_trading_period_window_reads_unknown_not_closed(self) -> None:
        """Corrupt session windows are evidence, never a crash or a closed label."""

        def handler(request: httpx.Request) -> httpx.Response:
            meta = dict(NVDA_CHART_META)
            meta["currentTradingPeriod"] = {"regular": {"start": 1790947800, "end": float("inf")}}
            return yahoo_chart_raw_response(meta)

        backend = YahooChartStockReferenceBackend(
            now=lambda: POST_MARKET_FETCH_AT, transport=httpx.MockTransport(handler)
        )
        quote = backend.fetch_underlying_quote("NVDAc", "NVDA")
        # The session is diagnostic-only evidence: a window that does not
        # parse degrades the label to unknown instead of failing the quote
        # or asserting a closed market it cannot prove.
        assert quote.session is StockReferenceSession.UNKNOWN
        assert quote.as_of == NVDA_CLOSE_AS_OF

    def test_string_epoch_windows_bracketing_fetch_select_regular(self) -> None:
        """String-serialized windows still place an in-session fetch."""
        meta = dict(NVDA_CHART_META)
        # The provider has served exactly this variant live: every window
        # epoch serialized as a strict decimal-integer string.
        meta["currentTradingPeriod"] = {
            "pre": {
                "timezone": "EDT",
                "start": "1790928000",
                "end": "1790947800",
                "gmtoffset": -14400,
            },
            "regular": {
                "timezone": "EDT",
                "start": "1790947800",
                "end": "1790971200",
                "gmtoffset": -14400,
            },
            "post": {
                "timezone": "EDT",
                "start": "1790971200",
                "end": "1790985600",
                "gmtoffset": -14400,
            },
        }
        # An in-session tick matching the fixture's regular window.
        meta["regularMarketTime"] = 1790962199
        backend = fixed_yahoo_backend(
            yahoo_chart_response(meta),
            fetched_at=datetime(2026, 10, 2, 17, 30, tzinfo=UTC),
        )
        quote = backend.fetch_underlying_quote("NVDAc", "NVDA")
        assert quote.session is StockReferenceSession.REGULAR

    def test_malformed_window_evidence_reads_unknown_not_closed(self) -> None:
        """Present-but-corrupt windows never assert a closed market."""
        malformed_windows = [
            # Booleans are not epochs, whatever JSON calls them.
            {"regular": {"start": True, "end": "1790971200"}},
            # Loose strings are not strict decimal integers.
            {"regular": {"start": " 1790947800", "end": "1790971200"}},
            {"regular": {"start": "1790947800.0", "end": "1790971200"}},
            {"regular": {"start": "1_79_094_7800", "end": "1790971200"}},
            {"regular": {"start": "0x6AE5B9F0", "end": "1790971200"}},
            # Nonfinite floats are corrupt evidence.
            {"regular": {"start": 1790947800, "end": float("nan")}},
            # Epochs beyond the datetime-representable span.
            {"regular": {"start": 1790947800, "end": 10**20}},
            {"regular": {"start": 1790947800, "end": "99999999999999999999"}},
            # Inverted intervals are structurally broken.
            {"regular": {"start": 1790971200, "end": 1790947800}},
            # A window that is not a mapping at all.
            {"regular": "9:30 to 16:00"},
            # No window mappings published.
            {},
            # One corrupt window among valid ones keeps closed unprovable:
            # the fetch below sits inside the corrupted post window.
            {
                "pre": {"start": 1790928000, "end": 1790947800},
                "regular": {"start": 1790947800, "end": 1790971200},
                "post": {"start": 1790971200, "end": "later today"},
            },
        ]
        for windows in malformed_windows:
            meta = dict(NVDA_CHART_META)
            meta["currentTradingPeriod"] = windows
            # The raw builder carries the NaN case httpx's json encoder
            # refuses; every other case serializes identically through it.
            backend = fixed_yahoo_backend(
                yahoo_chart_raw_response(meta), fetched_at=POST_MARKET_FETCH_AT
            )
            quote = backend.fetch_underlying_quote("NVDAc", "NVDA")
            assert quote.session is StockReferenceSession.UNKNOWN, windows

    def test_nonobject_or_missing_regular_window_reads_unknown_not_closed(self) -> None:
        """Corrupt or absent regular evidence never proves a closed market."""
        pre = {"start": 1790928000, "end": 1790947800}
        post = {"start": 1790971200, "end": 1790985600}
        incomplete_periods = [
            # A published regular entry that is not an object.
            {"pre": pre, "regular": "9:30 to 16:00", "post": post},
            {"pre": pre, "regular": None, "post": post},
            {"pre": pre, "regular": 42, "post": post},
            {"pre": pre, "regular": True, "post": post},
            # The required regular window missing while siblings publish.
            {"pre": pre, "post": post},
        ]
        # An in-session tick matching the fixture's regular window: the
        # broken regular window is exactly the one that would have placed
        # the fetch, so its evidence can never support a closed label.
        meta = dict(NVDA_CHART_META)
        meta["regularMarketTime"] = 1790962199
        for periods in incomplete_periods:
            meta["currentTradingPeriod"] = periods
            backend = fixed_yahoo_backend(
                yahoo_chart_response(meta), fetched_at=datetime(2026, 10, 2, 17, 30, tzinfo=UTC)
            )
            quote = backend.fetch_underlying_quote("NVDAc", "NVDA")
            assert quote.session is StockReferenceSession.UNKNOWN, periods

    def test_valid_open_window_survives_a_corrupt_regular_sibling(self) -> None:
        """A cleanly published bracketing window still establishes OPEN."""
        meta = dict(NVDA_CHART_META)
        meta["currentTradingPeriod"] = {
            "pre": {"start": 1790928000, "end": 1790947800},
            "regular": "9:30 to 16:00",
            "post": {"start": 1790971200, "end": 1790985600},
        }
        # The fetch sits inside the valid post window, so the session is
        # known open despite the corrupt regular sibling.
        backend = fixed_yahoo_backend(yahoo_chart_response(meta), fetched_at=POST_MARKET_FETCH_AT)
        quote = backend.fetch_underlying_quote("NVDAc", "NVDA")
        assert quote.session is StockReferenceSession.POST_MARKET

    def test_missing_sibling_windows_read_unknown_not_closed(self) -> None:
        """An incomplete pre/regular/post set never proves a closed market."""
        regular = {"start": 1790947800, "end": 1790971200}
        post = {"start": 1790971200, "end": 1790985600}
        incomplete_periods = [
            # Only the regular window published: pre and post absent.
            {"regular": regular},
            # The pre window absent while regular and post publish.
            {"regular": regular, "post": post},
        ]
        # A Sunday fetch outside every published window: a closed label
        # could only come from trusting the absent windows to have missed
        # the fetch too, which is exactly what incomplete evidence cannot
        # prove.
        meta = dict(NVDA_CHART_META)
        for periods in incomplete_periods:
            meta["currentTradingPeriod"] = periods
            backend = fixed_yahoo_backend(
                yahoo_chart_response(meta), fetched_at=datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
            )
            quote = backend.fetch_underlying_quote("NVDAc", "NVDA")
            assert quote.session is StockReferenceSession.UNKNOWN, periods

    def test_valid_bracket_survives_missing_sibling_windows(self) -> None:
        """A cleanly published bracketing window still establishes OPEN."""
        meta = dict(NVDA_CHART_META)
        meta["currentTradingPeriod"] = {"regular": {"start": 1790947800, "end": 1790971200}}
        # The fetch sits inside the valid regular window, so the session is
        # known open even though pre and post never published.
        meta["regularMarketTime"] = 1790962199
        backend = fixed_yahoo_backend(
            yahoo_chart_response(meta), fetched_at=datetime(2026, 10, 2, 17, 30, tzinfo=UTC)
        )
        quote = backend.fetch_underlying_quote("NVDAc", "NVDA")
        assert quote.session is StockReferenceSession.REGULAR

    def test_whole_dollar_integer_price_is_accepted(self) -> None:
        """A bare JSON integer last sale is a valid positive price."""
        meta = dict(NVDA_CHART_META)
        meta["regularMarketPrice"] = 262
        backend = fixed_yahoo_backend(yahoo_chart_response(meta), fetched_at=POST_MARKET_FETCH_AT)
        quote = backend.fetch_underlying_quote("NVDAc", "NVDA")
        assert quote.price_usd == Decimal("262")

    def test_boolean_price_is_refused(self) -> None:
        """A JSON true is never a price despite its integer flavor."""
        meta = dict(NVDA_CHART_META)
        meta["regularMarketPrice"] = True
        backend = fixed_yahoo_backend(yahoo_chart_response(meta), fetched_at=POST_MARKET_FETCH_AT)
        with pytest.raises(StockReferenceUnavailableError, match="no positive price"):
            backend.fetch_underlying_quote("NVDAc", "NVDA")

    def test_chart_error_object_fails_closed(self) -> None:
        """The endpoint's own error object surfaces as evidence."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "chart": {
                        "result": None,
                        "error": {"code": "Not Found", "description": "No data found"},
                    }
                },
            )

        backend = YahooChartStockReferenceBackend(
            now=lambda: POST_MARKET_FETCH_AT, transport=httpx.MockTransport(handler)
        )
        with pytest.raises(StockReferenceUnavailableError, match="Not Found"):
            backend.fetch_underlying_quote("NVDAc", "NVDA")

    def test_transient_429_retries_once_then_succeeds(self) -> None:
        """One rate-limit hiccup retries after the backoff and passes."""
        calls: list[int] = []
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, json={"detail": "rate limited"})
            return yahoo_chart_response(NVDA_CHART_META)

        backend = YahooChartStockReferenceBackend(
            now=lambda: POST_MARKET_FETCH_AT,
            sleep=sleeps.append,
            transport=httpx.MockTransport(handler),
        )
        quote = backend.fetch_underlying_quote("NVDAc", "NVDA")
        assert quote.price_usd == Decimal("233.95")
        assert len(calls) == 2
        assert sleeps == [2.0]

    def test_persistent_429_fails_closed_with_status_evidence(self) -> None:
        """A persistent rate limit exhausts the budget and names the status."""
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"detail": "rate limited"})

        backend = YahooChartStockReferenceBackend(
            now=lambda: POST_MARKET_FETCH_AT,
            sleep=sleeps.append,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(StockReferenceUnavailableError, match="HTTP 429"):
            backend.fetch_underlying_quote("NVDAc", "NVDA")
        assert len(sleeps) == 1

    def test_transport_error_fails_closed_without_hanging(self) -> None:
        """A network fault surfaces as typed evidence after the retry."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        backend = YahooChartStockReferenceBackend(
            now=lambda: POST_MARKET_FETCH_AT,
            sleep=lambda seconds: None,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(StockReferenceUnavailableError, match="transport error"):
            backend.fetch_underlying_quote("NVDAc", "NVDA")

    def test_oversized_body_fails_closed(self) -> None:
        """A body above the byte bound is rejected before parsing."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * (1024 * 1024 + 1))

        backend = YahooChartStockReferenceBackend(
            now=lambda: POST_MARKET_FETCH_AT,
            transport=httpx.MockTransport(handler),
            max_response_bytes=1024 * 1024,
        )
        with pytest.raises(StockReferenceUnavailableError, match="above the configured"):
            backend.fetch_underlying_quote("NVDAc", "NVDA")

    def test_oversized_body_is_cut_off_mid_stream(self) -> None:
        """The byte bound stops the download itself, not just the parse."""
        pulled = 0

        def handler(request: httpx.Request) -> httpx.Response:
            def endless() -> Iterator[bytes]:
                nonlocal pulled
                while True:
                    pulled += 8192
                    yield b"x" * 8192

            return httpx.Response(200, content=endless())

        backend = YahooChartStockReferenceBackend(
            now=lambda: POST_MARKET_FETCH_AT,
            transport=httpx.MockTransport(handler),
            max_response_bytes=1024,
        )
        with pytest.raises(StockReferenceUnavailableError, match="above the configured"):
            backend.fetch_underlying_quote("NVDAc", "NVDA")
        # Only the one chunk that crossed the bound was pulled from the
        # endless body: the rest was never downloaded.
        assert pulled == 8192

    def test_invalid_underlying_ticker_is_refused_before_the_url(self) -> None:
        """Ticker metacharacters never reach a URL."""
        backend = fixed_yahoo_backend(
            yahoo_chart_response(NVDA_CHART_META), fetched_at=POST_MARKET_FETCH_AT
        )
        with pytest.raises(StockReferenceUnavailableError, match="valid exchange ticker"):
            backend.fetch_underlying_quote("AAPLc", "AAPL/../../secret")


class TestFinnhubBackend:
    """The documented keyed backend against its own sample contract."""

    def test_parses_the_documented_sample_with_timestamp(self) -> None:
        """The sample response yields a quote whose as-of is its own t."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["symbol"] == "AAPL"
            assert request.headers["X-Finnhub-Token"] == "sealed-token"
            return httpx.Response(200, json={**FINNHUB_SAMPLE_BODY, "t": 1582641000})

        backend = FinnhubQuoteStockReferenceBackend(
            "sealed-token",
            now=lambda: POST_MARKET_FETCH_AT,
            transport=httpx.MockTransport(handler),
        )
        quote = backend.fetch_underlying_quote("AAPLc", "AAPL")
        assert quote.provider_id == "finnhub-quote"
        assert quote.price_usd == Decimal("261.74")
        assert quote.as_of == datetime(2020, 2, 25, 14, 30, tzinfo=UTC)
        assert quote.session is StockReferenceSession.UNKNOWN
        assert FINNHUB_QUOTE_DOCS_URL in quote.delay_label

    def test_missing_timestamp_is_refused_not_relabeled_fresh(self) -> None:
        """A quote without the provider t is refused, never fetch-stamped."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=FINNHUB_SAMPLE_BODY)

        backend = FinnhubQuoteStockReferenceBackend(
            "sealed-token",
            now=lambda: POST_MARKET_FETCH_AT,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(StockReferenceUnavailableError, match="relabel the fetch"):
            backend.fetch_underlying_quote("AAPLc", "AAPL")

    def test_zero_current_price_is_refused(self) -> None:
        """Finnhub's zero-value placeholder never becomes a price."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"c": 0, "t": 1582641000})

        backend = FinnhubQuoteStockReferenceBackend(
            "sealed-token",
            now=lambda: POST_MARKET_FETCH_AT,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(StockReferenceUnavailableError, match="no positive current price"):
            backend.fetch_underlying_quote("AAPLc", "AAPL")

    def test_whole_dollar_integer_price_is_accepted_and_bool_refused(self) -> None:
        """A bare JSON integer last sale quotes; a JSON true never does."""

        def integer_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"c": 262, "t": 1582641000})

        def boolean_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"c": True, "t": 1582641000})

        backend = FinnhubQuoteStockReferenceBackend(
            "sealed-token",
            now=lambda: POST_MARKET_FETCH_AT,
            transport=httpx.MockTransport(integer_handler),
        )
        quote = backend.fetch_underlying_quote("AAPLc", "AAPL")
        assert quote.price_usd == Decimal("262")

        backend = FinnhubQuoteStockReferenceBackend(
            "sealed-token",
            now=lambda: POST_MARKET_FETCH_AT,
            transport=httpx.MockTransport(boolean_handler),
        )
        with pytest.raises(StockReferenceUnavailableError, match="no positive current price"):
            backend.fetch_underlying_quote("AAPLc", "AAPL")

    def test_out_of_range_timestamp_fails_the_symbol_closed(self) -> None:
        """A stamp no calendar holds is per-symbol evidence, never a crash."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"c": 261.74, "t": 10**20})

        backend = FinnhubQuoteStockReferenceBackend(
            "sealed-token",
            now=lambda: POST_MARKET_FETCH_AT,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(StockReferenceUnavailableError, match="failed validation"):
            backend.fetch_underlying_quote("AAPLc", "AAPL")

    def test_missing_token_fails_at_construction(self) -> None:
        """The keyed backend refuses to exist without its sealed token."""
        with pytest.raises(ValueError, match="non-empty API token"):
            FinnhubQuoteStockReferenceBackend("   ")


class TestUnderlyingMapping:
    """The verified B20-to-underlying table."""

    def test_every_registry_symbol_maps_to_a_verified_ticker(self) -> None:
        """All ten official B20 symbols carry reviewed mappings."""
        expected = {
            "NVDAc": "NVDA",
            "METAc": "META",
            "AAPLc": "AAPL",
            "GOOGLc": "GOOGL",
            "AMZNc": "AMZN",
            "MSFTc": "MSFT",
            "MSTRc": "MSTR",
            "SNDKc": "SNDK",
            "SPCXc": "SPCX",
            "TSLAc": "TSLA",
        }
        assert dict(UNDERLYING_BY_B20_SYMBOL) == expected
        assert verified_underlying_for("AAPLc") == "AAPL"

    def test_unmapped_symbol_fails_closed(self) -> None:
        """A registry symbol without a reviewed mapping never guesses."""
        assert verified_underlying_for("FIXc") is None


class TestStockReferenceFeed:
    """The feed's per-symbol isolation and honest ages."""

    def test_quotes_every_requested_symbol_in_deterministic_order(self) -> None:
        """Every mapped symbol is quoted and ordered by B20 symbol."""
        backend = StubBackend(
            quotes={
                "TSLAc": make_quote(
                    "TSLAc", as_of=NVDA_CLOSE_AS_OF, fetched_at=POST_MARKET_FETCH_AT
                ),
                "AAPLc": make_quote(
                    "AAPLc", as_of=NVDA_CLOSE_AS_OF, fetched_at=POST_MARKET_FETCH_AT
                ),
            }
        )
        feed = StockReferenceFeed(
            backend,
            underlying_by_symbol={"AAPLc": "AAPL", "TSLAc": "TSLA"},
            now=lambda: POST_MARKET_FETCH_AT,
        )
        result = feed.fetch_quotes(["TSLAc", "AAPLc"])
        assert [quote.b20_symbol for quote in result.quotes] == ["AAPLc", "TSLAc"]
        assert result.diagnostics[0] == "reference feed stub quoted 2 of 2 requested symbol(s)"

    def test_one_symbols_outage_never_blanks_the_board(self) -> None:
        """A failing symbol is diagnosed; the rest still quote."""
        backend = StubBackend(
            quotes={
                "AAPLc": make_quote(
                    "AAPLc", as_of=NVDA_CLOSE_AS_OF, fetched_at=POST_MARKET_FETCH_AT
                )
            },
            failures={"TSLAc": StockReferenceUnavailableError("HTTP 429")},
        )
        feed = StockReferenceFeed(
            backend,
            underlying_by_symbol={"AAPLc": "AAPL", "TSLAc": "TSLA"},
            now=lambda: POST_MARKET_FETCH_AT,
        )
        result = feed.fetch_quotes(["AAPLc", "TSLAc"])
        assert [quote.b20_symbol for quote in result.quotes] == ["AAPLc"]
        assert any("TSLAc unavailable via stub" in line for line in result.diagnostics)

    def test_unmapped_symbol_is_diagnosed_fail_closed(self) -> None:
        """A symbol without a mapping carries explicit evidence."""
        feed = StockReferenceFeed(
            StubBackend(), underlying_by_symbol={}, now=lambda: POST_MARKET_FETCH_AT
        )
        result = feed.fetch_quotes(["FIXc"])
        assert result.quotes == ()
        assert any("no verified underlying mapping" in line for line in result.diagnostics)

    def test_duplicate_symbols_read_the_provider_once(self) -> None:
        """A symbol repeated in one call is deduplicated, not re-read."""
        backend = StubBackend(
            quotes={
                "AAPLc": make_quote(
                    "AAPLc", as_of=NVDA_CLOSE_AS_OF, fetched_at=POST_MARKET_FETCH_AT
                )
            }
        )
        feed = StockReferenceFeed(
            backend, underlying_by_symbol={"AAPLc": "AAPL"}, now=lambda: POST_MARKET_FETCH_AT
        )
        result = feed.fetch_quotes(["AAPLc", "AAPLc"])
        assert backend.calls == ["AAPL"]
        assert [quote.b20_symbol for quote in result.quotes] == ["AAPLc"]

    def test_every_call_reads_the_provider_anew(self) -> None:
        """No quote is carried between calls: each read hits the provider."""
        backend = StubBackend(
            quotes={
                "AAPLc": make_quote(
                    "AAPLc", as_of=NVDA_CLOSE_AS_OF, fetched_at=POST_MARKET_FETCH_AT
                )
            }
        )
        feed = StockReferenceFeed(
            backend, underlying_by_symbol={"AAPLc": "AAPL"}, now=lambda: POST_MARKET_FETCH_AT
        )
        first = feed.fetch_quotes(["AAPLc"])
        second = feed.fetch_quotes(["AAPLc"])
        assert backend.calls == ["AAPL", "AAPL"]
        assert second.quotes == first.quotes

    def test_one_corrupt_timestamp_never_blanks_the_board(self) -> None:
        """A non-finite provider timestamp fails its symbol alone."""

        def handler(request: httpx.Request) -> httpx.Response:
            symbol = request.url.path.rsplit("/", 1)[-1]
            if symbol == "TSLA":
                meta = dict(NVDA_CHART_META)
                meta["regularMarketTime"] = float("inf")
                return yahoo_chart_raw_response(meta)
            return yahoo_chart_response(NVDA_CHART_META)

        feed = StockReferenceFeed(
            YahooChartStockReferenceBackend(
                now=lambda: POST_MARKET_FETCH_AT, transport=httpx.MockTransport(handler)
            ),
            underlying_by_symbol={"AAPLc": "AAPL", "TSLAc": "TSLA"},
            now=lambda: POST_MARKET_FETCH_AT,
        )
        result = feed.fetch_quotes(["AAPLc", "TSLAc"])
        assert [quote.b20_symbol for quote in result.quotes] == ["AAPLc"]
        assert any(
            "TSLAc unavailable via yahoo-chart" in line and "failed validation" in line
            for line in result.diagnostics
        )

    def test_closed_market_quote_reads_hours_old_at_consumption(self) -> None:
        """A Friday close fetched Friday night never passes a 300s bound."""
        backend = StubBackend(
            quotes={
                "AAPLc": make_quote(
                    "AAPLc",
                    as_of=NVDA_CLOSE_AS_OF,
                    fetched_at=POST_MARKET_FETCH_AT,
                    session=StockReferenceSession.POST_MARKET,
                )
            }
        )
        feed = StockReferenceFeed(
            backend, underlying_by_symbol={"AAPLc": "AAPL"}, now=lambda: POST_MARKET_FETCH_AT
        )
        result = feed.fetch_quotes(["AAPLc"])
        ages = result.age_seconds_by_symbol(POST_MARKET_FETCH_AT)
        # The poll ran seconds ago, yet the age honestly reads 14,279s:
        # the fetch can never relabel a closed-market close as fresh.
        assert ages["AAPLc"] == 14_279
        assert ages["AAPLc"] > 900

    def test_notes_expose_the_closed_market_conflict_with_the_247_ruling(self) -> None:
        """Non-regular sessions carry the explicit reassessment flag."""
        backend = StubBackend(
            quotes={
                "AAPLc": make_quote(
                    "AAPLc",
                    as_of=NVDA_CLOSE_AS_OF,
                    fetched_at=POST_MARKET_FETCH_AT,
                    session=StockReferenceSession.CLOSED,
                )
            }
        )
        feed = StockReferenceFeed(
            backend, underlying_by_symbol={"AAPLc": "AAPL"}, now=lambda: POST_MARKET_FETCH_AT
        )
        notes = feed.fetch_quotes(["AAPLc"]).notes(POST_MARKET_FETCH_AT)
        assert any("outside the regular session" in line and "24/7" in line for line in notes)
        assert any(line.startswith("reference AAPLc=") for line in notes)

    def test_regular_session_notes_carry_no_conflict_line(self) -> None:
        """A regular-session board needs no closed-market reassessment."""
        backend = StubBackend(
            quotes={
                "AAPLc": make_quote("AAPLc", as_of=NVDA_CLOSE_AS_OF, fetched_at=NVDA_CLOSE_AS_OF)
            }
        )
        feed = StockReferenceFeed(
            backend, underlying_by_symbol={"AAPLc": "AAPL"}, now=lambda: NVDA_CLOSE_AS_OF
        )
        notes = feed.fetch_quotes(["AAPLc"]).notes(NVDA_CLOSE_AS_OF)
        assert not any("outside the regular session" in line for line in notes)

    def test_result_model_rejects_unordered_or_duplicate_quotes(self) -> None:
        """Feed results stay deterministic and reproducible."""
        quote = make_quote("AAPLc", as_of=NVDA_CLOSE_AS_OF, fetched_at=POST_MARKET_FETCH_AT)
        with pytest.raises(ValueError, match="ordered by b20 symbol"):
            StockReferenceFeedResult(
                provider_id="stub",
                fetched_at=POST_MARKET_FETCH_AT,
                quotes=(
                    make_quote("TSLAc", as_of=NVDA_CLOSE_AS_OF, fetched_at=POST_MARKET_FETCH_AT),
                    quote,
                ),
                diagnostics=("summary",),
            )
        with pytest.raises(ValueError, match="not repeat a symbol"):
            StockReferenceFeedResult(
                provider_id="stub",
                fetched_at=POST_MARKET_FETCH_AT,
                quotes=(quote, quote),
                diagnostics=("summary",),
            )
