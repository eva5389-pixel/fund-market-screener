import pandas as pd
import streamlit as st
import yfinance as yf

# 設定網頁版面與暗色風格
st.set_page_config(
    page_title="全球科技與供應鏈即時漲跌儀表板", page_icon="📈", layout="wide"
)

# 注入自定義 CSS 打造專業投行儀表板風格
st.markdown(
    """
    <style>
    .main { background-color: #0e1117; color: #ffffff; }
    .stMetric { background-color: #161b22; padding: 15px; border-radius: 8px; border: 1px solid #30363d; }
    </style>
""",
    unsafe_allow_html=True,
)

st.title("🌐 全球科技、軍工、低軌衛星與供應鏈即時漲跌總覽")
st.markdown(
    "即時串接 Yahoo Finance，追蹤美、台、日、中、港各關鍵產業供應鏈、量子 AI、軍工、低軌衛星與科技巨頭股價。"
)

# 定義跨國供應鏈與 Yahoo Finance 代碼清單
supply_chains = {
    "低軌衛星": {
        "SpaceX (概念/特斯拉)": "TSLA",
        "思佳訊/Skyworks (美)": "SWKS",
        "Viasat (美)": "VSAT",
        "升達科 (台)": "3491.TW",
        "耀登 (台)": "3138.TWO",
        "芳興 (台)": "4526.TW",
        "NEC (日)": "6701.T",
    },
    "軍工/國防": {
        "洛歇馬丁 (美)": "LMT",
        "雷神技術 (美)": "RTX",
        "諾斯洛普格魯曼 (美)": "NOC",
        "通用動力 (美)": "GD",
        "帕蘭提爾 (美)": "PLTR",
        "漢翔 (台)": "2634.TW",
        "雷虎 (台)": "8033.TW",
        "駐龍 (台)": "4572.TW",
        "寶一 (台)": "8222.TW",
        "三菱重工業 (日)": "7011.T",
        "川崎重工業 (日)": "7012.T",
        "萊茵金屬 (德)": "RHM.DE",
        "航發動力 (中)": "600893.SS",
    },
    "被動元件/MLCC": {
        "村田製作所 (日)": "6981.T",
        "TDK (日)": "6762.T",
        "國巨 (台)": "2327.TW",
        "華新科 (台)": "2492.TW",
        "合昇堂 (台)": "8936.TWO",
        "三環集團 (中)": "300408.SZ",
    },
    "光通訊/電網線材": {
        "古河電工 (日)": "5801.T",
        "Lumentum (美)": "LITE",
        "Coherent (美)": "COHR",
        "聯亞 (台)": "3081.TWO",
        "華星光 (台)": "4979.TWO",
        "中際旭創 (中)": "300308.SZ",
    },
    "量子電腦": {
        "IBM (美)": "IBM",
        "Google/Alphabet (美)": "GOOGL",
        "IonQ (美)": "IONQ",
        "Rigetti Computing (美)": "RGTI",
        "D-Wave Quantum (美)": "QBTS",
        "廣達 (台)": "2382.TW",
        "鴻海 (台)": "2317.TW",
    },
    "半導體": {
        "台積電 (台)": "2330.TW",
        "NVIDIA (美)": "NVDA",
        "艾司摩爾 (美)": "ASML",
        "艾德萬測試 (日)": "6857.T",
        "中芯國際 (港)": "0981.HK",
        "東京威力科創 (日)": "8035.T",
    },
    "記憶體": {
        "美光 (美)": "MU",
        "長鑫存儲/長鑫科技 (中)": "688825.SS",
        "三星電子 (韓/參考)": "005930.KS",
        "南亞科 (台)": "2408.TW",
        "華邦電 (台)": "2344.TW",
        "旺宏 (台)": "2337.TW",
    },
    "金融": {
        "摩根大通 (美)": "JPM",
        "波克夏 (美)": "BRK-B",
        "富邦金 (台)": "2881.TW",
        "國泰金 (台)": "2882.TW",
        "中國平安 (港)": "2318.HK",
    },
    "功率元件": {
        "英飛凌 (德/歐)": "IFX.DE",
        "安森美 (美)": "ON",
        "德州儀器 (美)": "TXN",
        "強茂 (台)": "2481.TW",
        "台半 (台)": "5425.TWO",
    },
    "矽晶圓": {
        "信越化學 (日)": "4063.T",
        "勝高 (日)": "3436.T",
        "環球晶 (台)": "6488.TW",
        "台勝科 (台)": "3532.TW",
        "合晶 (台)": "6182.TW",
    },
    "電網/重電": {
        "伊頓 (美)": "ETN",
        "施耐德電機 (法)": "SU.PA",
        "士電 (台)": "1503.TW",
        "華城 (台)": "1519.TW",
        "中興電 (台)": "1513.TW",
    },
    "封裝測試": {
        "日月光投控 (台)": "3711.TW",
        "艾克爾 (美)": "AMKR",
        "京元電子 (台)": "2449.TW",
        "力成 (台)": "6239.TW",
        "長電科技 (中)": "600584.SS",
    },
    "機器人": {
        "宇樹科技 (中)": "688836.SS",
        "發那科 (日)": "6954.T",
        "安川電機 (日)": "6506.T",
        "特斯拉 (美)": "TSLA",
        "所羅門 (台)": "2359.TW",
        "大族激光 (中)": "002008.SZ",
    },
    "PCB版": {
        "臻鼎-KY (台)": "4958.TW",
        "欣興 (台)": "3037.TW",
        "金像電 (台)": "2368.TW",
        "Ibiden 揖斐電 (日)": "4062.T",
        "深南電路 (中)": "002916.SZ",
    },
    "航運": {
        "長榮 (台)": "2603.TW",
        "陽明 (台)": "2609.TW",
        "萬海 (台)": "2615.TW",
        "馬士基 (丹麥)": "MAERSK-B.CO",
        "中遠海控 (港)": "1919.HK",
    },
    "中國科技與AI平台": {
        "月之暗面/Moonshot AI (未上市)": "MOONSHOT_PRIVATE",
        "阿里巴巴 (美/港)": "BABA",
        "美團 (港)": "3690.HK",
        "拼多多 (美)": "PDD",
        "小米集團 (港)": "1810.HK",
        "DeepSeek (概念/算力連動)": "300033.SZ",
    },
}


@st.cache_data(ttl=600)
def fetch_stock_data(cache_version):
  results = []
  for category, stocks in supply_chains.items():
    for name, ticker in stocks.items():
      if ticker == "MOONSHOT_PRIVATE":
        results.append({
            "產業板塊": category,
            "股票名稱": name,
            "代碼": "未上市",
            "最新收盤價": "籌備上市中",
            "漲跌金額": 0.0,
            "漲跌幅(%)": "0.0%",
        })
        continue

      try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="2d")

        if len(hist) >= 2:
          close_price = hist["Close"].iloc[-1]
          prev_close = hist["Close"].iloc[-2]
          change = close_price - prev_close
          pct_change = (change / prev_close) * 100
        elif len(hist) == 1:
          close_price = hist["Close"].iloc[-1]
          prev_close = stock.info.get("previousClose", close_price)
          change = close_price - prev_close
          pct_change = (
              (change / prev_close) * 100 if prev_close else 0.0
          )
        else:
          close_price = stock.info.get("regularMarketPrice", 0.0)
          prev_close = stock.info.get("previousClose", 0.0)
          change = close_price - prev_close
          pct_change = (
              (change / prev_close) * 100 if prev_close else 0.0
          )

        results.append({
            "產業板塊": category,
            "股票名稱": name,
            "代碼": ticker,
            "最新收盤價": round(close_price, 2),
            "漲跌金額": round(change, 2),
            "漲跌幅(%)": f"{round(pct_change, 2)}%",
        })
      except Exception:
        results.append({
            "產業板塊": category,
            "股票名稱": name,
            "代碼": ticker,
            "最新收盤價": 0.0,
            "漲跌金額": 0.0,
            "漲跌幅(%)": "0.0%",
        })
  return pd.DataFrame(results)


# 側邊欄篩選
st.sidebar.header("🔍 篩選與控制面板")
if st.sidebar.button("🔄 重新整理即時股價"):
  st.cache_data.clear()

selected_category = st.sidebar.selectbox(
    "選擇產業板塊篩選：", ["全部顯示"] + list(supply_chains.keys())
)

with st.spinner("正在從 Yahoo Finance 抓取最新跨國股價數據，請稍候..."):
  df_stocks = fetch_stock_data("20260825-red-up-green-down-v2")

if selected_category != "全部顯示":
  df_filtered = df_stocks[df_stocks["產業板塊"] == selected_category]
else:
  df_filtered = df_stocks


def change_color(value):
  try:
    number = float(str(value).replace("%", "").replace(",", ""))
  except (TypeError, ValueError):
    return "color: #ffffff"
  if number > 0:
    return "color: #ff4d4d; font-weight: 700"
  if number < 0:
    return "color: #2eb82e; font-weight: 700"
  return "color: #ffffff"


# 顯示總覽表格
styled_table = df_filtered.style.map(
    change_color, subset=["漲跌金額", "漲跌幅(%)"]
)
st.dataframe(styled_table, width="stretch", height=650, hide_index=True)
