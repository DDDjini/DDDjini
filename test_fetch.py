import ccxt, time, os
from datetime import datetime, timezone

proxy = "http://127.0.0.1:7897"
exchange = ccxt.okx({
    "enableRateLimit": True,
    "timeout": 15000,
    "options": {"defaultType": "swap"},
    "proxies": {"http": proxy, "https": proxy}
})

since = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)

for limit in [100, 300]:
    t0 = time.time()
    try:
        rows = exchange.fetch_ohlcv("BTC/USDT:USDT", "1h", since=since, limit=limit)
        print(f"limit={limit}: 获取 {len(rows)} 行, 耗时 {time.time()-t0:.2f}s")
        print(f"  首行={datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc)}, 末行={datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc)}")
    except Exception as e:
        print(f"limit={limit}: 失败 {e}")
