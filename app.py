from __future__ import annotations

import json
import os
import re
import subprocess
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from pathlib import Path
from io import BytesIO

import numpy as np
import pandas as pd
import streamlit as st


st.set_page_config(
    page_title="全球共同基金篩選器",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RANKINGS_FILE = DATA_DIR / "rankings.csv"
STATUS_FILE = DATA_DIR / "status.json"
NAV_HISTORY_DIR = DATA_DIR / "nav_history"
PORTFOLIO_DIR = DATA_DIR / "portfolio"
GRAFANA_TEMPLATES_FILE = DATA_DIR / "grafana_fund_templates.json"
ETF_FLOW_FILE = DATA_DIR / "etf_flows.csv"
UPDATE_SCRIPT = BASE_DIR / "src" / "update_dashboard.py"
AUTO_UPDATE_INTERVAL = timedelta(hours=24)

METRIC_COLUMNS = [
    "return_1m",
    "return_3m",
    "return_1y",
    "benchmark_return_1y",
    "excess_return_1y",
    "momentum_6m",
    "sharpe",
    "max_drawdown",
    "recovery_days",
    "score",
]

DISPLAY_COLUMNS = {
    "screen_rank": "篩選排名",
    "rank": "原分類排名",
    "category_name": "市場／主題",
    "name": "基金",
    "moneydj_id": "MoneyDJ ID",
    "tcb_url": "合庫連結",
    "benchmark": "Benchmark",
    "return_1m": "一個月報酬%",
    "return_3m": "三個月報酬%",
    "return_1y": "一年報酬%",
    "benchmark_return_1y": "Benchmark一年報酬%",
    "excess_return_1y": "超額報酬%",
    "momentum_6m": "六個月動能%",
    "sharpe": "夏普",
    "max_drawdown": "最大回撤%",
    "recovery_days": "恢復天數",
    "score": "綜合評分",
    "signal": "訊號",
    "status": "資料狀態",
    "data_source": "資料來源",
    "data_date": "資料日期",
}


@st.cache_data(ttl=300, show_spinner=False)
def load_rankings(file_mtime: float) -> pd.DataFrame:
    del file_mtime
    if not RANKINGS_FILE.exists():
        return pd.DataFrame()
    frame = pd.read_csv(RANKINGS_FILE)
    for column in ("return_1m", "return_3m"):
        if column not in frame.columns:
            frame[column] = np.nan
    for column in METRIC_COLUMNS:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("category_name", "name", "signal", "status"):
        if column in frame.columns:
            frame[column] = frame[column].fillna("").astype(str)
    if "data_source" not in frame.columns:
        frame["data_source"] = frame.get("status", "")
    else:
        frame["data_source"] = frame["data_source"].fillna(frame.get("status", "")).replace("", "尚無可用來源")
    if "data_date" not in frame.columns:
        fallback_date = datetime.fromtimestamp(RANKINGS_FILE.stat().st_mtime).strftime("%Y-%m-%d")
        frame["data_date"] = fallback_date
    else:
        fallback_date = datetime.fromtimestamp(RANKINGS_FILE.stat().st_mtime).strftime("%Y-%m-%d")
        frame["data_date"] = frame["data_date"].fillna(fallback_date).replace("", fallback_date)
    if "moneydj_id" in frame.columns:
        def build_tcb_url(identifier) -> str:
            value = str(identifier or "").strip()
            if not value or value.lower() == "nan":
                return ""
            section = "wr" if value.upper().startswith("AC") else "wb"
            return f"https://tcbbankfund.moneydj.com/w/{section}/{section}902.djhtm?a={value}"
        frame["tcb_url"] = frame["moneydj_id"].map(build_tcb_url)
        # Backfill the new short-term returns immediately from the already
        # downloaded MoneyDJ NAV history; the daily updater persists them later.
        for index, row in frame.iterrows():
            identifier = str(row.get("moneydj_id") or "").strip()
            if not identifier or identifier.lower() == "nan":
                continue
            path = NAV_HISTORY_DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '_', identifier)}.csv"
            if not path.exists():
                continue
            try:
                history = pd.read_csv(path)
                values = pd.to_numeric(history["nav"], errors="coerce").dropna()
                for column, periods in (("return_1m", 21), ("return_3m", 63)):
                    if len(values) > periods and pd.isna(frame.at[index, column]):
                        frame.at[index, column] = (values.iloc[-1] / values.iloc[-periods - 1] - 1) * 100
            except (OSError, KeyError, ValueError):
                continue
    return frame


@st.cache_data(ttl=300, show_spinner=False)
def load_status(file_mtime: float) -> dict:
    del file_mtime
    if not STATUS_FILE.exists():
        return {}
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def file_mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


def yahoo_stock_url(name: object) -> str:
    """Use Yahoo Taiwan search so holdings without a ticker still resolve safely."""
    value = re.sub(r",.*$", "", str(name or "")).strip()
    return f"https://tw.stock.yahoo.com/search?q={quote(value)}" if value else ""


def last_update_time() -> datetime | None:
    status = load_status(file_mtime(STATUS_FILE))
    raw = status.get("updated_at")
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    if RANKINGS_FILE.exists():
        return datetime.fromtimestamp(RANKINGS_FILE.stat().st_mtime, tz=timezone.utc)
    return None


def update_data(force: bool = False) -> tuple[bool, str]:
    """Run the provider pipeline; failed providers retain last successful values."""
    if not UPDATE_SCRIPT.exists():
        return False, "找不到資料更新程式"
    previous = last_update_time()
    if not force and previous and datetime.now(timezone.utc) - previous < AUTO_UPDATE_INTERVAL:
        return False, "資料仍在每日更新週期內"
    environment = os.environ.copy()
    environment["ALLOW_PENDING"] = "1"
    try:
        result = subprocess.run(
            [os.sys.executable, str(UPDATE_SCRIPT)],
            cwd=BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"更新未完成：{type(exc).__name__}；已保留上次資料"
    if result.returncode != 0:
        return False, "資料來源暫時無法更新；已保留上次資料"
    st.cache_data.clear()
    return True, "資料更新完成"


def fmt_number(value, digits=2, suffix="") -> str:
    if pd.isna(value):
        return "—"
    return f"{float(value):,.{digits}f}{suffix}"


def status_badge(signal: str) -> str:
    return {
        "買進": "🟢 買進",
        "觀察": "🟡 觀察",
        "賣出": "🔴 賣出",
        "待資料": "⚪ 待資料",
    }.get(str(signal), f"⚪ {signal or '待資料'}")


def _match_column(columns, aliases: list[str]):
    normalized = {str(column).strip().lower().replace(" ", ""): column for column in columns}
    for alias in aliases:
        key = alias.lower().replace(" ", "")
        if key in normalized:
            return normalized[key]
    return None


@st.cache_data(show_spinner=False)
def parse_backtest_upload(file_bytes: bytes, filename: str) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Accept long/wide NAV data plus an optional industry-allocation sheet."""
    errors: list[str] = []
    raw_sheets: dict[str, pd.DataFrame] = {}
    try:
        if filename.lower().endswith(".csv"):
            raw_sheets["淨值"] = pd.read_csv(BytesIO(file_bytes))
        else:
            book = pd.ExcelFile(BytesIO(file_bytes))
            raw_sheets = {sheet: pd.read_excel(book, sheet_name=sheet) for sheet in book.sheet_names}
    except Exception as exc:
        return pd.DataFrame(), pd.DataFrame(), [f"檔案無法讀取：{type(exc).__name__}"]

    history = pd.DataFrame()
    allocation = pd.DataFrame()
    for sheet_name, raw in raw_sheets.items():
        if raw.empty:
            continue
        date_col = _match_column(raw.columns, ["日期", "date", "nav_date", "淨值日期"])
        fund_col = _match_column(raw.columns, ["基金", "基金名稱", "name", "fund", "fund_name"])
        nav_col = _match_column(raw.columns, ["淨值", "nav", "price", "close", "收盤價"])
        industry_col = _match_column(raw.columns, ["產業", "產業名稱", "industry", "sector"])
        weight_col = _match_column(raw.columns, ["占比", "比重", "weight", "比例", "allocation", "percent"])

        if fund_col is not None and industry_col is not None and weight_col is not None:
            part = raw[[fund_col, industry_col, weight_col]].copy()
            part.columns = ["fund", "industry", "weight"]
            part["weight"] = pd.to_numeric(part["weight"].astype(str).str.replace("%", "", regex=False), errors="coerce")
            if part["weight"].dropna().gt(1).any():
                part["weight"] = part["weight"] / 100
            allocation = pd.concat([allocation, part], ignore_index=True)
            continue

        if date_col is not None and fund_col is not None and nav_col is not None:
            part = raw[[date_col, fund_col, nav_col]].copy()
            part.columns = ["date", "fund", "nav"]
            history = pd.concat([history, part], ignore_index=True)
        elif date_col is not None:
            value_columns = [column for column in raw.columns if column != date_col]
            if value_columns:
                part = raw.melt(id_vars=[date_col], value_vars=value_columns, var_name="fund", value_name="nav")
                part = part.rename(columns={date_col: "date"})
                history = pd.concat([history, part], ignore_index=True)

    if history.empty:
        errors.append("找不到淨值資料；需要「日期、基金、淨值」三欄，或第一欄日期、其餘欄為基金。")
    else:
        history["date"] = pd.to_datetime(history["date"], errors="coerce")
        history["nav"] = pd.to_numeric(history["nav"], errors="coerce")
        history["fund"] = history["fund"].astype(str).str.strip()
        history = history.dropna(subset=["date", "nav"]).query("nav > 0")
        history = history.drop_duplicates(["fund", "date"], keep="last").sort_values(["fund", "date"])
    if not allocation.empty:
        allocation["fund"] = allocation["fund"].astype(str).str.strip()
        allocation["industry"] = allocation["industry"].astype(str).str.strip()
        allocation = allocation.dropna(subset=["weight"]).query("weight >= 0")
    return history, allocation, errors


def calculate_backtest(history: pd.DataFrame, annual_rf: float = 0.02) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    metrics = []
    normalized_series = []
    drawdown_series = []
    for fund, group in history.groupby("fund"):
        group = group.sort_values("date")
        values = group["nav"].astype(float)
        if len(values) < 2:
            continue
        daily = values.pct_change().dropna()
        elapsed_days = max((group["date"].iloc[-1] - group["date"].iloc[0]).days, 1)
        total_return = values.iloc[-1] / values.iloc[0] - 1
        annual_return = (values.iloc[-1] / values.iloc[0]) ** (365.25 / elapsed_days) - 1
        volatility = daily.std() * np.sqrt(252) if len(daily) > 1 else np.nan
        sharpe_value = (daily.mean() * 252 - annual_rf) / volatility if volatility and volatility > 0 else np.nan
        wealth = values / values.iloc[0]
        drawdown = wealth / wealth.cummax() - 1
        trough_position = int(np.argmin(drawdown.to_numpy()))
        peak_before = float(wealth.iloc[:trough_position + 1].max())
        recovered = wealth.iloc[trough_position + 1:].ge(peak_before)
        recovery = int(np.argmax(recovered.to_numpy()) + 1) if recovered.any() else np.nan
        score_value = annual_return + (0 if pd.isna(sharpe_value) else sharpe_value / 5) + float(drawdown.min()) / 2
        metrics.append({
            "基金": fund,
            "起始日": group["date"].iloc[0].date(),
            "結束日": group["date"].iloc[-1].date(),
            "資料筆數": len(group),
            "累積報酬%": total_return * 100,
            "年化報酬%": annual_return * 100,
            "年化波動%": volatility * 100 if pd.notna(volatility) else np.nan,
            "夏普": sharpe_value,
            "最大回撤%": float(drawdown.min()) * 100,
            "恢復天數": recovery,
            "回測評分": score_value,
        })
        normalized_series.append(pd.DataFrame({"日期": group["date"], "基金": fund, "標準化淨值": wealth.to_numpy() * 100}))
        drawdown_series.append(pd.DataFrame({"日期": group["date"], "基金": fund, "回撤%": drawdown.to_numpy() * 100}))
    ranking = pd.DataFrame(metrics)
    if not ranking.empty:
        ranking = ranking.sort_values("回測評分", ascending=False, na_position="last").reset_index(drop=True)
        ranking.insert(0, "排名", np.arange(1, len(ranking) + 1))
    return ranking, pd.concat(normalized_series, ignore_index=True) if normalized_series else pd.DataFrame(), pd.concat(drawdown_series, ignore_index=True) if drawdown_series else pd.DataFrame()


def load_scraped_backtest_history(funds: pd.DataFrame) -> pd.DataFrame:
    frames = []
    if not NAV_HISTORY_DIR.exists():
        return pd.DataFrame(columns=["date", "fund", "nav"])
    for _, fund_row in funds.iterrows():
        identifier = str(fund_row.get("moneydj_id") or "").strip()
        if not identifier:
            continue
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", identifier)
        path = NAV_HISTORY_DIR / f"{safe_name}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        if not {"date", "nav"}.issubset(frame.columns):
            continue
        frame["date"] = pd.to_datetime(frame["date"], format="%Y%m%d", errors="coerce")
        frame["nav"] = pd.to_numeric(frame["nav"], errors="coerce")
        frame["fund"] = str(fund_row["name"])
        frames.append(frame[["date", "fund", "nav"]].dropna())
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["date", "fund", "nav"])


def load_portfolio_details(funds: pd.DataFrame) -> pd.DataFrame:
    frames = []
    if not PORTFOLIO_DIR.exists():
        return pd.DataFrame()
    for _, fund_row in funds.iterrows():
        identifier = str(fund_row.get("moneydj_id") or "").strip()
        if not identifier:
            continue
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", identifier)
        path = PORTFOLIO_DIR / f"{safe_name}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        if not {"kind", "name", "weight", "data_date"}.issubset(frame.columns):
            continue
        frame["fund"] = str(fund_row["name"])
        frame["category_name"] = str(fund_row["category_name"])
        frame["weight"] = pd.to_numeric(frame["weight"], errors="coerce")
        frames.append(frame.dropna(subset=["weight"]))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_grafana_templates() -> dict[str, list[str]]:
    if not GRAFANA_TEMPLATES_FILE.exists():
        return {}
    try:
        payload = json.loads(GRAFANA_TEMPLATES_FILE.read_text(encoding="utf-8"))
        return {item["category"]: item.get("funds", []) for item in payload.get("categories", [])}
    except (OSError, json.JSONDecodeError, KeyError):
        return {}


def load_etf_flows() -> pd.DataFrame:
    if not ETF_FLOW_FILE.exists():
        return pd.DataFrame()
    frame = pd.read_csv(ETF_FLOW_FILE)
    if "net_flow_eur_m" in frame:
        frame["net_flow_eur_m"] = pd.to_numeric(frame["net_flow_eur_m"], errors="coerce")
    return frame


def build_flow_recommendations(flows: pd.DataFrame, eligible_funds: pd.DataFrame) -> pd.DataFrame:
    """Match positive ETF flows with funds that passed the active MoneyDJ screen."""
    if flows.empty or eligible_funds.empty:
        return pd.DataFrame()
    positive = flows[flows["net_flow_eur_m"].gt(0) & flows["template_category"].ne("其他")]
    totals = positive.groupby("template_category", as_index=False)["net_flow_eur_m"].sum().sort_values("net_flow_eur_m", ascending=False)
    category_expansion = {
        "科技主題": ["半導體", "記憶體", "機器人", "光通訊", "量子電腦"],
        "新興市場": ["印度", "東協", "中國", "巴西"],
    }
    required_metrics = ["return_1y", "momentum_6m", "sharpe", "max_drawdown", "score"]
    moneydj = eligible_funds.copy()
    moneydj = moneydj[moneydj["moneydj_id"].fillna("").astype(str).str.strip().ne("")]
    moneydj = moneydj.dropna(subset=[column for column in required_metrics if column in moneydj.columns])
    candidates = []
    for _, flow in totals.iterrows():
        flow_group = str(flow["template_category"])
        categories = category_expansion.get(flow_group, [flow_group])
        for category in categories:
            matched = moneydj[moneydj["category_name"].eq(category)]
            for _, fund in matched.iterrows():
                candidates.append({
                    "基金": fund["name"],
                    "對應市場／產業": category,
                    "ETF近月淨流入（百萬歐元）": float(flow["net_flow_eur_m"]),
                    "一年報酬%": fund["return_1y"],
                    "夏普": fund["sharpe"],
                    "最大回撤%": fund["max_drawdown"],
                    "綜合評分": fund["score"],
                    "合庫MoneyDJ": fund.get("tcb_url", ""),
                    "篩選理由": f"通過目前篩選條件；{flow_group} ETF 類別近月呈淨流入",
                })
    result = pd.DataFrame(candidates).drop_duplicates("基金") if candidates else pd.DataFrame()
    if result.empty:
        return result
    result = result.sort_values(
        ["ETF近月淨流入（百萬歐元）", "綜合評分"], ascending=[False, False]
    ).head(10).reset_index(drop=True)
    result.insert(0, "排名", np.arange(1, len(result) + 1))
    return result


def apply_filters(data: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    with st.sidebar:
        st.header("🔎 基金篩選條件")

        categories = sorted(data["category_name"].dropna().unique().tolist())
        selected_categories = st.multiselect(
            "市場／主題",
            categories,
            default=categories,
            help="可同時選擇台灣、日本、金融、醫療等多個分類。",
        )
        keyword = st.text_input("基金名稱關鍵字", placeholder="例如：台灣、科技、生技")

        signals = [value for value in ["買進", "觀察", "賣出", "待資料"] if value in set(data["signal"])]
        selected_signals = st.multiselect("訊號", signals, default=signals)

        completeness = st.radio(
            "資料完整度",
            ["全部", "核心指標完整", "仍有待補資料"],
            horizontal=False,
        )

        st.markdown("#### 數值門檻")
        use_return = st.checkbox("啟用最低一年報酬")
        min_return = st.number_input("最低一年報酬（%）", value=0.0, step=1.0, disabled=not use_return)
        use_return_1m = st.checkbox("啟用最低一個月報酬")
        min_return_1m = st.number_input("最低一個月報酬（%）", value=0.0, step=0.5, disabled=not use_return_1m)
        use_return_3m = st.checkbox("啟用最低三個月報酬")
        min_return_3m = st.number_input("最低三個月報酬（%）", value=0.0, step=0.5, disabled=not use_return_3m)
        use_momentum = st.checkbox("啟用最低六個月動能")
        min_momentum = st.number_input("最低六個月動能（%）", value=0.0, step=1.0, disabled=not use_momentum)
        use_sharpe = st.checkbox("啟用最低夏普")
        min_sharpe = st.number_input("最低夏普", value=0.0, step=0.1, disabled=not use_sharpe)
        use_drawdown = st.checkbox("限制最大回撤")
        max_drawdown_abs = st.number_input(
            "最大可接受回撤幅度（%）",
            min_value=0.0,
            value=30.0,
            step=1.0,
            disabled=not use_drawdown,
        )

        sort_labels = {
            "綜合評分（高至低）": "score",
            "一個月報酬（高至低）": "return_1m",
            "三個月報酬（高至低）": "return_3m",
            "一年報酬（高至低）": "return_1y",
            "六個月動能（高至低）": "momentum_6m",
            "夏普（高至低）": "sharpe",
            "最大回撤（較小優先）": "max_drawdown",
            "原分類排名": "rank",
        }
        sort_label = st.selectbox("排序方式", list(sort_labels))

        st.divider()
        if st.button("🔄 立即更新資料", use_container_width=True):
            with st.spinner("正在更新基金資料…"):
                success, message = update_data(force=True)
            (st.success if success else st.warning)(message)
            if success:
                st.rerun()
        st.caption("每小時檢查一次；資料超過 24 小時會自動更新。")

    filtered = data.copy()
    if selected_categories:
        filtered = filtered[filtered["category_name"].isin(selected_categories)]
    else:
        filtered = filtered.iloc[0:0]
    if keyword.strip():
        filtered = filtered[filtered["name"].str.contains(keyword.strip(), case=False, na=False)]
    if selected_signals:
        filtered = filtered[filtered["signal"].isin(selected_signals)]
    else:
        filtered = filtered.iloc[0:0]

    core_columns = ["return_1y", "excess_return_1y", "momentum_6m", "sharpe", "max_drawdown"]
    complete_mask = filtered[core_columns].notna().all(axis=1)
    if completeness == "核心指標完整":
        filtered = filtered[complete_mask]
    elif completeness == "仍有待補資料":
        filtered = filtered[~complete_mask]

    if use_return:
        filtered = filtered[filtered["return_1y"].ge(min_return)]
    if use_return_1m:
        filtered = filtered[filtered["return_1m"].ge(min_return_1m)]
    if use_return_3m:
        filtered = filtered[filtered["return_3m"].ge(min_return_3m)]
    if use_momentum:
        filtered = filtered[filtered["momentum_6m"].ge(min_momentum)]
    if use_sharpe:
        filtered = filtered[filtered["sharpe"].ge(min_sharpe)]
    if use_drawdown:
        filtered = filtered[filtered["max_drawdown"].ge(-max_drawdown_abs)]

    sort_column = sort_labels[sort_label]
    ascending = sort_column in {"rank", "max_drawdown"}
    filtered = filtered.sort_values(sort_column, ascending=ascending, na_position="last").reset_index(drop=True)
    filtered.insert(0, "screen_rank", np.arange(1, len(filtered) + 1))
    return filtered, sort_label


def render_table(filtered: pd.DataFrame) -> None:
    columns = [column for column in DISPLAY_COLUMNS if column in filtered.columns]
    display = filtered[columns].rename(columns=DISPLAY_COLUMNS).copy()
    if "訊號" in display:
        display["訊號"] = display["訊號"].map(status_badge)

    numeric_formats = {
        "一個月報酬%": "%.2f",
        "三個月報酬%": "%.2f",
        "一年報酬%": "%.2f",
        "Benchmark一年報酬%": "%.2f",
        "超額報酬%": "%.2f",
        "六個月動能%": "%.2f",
        "夏普": "%.3f",
        "最大回撤%": "%.2f",
        "恢復天數": "%.0f",
        "綜合評分": "%.3f",
    }
    column_config = {
        key: st.column_config.NumberColumn(key, format=value)
        for key, value in numeric_formats.items()
        if key in display.columns
    }
    if "合庫連結" in display.columns:
        column_config["合庫連結"] = st.column_config.LinkColumn(
            "合庫連結", display_text="開啟基金頁 ↗"
        )
    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        height=min(720, 38 * (len(display) + 1) + 8),
        column_config=column_config,
    )


def render_cards(filtered: pd.DataFrame) -> None:
    if filtered.empty:
        st.info("目前沒有符合條件的基金。")
        return
    for category, category_rows in filtered.groupby("category_name", sort=False):
        st.subheader(f"{category}｜篩選結果")
        for start in range(0, len(category_rows), 3):
            cols = st.columns(3)
            for col, (_, row) in zip(cols, category_rows.iloc[start:start + 3].iterrows()):
                with col:
                    with st.container(border=True):
                        st.markdown(f"#### {int(row['screen_rank'])}. {row['name']}")
                        st.caption(f"{category}｜Benchmark：{row.get('benchmark') or '—'}")
                        m1, m2, m3, m4 = st.columns(4)
                        m1.metric("一個月", fmt_number(row.get("return_1m"), suffix="%"))
                        m2.metric("三個月", fmt_number(row.get("return_3m"), suffix="%"))
                        m3.metric("一年", fmt_number(row.get("return_1y"), suffix="%"))
                        m4.metric("夏普", fmt_number(row.get("sharpe"), 3))
                        st.markdown(f"**訊號：{status_badge(row.get('signal', '待資料'))}**")
                        st.caption(str(row.get("status") or "尚無資料狀態說明"))
                        st.caption(f"資料日期：{row.get('data_date') or '—'}｜來源：{row.get('data_source') or '—'}")
                        if row.get("tcb_url"):
                            st.link_button("🔗 開啟合庫基金頁", row["tcb_url"], use_container_width=True)


st.title("📊 全球共同基金篩選器")
st.caption("與 Grafana 全球共同基金策略平台共用相同排名資料；網頁版提供多條件交叉篩選、排序及匯出。")

if not RANKINGS_FILE.exists():
    st.error(f"找不到基金排名資料：{RANKINGS_FILE}")
    st.stop()

rankings = load_rankings(file_mtime(RANKINGS_FILE))
status = load_status(file_mtime(STATUS_FILE))
if rankings.empty:
    st.warning("基金排名資料目前是空的，請先執行資料更新程式。")
    st.stop()

placeholder_count = int(rankings["name"].eq("基金清單建置中").sum())
grafana_templates = load_grafana_templates()
configured_categories = list(grafana_templates) or rankings["category_name"].dropna().drop_duplicates().tolist()
rankings = rankings[~rankings["name"].eq("基金清單建置中")].reset_index(drop=True)

filtered, active_sort = apply_filters(rankings)
updated_at = status.get("updated_at")
if updated_at:
    try:
        updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
else:
    updated_at = datetime.fromtimestamp(file_mtime(RANKINGS_FILE)).strftime("%Y-%m-%d %H:%M:%S")

metric1, metric2, metric3, metric4 = st.columns(4)
metric1.metric("全部基金", f"{len(rankings):,}")
metric2.metric("符合條件", f"{len(filtered):,}")
metric3.metric("市場／主題數", f"{filtered['category_name'].nunique():,}" if not filtered.empty else "0")
complete_count = int(filtered[["return_1y", "momentum_6m", "sharpe"]].notna().all(axis=1).sum()) if not filtered.empty else 0
metric4.metric("主要數據完整", f"{complete_count:,}")
st.caption(f"最後資料更新：{updated_at}｜目前排序：{active_sort}｜每小時檢查、超過 24 小時自動更新")
if placeholder_count:
    st.info(f"已隱藏 {placeholder_count} 列「基金清單建置中」占位資料；它們不是基金，只代表 Grafana 分類尚未設定基金名單。")

tab_table, tab_cards, tab_portfolio, tab_flows, tab_backtest, tab_rules, tab_status = st.tabs([
    "📋 篩選排名表",
    "🗂️ 基金卡片",
    "🏭 產業與持股",
    "💧 ETF 資金流向",
    "📈 Excel 回測",
    "🧮 排名規則",
    "🛰️ 資料狀態",
])

with tab_table:
    if filtered.empty:
        st.info("目前沒有符合條件的基金，請放寬左側篩選條件。")
    else:
        render_table(filtered)
        export_columns = [column for column in DISPLAY_COLUMNS if column in filtered.columns]
        export = filtered[export_columns].rename(columns=DISPLAY_COLUMNS)
        st.download_button(
            "⬇️ 下載目前篩選結果 CSV",
            export.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"fund_screener_{datetime.now():%Y%m%d_%H%M}.csv",
            mime="text/csv",
        )

with tab_cards:
    render_cards(filtered)

with tab_portfolio:
    portfolio_data = load_portfolio_details(rankings)
    portfolio_category = st.selectbox("市場／主題分類", configured_categories, key="portfolio_category")
    template_fund_names = grafana_templates.get(portfolio_category, [])
    category_funds = rankings[rankings["category_name"].eq(portfolio_category)]
    # A fund may belong to a broad market in MoneyDJ and several thematic
    # templates in Grafana. Include those matches in every applicable template.
    if template_fund_names:
        template_matches = rankings[rankings["name"].isin(template_fund_names)]
        category_funds = pd.concat([category_funds, template_matches]).drop_duplicates("name")
    if category_funds.empty:
        st.markdown(f"**Grafana 基金名單（{len(template_fund_names)} 檔）**")
        if template_fund_names:
            st.dataframe(pd.DataFrame({"基金": template_fund_names, "資料狀態": "正在配對 MoneyDJ ID"}), use_container_width=True, hide_index=True)
        else:
            st.warning(f"{portfolio_category} 模板目前沒有基金資料列。")
    elif portfolio_data.empty:
        st.info("尚未建立持股資料，請按左側「立即更新資料」執行爬蟲。")
    else:
        category_portfolio = portfolio_data[portfolio_data["fund"].isin(category_funds["name"])]
        summary_rows = []
        for _, fund_row in category_funds.iterrows():
            fund_holdings = category_portfolio[
                category_portfolio["fund"].eq(fund_row["name"])
                & category_portfolio["kind"].eq("holding")
            ].sort_values("weight", ascending=False)
            dates = fund_holdings["data_date"].dropna().astype(str)
            names = fund_holdings["name"].dropna().astype(str).tolist()
            summary_rows.append({
                "基金": fund_row["name"],
                "是否配息": fund_row.get("distribution") or "待確認",
                "供應鏈家數": len(fund_holdings),
                "相關持股%": fund_holdings["weight"].sum() if not fund_holdings.empty else np.nan,
                "實際持股": "、".join(names[:4]) if names else "待抓取",
                "持股日期": dates.max() if not dates.empty else "—",
            })
        if summary_rows:
            st.markdown(f"**{portfolio_category}｜基金 Top 10 與相關持股**")
            st.dataframe(
                pd.DataFrame(summary_rows).head(10),
                use_container_width=True,
                hide_index=True,
                column_config={"相關持股%": st.column_config.NumberColumn(format="%.2f")},
            )
            st.caption("下方展開各基金後，每一檔實際持股都有 Yahoo 股票技術線連結。")
        available_industries = sorted(
            category_portfolio.loc[category_portfolio["kind"].eq("industry"), "name"].dropna().unique().tolist()
        )
        p1, p2 = st.columns([2, 1])
        portfolio_industries = p1.multiselect("篩選產業", available_industries, key="portfolio_industries")
        portfolio_minimum = p2.slider("產業最低比重（%）", 0, 100, 0, 5, key="portfolio_minimum")
        visible_funds = category_funds["name"].tolist()
        if portfolio_industries:
            matched = category_portfolio[
                category_portfolio["kind"].eq("industry")
                & category_portfolio["name"].isin(portfolio_industries)
            ].groupby("fund")["weight"].sum()
            visible_funds = matched[matched.ge(portfolio_minimum)].index.tolist()
        st.caption(f"符合條件：{len(visible_funds)} 檔基金")
        for fund_name in visible_funds:
            fund_portfolio = category_portfolio[category_portfolio["fund"].eq(fund_name)]
            industries = fund_portfolio[fund_portfolio["kind"].eq("industry")].sort_values("weight", ascending=False)
            holdings = fund_portfolio[fund_portfolio["kind"].eq("holding")].sort_values("weight", ascending=False)
            data_dates = fund_portfolio["data_date"].dropna().astype(str)
            data_date = data_dates.max() if not data_dates.empty else "—"
            with st.expander(f"{fund_name}｜資料日期：{data_date}", expanded=True):
                fund_link = category_funds.loc[category_funds["name"].eq(fund_name), "tcb_url"]
                if not fund_link.empty and fund_link.iloc[0]:
                    st.link_button("🔗 開啟合庫基金頁", fund_link.iloc[0])
                left, right = st.columns(2)
                with left:
                    st.markdown("**產業比重**")
                    if industries.empty:
                        st.caption("來源未公布產業配置")
                    else:
                        st.bar_chart(industries.set_index("name")[["weight"]], use_container_width=True)
                        st.dataframe(
                            industries[["name", "weight"]].rename(columns={"name": "產業", "weight": "比重%"}),
                            use_container_width=True,
                            hide_index=True,
                            column_config={"比重%": st.column_config.NumberColumn(format="%.2f")},
                        )
                with right:
                    st.markdown("**前十大個股成分**")
                    if holdings.empty:
                        st.caption("來源未公布主要持股")
                    else:
                        st.bar_chart(holdings.set_index("name")[["weight"]], use_container_width=True)
                        holdings_display = holdings[["name", "weight"]].rename(
                            columns={"name": "個股", "weight": "比重%"}
                        )
                        holdings_display["Yahoo技術線"] = holdings_display["個股"].map(yahoo_stock_url)
                        st.dataframe(
                            holdings_display,
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "比重%": st.column_config.NumberColumn(format="%.2f"),
                                "Yahoo技術線": st.column_config.LinkColumn(
                                    "Yahoo 股票技術線", display_text="查看走勢 ↗"
                                ),
                            },
                        )

with tab_flows:
    etf_flows = load_etf_flows()
    if etf_flows.empty:
        st.info("尚未建立 ETF 資金流向資料，請按左側「立即更新資料」。")
    else:
        positive_flows = etf_flows[etf_flows["net_flow_eur_m"].gt(0)].sort_values("net_flow_eur_m", ascending=False)
        data_date = str(etf_flows["data_date"].dropna().iloc[0]) if etf_flows["data_date"].notna().any() else "—"
        top_mapped = positive_flows[positive_flows["template_category"].ne("其他")]
        top_name = str(top_mapped.iloc[0]["template_category"]) if not top_mapped.empty else "—"
        top_flow = float(top_mapped.iloc[0]["net_flow_eur_m"]) if not top_mapped.empty else np.nan
        f1, f2, f3 = st.columns(3)
        f1.metric("資料月份", data_date)
        f2.metric("最高淨流入市場／產業", top_name)
        f3.metric("淨流入", fmt_number(top_flow, 0, " 百萬歐元"))
        st.caption("依公開的歐洲上市 ETF 月度類別淨流量比較；不同市場幣別、產品範圍與台灣共同基金並不完全相同。")

        chart_data = positive_flows.head(10).set_index("flow_category")[["net_flow_eur_m"]]
        st.subheader("近一個月 ETF 淨流入前十類別")
        st.bar_chart(chart_data, use_container_width=True)

        recommendations = build_flow_recommendations(etf_flows, filtered)
        st.subheader("ETF 資金流向 × MoneyDJ 挑選規則｜最多 10 檔")
        st.caption("只納入已有 MoneyDJ ID、核心指標完整，且通過左側目前篩選條件的基金；再按 ETF 淨流入及綜合評分排序。")
        if recommendations.empty:
            st.warning("目前沒有同時符合 ETF 流入類別、MoneyDJ 完整資料及左側挑選規則的基金，請調整篩選條件或等待資料補齊。")
        else:
            st.dataframe(
                recommendations,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "ETF近月淨流入（百萬歐元）": st.column_config.NumberColumn(format="%.0f"),
                    "一年報酬%": st.column_config.NumberColumn(format="%.2f"),
                    "夏普": st.column_config.NumberColumn(format="%.2f"),
                    "最大回撤%": st.column_config.NumberColumn(format="%.2f"),
                    "綜合評分": st.column_config.NumberColumn(format="%.3f"),
                    "合庫MoneyDJ": st.column_config.LinkColumn("合庫 MoneyDJ", display_text="開啟基金頁 ↗"),
                },
            )
        source_url = str(etf_flows["source_url"].dropna().iloc[0]) if etf_flows["source_url"].notna().any() else ""
        if source_url:
            st.link_button("查看 ETF 流向資料來源 ↗", source_url)
        st.warning("資金流入不代表未來報酬；此清單僅供研究篩選，不構成投資建議。")

with tab_backtest:
    st.subheader("📤 上傳 Excel／CSV 執行回測")
    uploaded_backtest = st.file_uploader(
        "選擇淨值與產業占比檔案",
        type=["xlsx", "xls", "csv"],
        key="backtest_upload",
        help="支援長格式（日期、基金、淨值）或寬格式；可另加產業配置工作表（基金、產業、占比）。",
    )
    with st.expander("查看支援的 Excel 格式"):
        st.markdown(
            """
            **淨值工作表（長格式）**：`日期｜基金｜淨值`  
            **淨值工作表（寬格式）**：第一欄為 `日期`，後續每欄是一檔基金  
            **產業配置工作表（選填）**：`基金｜產業｜占比`

            上傳後會自動計算累積／年化報酬、波動、夏普、最大回撤、恢復天數及回測評分，並產生淨值、回撤和排名圖。
            """
        )
    st.caption("檔案只在本次瀏覽工作階段使用；未上傳時仍可使用每日自動爬取的 MoneyDJ 淨值回測。")
    scraped_history = load_scraped_backtest_history(rankings)
    source_options = ["自動爬蟲資料"]
    if uploaded_backtest is not None:
        source_options.append("上傳 Excel／CSV")
    backtest_source = st.radio("回測資料來源", source_options, horizontal=True)
    if backtest_source == "自動爬蟲資料":
        history_data = scraped_history
        allocation_data = pd.DataFrame()
        upload_errors = []
        if history_data.empty:
            st.info("尚未建立逐日淨值資料，請按左側「立即更新資料」執行爬蟲。")
        else:
            st.caption(f"已載入 {history_data['fund'].nunique()} 檔基金、{len(history_data):,} 筆逐日淨值；更新時會自動重抓。")
    else:
        history_data, allocation_data, upload_errors = parse_backtest_upload(
            uploaded_backtest.getvalue(), uploaded_backtest.name
        )
    for error in upload_errors:
        st.warning(error)
    if not history_data.empty:
            selected_history = history_data
            st.subheader("產業占比篩選")
            if allocation_data.empty:
                if backtest_source == "自動爬蟲資料":
                    st.caption("公開淨值頁不含產業占比；如需產業篩選，可上傳含「基金、產業、占比」工作表的 Excel。")
                else:
                    st.caption("這份檔案沒有辨識到產業占比表，因此先對全部基金回測。")
            else:
                industry_options = sorted(allocation_data["industry"].dropna().unique().tolist())
                f1, f2 = st.columns([2, 1])
                selected_industries = f1.multiselect("指定產業", industry_options)
                minimum_weight = f2.slider("合計最低占比（%）", 0, 100, 0, 5)
                eligible_funds = set(history_data["fund"].unique())
                if selected_industries:
                    weights = (
                        allocation_data[allocation_data["industry"].isin(selected_industries)]
                        .groupby("fund")["weight"].sum()
                    )
                    eligible_funds = set(weights[weights.ge(minimum_weight / 100)].index)
                selected_history = history_data[history_data["fund"].isin(eligible_funds)]
                st.caption(f"符合產業條件：{len(eligible_funds)} 檔基金")

            ranking_result, normalized_result, drawdown_result = calculate_backtest(selected_history)
            if ranking_result.empty:
                st.warning("目前篩選條件下沒有足夠的淨值資料可回測。")
            else:
                r1, r2, r3, r4 = st.columns(4)
                r1.metric("參與排名基金", len(ranking_result))
                r2.metric("最高累積報酬", fmt_number(ranking_result["累積報酬%"].max(), suffix="%"))
                r3.metric("最佳夏普", fmt_number(ranking_result["夏普"].max(), 3))
                r4.metric("最佳回測基金", str(ranking_result.iloc[0]["基金"]))

                st.subheader("回測排名")
                st.dataframe(
                    ranking_result,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "累積報酬%": st.column_config.NumberColumn(format="%.2f"),
                        "年化報酬%": st.column_config.NumberColumn(format="%.2f"),
                        "年化波動%": st.column_config.NumberColumn(format="%.2f"),
                        "夏普": st.column_config.NumberColumn(format="%.3f"),
                        "最大回撤%": st.column_config.NumberColumn(format="%.2f"),
                        "回測評分": st.column_config.NumberColumn(format="%.3f"),
                    },
                )

                st.subheader("標準化淨值走勢（起點＝100）")
                nav_chart = normalized_result.pivot(index="日期", columns="基金", values="標準化淨值")
                st.line_chart(nav_chart, use_container_width=True)
                st.subheader("回撤走勢")
                dd_chart = drawdown_result.pivot(index="日期", columns="基金", values="回撤%")
                st.line_chart(dd_chart, use_container_width=True)
                st.subheader("年化報酬排名")
                st.bar_chart(
                    ranking_result.set_index("基金")[["年化報酬%"]],
                    use_container_width=True,
                )

                if not allocation_data.empty:
                    st.subheader("基金產業配置")
                    allocation_funds = [fund for fund in ranking_result["基金"] if fund in set(allocation_data["fund"])]
                    if allocation_funds:
                        allocation_fund = st.selectbox("查看基金", allocation_funds)
                        allocation_view = allocation_data[allocation_data["fund"].eq(allocation_fund)].copy()
                        allocation_view["占比%"] = allocation_view["weight"] * 100
                        allocation_view = allocation_view.sort_values("占比%", ascending=False)
                        st.bar_chart(allocation_view.set_index("industry")[["占比%"]], use_container_width=True)
                        st.dataframe(
                            allocation_view[["industry", "占比%"]].rename(columns={"industry": "產業"}),
                            use_container_width=True,
                            hide_index=True,
                            column_config={"占比%": st.column_config.NumberColumn(format="%.2f")},
                        )

with tab_rules:
    st.markdown(
        """
        ### 排名及訊號規則

        - 一般基金先依市場／主題分類，同基金不同幣別或級別可在資料更新階段去重。
        - 綜合評分使用：一年報酬、Benchmark 超額報酬、六個月動能、夏普及最大回撤。
        - 超額報酬、六個月動能、夏普三項皆為正：**買進**。
        - 上述三項有兩項為正：**觀察**；其餘為**賣出**。
        - 缺少必要指標時標示為**待資料**，不以零代替缺值。

        本工具僅供資料研究與基金比較，不構成投資建議。
        """
    )

with tab_status:
    st.json(status or {"updated_at": updated_at, "rows": len(rankings)})
    pending = rankings[rankings["signal"].eq("待資料")]
    st.write(f"待補資料基金：{len(pending)} 檔")
    if not pending.empty:
        st.dataframe(
            pending[["category_name", "name", "status"]].rename(
                columns={"category_name": "市場／主題", "name": "基金", "status": "待補說明"}
            ),
            use_container_width=True,
            hide_index=True,
        )


@st.fragment(run_every="1h")
def automatic_data_refresh() -> None:
    """Refresh stale provider data, then reload the table when the file changes."""
    update_data(force=False)
    current_mtime = file_mtime(RANKINGS_FILE)
    previous_mtime = st.session_state.get("rankings_mtime", current_mtime)
    st.session_state["rankings_mtime"] = current_mtime
    if current_mtime != previous_mtime:
        st.cache_data.clear()
        st.rerun()


automatic_data_refresh()
