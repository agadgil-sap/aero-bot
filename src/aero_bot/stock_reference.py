"""Bounded live underlying-equity reference quotes for the verified B20 board.

This module is the keyless real-market reference feed the policy engine's
reference gates consume: one read-only HTTP quote per underlying equity,
carrying provider provenance that keeps a delayed or closed-market last
price from ever being relabeled as fresh. The provider observation time -
never the fetch time - drives the quote age the 300-second entry bound,
the 900-second open-position bound, and the 0.15-percent dislocation
monitor read.
"""

import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG

# Yahoo's public chart endpoint is credential-free and returns the quote
# meta this adapter reads; it is a website-grade public endpoint without a
# published API contract, so the response fields this adapter depends on
# are pinned by fixtures captured from live responses and every structural
# surprise fails closed.
YAHOO_CHART_URL_TEMPLATE = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
)
# Finnhub documents the /quote endpoint (US stocks, API-key security) in its
# primary API reference; the sample response carries the "t" price
# timestamp even though the formal schema omits it, so the adapter requires
# "t" and fails closed when it is absent.
FINNHUB_QUOTE_URL: str = "https://finnhub.io/api/v1/quote"
FINNHUB_QUOTE_DOCS_URL: str = "https://finnhub.io/docs/api/quote"
# Ten seconds bounds one failed external request without stalling a cycle
# whose board sweep has already spent minutes on-chain.
REFERENCE_REQUEST_TIMEOUT_SECONDS = 10.0
# One MiB is far above one symbol's chart meta while still bounding memory
# use against a hostile or corrupt response; the read stops at the first
# chunk that crosses it instead of downloading the rest.
REFERENCE_MAX_RESPONSE_BYTES = 1024 * 1024
# One retry absorbs a single transient 429/5xx/network hiccup per symbol;
# anything persistent surfaces as explicit fail-closed evidence.
REFERENCE_FETCH_ATTEMPTS = 2
# The retry backoff respects a rate-limited provider instead of hammering it.
REFERENCE_RETRY_BACKOFF_SECONDS = 2.0
# A provider observation later than the fetch by more than this skew is a
# clock or data fault, never a fresh quote.
MAX_AS_OF_FUTURE_SKEW_SECONDS = 30
# Underlying tickers are plain uppercase exchange symbols; anything else is
# rejected before it can reach a URL.
UNDERLYING_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")
# Yahoo has served trading-period window epochs as strict decimal-integer
# strings as well as JSON numbers; fullmatch keeps the string alphabet to
# plain ASCII digits - no signs, whitespace, underscores, or fractions.
WINDOW_EPOCH_STRING_PATTERN = re.compile(r"[0-9]+")
# Window epochs must stay inside the datetime-representable span (year 1
# through 9999 UTC); anything wider is corrupt evidence, never a window.
MIN_WINDOW_EPOCH = int(datetime.min.replace(tzinfo=UTC).timestamp())
MAX_WINDOW_EPOCH = int(datetime.max.replace(tzinfo=UTC).timestamp())
# The documented contract is US-dollar quotes in USDC-per-share terms.
REFERENCE_CURRENCY = "USD"

# Each entry maps one official B20 registry symbol to its underlying
# listing ticker. Every pair below was verified live on 2026-10-03 against
# Yahoo's chart meta (currency USD, instrument EQUITY, NasdaqGS) and the
# official registry's own company name; the provider longName for MSTRc
# reads "Strategy Inc" because MicroStrategy renamed while keeping the
# MSTR ticker - the mapping is the ticker, which the rename left alone.
# A B20 symbol absent from this table has NO verified underlying mapping
# and fails closed with explicit evidence rather than a guessed ticker.
UNDERLYING_BY_B20_SYMBOL: Mapping[str, str] = {
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


class StockReferenceSession(StrEnum):
    """Describe the underlying market session a quote was observed in."""

    # Pre-market window as published by the provider's trading-period meta.
    PRE_MARKET = "pre_market"
    # The regular trading session; the only session the conservative
    # policy treats as trade-eligible reference evidence.
    REGULAR = "regular"
    # The post-market window after the regular session close.
    POST_MARKET = "post_market"
    # Outside every published window: overnight, weekend, or holiday.
    CLOSED = "closed"
    # The provider publishes no usable session windows - none at all, or
    # none this parser can trust; only the as-of time speaks.
    UNKNOWN = "unknown"


class StockReferenceUnavailableError(RuntimeError):
    """Signal that one bounded underlying-quote read could not complete."""


class StockReferenceQuote(BaseModel):
    """Carry one underlying quote with complete honest provenance."""

    # Frozen strict fields keep quote evidence immutable once validated.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Provider identity distinguishes the keyless feed from keyed ones.
    provider_id: str
    # The official B20 registry symbol the quote serves.
    b20_symbol: str
    # The underlying listing ticker the provider actually resolved.
    underlying_symbol: str
    # The provider's price in US dollars per one underlying share.
    price_usd: Annotated[Decimal, Field(gt=0)]
    # The quote currency; only USD is accepted for the USDC-per-share math.
    currency: str
    # The provider's own observation time for this price - the as-of that
    # drives every freshness decision, never the fetch time.
    as_of: datetime
    # When this adapter received the response carrying the quote.
    fetched_at: datetime
    # The session the provider's trading-period meta places the fetch in.
    session: StockReferenceSession
    # The provider's own delay claim, or an explicit unlabeled statement;
    # an unlabeled delay never borrows freshness from the fetch time.
    delay_label: str
    # The listing venue name the provider reported.
    exchange_name: str
    # The exact endpoint URL the quote was read from.
    source_url: str

    @model_validator(mode="after")
    def require_honest_timestamps_and_currency(self) -> "StockReferenceQuote":
        """Reject naive timestamps, wrong currency, and future observations."""
        # Both instants must carry offsets before any subtraction is safe.
        for stamp in (self.as_of, self.fetched_at):
            if stamp.utcoffset() is None:
                raise ValueError("stock reference timestamps must be timezone-aware")
        # Only USD quotes may feed the USDC-per-share policy surfaces.
        if self.currency != REFERENCE_CURRENCY:
            raise ValueError(
                f"stock reference currency must be {REFERENCE_CURRENCY}, not {self.currency}"
            )
        # An observation dated after its own fetch is a provider or clock
        # fault; the bounded skew mirrors the oracle-health future gate.
        if (self.as_of - self.fetched_at).total_seconds() > MAX_AS_OF_FUTURE_SKEW_SECONDS:
            raise ValueError("stock reference as-of is later than its fetch beyond the skew")
        return self

    def age_seconds(self, at: datetime) -> int:
        """Return whole elapsed seconds from the provider as-of to ``at``.

        Args:
            at: The consumption instant, timezone-aware (the decision time).

        Returns:
            The non-negative age; a provider timestamp inside the skew
            window still reads as zero, never negative.
        """
        return max(0, int((at - self.as_of).total_seconds()))

    def provenance_line(self, at: datetime) -> str:
        """Compose one human-readable provenance line for reports.

        Args:
            at: The consumption instant the age is measured at.

        Returns:
            One line naming price, provider, as-of, fetch time, session,
            delay, and venue so an operator can audit exactly what the
            reference was.
        """
        return (
            f"reference {self.b20_symbol}={self.price_usd} {self.currency} "
            f"via {self.provider_id} as-of {self.as_of.isoformat()} "
            f"(age {self.age_seconds(at)}s, session {self.session.value}, "
            f"delay {self.delay_label}, exchange {self.exchange_name}, "
            f"fetched {self.fetched_at.isoformat()})"
        )


class StockReferenceFeedResult(BaseModel):
    """Expose one bounded feed read over any number of board symbols."""

    # Frozen strict fields keep one feed snapshot coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The provider identity every quote in this result came from.
    provider_id: str
    # The instant the feed assembled this result.
    fetched_at: datetime
    # Verified quotes in deterministic b20-symbol order.
    quotes: tuple[StockReferenceQuote, ...]
    # Explicit evidence for every symbol left unquoted, plus the summary.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_deterministic_quote_order(self) -> "StockReferenceFeedResult":
        """Reject unordered or duplicate quotes and naive stamps."""
        # Deterministic order keeps report evidence reproducible.
        symbols = [quote.b20_symbol for quote in self.quotes]
        if symbols != sorted(symbols):
            raise ValueError("stock reference quotes must be ordered by b20 symbol")
        if len(set(symbols)) != len(symbols):
            raise ValueError("stock reference quotes must not repeat a symbol")
        if self.fetched_at.utcoffset() is None:
            raise ValueError("stock reference result stamps must be timezone-aware")
        return self

    def price_by_symbol(self) -> dict[str, Decimal]:
        """Map every quoted B20 symbol to its USD price."""
        return {quote.b20_symbol: quote.price_usd for quote in self.quotes}

    def age_seconds_by_symbol(self, at: datetime) -> dict[str, int]:
        """Map every quoted B20 symbol to its honest age at ``at``."""
        return {quote.b20_symbol: quote.age_seconds(at) for quote in self.quotes}

    def notes(self, at: datetime) -> tuple[str, ...]:
        """Compose the report notes carrying provenance and failure evidence.

        Args:
            at: The consumption instant ages and sessions are judged at.

        Returns:
            The summary line, one provenance line per quote, one line per
            diagnostic, and - when any quote sits outside the regular
            session - the closed-market conflict line that keeps the 24/7
            ruling and closed reference availability exposed for
            reassessment instead of silently reconciled.
        """
        lines = list(self.diagnostics)
        lines.extend(quote.provenance_line(at) for quote in self.quotes)
        outside_regular = tuple(
            quote for quote in self.quotes if quote.session is not StockReferenceSession.REGULAR
        )
        if outside_regular:
            sessions = ", ".join(
                f"{quote.b20_symbol}={quote.session.value}" for quote in outside_regular
            )
            lines.append(
                "reference market is outside the regular session "
                f"({sessions}); the 24/7 ruling keeps the cycle trading on "
                "pool authority with references diagnostic-only - "
                "closed-market reference availability versus 24/7 operation "
                "stays exposed for captain reassessment (docs/oracle-health.md)"
            )
        return tuple(lines)


@runtime_checkable
class StockReferenceBackend(Protocol):
    """Define the narrow read-only quote operation the feed depends on."""

    # The provider identity stamped on every quote this backend produces.
    provider_id: str

    def fetch_underlying_quote(
        self, b20_symbol: str, underlying_symbol: str
    ) -> StockReferenceQuote:
        """Return one verified underlying quote or raise unavailable."""
        ...


def verified_underlying_for(b20_symbol: str) -> str | None:
    """Return the verified underlying ticker for one B20 symbol, if any.

    Args:
        b20_symbol: The official registry symbol, like AAPLc.

    Returns:
        The verified underlying ticker, or None when the symbol carries no
        reviewed mapping (the feed then fails closed for that symbol).
    """
    return UNDERLYING_BY_B20_SYMBOL.get(b20_symbol)


class _BoundedHttpQuoteBackend:
    """Share the bounded request/retry/size skeleton between providers."""

    def __init__(
        self,
        *,
        provider_id: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        timeout_seconds: float = REFERENCE_REQUEST_TIMEOUT_SECONDS,
        max_response_bytes: int = REFERENCE_MAX_RESPONSE_BYTES,
        attempts: int = REFERENCE_FETCH_ATTEMPTS,
        retry_backoff_seconds: float = REFERENCE_RETRY_BACKOFF_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Configure one bounded read-only HTTP quote backend.

        Args:
            provider_id: The stable provider identity for quote stamps.
            now: Injected clock producing timezone-aware fetch stamps.
            sleep: Injected delay used only for the bounded retry backoff.
            timeout_seconds: Complete request timeout in seconds.
            max_response_bytes: Maximum accepted response body size.
            attempts: Total attempts per symbol, including the first.
            retry_backoff_seconds: Delay before every retry.
            transport: Optional httpx transport for deterministic tests.
        """
        # Positive bounds keep the client configuration valid by construction.
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if attempts < 1:
            raise ValueError("attempts must be at least one")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        self.provider_id = provider_id
        self._now = now
        self._sleep = sleep
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._attempts = attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._transport = transport

    def _bounded_get(self, url: str, headers: dict[str, str]) -> object:
        """Fetch one URL with bounded retries or raise unavailable.

        Args:
            url: The exact endpoint URL to read.
            headers: The request headers, including the provider user agent.

        Returns:
            The JSON-decoded payload.

        Raises:
            StockReferenceUnavailableError: When every bounded attempt
                failed, when the final status is not a success, when the
                body exceeds the size bound while streaming, or when it is
                not valid JSON.
        """
        last_error = "no attempt was made"
        body: bytes | None = None
        # One shared client applies the complete per-request timeout and
        # rejects redirects so a moved endpoint surfaces as evidence.
        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            headers=headers,
            transport=self._transport,
        ) as client:
            request = client.build_request("GET", url)
            for attempt in range(1, self._attempts + 1):
                response: httpx.Response | None = None
                try:
                    response = client.send(request, stream=True)
                    if response.status_code // 100 == 2:
                        body = self._read_body_bounded(response)
                        break
                    last_error = f"HTTP {response.status_code}"
                except httpx.HTTPError as error:
                    last_error = f"transport error {type(error).__name__}: {error}"
                finally:
                    if response is not None:
                        response.close()
                # Only the attempts budget bounds the retries, and every
                # retry waits out the backoff first so a rate-limited
                # provider is respected rather than hammered.
                if attempt < self._attempts:
                    self._sleep(self._retry_backoff_seconds)
        if body is None:
            raise StockReferenceUnavailableError(
                f"{self.provider_id} request failed after {self._attempts} attempt(s): {last_error}"
            )
        try:
            # parse_float=Decimal preserves the provider's decimal price
            # exactly instead of routing through binary floats.
            return json.loads(body, parse_float=Decimal)
        except ValueError as error:
            raise StockReferenceUnavailableError(
                f"{self.provider_id} response was not valid JSON"
            ) from error

    def _read_body_bounded(self, response: httpx.Response) -> bytes:
        """Read one streamed body incrementally under the size bound.

        Args:
            response: The open success response whose body is being read.

        Returns:
            The complete body, at or under the configured byte bound.

        Raises:
            StockReferenceUnavailableError: When the body streams past the
                configured byte bound; the read stops at the first chunk
                that crosses it instead of downloading the rest.
        """
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self._max_response_bytes:
                raise StockReferenceUnavailableError(
                    f"{self.provider_id} response streamed {total} bytes, above the "
                    f"configured {self._max_response_bytes}-byte limit"
                )
            chunks.append(chunk)
        return b"".join(chunks)


class YahooChartStockReferenceBackend(_BoundedHttpQuoteBackend):
    """Read one underlying quote from Yahoo's public chart endpoint."""

    def __init__(self, **kwargs: object) -> None:
        """Create the keyless Yahoo chart backend.

        Args:
            kwargs: Bounded-behavior overrides forwarded to the base class.
        """
        super().__init__(provider_id="yahoo-chart", **kwargs)  # type: ignore[arg-type]

    def fetch_underlying_quote(
        self, b20_symbol: str, underlying_symbol: str
    ) -> StockReferenceQuote:
        """Fetch and validate one underlying's chart-meta quote.

        Args:
            b20_symbol: The B20 symbol the quote is destined for.
            underlying_symbol: The verified underlying ticker, like AAPL.

        Returns:
            A complete quote with session, currency, and as-of provenance.

        Raises:
            StockReferenceUnavailableError: On any bound, transport,
                status, structural, currency, price, or timestamp failure.
        """
        _require_valid_underlying(underlying_symbol)
        url = YAHOO_CHART_URL_TEMPLATE.format(symbol=underlying_symbol)
        payload = self._bounded_get(url, {"User-Agent": "aero-bot/0.1 read-only-stock-reference"})
        # The endpoint reports its own failures as a chart error object.
        chart = _require_mapping(
            payload.get("chart") if isinstance(payload, dict) else None, "chart"
        )
        error = chart.get("error")
        if error is not None:
            raise StockReferenceUnavailableError(
                f"yahoo-chart reported an error for {underlying_symbol}: {error}"
            )
        results = chart.get("result")
        if not isinstance(results, list) or not results:
            raise StockReferenceUnavailableError(
                f"yahoo-chart returned no result for {underlying_symbol}"
            )
        result_entry = _require_mapping(results[0], "chart.result[0]")
        meta = _require_mapping(result_entry.get("meta"), "chart.result[0].meta")
        currency = meta.get("currency")
        if currency != REFERENCE_CURRENCY:
            raise StockReferenceUnavailableError(
                f"yahoo-chart quoted {underlying_symbol} in {currency!r}, not "
                f"{REFERENCE_CURRENCY}; refusing the quote"
            )
        price = meta.get("regularMarketPrice")
        if isinstance(price, bool) or not isinstance(price, (Decimal, int)) or price <= 0:
            raise StockReferenceUnavailableError(
                f"yahoo-chart returned no positive price for {underlying_symbol}"
            )
        if isinstance(price, int):
            price = Decimal(price)
        as_of_raw = meta.get("regularMarketTime")
        # The provider's own observation time is mandatory: a quote whose
        # as-of is unknown is never relabeled fresh by the fetch.
        if not isinstance(as_of_raw, (int, float)) or isinstance(as_of_raw, bool):
            raise StockReferenceUnavailableError(
                f"yahoo-chart returned no regularMarketTime for {underlying_symbol}; "
                "refusing to relabel the fetch as an observation time"
            )
        fetched_at = self._now()
        exchange = meta.get("fullExchangeName")
        exchange_name = exchange if isinstance(exchange, str) and exchange else "unlabeled"
        try:
            as_of = datetime.fromtimestamp(as_of_raw, tz=UTC)
            session = _yahoo_session(meta, fetched_at)
            return StockReferenceQuote(
                provider_id=self.provider_id,
                b20_symbol=b20_symbol,
                underlying_symbol=underlying_symbol,
                price_usd=price,
                currency=currency,
                as_of=as_of,
                fetched_at=fetched_at,
                session=session,
                # The endpoint publishes no delay claim; the honest label is
                # unlabeled, and freshness rides the as-of age alone.
                delay_label="unlabeled by provider",
                exchange_name=exchange_name,
                source_url=url,
            )
        except (ValueError, ArithmeticError, OSError) as error:
            raise StockReferenceUnavailableError(
                f"yahoo-chart quote for {underlying_symbol} failed validation: {error}"
            ) from error


class FinnhubQuoteStockReferenceBackend(_BoundedHttpQuoteBackend):
    """Read one underlying quote from Finnhub's documented /quote endpoint.

    Finnhub's API reference documents GET /quote for US stocks under API-key
    security and describes the endpoint as real-time US quote data. The
    documented response schema lists the price fields; the reference's own
    sample response additionally carries the ``t`` price timestamp, which
    this backend requires - a quote without a provider timestamp cannot be
    judged fresh and fails closed. The response carries no currency or
    session fields, so the quote is labeled with the endpoint's documented
    US-stocks scope and an unknown session rather than an invented one.
    """

    def __init__(self, api_token: str, **kwargs: object) -> None:
        """Create the keyed Finnhub quote backend.

        Args:
            api_token: The Finnhub API token (a free signup key sealed by
                the operator); never displayed or logged by this backend.
            kwargs: Bounded-behavior overrides forwarded to the base class.
        """
        if not api_token or not api_token.strip():
            raise ValueError("the Finnhub quote backend requires a non-empty API token")
        super().__init__(provider_id="finnhub-quote", **kwargs)  # type: ignore[arg-type]
        # The token lives only in request headers for this backend's lifetime.
        self._api_token = api_token.strip()

    def fetch_underlying_quote(
        self, b20_symbol: str, underlying_symbol: str
    ) -> StockReferenceQuote:
        """Fetch and validate one underlying's keyed quote.

        Args:
            b20_symbol: The B20 symbol the quote is destined for.
            underlying_symbol: The verified underlying ticker, like AAPL.

        Returns:
            A complete quote with the provider timestamp as its as-of.

        Raises:
            StockReferenceUnavailableError: On any bound, transport,
                status, structural, price, or timestamp failure.
        """
        _require_valid_underlying(underlying_symbol)
        request_url = f"{FINNHUB_QUOTE_URL}?symbol={underlying_symbol}"
        payload = self._bounded_get(
            request_url,
            {
                "User-Agent": "aero-bot/0.1 read-only-stock-reference",
                "X-Finnhub-Token": self._api_token,
            },
        )
        body = _require_mapping(payload, "quote body")
        price = body.get("c")
        if isinstance(price, bool) or not isinstance(price, (Decimal, int)) or price <= 0:
            raise StockReferenceUnavailableError(
                f"finnhub-quote returned no positive current price for {underlying_symbol}"
            )
        if isinstance(price, int):
            price = Decimal(price)
        as_of_raw = body.get("t")
        # The documented schema omits "t" while the reference's own sample
        # carries it; without it the quote has no observation time and is
        # refused rather than stamped with the fetch instant.
        if (
            not isinstance(as_of_raw, (int, float))
            or isinstance(as_of_raw, bool)
            or (as_of_raw <= 0)
        ):
            raise StockReferenceUnavailableError(
                f"finnhub-quote returned no price timestamp for {underlying_symbol}; "
                "refusing to relabel the fetch as an observation time"
            )
        fetched_at = self._now()
        try:
            return StockReferenceQuote(
                provider_id=self.provider_id,
                b20_symbol=b20_symbol,
                underlying_symbol=underlying_symbol,
                price_usd=price,
                # The payload has no currency field; USD is the endpoint's
                # documented denomination for US stocks, and the delay
                # label names that provenance.
                currency=REFERENCE_CURRENCY,
                as_of=datetime.fromtimestamp(as_of_raw, tz=UTC),
                fetched_at=fetched_at,
                session=StockReferenceSession.UNKNOWN,
                delay_label="real-time US quotes per provider documentation "
                f"({FINNHUB_QUOTE_DOCS_URL})",
                exchange_name="unlabeled by provider",
                source_url=FINNHUB_QUOTE_URL,
            )
        except (ValueError, ArithmeticError, OSError) as error:
            raise StockReferenceUnavailableError(
                f"finnhub-quote quote for {underlying_symbol} failed validation: {error}"
            ) from error


class StockReferenceFeed:
    """Serve honest per-symbol reference quotes for the B20 board.

    The feed resolves each B20 symbol through the verified underlying
    mapping, isolates every symbol's failures, and stamps ages from the
    provider's own as-of time so a delayed, held, or closed-market quote
    can never pass a freshness bound just because a poll ran.
    """

    def __init__(
        self,
        backend: StockReferenceBackend,
        *,
        underlying_by_symbol: Mapping[str, str] = UNDERLYING_BY_B20_SYMBOL,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Configure one feed over one read-only backend.

        Args:
            backend: The bounded provider backend serving quotes.
            underlying_by_symbol: The verified B20-to-underlying mapping;
                defaults to the reviewed registry table, tests may scope it.
            now: Injected clock producing timezone-aware stamps.
        """
        self._backend = backend
        self._underlying_by_symbol = underlying_by_symbol
        self._now = now

    def fetch_quotes(self, b20_symbols: Sequence[str]) -> StockReferenceFeedResult:
        """Read one bounded quote per requested B20 symbol, fail-closed.

        Args:
            b20_symbols: The board symbols needing quotes; duplicates are
                read once.

        Returns:
            A complete result with every verified quote in deterministic
            order and explicit diagnostics for every unquoted symbol; a
            fully failed read still returns a result with zero quotes and
            per-symbol evidence, never an exception.
        """
        quotes: list[StockReferenceQuote] = []
        diagnostics: list[str] = []
        # Deduplicate while preserving the caller's order for fetches.
        ordered_symbols = list(dict.fromkeys(b20_symbols))
        for b20_symbol in ordered_symbols:
            underlying = self._underlying_by_symbol.get(b20_symbol)
            if underlying is None:
                # An unmapped registry symbol is a corporate-action or
                # coverage gap: it fails closed with its own evidence.
                diagnostics.append(
                    f"reference symbol {b20_symbol} has no verified underlying mapping; "
                    "quote omitted fail-closed"
                )
                continue
            try:
                quote = self._backend.fetch_underlying_quote(b20_symbol, underlying)
            except StockReferenceUnavailableError as error:
                # One symbol's outage never blanks the board: the failure
                # is recorded and the remaining symbols still read.
                diagnostics.append(
                    f"reference symbol {b20_symbol} unavailable via "
                    f"{self._backend.provider_id}: {error}"
                )
                continue
            quotes.append(quote)
        fetched_at = self._now()
        diagnostics.insert(
            0,
            f"reference feed {self._backend.provider_id} quoted {len(quotes)} of "
            f"{len(ordered_symbols)} requested symbol(s)",
        )
        return StockReferenceFeedResult(
            provider_id=self._backend.provider_id,
            fetched_at=fetched_at,
            quotes=tuple(sorted(quotes, key=lambda quote: quote.b20_symbol)),
            diagnostics=tuple(diagnostics),
        )


def _require_valid_underlying(underlying_symbol: str) -> None:
    """Reject any underlying ticker that is not a plain exchange symbol.

    Args:
        underlying_symbol: The ticker about to enter a URL.

    Raises:
        StockReferenceUnavailableError: When the ticker could carry URL
            metacharacters or is not an uppercase exchange symbol.
    """
    if not UNDERLYING_SYMBOL_PATTERN.match(underlying_symbol):
        raise StockReferenceUnavailableError(
            f"underlying symbol {underlying_symbol!r} is not a valid exchange ticker"
        )


def _require_mapping(node: object, path: str) -> dict[str, object]:
    """Validate that one payload node is a JSON object.

    Args:
        node: The decoded node to check.
        path: The structural path named in failure evidence.

    Returns:
        The node as a plain mapping.

    Raises:
        StockReferenceUnavailableError: When the node is not an object.
    """
    if not isinstance(node, dict):
        raise StockReferenceUnavailableError(f"provider payload path {path!r} is not an object")
    return node


def _parse_window_epoch(value: object) -> int | None:
    """Parse one trading-period epoch, strict integer strings included.

    Args:
        value: The provider's raw ``start``/``end`` window field.

    Returns:
        The whole-second epoch, or None when the evidence is corrupt:
        booleans, non-numeric or non-strict strings, nonfinite floats, and
        epochs outside the datetime-representable span.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        epoch = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        epoch = int(value)
    elif isinstance(value, str) and WINDOW_EPOCH_STRING_PATTERN.fullmatch(value):
        epoch = int(value)
    else:
        return None
    if not MIN_WINDOW_EPOCH <= epoch <= MAX_WINDOW_EPOCH:
        return None
    return epoch


def _yahoo_session(meta: Mapping[str, object], fetched_at: datetime) -> StockReferenceSession:
    """Derive the session from the provider's trading-period windows.

    Args:
        meta: The chart meta mapping, possibly carrying
            ``currentTradingPeriod`` with pre/regular/post windows whose
            epochs arrive as JSON numbers or strict decimal-integer
            strings.
        fetched_at: The fetch instant the windows are judged against.

    Returns:
        The session containing the fetch, CLOSED when the fetch sits in no
        cleanly published window, or UNKNOWN when the provider published
        none - or published any window this parser cannot trust, which can
        never prove the market closed.
    """
    periods = meta.get("currentTradingPeriod")
    if not isinstance(periods, Mapping):
        return StockReferenceSession.UNKNOWN
    windows: list[tuple[StockReferenceSession, int, int]] = []
    corrupt_evidence = False
    session_by_key = {
        "pre": StockReferenceSession.PRE_MARKET,
        "regular": StockReferenceSession.REGULAR,
        "post": StockReferenceSession.POST_MARKET,
    }
    for key, session in session_by_key.items():
        if key not in periods:
            if key == "regular":
                # Published windows without the required regular one are
                # incomplete evidence: the missing window might have been
                # the one bracketing the fetch, so closed stays unproven.
                corrupt_evidence = True
            continue
        window = periods[key]
        if not isinstance(window, Mapping):
            # A published entry that is not an object is corrupt evidence
            # for the same reason: it might have been the bracketing window.
            corrupt_evidence = True
            continue
        start = _parse_window_epoch(window.get("start"))
        end = _parse_window_epoch(window.get("end"))
        if start is None or end is None or end <= start:
            # A published window that does not parse is corrupt evidence:
            # it might have been the one bracketing the fetch, so it can
            # never support a closed label.
            corrupt_evidence = True
            continue
        windows.append((session, start, end))
    for session, start, end in windows:
        if start <= fetched_at.timestamp() < end:
            return session
    if windows and not corrupt_evidence:
        # Every published window missed cleanly: overnight, weekend,
        # holiday, or a provider whose windows all predate the fetch.
        return StockReferenceSession.CLOSED
    return StockReferenceSession.UNKNOWN
