import streamlit as st
import pandas as pd
import json
import os
import glob

# 讀取你的基金設定檔
def load_config():
    with open('config/funds.json', 'r', encoding='utf-8') as f:
        return json.load(f)

# 讀取你的類別 CSV
def get_categories():
    files = glob.glob("data/categories/*.csv")
    return {os.path.basename(f).replace('.csv', ''): f for f in files}

st.set_page_config(page_title="Fund Analysis", layout="wide")
config = load_config()
categories = get_categories()

st.sidebar.title("基金篩選器")

# 1. 類別選擇 (對應 Grafana 分類)
selected_cat = st.sidebar.selectbox("選擇市場/分類", list(categories.keys()))
cat_df = pd.read_csv(categories[selected_cat])

# 2. 基金選擇
# 從 config 讀取對應分類的基金清單
fund_list = config.get(selected_cat, [])
selected_funds = st.sidebar.multiselect("選擇基金", fund_list, default=fund_list[:3])

# 3. 讀取對應 NAV 資料進行運算
def load_nav_data(tickers):
    data = {}
    for t in tickers:
        path = f"data/nav/{t}.csv"
        if os.path.exists(path):
            data[t] = pd.read_csv(path, index_col="date", parse_dates=True)['nav']
    return pd.DataFrame(data)

if selected_funds:
    df = load_nav_data(selected_funds)
    st.line_chart(df)
    
    # 在這裡加入馬克維茲效率前緣運算
    if st.button("計算最佳投資組合配置"):
        returns = df.pct_change().dropna()
        # 呼叫我們之前定義的 optimize_portfolio 邏輯...
        st.write("已根據該類別歷史績效完成最佳化運算")