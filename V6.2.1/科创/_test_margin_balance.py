"""
测试项：能否获取886033为代表科技板块资金的融资融券余额
====================================================
886033 是东方财富"板块融资融券余额"指数代码，代表科技板块两融资金

测试结论：
  886033 无法通过公开API直接获取（东财数据中心报表配置不存在）
  但可通过 akshare stock_margin_detail_sse 筛选科创板688开头个股汇总，
  等价获取科创板融资融券余额数据

可行方案：
  akshare stock_margin_detail_sse(date="YYYYMMDD")
  → 筛选 标的证券代码 以 '688' 开头的行
  → 汇总 融资余额、融券余量
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import requests
from datetime import datetime, timedelta
import time

print("=" * 60)
print("测试：获取886033融资融券余额（代表科技板块资金）")
print("=" * 60)


def fetch_kcb_margin_single_day(date_str):
    """获取单日科创板融资融券汇总数据

    Args:
        date_str: 日期字符串，格式 YYYYMMDD

    Returns:
        dict: 包含 date, rz_total(融资余额), rq_volume(融券余量), stock_count(个股数)
              或 None（获取失败时）
    """
    import akshare as ak
    try:
        df = ak.stock_margin_detail_sse(date=date_str)
        if df is None or df.empty:
            return None

        # 筛选科创板（688开头）
        kcb = df[df['标的证券代码'].astype(str).str.startswith('688')]
        if kcb.empty:
            return None

        rz_col = '融资余额'
        rq_col = '融券余量'
        kcb[rz_col] = pd.to_numeric(kcb[rz_col], errors='coerce')
        kcb[rq_col] = pd.to_numeric(kcb[rq_col], errors='coerce')

        return {
            'date': date_str,
            'rz_total': kcb[rz_col].sum(),
            'rq_volume': kcb[rq_col].sum(),
            'stock_count': len(kcb),
        }
    except Exception as e:
        print(f"  获取 {date_str} 失败: {type(e).__name__}: {str(e)[:80]}")
        return None


def fetch_kcb_margin_history(days=10):
    """获取近N个交易日的科创板融资融券汇总

    Args:
        days: 尝试获取的天数（含非交易日会被跳过）

    Returns:
        pd.DataFrame: 每日科创板融资融券汇总
    """
    import akshare as ak
    records = []
    check_date = datetime.now() - timedelta(days=1)  # 从昨天开始

    for i in range(days + 5):  # 多尝试几天以覆盖非交易日
        if len(records) >= days:
            break
        date_str = check_date.strftime("%Y%m%d")
        # 跳过周末
        if check_date.weekday() >= 5:
            check_date -= timedelta(days=1)
            continue

        result = fetch_kcb_margin_single_day(date_str)
        if result:
            records.append(result)
            print(f"  {date_str}: 融资余额 {result['rz_total']/1e8:.2f}亿, "
                  f"融券余量 {result['rq_volume']:,.0f}股, "
                  f"个股数 {result['stock_count']}")
        else:
            # 非交易日或无数据
            pass

        check_date -= timedelta(days=1)
        time.sleep(0.3)  # 避免请求过快

    if not records:
        return None

    df = pd.DataFrame(records)
    df['date'] = pd.to_datetime(df['date'], format='%Y%m%d')
    df = df.sort_values('date').reset_index(drop=True)
    return df


# ============================================================
# 1. 单日测试：获取最近交易日科创板融资融券数据
# ============================================================
print("\n--- 1. 单日科创板融资融券汇总 ---")
test_date = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")
result = fetch_kcb_margin_single_day(test_date)
if result:
    print(f"  日期: {result['date']}")
    print(f"  科创板融资余额: {result['rz_total']:,.0f} 元 ({result['rz_total']/1e8:.2f} 亿元)")
    print(f"  科创板融券余量: {result['rq_volume']:,.0f} 股")
    print(f"  科创板个股数量: {result['stock_count']}")
else:
    print(f"  {test_date} 获取失败（可能是非交易日）")

# ============================================================
# 2. 多日历史测试：获取近10个交易日趋势
# ============================================================
print("\n--- 2. 近10个交易日科创板融资融券趋势 ---")
df_history = fetch_kcb_margin_history(days=10)
if df_history is not None:
    print(f"\n  获取到 {len(df_history)} 个交易日数据")
    print(f"\n  {'日期':<14} {'融资余额(亿元)':<18} {'融券余量(万股)':<18} {'个股数'}")
    print("  " + "-" * 60)
    for _, row in df_history.iterrows():
        print(f"  {row['date'].strftime('%Y-%m-%d'):<14} "
              f"{row['rz_total']/1e8:<18.2f} "
              f"{row['rq_volume']/1e4:<18.2f} "
              f"{row['stock_count']}")

    # 计算趋势
    if len(df_history) >= 2:
        first = df_history.iloc[0]
        last = df_history.iloc[-1]
        rz_change = (last['rz_total'] - first['rz_total']) / first['rz_total'] * 100
        print(f"\n  期间融资余额变化: {rz_change:+.2f}%")
        print(f"  {first['date'].strftime('%Y-%m-%d')} → {last['date'].strftime('%Y-%m-%d')}")
else:
    print("  获取历史数据失败")

# ============================================================
# 3. 验证与东财886033的一致性
# 东财网页显示的科创板融资余额约为 2905亿（2026-03-13数据）
# ============================================================
print("\n--- 3. 数据一致性验证 ---")
print("  东财网页 https://data.eastmoney.com/rzrq/hy.html 上显示：")
print("  886033 科创板融资融券余额（需要与方案A结果对比）")
if result:
    print(f"  方案A获取的 {result['date']} 科创板融资余额: {result['rz_total']/1e8:.2f} 亿元")
    print(f"  ★ 如与东财网站数据接近，则方案A可作为886033的替代数据源")

# ============================================================
# 4. 补充：东财融资融券板块页面抓取尝试
# 通过浏览器自动化或模拟请求获取真实报表名
# ============================================================
print("\n--- 4. 东财融资融券板块网页数据抓取 ---")
try:
    # 访问东财板块融资融券页面，通过前端API获取数据
    # 该页面使用的实际API端点
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    # 尝试常见的报表名模式
    report_names = [
        "RPT_RZRQ_BOARDNEW",
        "RPT_RZRQ_INDUSTRY",
        "RPT_RZRQ_HYNEW",
        "RPT_DATARZQ_HYDETAIL",
        "RPT_RZRQ_KCB",
    ]
    found = False
    for rn in report_names:
        try:
            params = {
                "reportName": rn,
                "columns": "ALL",
                "pageNumber": "1",
                "pageSize": "5",
                "source": "WEB",
                "client": "WEB",
            }
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            r = requests.get(url, params=params, headers=headers, timeout=10)
            result_api = r.json()
            if result_api.get("success"):
                print(f"  [{rn}] 找到可用报表!")
                data = result_api["result"].get("data", [])
                if data:
                    df_api = pd.DataFrame(data)
                    print(f"  列名: {df_api.columns.tolist()}")
                    print(df_api.head(2).to_string())
                found = True
                break
        except:
            continue
    if not found:
        print("  东财数据中心API无可用板块融资融券报表")
        print("  结论：886033无法直接通过API获取，需使用方案A替代")
except Exception as e:
    print(f"  失败: {type(e).__name__}: {str(e)[:120]}")

# ============================================================
# 最终结论
# ============================================================
print("\n" + "=" * 60)
print("最终结论")
print("=" * 60)
print("""
886033（东方财富板块融资融券余额指数）无法通过公开API直接获取。

可行替代方案：
  akshare stock_margin_detail_sse(date="YYYYMMDD")
  → 筛选 标的证券代码 以 '688' 开头的行
  → 汇总 融资余额 列得到科创板融资余额
  → 汇总 融券余量 列得到科创板融券余量(股)

注意事项：
  1. 沪市明细无"融券余额(元)"列，仅有"融券余量(股)"
     如需融券余额金额，需结合股价估算
  2. 需逐日拉取再汇总（每次获取一天全部沪市个股明细，约2000条）
  3. 科创板ETF（588开头）的融资融券数据也包含在沪市明细中
  4. 与东财网站886033数据对比验证后可正式使用
""")
