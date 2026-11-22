import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Set, Tuple

import pandas as pd
import pytz
import requests

try:
    import tsl1
except ImportError:
    tsl1 = None

try:
    from dmitrack import record_signal
except ImportError:
    record_signal = None

# System Parameters
DMI_PERIOD = 14
ADX_PERIOD = 14
ADX_MIN = 20.0
ADX_VELOCITY_MIN = 1.5
EXTRA_ADX_AS_STRICT_FILTER = os.getenv("EXTRA_ADX_AS_STRICT_FILTER", "false").lower() in {
    "1",
    "true",
    "yes",
}

TOP_SYMBOLS = 25
KLINE_LIMIT = 200
SCAN_INTERVAL = 10
REQUEST_TIMEOUT = 10
TIMEFRAMES = ["15m", "1h", "4h", "1d"]

# Exclude non-crypto / gold / specific excluded pairs
EXCLUDED_SYMBOLS = {
    "USDC", "USD1", "USD0", "USDE", "USDD", "USDP", "TUSD", "FDUSD", "BUSD",
    "DAI", "EUR", "AEUR", "GBP", "AUD", "TRY", "BRL", "RUB", "PYUSD",
    "FRAX", "LUSD", "USTC", "UST", "CUSD", "SUSD", "OUSD", "USDL", "USDY",
    # Global equities, indices & commodities
    "TSLA", "AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "META", "IBM", "DELL",
    "ADBE", "SHOP", "OPENAI", "CRWD", "EWZ", "UVXY", "CSCO", "NOW", "AGPU",
    "LGELECTRONICS", "GOLD", "SILVER", "OIL", "USOIL", "UKOIL", "CRUDE",
    "US30", "SPX500", "NAS100", "NDX", "DJI", "DXY", "XAU", "XAG", "COPPER", "NGAS"}
EXCLUDED_SUBSTRINGS = ("XAU", "GOLD", "USUSDT")

# Timeframe mapping hierarchy for LTF scanning
TF_HIERARCHY = ["1d", "4h", "1h", "15m", "5m", "1m"]

# Telegram Config
TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "9295199451:AAGzODnQpzFd9qt06M8dAS0ePfa6-rDmqXg",
)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8970361774")

# Track emitted signals by (symbol, timeframe, signal_status, bar_timestamp)
SEEN_SIGNALS: Set[Tuple[str, str, str, str]] = set()
HTTP_SESSION = requests.Session()


@dataclass
class FVGInfo:
    timeframe: str
    fvg_type: str  # "BULLISH" or "BEARISH"
    top: float
    bottom: float


@dataclass
class Setup:
    symbol: str
    current_price: float
    timeframe: str
    dmi_status: str
    rsi: float
    sl: float
    bar_ts: str
    fvgs: List[FVGInfo] = field(default_factory=list)


def is_valid_crypto_symbol(symbol: str) -> bool:
    if not isinstance(symbol, str) or not symbol.endswith("USDT"):
        return False
    if symbol in EXCLUDED_SYMBOLS:
        return False
    if any(sub in symbol for sub in EXCLUDED_SUBSTRINGS):
        return False
    base = symbol[:-4]
    return bool(base) and base.isalnum()


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = HTTP_SESSION.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if not resp.ok:
            print(f"Telegram send failed ({resp.status_code}): {resp.text}")
    except Exception as e:
        print(f"Telegram error: {e}")


def play_alert() -> None:
    if os.getenv("GITHUB_ACTIONS") == "true":
        return
    try:
        if sys.platform == "win32":
            import winsound
            for _ in range(3):
                winsound.Beep(1000, 400)
                time.sleep(0.2)
        else:
            for _ in range(3):
                print("\a", end="", flush=True)
                time.sleep(0.3)
    except Exception:
        pass


def get_top_symbols() -> List[str]:
    try:
        url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        resp = HTTP_SESSION.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        df = pd.DataFrame(data)

        if df.empty or "symbol" not in df.columns or "quoteVolume" not in df.columns:
            return []

        df = df[df["symbol"].apply(is_valid_crypto_symbol)].copy()
        df["quoteVolume"] = pd.to_numeric(df["quoteVolume"], errors="coerce")
        df = df.dropna(subset=["quoteVolume"])
        return (
            df.sort_values("quoteVolume", ascending=False)
            .head(TOP_SYMBOLS)["symbol"]
            .tolist()
        )
    except Exception as e:
        print(f"Error getting top volume symbols: {e}")
        return []


def get_klines(symbol: str, interval: str, limit: int = KLINE_LIMIT) -> pd.DataFrame:
    try:
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        resp = HTTP_SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(
            data,
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_asset_volume", "trades",
                "taker_base", "taker_quote", "ignore",
            ],
        )

        for column in ["open", "high", "low", "close", "volume"]:
            df[column] = pd.to_numeric(df[column], errors="coerce")

        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        df = df.dropna(subset=["open", "high", "low", "close"])
        return df.reset_index(drop=True)
    except Exception as e:
        print(f"{symbol} {interval} kline error: {e}")
        return pd.DataFrame()


def wilder_rma(series: pd.Series, period: int) -> pd.Series:
    values = series.astype(float).to_numpy()
    result = [float("nan")] * len(values)

    if len(values) < period:
        return pd.Series(result, index=series.index)

    initial_window = values[:period]
    if pd.isna(initial_window).any():
        valid_init = pd.Series(initial_window).dropna()
        if valid_init.empty:
            return pd.Series(result, index=series.index)
        rma_value = float(valid_init.mean())
    else:
        rma_value = float(initial_window.mean())

    result[period - 1] = rma_value

    for i in range(period, len(values)):
        val = values[i]
        if pd.isna(val):
            val = 0.0
        rma_value = (rma_value * (period - 1) + float(val)) / period
        result[i] = rma_value

    return pd.Series(result, index=series.index)


def calc_rsi(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return 50.0

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = wilder_rma(gain, period)
    avg_loss = wilder_rma(loss, period)

    last_gain = avg_gain.iloc[-1]
    last_loss = avg_loss.iloc[-1]

    if pd.isna(last_gain) or pd.isna(last_loss):
        return 50.0
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0

    rs = last_gain / last_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return 50.0 if pd.isna(rsi) else float(rsi)


def calculate_dmi(
    df: pd.DataFrame,
    period: int = DMI_PERIOD,
    adx_period: int = ADX_PERIOD,
) -> pd.DataFrame:
    out = df.copy()
    high = out["high"]
    low = out["low"]
    close = out["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(0.0, index=out.index)
    minus_dm = pd.Series(0.0, index=out.index)

    plus_cond = (up_move > down_move) & (up_move > 0)
    minus_cond = (down_move > up_move) & (down_move > 0)

    plus_dm.loc[plus_cond] = up_move.loc[plus_cond]
    minus_dm.loc[minus_cond] = down_move.loc[minus_cond]

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = wilder_rma(tr, period)
    smoothed_plus_dm = wilder_rma(plus_dm, period)
    smoothed_minus_dm = wilder_rma(minus_dm, period)

    plus_di = 100.0 * (smoothed_plus_dm / atr)
    minus_di = 100.0 * (smoothed_minus_dm / atr)

    di_sum = (plus_di + minus_di).replace(0, float("nan"))
    di_diff = (plus_di - minus_di).abs()
    dx = (100.0 * (di_diff / di_sum)).fillna(0.0)
    adx = wilder_rma(dx, adx_period)

    out["plus_di"] = plus_di
    out["minus_di"] = minus_di
    out["dx"] = dx
    out["adx"] = adx
    return out


def check_dmi_crossover(
    df: pd.DataFrame,
    period: int = DMI_PERIOD,
    adx_period: int = ADX_PERIOD,
    velocity_min: float = ADX_VELOCITY_MIN,
    strict_extra_adx: bool = EXTRA_ADX_AS_STRICT_FILTER,
) -> str:
    minimum_required = period + adx_period + 10
    if len(df) < minimum_required:
        return "NONE"

    dmi_df = calculate_dmi(df, period, adx_period)

    curr_idx, prev_idx, prev_prev_idx = -2, -3, -4
    curr_plus = dmi_df["plus_di"].iloc[curr_idx]
    curr_minus = dmi_df["minus_di"].iloc[curr_idx]
    curr_adx = dmi_df["adx"].iloc[curr_idx]

    prev_plus = dmi_df["plus_di"].iloc[prev_idx]
    prev_minus = dmi_df["minus_di"].iloc[prev_idx]
    prev_adx = dmi_df["adx"].iloc[prev_idx]

    prev_prev_plus = dmi_df["plus_di"].iloc[prev_prev_idx]
    prev_prev_minus = dmi_df["minus_di"].iloc[prev_prev_idx]
    prev_prev_adx = dmi_df["adx"].iloc[prev_prev_idx]

    values = [
        curr_plus, curr_minus, curr_adx,
        prev_plus, prev_minus, prev_adx,
        prev_prev_plus, prev_prev_minus, prev_prev_adx,
    ]
    if any(pd.isna(v) for v in values):
        return "NONE"

    if not (curr_adx > ADX_MIN and curr_adx > prev_adx and prev_adx > prev_prev_adx):
        return "NONE"

    delta1 = curr_adx - prev_adx
    delta2 = prev_adx - prev_prev_adx
    extra_adx = (delta1 > velocity_min) and (delta2 > velocity_min)

    if strict_extra_adx and not extra_adx:
        return "NONE"

    bullish = (
        prev_prev_plus <= prev_prev_minus
        and prev_plus > prev_minus
        and curr_plus > prev_plus
    )
    if bullish:
        return "BULL$" if extra_adx else "BULLISH"

    bearish = (
        prev_prev_plus >= prev_prev_minus
        and prev_plus < prev_minus
        and curr_minus > prev_minus
    )
    if bearish:
        return "BEAR$" if extra_adx else "BEARISH"

    bullish_fallback = (
        prev_prev_plus > prev_prev_minus
        and prev_plus > prev_prev_plus
        and prev_plus < curr_plus
        and prev_plus > prev_minus
        and curr_plus > curr_minus
        and prev_prev_adx < prev_adx
        and prev_adx < curr_adx
        and curr_adx > ADX_MIN
    )
    if bullish_fallback:
        return "BULL$" if extra_adx else "BULLISH"

    bearish_fallback = (
        prev_prev_minus > prev_prev_plus
        and prev_minus > prev_prev_minus
        and prev_minus < curr_minus
        and prev_minus > prev_plus
        and curr_minus > curr_plus
        and prev_prev_adx < prev_adx
        and prev_adx < curr_adx
        and curr_adx > ADX_MIN
    )
    if bearish_fallback:
        return "BEAR$" if extra_adx else "BEARISH"

    return "NONE"


def find_recent_unfilled_fvg(df: pd.DataFrame, tf: str, target_signal: str) -> Optional[FVGInfo]:
    if len(df) < 6:
        return None

    start_i = len(df) - 2
    stop_i = max(1, len(df) - 6)

    for i in range(start_i, stop_i, -1):
        high_i_2 = float(df["high"].iloc[i - 2])
        low_i = float(df["low"].iloc[i])
        low_i_2 = float(df["low"].iloc[i - 2])
        high_i = float(df["high"].iloc[i])
        subsequent = df.iloc[i + 1:]

        if target_signal == "BULLISH" and low_i > high_i_2:
            fvg_bottom = high_i_2
            fvg_top = low_i
            is_covered = bool((subsequent["low"] <= fvg_bottom).any()) if not subsequent.empty else False
            if not is_covered:
                return FVGInfo(
                    timeframe=tf,
                    fvg_type="BULLISH",
                    top=round(fvg_top, 4),
                    bottom=round(fvg_bottom, 4),
                )

        elif target_signal == "BEARISH" and high_i < low_i_2:
            fvg_top = low_i_2
            fvg_bottom = high_i
            is_covered = bool((subsequent["high"] >= fvg_top).any()) if not subsequent.empty else False
            if not is_covered:
                return FVGInfo(
                    timeframe=tf,
                    fvg_type="BEARISH",
                    top=round(fvg_top, 4),
                    bottom=round(fvg_bottom, 4),
                )

    return None


def scan_lower_timeframe_fvgs(symbol: str, signal_tf: str, target_signal: str) -> List[FVGInfo]:
    if signal_tf not in TF_HIERARCHY:
        return []

    start_idx = TF_HIERARCHY.index(signal_tf) + 1
    lower_tfs = TF_HIERARCHY[start_idx:]

    found_fvgs: List[FVGInfo] = []
    for tf in lower_tfs:
        df_ltf = get_klines(symbol, interval=tf, limit=30)
        if not df_ltf.empty:
            fvg = find_recent_unfilled_fvg(df_ltf, tf, target_signal)
            if fvg:
                found_fvgs.append(fvg)

    return found_fvgs


def format_pair(pair: str) -> str:
    if not pair:
        return ""
    cleaned = pair.split("-", 1)[-1]
    return cleaned.replace("_", "")


def emit_setup(setup: Setup) -> None:
    ist_tz = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist_tz)
    timestamp = now_ist.strftime("%Y-%m-%d %H:%M:%S")
    day_name = now_ist.strftime("%a").upper()

    fvg_lines = (
        "\n".join([f"• [{f.timeframe}] {f.fvg_type}: {f.bottom} - {f.top}" for f in setup.fvgs])
        if setup.fvgs
        else "• None"
    )
    fvg_str = (
        "; ".join([f"{f.timeframe}:{f.fvg_type}({f.bottom}-{f.top})" for f in setup.fvgs])
        if setup.fvgs
        else "None"
    )

    print("================================================")
    print(setup.symbol)
    print(f"Timeframe     : {setup.timeframe}")
    print(f"Current Price : {setup.current_price}")
    print(f"DMI Status    : {setup.dmi_status}")
    print(f"RSI           : {setup.rsi:.2f}")
    print(f"Stop Loss     : {setup.sl:.4f}")
    print("--- Matching Uncovered FVGs (Last 3-4 Candles) ---")
    print(fvg_lines)
    print("================================================")

    play_alert()

    tg_msg = (
        f"<b>DMI Signal: {setup.symbol}</b> ({setup.dmi_status})\n"
        f"<b>TF:</b> {setup.timeframe} | <b>RSI:</b> {setup.rsi:.2f}\n"
        f"<b>Price:</b> {setup.current_price} | <b>SL:</b> {setup.sl:.4f}\n"
        f"<b>LTF FVGs:</b>\n{fvg_lines}\n"
        f"<b>Time (IST):</b> {timestamp} ({day_name})"
    )
    send_telegram_message(tg_msg)

    signal_side = "LONG" if setup.dmi_status in ["BULLISH", "BULL$"] else "SHORT"

    if tsl1 is not None:
        try:
            tsl1.store_s1_signal_in_excels(
                symbol=format_pair(setup.symbol),
                market_price=setup.current_price,
                signal=signal_side,
                investment=10,
                leverage=16,
                tp=0,
                sl=round(setup.sl, 4),
                strategies_str=f"FVG:{fvg_str}",
                total_inv_pnl=10,
            )
        except Exception as e:
            print(f"Error executing tsl1 store method: {e}")

    if record_signal is not None:
        try:
            record_signal(
                symbol=setup.symbol,
                side=signal_side,
                entry_price=setup.current_price,
            )
        except Exception as e:
            print(f"Error storing signal in percentage tracker: {e}")


def scan_symbol(symbol: str) -> None:
    if not is_valid_crypto_symbol(symbol):
        return

    for tf in TIMEFRAMES:
        df = get_klines(symbol, interval=tf, limit=KLINE_LIMIT)
        if df.empty or len(df) < 50:
            continue

        current_price = float(df.iloc[-1]["close"])
        dmi_status = check_dmi_crossover(df)
        rsi_val = calc_rsi(df)

        if (dmi_status in ["BULLISH", "BULL$"]) and rsi_val > 85:
            dmi_status = "NONE"
        elif (dmi_status in ["BEARISH", "BEAR$"]) and rsi_val < 20:
            dmi_status = "NONE"

        if dmi_status == "NONE":
            continue

        bar_ts = str(df.iloc[-2]["open_time"])
        signal_key = (symbol, tf, dmi_status, bar_ts)
        if signal_key in SEEN_SIGNALS:
            continue

        if dmi_status in ["BULLISH", "BULL$"]:
            sl_val = float(df.iloc[-3:-1]["low"].min())
            target_signal = "BULLISH"
        else:
            sl_val = float(df.iloc[-3:-1]["high"].max())
            target_signal = "BEARISH"

        ltf_fvgs = scan_lower_timeframe_fvgs(symbol, tf, target_signal)
        SEEN_SIGNALS.add(signal_key)

        emit_setup(
            Setup(
                symbol=symbol,
                current_price=current_price,
                timeframe=tf,
                dmi_status=dmi_status,
                rsi=rsi_val,
                sl=sl_val,
                bar_ts=bar_ts,
                fvgs=ltf_fvgs,
            )
        )


def run_once() -> None:
    symbols = get_top_symbols()
    print(f"Scanning {len(symbols)} crypto symbols across {TIMEFRAMES}...")
    for symbol in symbols:
        try:
            scan_symbol(symbol)
        except Exception as e:
            print(f"{symbol} scan error: {e}")


def main(single_run: bool = False) -> None:
    if single_run:
        run_once()
        return

    while True:
        try:
            print("\nScanner running...")
            run_once()
            print(f"Waiting {SCAN_INTERVAL} seconds...")
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            print("\nScanner stopped.")
            break
        except Exception as e:
            print(f"Error in main loop: {e}")
            time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EMA3 DMI/ADX/FVG Crypto Scanner")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan pass and exit (ideal for GitHub Actions)",
    )
    args = parser.parse_args()
    env_once = os.getenv("RUN_ONCE", "").lower() in {"1", "true", "yes"}
    main(single_run=(args.once or env_once))
