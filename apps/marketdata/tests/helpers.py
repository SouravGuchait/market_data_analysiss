"""Shared builders for the market-data tests."""

from datetime import date
from decimal import Decimal

from apps.instruments.models import AssetType, Exchange, Instrument
from apps.marketdata.models import PriceRecord, TradingCalendar


def create_exchange(code="NASDAQ", name="Nasdaq", timezone="America/New_York"):
    return Exchange.objects.create(code=code, name=name, timezone=timezone)


def default_exchange():
    """Every test instrument shares one exchange, so the code stays unique."""
    return Exchange.objects.get_or_create(
        code="NASDAQ",
        defaults={"name": "Nasdaq", "timezone": "America/New_York"},
    )[0]


def create_instrument(symbol, asset_type=AssetType.EQUITY, exchange=None, currency="USD"):
    return Instrument.objects.create(
        symbol=symbol,
        name=f"{symbol} Inc.",
        asset_type=asset_type,
        exchange=exchange or default_exchange(),
        currency=currency,
    )


def add_price(instrument, trading_date, open_, high, low, close, volume, adjusted_close=None, ingestion_job=None, source="test"):
    return PriceRecord.objects.create(
        instrument=instrument,
        trading_date=trading_date,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        adjusted_close=adjusted_close,
        ingestion_job=ingestion_job,
        source=source,
    )


def add_calendar(*dates):
    return TradingCalendar.objects.bulk_create([TradingCalendar(date=d) for d in dates])


# Five consecutive weekdays used as the standard sample period.
DAY1 = date(2024, 3, 11)
DAY2 = date(2024, 3, 12)
DAY3 = date(2024, 3, 13)
DAY4 = date(2024, 3, 14)
DAY5 = date(2024, 3, 15)
SAMPLE_DATES = [DAY1, DAY2, DAY3, DAY4, DAY5]

# (symbol, [closes], volume) - values are chosen so every notional is distinct.
SAMPLE_SERIES = [
    ("AAPL", ["100", "102", "101", "103", "104"], 1000),
    ("MSFT", ["200", "190", "195", "205", "210"], 500),
    ("GOOG", ["50", "51", "52", "53", "54"], 2000),
]

# Notional (close * volume) for the sample series, used as the expected result.
EXPECTED_TRADE_VALUE = {
    "AAPL": ["100000", "102000", "101000", "103000", "104000"],
    "MSFT": ["100000", "95000", "97500", "102500", "105000"],
    "GOOG": ["100000", "102000", "104000", "106000", "108000"],
}
TOTAL_TRADE_VALUE = Decimal("1530000")
TOTAL_VOLUME = Decimal("17500")

# Return over the period: (last close - first close) / first close.
EXPECTED_RETURN = {"GOOG": "0.08", "MSFT": "0.05", "AAPL": "0.04"}
EXPECTED_RETURN_ORDER = ["GOOG", "MSFT", "AAPL"]


def load_sample_series():
    """Loads the standard three-instrument five-day sample and returns them."""
    instruments = {}
    for symbol, closes, volume in SAMPLE_SERIES:
        instrument = create_instrument(symbol)
        instruments[symbol] = instrument
        for trading_date, close in zip(SAMPLE_DATES, closes):
            price = Decimal(close)
            add_price(
                instrument,
                trading_date,
                open_=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
            )
    add_calendar(*SAMPLE_DATES)
    return instruments
