"""腾讯股票接口：港股行情兜底源。

当 AkShare（东财）和 yfinance 都拿不到某只港股时——尤其是新上市股，yahoo 收录
严重滞后（如 MiniMax 00100.HK）——用腾讯免费接口补齐历史日 K。

接口走直连（trust_env=False），不经代理；腾讯财经接口全球可达，CI 环境同样适用。

⚠️ 字段顺序坑：腾讯日 K 每根是 [日期, 开, 收, 高, 低, 量]，是「开收高低」而非
标准 OHLC（开高低收），解析时务必按此顺序映射。
"""
import httpx
import pandas as pd

_TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"


def _tx_code(ticker: str) -> str:
    """portfolio 港股代码 → 腾讯代码：'00100' → 'hk00100'（保留原始位数，不去前导零）。"""
    return f"hk{ticker}"


def fetch_hk_daily_tencent(ticker: str, days: int = 60) -> pd.DataFrame | None:
    """拉取港股前复权日 K，返回 open/high/low/close/volume（DatetimeIndex）。

    失败或无数据返回 None，由调用方决定后续兜底。
    """
    code = _tx_code(ticker)
    # 多取一些保证够 20 条算指标；腾讯单次上限 ~500
    n = min(max(days + 10, 30), 500)
    param = f"{code},day,,,{n},qfq"
    try:
        with httpx.Client(trust_env=False, timeout=15, follow_redirects=True) as client:
            # 逗号不能被 URL 编码（腾讯接口要求原样），故直接拼接而非用 params=
            resp = client.get(f"{_TX_KLINE_URL}?param={param}")
            data = resp.json()
        node = data.get("data", {}).get(code, {})
        kl = node.get("qfqday") or node.get("day") or []
        rows = []
        for item in kl:
            if len(item) < 6:
                continue
            # [date, open, close, high, low, volume, ...] —— 开收高低，非 OHLC
            rows.append({
                "date": item[0],
                "open": float(item[1]),
                "close": float(item[2]),
                "high": float(item[3]),
                "low": float(item[4]),
                "volume": float(item[5]),
            })
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        return df[["open", "high", "low", "close", "volume"]].tail(days)
    except Exception as e:
        print(f"[Tencent] {ticker} 获取失败: {e}")
        return None
