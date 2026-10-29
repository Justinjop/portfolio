"""Requires Python 3.10 or newer."""

import http.client
import importlib
import json
import logging
import ssl
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from math import isfinite
from pathlib import Path
from unicodedata import east_asian_width
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "https://fapi.binance.com"
INTERVAL_DURATIONS_MS = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
    "1w": 7 * 24 * 60 * 60 * 1000,
}
KLINE_LIMIT = 1500
TENKAN_PERIOD = 9
KIJUN_PERIOD = 26
SENKOU_B_PERIOD = 52
DISPLACEMENT = 26
MIN_KLINES = SENKOU_B_PERIOD + DISPLACEMENT
MAX_WORKERS = 5
SCAN_BATCH_SIZE = 50
MIN_SIGNAL_COUNT = 20
MAX_SCAN_SYMBOL_COUNT = 200
HTTP_REQUEST_INTERVAL_SECONDS = 0.05
HTTP_MAX_REQUEST_INTERVAL_SECONDS = 2.0
BINANCE_WEIGHT_LIMIT_PER_MINUTE = 2400
BINANCE_WEIGHT_THROTTLE_START_RATIO = 0.5
CHART_CANDLES_BEFORE_SIGNAL = 150
SIGNAL_CHART_MAX_AGE_CANDLES = 6
CHART_OUTPUT_DIR = Path(__file__).resolve().parent / "signal_charts"
MAX_HISTORY_CANDLES = 3000
HTTP_MAX_ATTEMPTS = 4
HTTP_RETRY_BASE_SECONDS = 1
HTTP_RETRY_MAX_SECONDS = 8
logger = logging.getLogger(__name__)
_http_request_condition = threading.Condition()
_next_http_request_at = 0.0
_http_rate_limit_until = 0.0
_http_request_interval_seconds = HTTP_REQUEST_INTERVAL_SECONDS
_weight_window = 0
_used_weight = 0


def _wait_for_http_request_slot() -> None:
    global _next_http_request_at
    with _http_request_condition:
        while True:
            now = time.monotonic()
            next_allowed_at = max(_next_http_request_at, _http_rate_limit_until)
            delay = next_allowed_at - now
            if delay <= 0:
                _next_http_request_at = now + _http_request_interval_seconds
                return
            _http_request_condition.wait(min(delay, 60.0))


def _defer_http_requests(delay: float) -> None:
    global _http_rate_limit_until
    with _http_request_condition:
        _http_rate_limit_until = max(
            _http_rate_limit_until,
            time.monotonic() + delay,
        )
        _http_request_condition.notify_all()


def _update_http_weight(headers: http.client.HTTPMessage) -> None:
    global _http_request_interval_seconds, _weight_window, _used_weight
    used_weight_header = headers.get("X-MBX-USED-WEIGHT-1M")
    if used_weight_header is None:
        return
    try:
        used_weight = int(used_weight_header)
        date_header = headers.get("Date")
        response_time = (
            parsedate_to_datetime(date_header).timestamp()
            if date_header
            else time.time()
        )
    except (TypeError, ValueError, OverflowError):
        logger.warning("Ignoring invalid Binance request-weight response headers")
        return

    weight_window = int(response_time // 60)
    with _http_request_condition:
        if weight_window < _weight_window:
            return
        if weight_window > _weight_window:
            _weight_window = weight_window
            _used_weight = used_weight
        else:
            _used_weight = max(_used_weight, used_weight)

        utilization = _used_weight / BINANCE_WEIGHT_LIMIT_PER_MINUTE
        if utilization <= BINANCE_WEIGHT_THROTTLE_START_RATIO:
            _http_request_interval_seconds = HTTP_REQUEST_INTERVAL_SECONDS
        else:
            remaining_capacity = max(1 - utilization, 0.025)
            _http_request_interval_seconds = min(
                HTTP_REQUEST_INTERVAL_SECONDS
                * (1 + utilization - BINANCE_WEIGHT_THROTTLE_START_RATIO)
                / remaining_capacity,
                HTTP_MAX_REQUEST_INTERVAL_SECONDS,
            )
        _http_request_condition.notify_all()


def select_interval() -> str:
    supported_intervals = ", ".join(INTERVAL_DURATIONS_MS)
    while True:
        interval = input(
            f"請選擇掃描週期（{supported_intervals}）："
        ).strip()
        if interval in INTERVAL_DURATIONS_MS:
            return interval
        print(f"無效的週期，請輸入：{supported_intervals}")


@dataclass(frozen=True)
class IchimokuSeries:
    tenkan: list[float]
    kijun: list[float]
    senkou_a: list[float]
    senkou_b: list[float]
    leading_a: list[float]
    leading_b: list[float]
    chikou: list[float]


@dataclass(frozen=True)
class ScanResult:
    symbol: str
    signal: str
    signal_time: int
    quote_volume: float
    quote_asset: str
    entry_price: float
    current_price: float
    current_return_pct: float
    klines: list[list]
    signal_index: int
    ichimoku: IchimokuSeries
    interval: str


@dataclass(frozen=True)
class ScanSummary:
    results: list[ScanResult]
    scanned_count: int
    failure_count: int
    server_time: int


def get_json(url: str) -> dict | list:
    request = Request(url, headers={"User-Agent": "ichimoku-signal-scanner/1.0"})
    for attempt in range(1, HTTP_MAX_ATTEMPTS + 1):
        try:
            _wait_for_http_request_slot()
            with urlopen(request, timeout=15) as response:
                _update_http_weight(response.headers)
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            retryable = error.code in (418, 429) or 500 <= error.code <= 599
            retry_after = error.headers.get("Retry-After") if error.headers else None
            try:
                delay = float(retry_after) if retry_after else 0
            except ValueError:
                try:
                    delay = (
                        parsedate_to_datetime(retry_after).timestamp() - time.time()
                        if retry_after
                        else 0
                    )
                except (TypeError, ValueError, OverflowError):
                    delay = 0
            if not isfinite(delay) or delay <= 0:
                delay = min(
                    HTTP_RETRY_BASE_SECONDS * 2 ** (attempt - 1),
                    HTTP_RETRY_MAX_SECONDS,
                )
            if error.code in (418, 429):
                _defer_http_requests(delay)
            else:
                delay = min(delay, HTTP_RETRY_MAX_SECONDS)
            if not retryable or attempt == HTTP_MAX_ATTEMPTS:
                raise
            logger.warning(
                "Binance HTTP %s for %s; retry %s/%s in %.1fs",
                error.code,
                request.full_url,
                attempt,
                HTTP_MAX_ATTEMPTS - 1,
                delay,
            )
            if error.code not in (418, 429):
                time.sleep(delay)
        except (
            http.client.IncompleteRead,
            ssl.SSLError,
            URLError,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as error:
            if attempt == HTTP_MAX_ATTEMPTS:
                raise
            delay = min(
                HTTP_RETRY_BASE_SECONDS * 2 ** (attempt - 1),
                HTTP_RETRY_MAX_SECONDS,
            )
            logger.warning(
                "Network error for %s; retry %s/%s in %.1fs: %s",
                request.full_url,
                attempt,
                HTTP_MAX_ATTEMPTS - 1,
                delay,
                error,
            )
            time.sleep(delay)
    raise RuntimeError("HTTP retry loop exited unexpectedly")


def get_symbols() -> dict[str, str]:
    exchange_info = get_json(f"{BASE_URL}/fapi/v1/exchangeInfo")
    return {
        item["symbol"]: item["quoteAsset"]
        for item in exchange_info["symbols"]
        if item["status"] == "TRADING" and item["contractType"] == "PERPETUAL"
    }


def get_24h_quote_volumes() -> dict[str, float]:
    tickers = get_json(f"{BASE_URL}/fapi/v1/ticker/24hr")
    return {item["symbol"]: float(item["quoteVolume"]) for item in tickers}


def get_current_prices() -> dict[str, float]:
    tickers = get_json(f"{BASE_URL}/fapi/v1/ticker/price")
    return {
        item["symbol"]: float(item["price"])
        for item in tickers
        if item.get("price") is not None
    }


def get_server_time() -> int:
    response = get_json(f"{BASE_URL}/fapi/v1/time")
    return int(response["serverTime"])


def rolling_midpoints(
    highs: list[float], lows: list[float], period: int
) -> list[float]:
    maxima: deque[int] = deque()
    minima: deque[int] = deque()
    values = [float("nan")] * len(highs)
    for index, (high, low) in enumerate(zip(highs, lows)):
        while maxima and highs[maxima[-1]] <= high:
            maxima.pop()
        maxima.append(index)
        while minima and lows[minima[-1]] >= low:
            minima.pop()
        minima.append(index)
        first_valid_index = index - period + 1
        while maxima and maxima[0] < first_valid_index:
            maxima.popleft()
        while minima and minima[0] < first_valid_index:
            minima.popleft()
        if first_valid_index >= 0:
            values[index] = (highs[maxima[0]] + lows[minima[0]]) / 2
    return values


def calculate_ichimoku(klines: list[list]) -> IchimokuSeries:
    highs = [float(kline[2]) for kline in klines]
    lows = [float(kline[3]) for kline in klines]
    closes = [float(kline[4]) for kline in klines]
    tenkan = rolling_midpoints(highs, lows, TENKAN_PERIOD)
    kijun = rolling_midpoints(highs, lows, KIJUN_PERIOD)
    source_a = [
        (tenkan[index] + kijun[index]) / 2
        if isfinite(tenkan[index]) and isfinite(kijun[index])
        else float("nan")
        for index in range(len(klines))
    ]
    source_b = rolling_midpoints(highs, lows, SENKOU_B_PERIOD)
    leading_a = [float("nan")] * len(klines)
    leading_b = [float("nan")] * len(klines)
    chikou = [float("nan")] * len(klines)
    for index in range(len(klines)):
        displaced_index = index + DISPLACEMENT
        if displaced_index < len(klines):
            leading_a[displaced_index] = source_a[index]
            leading_b[displaced_index] = source_b[index]
        if index >= DISPLACEMENT:
            chikou[index - DISPLACEMENT] = closes[index]
    return IchimokuSeries(
        tenkan,
        kijun,
        source_a,
        source_b,
        leading_a,
        leading_b,
        chikou,
    )


def get_signal_from_ichimoku(
    klines: list[list], ichimoku: IchimokuSeries, index: int
) -> str | None:
    if index < MIN_KLINES - 1:
        return None
    price = float(klines[index][4])
    chikou_reference_close = float(klines[index - DISPLACEMENT][4])
    current_senkou_a = ichimoku.leading_a[index]
    current_senkou_b = ichimoku.leading_b[index]
    if not isfinite(current_senkou_a) or not isfinite(current_senkou_b):
        return None
    tenkan = ichimoku.tenkan[index]
    kijun = ichimoku.kijun[index]
    new_senkou_a = ichimoku.senkou_a[index]
    new_senkou_b = ichimoku.senkou_b[index]

    bullish = (
        price > chikou_reference_close
        and tenkan > kijun
        and price > max(current_senkou_a, current_senkou_b)
        and new_senkou_a > new_senkou_b
    )
    bearish = (
        price < chikou_reference_close
        and tenkan < kijun
        and price < min(current_senkou_a, current_senkou_b)
        and new_senkou_a < new_senkou_b
    )
    if bullish:
        return "漲"
    if bearish:
        return "跌"
    return None


def build_signal_history(
    klines: list[list], server_time: int
) -> tuple[list[list], list[str | None], IchimokuSeries]:
    closed_klines = [kline for kline in klines if int(kline[6]) <= server_time]
    ichimoku = calculate_ichimoku(closed_klines)
    raw_signals = [
        get_signal_from_ichimoku(closed_klines, ichimoku, index)
        for index in range(len(closed_klines))
    ]
    last_signal: str | None = None
    signals: list[str | None] = []
    for raw_signal in raw_signals:
        if raw_signal is not None and raw_signal != last_signal:
            signals.append(raw_signal)
            last_signal = raw_signal
        else:
            signals.append(None)
    return closed_klines, signals, ichimoku


def find_latest_signal_index(signals: list[str | None]) -> int | None:
    for index in range(len(signals) - 1, -1, -1):
        if signals[index] is not None:
            return index
    return None


def sort_results_by_signal_recency(
    results: list[ScanResult],
) -> list[ScanResult]:
    return sorted(results, key=lambda result: (result.signal_time, result.symbol))


def is_signal_recent_for_chart(result: ScanResult, server_time: int) -> bool:
    signal_close_time = int(result.klines[result.signal_index][6])
    signal_age = server_time - signal_close_time
    max_age_ms = INTERVAL_DURATIONS_MS[result.interval] * SIGNAL_CHART_MAX_AGE_CANDLES
    return 0 <= signal_age <= max_age_ms


def should_fetch_older_history(
    signal_index: int | None, history_count: int
) -> bool:
    return history_count < MAX_HISTORY_CANDLES and (
        signal_index is None
        or signal_index <= max(MIN_KLINES, CHART_CANDLES_BEFORE_SIGNAL)
    )


def get_signal_chart_range(
    klines: list[list], signal_index: int
) -> tuple[int, int]:
    return max(0, signal_index - CHART_CANDLES_BEFORE_SIGNAL), len(klines)


def render_signal_chart(
    symbol: str,
    klines: list[list],
    signal_index: int,
    signal: str,
    interval: str,
    output_dir: Path = CHART_OUTPUT_DIR,
    ichimoku: IchimokuSeries | None = None,
) -> Path:
    matplotlib = importlib.import_module("matplotlib")
    matplotlib.use("Agg", force=True)
    mdates = importlib.import_module("matplotlib.dates")
    plt = importlib.import_module("matplotlib.pyplot")
    collections = importlib.import_module("matplotlib.collections")
    LineCollection = collections.LineCollection
    PolyCollection = collections.PolyCollection

    taipei_timezone = timezone(timedelta(hours=8))
    if ichimoku is None:
        ichimoku = calculate_ichimoku(klines)
    start_index, end_index = get_signal_chart_range(klines, signal_index)
    chart_klines = klines[start_index:end_index]
    chart_signal_index = signal_index - start_index
    times = [
        mdates.date2num(
            datetime.fromtimestamp(int(kline[0]) / 1000, tz=timezone.utc)
        )
        for kline in chart_klines
    ]
    opens = [float(kline[1]) for kline in chart_klines]
    highs = [float(kline[2]) for kline in chart_klines]
    lows = [float(kline[3]) for kline in chart_klines]
    closes = [float(kline[4]) for kline in chart_klines]
    visible_x = times
    visible_tenkan = ichimoku.tenkan[start_index:end_index]
    visible_kijun = ichimoku.kijun[start_index:end_index]
    visible_span_a = ichimoku.leading_a[start_index:end_index]
    visible_span_b = ichimoku.leading_b[start_index:end_index]
    visible_chikou = ichimoku.chikou[start_index:end_index]
    if len(times) > 1:
        candle_width = min(
            (times[index + 1] - times[index] for index in range(len(times) - 1))
        ) * 0.65
    else:
        candle_width = INTERVAL_DURATIONS_MS[interval] / 86_400_000 * 0.65

    figure, axis = plt.subplots(figsize=(16, 9))
    figure.patch.set_facecolor("#101820")
    axis.set_facecolor("#101820")
    axis.grid(True, color="#35424d", linewidth=0.6, alpha=0.7)
    candle_colors = []
    wick_segments = []
    candle_vertices = []
    for index, x_value in enumerate(visible_x):
        rising = closes[index] >= opens[index]
        color = "#26a69a" if rising else "#ef5350"
        candle_colors.append(color)
        wick_segments.append(((x_value, lows[index]), (x_value, highs[index])))
        body_bottom = min(opens[index], closes[index])
        body_top = max(opens[index], closes[index])
        body_height = body_top - body_bottom
        if body_height == 0:
            body_height = max(abs(closes[index]) * 0.00001, 1e-12)
            body_top = body_bottom + body_height
        candle_vertices.append(
            (
                (x_value - candle_width / 2, body_bottom),
                (x_value - candle_width / 2, body_top),
                (x_value + candle_width / 2, body_top),
                (x_value + candle_width / 2, body_bottom),
            )
        )
    axis.add_collection(
        LineCollection(
            wick_segments,
            colors=candle_colors,
            linewidths=0.8,
            zorder=2,
        )
    )
    axis.add_collection(
        PolyCollection(
            candle_vertices,
            facecolors=candle_colors,
            edgecolors=candle_colors,
            linewidths=0.6,
            zorder=3,
        )
    )

    axis.plot(visible_x, visible_tenkan, color="#42a5f5", linewidth=1, label="Tenkan")
    axis.plot(visible_x, visible_kijun, color="#ff7043", linewidth=1, label="Kijun")
    axis.plot(visible_x, visible_chikou, color="#ffee58", linewidth=0.9, label="Chikou")
    projected_start = max(0, len(klines) - DISPLACEMENT)
    projected_span_a = ichimoku.senkou_a[projected_start:]
    projected_span_b = ichimoku.senkou_b[projected_start:]
    interval_days = INTERVAL_DURATIONS_MS[interval] / 86_400_000
    future_x = [
        times[-1] + interval_days * offset
        for offset in range(1, min(len(projected_span_a), len(projected_span_b)) + 1)
    ]
    cloud_x = visible_x + future_x
    cloud_span_a = visible_span_a + projected_span_a[: len(future_x)]
    cloud_span_b = visible_span_b + projected_span_b[: len(future_x)]
    axis.plot(
        cloud_x,
        cloud_span_a,
        color="#66bb6a",
        linewidth=0.9,
        label="Senkou A",
    )
    axis.plot(
        cloud_x,
        cloud_span_b,
        color="#ab47bc",
        linewidth=0.9,
        label="Senkou B",
    )
    valid_cloud = [
        isfinite(first) and isfinite(second)
        for first, second in zip(cloud_span_a, cloud_span_b)
    ]
    bullish_cloud = [
        valid and first >= second
        for valid, first, second in zip(
            valid_cloud, cloud_span_a, cloud_span_b
        )
    ]
    bearish_cloud = [
        valid and first < second
        for valid, first, second in zip(
            valid_cloud, cloud_span_a, cloud_span_b
        )
    ]
    axis.fill_between(
        cloud_x,
        cloud_span_a,
        cloud_span_b,
        where=bullish_cloud,
        color="#26a69a",
        alpha=0.16,
        interpolate=True,
    )
    axis.fill_between(
        cloud_x,
        cloud_span_a,
        cloud_span_b,
        where=bearish_cloud,
        color="#ef5350",
        alpha=0.16,
        interpolate=True,
    )

    signal_x = visible_x[chart_signal_index]
    signal_high = highs[chart_signal_index]
    signal_bar = klines[signal_index]
    signal_time = datetime.fromtimestamp(
        int(signal_bar[0]) / 1000, tz=timezone.utc
    ).astimezone(taipei_timezone)
    signal_color = "#26a69a" if signal == "漲" else "#ef5350"
    signal_label = "Bullish" if signal == "漲" else "Bearish"
    axis.scatter(
        [signal_x],
        [signal_high],
        marker="^" if signal == "漲" else "v",
        s=100,
        color=signal_color,
        edgecolors="white",
        linewidths=0.7,
        zorder=5,
        label=f"{signal_label} signal",
    )
    axis.annotate(
        f"{signal_label} signal",
        (signal_x, signal_high),
        xytext=(0, 12 if signal == "漲" else -20),
        textcoords="offset points",
        color="white",
        ha="center",
        fontsize=9,
    )

    axis.set_title(
        f"{symbol} {interval} Ichimoku | {signal_label} signal at "
        f"{signal_time:%Y-%m-%d %H:%M} (Taipei)",
        color="white",
        fontsize=13,
    )
    axis.set_ylabel("Price", color="white")
    axis.tick_params(colors="#d5dde5")
    axis.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=12))
    axis.xaxis.set_major_formatter(
        mdates.DateFormatter("%m-%d %H:%M", tz=taipei_timezone)
    )
    for spine in axis.spines.values():
        spine.set_color("#607d8b")
    axis.update_datalim(
        [
            (times[index], lows[index])
            for index in range(len(times))
        ]
        + [
            (times[index], highs[index])
            for index in range(len(times))
        ]
    )
    axis.autoscale_view()
    if future_x:
        axis.set_xlim(times[0] - candle_width, future_x[-1] + candle_width)
    legend = axis.legend(loc="upper left", ncol=3, fontsize=8)
    legend.get_frame().set_facecolor("#18232d")
    for text in legend.get_texts():
        text.set_color("white")
    figure.autofmt_xdate()
    figure.tight_layout()

    base_symbol = symbol.removesuffix("USDT").removesuffix("USDC")
    generated_date = datetime.now(taipei_timezone).strftime("%Y%m%d")
    filename = f"{base_symbol}_{generated_date}.png"
    output_path = output_dir / filename
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        figure.savefig(output_path, dpi=150, facecolor=figure.get_facecolor())
    finally:
        plt.close(figure)
    return output_path


def scan_symbol(
    symbol: str,
    quote_asset: str,
    quote_volume: float,
    current_price: float,
    server_time: int,
    interval: str,
) -> ScanResult | None:
    query = urlencode({"symbol": symbol, "interval": interval, "limit": KLINE_LIMIT})
    klines = get_json(f"{BASE_URL}/fapi/v1/klines?{query}")
    forming_klines = [
        kline for kline in klines if int(kline[6]) > server_time
    ]
    closed_klines, signals, ichimoku = build_signal_history(klines, server_time)
    signal_index = find_latest_signal_index(signals)
    while should_fetch_older_history(signal_index, len(closed_klines)):
        if not closed_klines:
            break
        oldest_open_time = int(closed_klines[0][0])
        previous_query = urlencode(
            {
                "symbol": symbol,
                "interval": interval,
                "endTime": oldest_open_time - 1,
                "limit": KLINE_LIMIT,
            }
        )
        previous_klines = get_json(
            f"{BASE_URL}/fapi/v1/klines?{previous_query}"
        )
        if not previous_klines:
            break

        previous_klines = [
            kline
            for kline in previous_klines
            if int(kline[0]) < oldest_open_time
        ]
        remaining_history = MAX_HISTORY_CANDLES - len(closed_klines)
        if not previous_klines or remaining_history <= 0:
            break
        previous_klines = previous_klines[-remaining_history:]
        klines = previous_klines + closed_klines + forming_klines
        closed_klines, signals, ichimoku = build_signal_history(
            klines, server_time
        )
        signal_index = find_latest_signal_index(signals)

    if signal_index is None:
        return None

    signal = signals[signal_index]
    signal_bar = closed_klines[signal_index]
    next_bar = next(
        (
            kline
            for kline in klines
            if int(kline[0]) > int(signal_bar[0])
        ),
        None,
    )
    if next_bar is None:
        return None
    entry_price = float(next_bar[1])
    if entry_price <= 0 or current_price <= 0:
        return None
    direction_sign = 1 if signal == "漲" else -1
    current_return_pct = (
        (current_price / entry_price - 1) * direction_sign * 100
    )
    return ScanResult(
        symbol=symbol,
        signal=signal,
        signal_time=int(signal_bar[0]),
        quote_volume=quote_volume,
        quote_asset=quote_asset,
        entry_price=entry_price,
        current_price=current_price,
        current_return_pct=current_return_pct,
        klines=closed_klines,
        signal_index=signal_index,
        ichimoku=ichimoku,
        interval=interval,
    )


def scan_symbols_by_volume(
    symbols: list[str],
    symbol_quote_assets: dict[str, str],
    quote_volumes: dict[str, float],
    interval: str,
) -> ScanSummary:
    results: list[ScanResult] = []
    scanned_count = 0
    failure_count = 0
    server_time = 0
    scan_limit = min(len(symbols), MAX_SCAN_SYMBOL_COUNT)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for start in range(0, scan_limit, SCAN_BATCH_SIZE):
            batch = symbols[start : start + SCAN_BATCH_SIZE]
            try:
                current_prices = get_current_prices()
                server_time = get_server_time()
            except Exception:
                logger.exception(
                    "Failed to refresh market snapshot for batch starting at %s",
                    start,
                )
                scanned_count += len(batch)
                failure_count += len(batch)
                continue

            futures = [
                executor.submit(
                    scan_symbol,
                    symbol,
                    symbol_quote_assets[symbol],
                    quote_volumes[symbol],
                    current_prices.get(symbol, 0.0),
                    server_time,
                    interval,
                )
                for symbol in batch
            ]
            future_symbols = {
                future: symbol for future, symbol in zip(futures, batch)
            }
            for future in as_completed(futures):
                scanned_count += 1
                try:
                    result = future.result()
                except Exception:
                    failure_count += 1
                    logger.exception(
                        "Failed to scan %s", future_symbols[future]
                    )
                    continue
                if result:
                    results.append(result)

            chart_eligible_count = sum(
                is_signal_recent_for_chart(result, server_time)
                for result in results
            )
            logger.info(
                "已掃描 %s 個交易對，失敗 %s 個，找到 %s 個可生成圖片的訊號",
                start + len(batch),
                failure_count,
                chart_eligible_count,
            )
            if chart_eligible_count >= MIN_SIGNAL_COUNT:
                break

    return ScanSummary(results, scanned_count, failure_count, server_time)


def display_width(value: str) -> int:
    return sum(
        2 if east_asian_width(character) in ("F", "W") else 1
        for character in value
    )


def pad_cell(value: str, width: int, align: str = "left") -> str:
    padding = max(0, width - display_width(value))
    if align == "right":
        return " " * padding + value
    return value + " " * padding


def format_results_table(
    results: list[ScanResult],
) -> str:
    taipei_timezone = timezone(timedelta(hours=8))
    headers = (
        "排名", "交易對", "最近方向", "訊號成立時間 (台北)",
        "進場價格", "目前價格", "目前方向報酬率", "24h 成交額", "計價",
    )
    rows = []
    for rank, result in enumerate(
        sort_results_by_signal_recency(results),
        start=1,
    ):
        signal_time_text = datetime.fromtimestamp(
            result.signal_time / 1000,
            tz=taipei_timezone,
        ).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(
            (
                str(rank),
                result.symbol,
                result.signal,
                signal_time_text,
                f"{result.entry_price:.8g}",
                f"{result.current_price:.8g}",
                f"{result.current_return_pct:+.2f}%",
                f"{result.quote_volume:,.2f}",
                result.quote_asset,
            )
        )

    widths = [
        max(display_width(headers[index]), *(display_width(row[index]) for row in rows))
        if rows
        else display_width(headers[index])
        for index in range(len(headers))
    ]
    aligns = (
        "right", "left", "center", "left", "right",
        "right", "right", "right", "center",
    )

    def render_row(values: tuple[str, ...]) -> str:
        return "| " + " | ".join(
            pad_cell(value, widths[index], aligns[index])
            for index, value in enumerate(values)
        ) + " |"

    border = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    table = [border, render_row(headers), border]
    table.extend(render_row(row) for row in rows)
    table.append(border)
    return "\n".join(table)


def main() -> None:
    interval = select_interval()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    symbol_quote_assets = get_symbols()
    quote_volumes = get_24h_quote_volumes()
    symbols = sorted(
        (symbol for symbol in symbol_quote_assets if symbol in quote_volumes),
        key=quote_volumes.__getitem__,
        reverse=True,
    )
    summary = scan_symbols_by_volume(
        symbols,
        symbol_quote_assets,
        quote_volumes,
        interval,
    )
    results = summary.results
    server_time = summary.server_time

    try:
        current_prices = get_current_prices()
    except Exception:
        logger.exception("Failed to refresh final prices; using batch snapshots")
        current_prices = {}
    results = [
        replace(
            result,
            current_price=current_prices[result.symbol],
            current_return_pct=(
                (
                    current_prices[result.symbol] / result.entry_price - 1
                )
                * (1 if result.signal == "漲" else -1)
                * 100
            ),
        )
        if result.symbol in current_prices and current_prices[result.symbol] > 0
        else result
        for result in results
    ]
    try:
        server_time = get_server_time()
    except Exception:
        logger.exception("Failed to refresh final server time")

    chart_failure_count = 0
    for result in sort_results_by_signal_recency(results):
        if not is_signal_recent_for_chart(result, server_time):
            continue
        try:
            chart_path = render_signal_chart(
                result.symbol,
                result.klines,
                result.signal_index,
                result.signal,
                interval,
                ichimoku=result.ichimoku,
            )
            logger.info("Signal chart saved: %s", chart_path)
        except Exception:
            chart_failure_count += 1
            logger.exception("Failed to render signal chart for %s", result.symbol)

    print(f"\nIchimoku {interval} 訊號掃描完成")
    print(
        f"掃描交易對：{summary.scanned_count}/{len(symbols)}"
        f" | 掃描失敗：{summary.failure_count}"
        f" | 有訊號：{len(results)}"
        f" | 圖片失敗：{chart_failure_count}"
    )
    print(f"訊號圖目錄：{CHART_OUTPUT_DIR}")
    if results:
        print(format_results_table(results))
    else:
        print("目前沒有符合條件的交易對訊號。")
    if summary.failure_count:
        print("掃描未完整完成；請查看日誌中的失敗交易對。")


if __name__ == "__main__":
    main()