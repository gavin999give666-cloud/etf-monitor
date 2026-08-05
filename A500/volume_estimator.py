"""
V6.2 盘中成交量估算模块
======================
基于盘中已产生的部分成交量，估算当日完整成交量：
1. 获取5分钟线，求和得到当日已产生的成交量（partial_volume）
2. 用近N天5分钟线构建成交量时间分布（volume profile），
   自动标定当前时刻已完成全天成交量的比例
3. 历史数据不足（<3天）时，退化为固定比例 INTRADAY_VOLUME_RATIO_DEFAULT
"""
import json
import time
from datetime import datetime

import pandas as pd
import requests

import config


def fetch_5min_bars(stock_code=None, datalen=500):
    """从新浪财经获取5分钟K线数据（API与日线相同，仅 scale=5）

    Returns:
        DataFrame，列: date(datetime), open, high, low, close, volume；失败返回 None
    """
    if stock_code is None:
        stock_code = config.STOCK_CODE
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {
        "symbol": f"sh{stock_code}",
        "scale": "5",
        "ma": "no",
        "datalen": str(datalen),
    }
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    for attempt in range(5):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            if r.status_code != 200:
                print(f"Sina 5分钟线 API 状态码 {r.status_code}，重试...")
                time.sleep(2)
                continue

            data = json.loads(r.text)
            if not data:
                print("Sina 5分钟线 API 返回空数据")
                time.sleep(2)
                continue

            df = pd.DataFrame(data)
            df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df = df.dropna(subset=['close'])
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)

            print(f"成功获取 {len(df)} 条5分钟线数据（Sina）")
            return df

        except json.JSONDecodeError:
            print("Sina 5分钟线 API 返回非 JSON 格式，重试...")
            time.sleep(2)
        except Exception as e:
            print(f"Sina 5分钟线 API 出错: {type(e).__name__}: {str(e)[:80]}，重试...")
            time.sleep(2)

    return None


def build_volume_profile(df_5min):
    """从5分钟线数据构建成交量时间分布（累积比例）

    按 time-of-day（如 09:35, 09:40, ..., 14:55, 15:00）分组，
    计算每个时段的平均成交量，再归一化为累积比例。

    Returns:
        dict: {time_str: cumulative_ratio}，失败返回空 dict
    """
    df = df_5min.copy()
    df['time'] = df['date'].dt.strftime('%H:%M')
    avg_vol = df.groupby('time')['volume'].mean().sort_index()
    total = avg_vol.sum()
    if total <= 0:
        return {}
    cumulative = avg_vol.cumsum() / total
    return cumulative.to_dict()


def get_cumulative_ratio(volume_profile, target_time):
    """查询目标时刻（如 "14:40"）对应的累积比例

    从 volume_profile 中找到 <= target_time 的最近时段，返回其累积比例。
    """
    if not volume_profile:
        return None
    times = sorted(volume_profile.keys())
    best = None
    for t in times:
        if t <= target_time:
            best = t
        else:
            break
    if best is None:
        best = times[0]
    return volume_profile[best]


def estimate_volume(stock_code=None, now=None):
    """主入口：估算当日完整成交量与当日 OHLC

    Args:
        stock_code: 证券代码（默认取 config.STOCK_CODE）
        now: 当前时间（默认 datetime.now()）

    Returns:
        dict: {date, open, high, low, close, volume(估算), is_estimated(True)}
        失败返回 None
    """
    if stock_code is None:
        stock_code = config.STOCK_CODE
    if now is None:
        now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    target_time = now.strftime('%H:%M')

    try:
        # datalen 需覆盖回看天数 + 当日的5分钟线（每天约48根）
        datalen = (config.VOLUME_PROFILE_LOOKBACK_DAYS + 1) * 48 + 50
        df = fetch_5min_bars(stock_code, datalen=datalen)
        if df is None or df.empty:
            print("获取5分钟线失败，无法进行盘中估算")
            return None

        df['day'] = df['date'].dt.strftime('%Y-%m-%d')
        df_today = df[df['day'] == today_str]
        df_hist = df[df['day'] != today_str]

        if df_today.empty:
            print(f"未获取到当日（{today_str}）的5分钟线数据")
            return None

        partial_volume = float(df_today['volume'].sum())

        hist_days = df_hist['day'].nunique()
        if hist_days >= 3:
            profile = build_volume_profile(df_hist)
            ratio = get_cumulative_ratio(profile, target_time)
            if ratio is None or ratio <= 0:
                ratio = config.INTRADAY_VOLUME_RATIO_DEFAULT
                print(f"成交量分布标定失败，使用固定比例 {ratio}")
            else:
                print(f"使用 {hist_days} 天5分钟线自动标定：{target_time} 已完成全天成交量的 {ratio:.2%}")
        else:
            ratio = config.INTRADAY_VOLUME_RATIO_DEFAULT
            print(f"5分钟线历史数据不足（{hist_days} 天 < 3），使用固定比例 {ratio}")

        estimated_volume = int(partial_volume / ratio)

        result = {
            'date': today_str,
            'open': float(df_today['open'].iloc[0]),
            'high': float(df_today['high'].max()),
            'low': float(df_today['low'].min()),
            'close': float(df_today['close'].iloc[-1]),
            'volume': estimated_volume,
            'is_estimated': True,
        }
        print(f"盘中估算：已成交 {int(partial_volume)}，完成比例 {ratio:.4f}，估算全天成交量 {estimated_volume}")
        return result

    except Exception as e:
        print(f"盘中成交量估算失败: {type(e).__name__}: {e}")
        return None
