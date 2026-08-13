"""
从 OKX 真实调取 2h/4h 历史K线，佐证回测结果
"""
import ccxt
import pandas as pd
import time
from datetime import datetime, timezone
from pathlib import Path

PROXY = "http://127.0.0.1:7897"

ex = ccxt.okx({
    "enableRateLimit": True,
    "timeout": 30000,
    "options": {"defaultType": "swap"},
    "proxies": {"http": PROXY, "https": PROXY},
})
ex.set_sandbox_mode(False)
ex.load_markets()

SINCE = int(datetime(2025, 8, 12, tzinfo=timezone.utc).timestamp() * 1000)


def fetch_all(symbol, tf, since_ms, max_bars=20000):
    """分批拉取完整历史K线"""
    all_rows = []
    cursor = since_ms
    while len(all_rows) < max_bars:
        try:
            rows = ex.fetch_ohlcv(symbol, tf, since=cursor, limit=300)
        except Exception as e:
            print(f"  拉取异常，等待重试: {e}")
            time.sleep(3)
            continue
        if not rows:
            break
        if all_rows and rows[0][0] <= all_rows[-1][0]:
            rows = rows[1:]
        if not rows:
            break
        all_rows.extend(rows)
        cursor = rows[-1][0] + 1
        print(f"  {symbol} {tf}: {len(all_rows)} 根", end="\r")
        if len(rows) < 300:
            break
        time.sleep(ex.rateLimit / 1000)
    print()
    return all_rows


def main():
    Path("data").mkdir(exist_ok=True)

    tasks = [
        ("BTC/USDT:USDT", "bt_BTC_USDT_USDT", "2h"),
        ("BTC/USDT:USDT", "bt_BTC_USDT_USDT", "4h"),
        ("ETH/USDT:USDT", "bt_ETH_USDT_USDT", "2h"),
        ("ETH/USDT:USDT", "bt_ETH_USDT_USDT", "4h"),
    ]

    for symbol, base, tf in tasks:
        print(f"\n调取 {symbol} {tf} ...")
        rows = fetch_all(symbol, tf, SINCE)
        if not rows:
            print(f"  ⚠️ {symbol} {tf} 无数据")
            continue
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        fname = f"data/{base}_{tf}_2025-08-12.csv"
        df.to_csv(fname, index=False)
        print(f"  ✅ 已保存 {fname}: {len(df)} 根, "
              f"{df['datetime'].iloc[0]} ~ {df['datetime'].iloc[-1]}")
        time.sleep(1)

    print("\n全部调取完成")


if __name__ == "__main__":
    main()
