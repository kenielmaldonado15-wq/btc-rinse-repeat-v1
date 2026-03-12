from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import sleep

from .config import StrategyConfig
from .models import Candle


def _to_candles(rows: list[list[float]]) -> list[Candle]:
    candles: list[Candle] = []
    for row in rows:
        ts_ms, o, h, l, c, v = row
        candles.append(
            Candle(
                timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(v),
            )
        )
    return candles


def _fetch_ohlcv_with_retry(exchange, symbol: str, timeframe: str, limit: int, max_retries: int, since_ms: int | None = None) -> list[list[float]]:
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return exchange.fetch_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit, since=since_ms)
        except Exception as exc:  # network / exchange errors
            last_error = exc
            if attempt < max_retries:
                sleep(1.0)
    raise RuntimeError(f"Failed to fetch {symbol} {timeframe} candles after {max_retries} retries: {last_error}")


def _binance_exchange(config: StrategyConfig):
    try:
        import ccxt
    except ImportError as exc:
        raise RuntimeError("ccxt is required for live data fetching. Install dependencies first.") from exc

    if config.exchange_id != "binance":
        raise RuntimeError(f"Unsupported exchange_id '{config.exchange_id}' for v1.3 (expected 'binance').")

    return ccxt.binance({"enableRateLimit": True})


def fetch_latest_candles(config: StrategyConfig) -> tuple[list[Candle], list[Candle], datetime]:
    exchange = _binance_exchange(config)

    rows_4h = _fetch_ohlcv_with_retry(exchange, config.symbol, config.primary_timeframe, config.primary_limit, config.max_retries)
    rows_1h = _fetch_ohlcv_with_retry(exchange, config.symbol, config.confirm_timeframe, config.confirm_limit, config.max_retries)

    return _to_candles(rows_4h), _to_candles(rows_1h), datetime.now(timezone.utc)


def fetch_historical_candles(
    config: StrategyConfig,
    timeframe: str,
    since_utc: datetime,
    limit: int,
) -> list[Candle]:
    exchange = _binance_exchange(config)
    since_ms = int(since_utc.astimezone(timezone.utc).timestamp() * 1000)
    rows = _fetch_ohlcv_with_retry(exchange, config.symbol, timeframe, limit, config.max_retries, since_ms=since_ms)
    return _to_candles(rows)


def should_poll(last_run_utc: datetime | None, config: StrategyConfig, now_utc: datetime) -> bool:
    if last_run_utc is None:
        return True
    return now_utc - last_run_utc >= timedelta(minutes=config.poll_interval_minutes)
