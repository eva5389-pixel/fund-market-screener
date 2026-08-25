#!/usr/bin/env python3
"""Build ranked Grafana CSV inputs from interchangeable fund-data providers.

Only instruments with an explicit Twelve Data symbol are fetched. Missing symbols
remain visible as pending rows so the dashboard never substitutes invented data.
"""

from __future__ import annotations

import csv
import importlib
import json
import math
import os
import re
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from io import StringIO

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "funds.json"
DATA = ROOT / "data"
CATEGORY_DATA = DATA / "categories"
NAV_HISTORY = DATA / "nav_history"
PORTFOLIO_DATA = DATA / "portfolio"
ETF_FLOW_FILE = DATA / "etf_flows.csv"
METRIC_FIELDS = ("daily_change", "return_1m", "return_3m", "return_1y", "benchmark_return_1y", "excess_return_1y",
                 "momentum_6m", "momentum_acceleration", "sharpe", "max_drawdown", "recovery_days")


def load_previous_rankings() -> tuple[dict[str, dict], str]:
    """Keep the last successful values so a provider outage never blanks cards."""
    previous: dict[str, dict] = {}
    rankings_path = DATA / "rankings.csv"
    if rankings_path.exists():
        with rankings_path.open(encoding="utf-8-sig", newline="") as handle:
            for item in csv.DictReader(handle):
                key = (item.get("moneydj_id") or item.get("name") or "").strip()
                if key:
                    previous[key] = item
    previous_date = ""
    status_path = DATA / "status.json"
    if status_path.exists():
        try:
            previous_date = str(json.loads(status_path.read_text(encoding="utf-8")).get("updated_at", ""))[:10]
        except (OSError, json.JSONDecodeError):
            pass
    return previous, previous_date


def restore_previous_values(row: dict, previous: dict[str, dict], previous_date: str) -> bool:
    key = (row.get("moneydj_id") or row.get("name") or "").strip()
    old = previous.get(key)
    if not old:
        return False
    restored = False
    for field in METRIC_FIELDS:
        if row.get(field) is None and old.get(field) not in (None, ""):
            try:
                row[field] = float(old[field]) / 100 if field in {
                    "return_1m", "return_3m", "return_1y", "benchmark_return_1y", "excess_return_1y",
                    "daily_change", "momentum_6m", "momentum_acceleration", "max_drawdown",
                } else float(old[field])
                restored = True
            except (TypeError, ValueError):
                continue
    if restored:
        row["data_date"] = old.get("data_date") or previous_date or "日期待確認"
        row["data_source"] = old.get("data_source") or old.get("status") or "上次成功資料"
        row["status"] = f"沿用上次資料（{row['data_date']}）"
    return restored


def pct_change(values: list[float], periods: int) -> float | None:
    if len(values) <= periods or values[-periods - 1] == 0:
        return None
    return values[-1] / values[-periods - 1] - 1


def annualized_momentum(value: float | None, months: int) -> float | None:
    """Annualize a holding-period return for comparable 3M/6M momentum."""
    if value is None or value <= -1:
        return None
    return (1 + value) ** (12 / months) - 1


def momentum_acceleration(return_3m: float | None, return_6m: float | None) -> float | None:
    annualized_3m = annualized_momentum(return_3m, 3)
    annualized_6m = annualized_momentum(return_6m, 6)
    if annualized_3m is None or annualized_6m is None:
        return None
    return annualized_3m - annualized_6m


def daily_returns(values: list[float]) -> list[float]:
    return [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1]]


def sharpe(values: list[float], risk_free_rate: float) -> float | None:
    returns = daily_returns(values)
    if len(returns) < 30:
        return None
    volatility = statistics.stdev(returns)
    if volatility == 0:
        return None
    return (statistics.mean(returns) * 252 - risk_free_rate) / (volatility * math.sqrt(252))


def max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def recovery_days(values: list[float]) -> int | None:
    if not values:
        return None
    peak_index = 0
    trough_index = 0
    peak = values[0]
    worst = 0.0
    for i, value in enumerate(values):
        if value > peak:
            peak, peak_index = value, i
        drawdown = value / peak - 1
        if drawdown < worst:
            worst, trough_index = drawdown, i
    target = max(values[: trough_index + 1])
    for i in range(trough_index + 1, len(values)):
        if values[i] >= target:
            return i - trough_index
    return None


def api_json(endpoint: str, api_key: str, **params: str | int) -> dict:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"https://api.twelvedata.com/{endpoint}?{query}",
        headers={"Authorization": f"apikey {api_key}", "User-Agent": "grafana-fund-dashboard/1.0"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            if payload.get("status") == "error":
                raise RuntimeError(payload.get("message", "Twelve Data error"))
            return payload
        except (urllib.error.URLError, RuntimeError) as exc:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def fetch_closes(symbol: str, api_key: str) -> list[float]:
    payload = api_json("time_series", api_key, symbol=symbol, interval="1day", outputsize=800)
    rows = payload.get("values", [])
    return [float(row["close"]) for row in reversed(rows) if row.get("close")]


def fetch_yahoo_direct_closes(symbol: str) -> list[float]:
    """Fetch Yahoo chart history without guessing symbols or requiring login."""
    now = int(datetime.now(timezone.utc).timestamp())
    start = now - 800 * 24 * 60 * 60
    encoded_symbol = urllib.parse.quote(symbol, safe="")
    query = urllib.parse.urlencode({
        "period1": start,
        "period2": now,
        "interval": "1d",
        "events": "history",
    })
    request = urllib.request.Request(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}?{query}",
        headers={"User-Agent": "Mozilla/5.0 (fund-screener; contact: local-app)"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    results = payload.get("chart", {}).get("result") or []
    if not results:
        raise RuntimeError(f"No Yahoo chart result for {symbol}")
    closes = (results[0].get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")
    if not closes:
        closes = (results[0].get("indicators", {}).get("quote") or [{}])[0].get("close", [])
    values = [float(value) for value in closes if value is not None]
    if len(values) < 2:
        raise RuntimeError(f"No usable Yahoo history for {symbol}")
    return values


def fetch_moneydj_html(url: str, encoding: str = "big5") -> str:
    """Read a public MoneyDJ partner page (this host has a legacy TLS chain)."""
    allowed_host = "tcbbankfund.moneydj.com"
    if urllib.parse.urlparse(url).hostname != allowed_host:
        raise ValueError("Unexpected scraper host")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; local-fund-screener/1.0)",
        "Accept-Language": "zh-TW,zh;q=0.9",
    })
    with urllib.request.urlopen(request, context=context, timeout=30) as response:
        return response.read().decode(encoding, errors="ignore")


def _flat_columns(frame) -> dict[str, object]:
    result = {}
    for column in frame.columns:
        parts = column if isinstance(column, tuple) else (column,)
        label = " ".join(str(part) for part in parts if "Unnamed" not in str(part)).strip()
        result[label] = column
    return result


def fetch_moneydj_metrics(identifier: str) -> dict[str, object]:
    """Scrape published return, momentum and Sharpe without using an API."""
    pd = importlib.import_module("pandas")
    section, page = ("wr", "wr03") if identifier.upper().startswith("AC") else ("wb", "wb902")
    url = f"https://tcbbankfund.moneydj.com/w/{section}/{page}.djhtm?{urllib.parse.urlencode({'a': identifier})}"
    tables = pd.read_html(StringIO(fetch_moneydj_html(url)))
    result: dict[str, object] = {"data_source": "合庫 MoneyDJ 公開網頁（爬蟲）"}
    for frame in tables:
        columns = _flat_columns(frame)
        month = next((value for label, value in columns.items() if "一個月" in label or "1個月" in label), None)
        three = next((value for label, value in columns.items() if "三個月" in label or "3個月" in label), None)
        six = next((value for label, value in columns.items() if "六個月" in label), None)
        year = next((value for label, value in columns.items() if label.endswith("一年") or "一年(" in label), None)
        if six is not None and year is not None and not frame.empty:
            if month is not None:
                result["return_1m"] = float(frame.iloc[0][month]) / 100
            if three is not None:
                result["return_3m"] = float(frame.iloc[0][three]) / 100
            result["momentum_6m"] = float(frame.iloc[0][six]) / 100
            result["return_1y"] = float(frame.iloc[0][year]) / 100
            if len(frame) > 1:
                try:
                    result["benchmark_return_1y"] = float(frame.iloc[1][year]) / 100
                except (TypeError, ValueError):
                    pass
        sharpe_col = next((value for label, value in columns.items() if "Sharpe" in label), None)
        date_col = next((value for label, value in columns.items() if "淨值日期" in label), None)
        if sharpe_col is not None and not frame.empty:
            try:
                result["sharpe"] = float(frame.iloc[0][sharpe_col])
            except (TypeError, ValueError):
                pass
        if date_col is not None and not frame.empty:
            try:
                result["data_date"] = pd.to_datetime(frame.iloc[0][date_col]).date().isoformat()
            except (TypeError, ValueError):
                pass
    if "return_1y" not in result:
        raise RuntimeError(f"No published MoneyDJ performance for {identifier}")
    return result


def fetch_moneydj_nav_history(identifier: str) -> tuple[list[str], list[float]]:
    """Follow the public chart data URL and return roughly one year of daily NAV."""
    section = "wr" if identifier.upper().startswith("AC") else "wb"
    page_url = f"https://tcbbankfund.moneydj.com/w/{section}/{section}02.djhtm?{urllib.parse.urlencode({'a': identifier})}"
    page = fetch_moneydj_html(page_url)
    match = re.search(r"'BCDUrl':\s*'([^']+)'", page)
    if not match:
        raise RuntimeError(f"No MoneyDJ chart URL for {identifier}")
    chart_url = urllib.parse.urljoin("https://tcbbankfund.moneydj.com", match.group(1))
    payload = fetch_moneydj_html(chart_url)
    parts = payload.split()
    if len(parts) < 2:
        raise RuntimeError(f"No MoneyDJ NAV series for {identifier}")
    dates = [value for value in parts[0].split(",") if value]
    values = [float(value) for value in parts[1].split(",") if value]
    length = min(len(dates), len(values))
    if length < 30:
        raise RuntimeError(f"Insufficient MoneyDJ NAV series for {identifier}")
    return dates[-length:], values[-length:]


def save_nav_history(identifier: str, dates: list[str], values: list[float]) -> None:
    NAV_HISTORY.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", identifier)
    with (NAV_HISTORY / f"{safe_name}.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "nav"])
        writer.writerows(zip(dates, values))


def fetch_moneydj_portfolio(identifier: str) -> tuple[list[dict], list[dict]]:
    """Scrape sector allocation and top holdings from the public holdings page."""
    pd = importlib.import_module("pandas")
    section = "wr" if identifier.upper().startswith("AC") else "wb"
    page_url = f"https://tcbbankfund.moneydj.com/w/{section}/{section}04.djhtm?{urllib.parse.urlencode({'a': identifier})}"
    page = fetch_moneydj_html(page_url)
    fund_code = identifier.split("-", 1)[0]
    tag = "wr04p3" if section == "wr" else "wb04p2"
    industry_url = "https://tcbbankfund.moneydj.com/jsondata/djjson/fundjsondata.xdjjson?" + urllib.parse.urlencode({"x": tag, "a": fund_code})
    payload = json.loads(fetch_moneydj_html(industry_url, encoding="utf-8"))
    industries = []
    for item in payload.get("ResultSet", {}).get("Result", []):
        name = str(item.get("V2", "")).strip()
        if not name or name == "合計":
            continue
        industries.append({"kind": "industry", "name": name, "weight": float(item["V3"]), "data_date": item.get("V1", "")})

    holding_candidates = []
    for frame in pd.read_html(StringIO(page)):
        name_col = next((column for column in frame.columns if str(column) in {"股票名稱", "持股名稱"}), None)
        weight_col = next((column for column in frame.columns if str(column) == "比例"), None)
        if name_col is None or weight_col is None:
            continue
        current = []
        for _, item in frame.iterrows():
            name = str(item[name_col]).strip()
            try:
                weight = float(str(item[weight_col]).replace("%", "").strip())
            except ValueError:
                continue
            if name and name.lower() != "nan":
                current.append({"kind": "holding", "name": name, "weight": weight, "data_date": industries[0]["data_date"] if industries else ""})
        if current:
            holding_candidates.append(current)
    holdings = max(holding_candidates, key=len, default=[])
    return industries, holdings[:10]


def save_portfolio(identifier: str, industries: list[dict], holdings: list[dict]) -> None:
    PORTFOLIO_DATA.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", identifier)
    with (PORTFOLIO_DATA / f"{safe_name}.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["kind", "name", "weight", "data_date"])
        writer.writeheader()
        writer.writerows(industries + holdings)


def update_etf_flows() -> int:
    """Scrape the latest published one-month ETF category flow table."""
    pd = importlib.import_module("pandas")
    source_url = "https://moneyweek.com/investments/etfs/etf-sectors-fund-flows"
    request = urllib.request.Request(source_url, headers={"User-Agent": "Mozilla/5.0 (local-fund-screener/1.0)"})
    with urllib.request.urlopen(request, timeout=45) as response:
        html = response.read().decode("utf-8", errors="ignore")
    tables = pd.read_html(StringIO(html))
    if not tables:
        raise RuntimeError("No ETF flow table")
    table = tables[0]
    date_match = re.search(r"Data as of\s+([0-9]{1,2}\s+[A-Za-z]+\s+20[0-9]{2})", html, re.I)
    data_date = date_match.group(1) if date_match else date.today().isoformat()
    mapping = {
        "US large cap blend equity": "美國",
        "US large cap growth equity": "美國",
        "Japan large cap blend equity": "日本",
        "Europe large cap blend equity": "歐洲",
        "Sector equity financial services": "金融",
        "Sector equity technology": "科技主題",
        "Global emerging markets equity": "新興市場",
        "Brazil equity": "巴西",
        "China equity": "中國",
        "China equity – A shares": "中國",
        "Asia ex-Japan equity": "東協",
    }
    records = []
    for category_col, flow_col, direction in [
        (table.columns[0], table.columns[1], "流入"),
        (table.columns[2], table.columns[3], "流出"),
    ]:
        for _, item in table.iterrows():
            label = str(item[category_col]).strip()
            try:
                flow = float(item[flow_col])
            except (TypeError, ValueError):
                continue
            records.append({
                "flow_category": label,
                "template_category": mapping.get(label, "其他"),
                "net_flow_eur_m": flow,
                "direction": direction,
                "data_date": data_date,
                "source_url": source_url,
            })
    DATA.mkdir(exist_ok=True)
    pd.DataFrame(records).to_csv(ETF_FLOW_FILE, index=False, encoding="utf-8-sig")
    return len(records)


def fetch_apify_closes(symbol: str, token: str) -> list[float]:
    """Fetch one year of daily Yahoo Finance history through Apify."""
    endpoint = "https://api.apify.com/v2/acts/canadesk~yahoo-finance/run-sync-get-dataset-items"
    request = urllib.request.Request(
        f"{endpoint}?{urllib.parse.urlencode({'token': token})}",
        data=json.dumps({
            "tickers": [symbol],
            "period": "1y",
            "interval": "1d",
            "process": "gh",
            "storecsv": "no",
            "proxy": {"useApifyProxy": True},
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "grafana-fund-dashboard/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = json.load(response)
    records = payload if isinstance(payload, list) else [payload]
    points = next((item.get("data", []) for item in records if item.get("ticker") == symbol), [])
    dated = sorted(
        ((point.get("Date") or point.get("date"), point.get("close")) for point in points),
        key=lambda item: item[0] or "",
    )
    values = [float(close) for _, close in dated if close not in (None, "")]
    if len(values) < 2:
        raise RuntimeError(f"No usable Apify history for {symbol}")
    return values


def fetch_market_closes(symbol: str, twelve_key: str, apify_token: str) -> tuple[list[float], str]:
    errors = []
    if twelve_key:
        try:
            return fetch_closes(symbol, twelve_key), "Twelve Data"
        except Exception as exc:
            errors.append(f"Twelve Data: {type(exc).__name__}")
    try:
        return fetch_yahoo_direct_closes(symbol), "Yahoo Finance"
    except Exception as exc:
        errors.append(f"Yahoo Finance: {type(exc).__name__}")
    if apify_token:
        try:
            return fetch_apify_closes(symbol, apify_token), "Apify/Yahoo"
        except Exception as exc:
            errors.append(f"Apify: {type(exc).__name__}")
    raise RuntimeError("; ".join(errors) or "No market-data credential")


def yahoo_symbol_candidates(identifier: str) -> list[str]:
    """Return stable Yahoo fund symbol variants without duplicate requests."""
    value = identifier.strip()
    if not value:
        return []
    candidates = [value]
    if value.upper().endswith(":FO"):
        candidates.append(value[:-3])
    return list(dict.fromkeys(candidates))


def fetch_yahoo_fund_closes(identifier: str, apify_token: str) -> tuple[list[float], str]:
    """Try an explicit Yahoo fund id directly, then through Apify."""
    errors = []
    for candidate in yahoo_symbol_candidates(identifier):
        try:
            return fetch_yahoo_direct_closes(candidate), f"Yahoo Finance {candidate}"
        except Exception as exc:
            errors.append(f"Yahoo {candidate}: {type(exc).__name__}")
        if not apify_token:
            continue
        try:
            return fetch_apify_closes(candidate, apify_token), f"Apify/Yahoo {candidate}"
        except Exception as exc:
            errors.append(f"{candidate}: {type(exc).__name__}")
    raise RuntimeError("; ".join(errors) or "No Yahoo fund id")


def fetch_mstarpy_nav(identifier: str, session=None) -> tuple[list[float], object]:
    """Return Morningstar NAV history through optional MIT-licensed mstarpy.

    The provider is opt-in because current mstarpy versions launch Chrome.  A
    caller-owned session is returned and reused to avoid one browser per fund.
    """
    module = importlib.import_module("mstarpy")
    session = session or module.MorningstarSession()
    fund = module.Funds(identifier, session=session)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=800)
    points = fund.nav(start, end) or []
    dated = sorted(
        ((point.get("date"), point.get("nav")) for point in points),
        key=lambda item: item[0] or "",
    )
    values = [float(nav) for _, nav in dated if nav not in (None, "")]
    if len(values) < 2:
        raise RuntimeError(f"No usable Morningstar NAV for {identifier}")
    return values, session


def quantstats_metrics(values: list[float], risk_free_rate: float) -> dict[str, float | None]:
    """Optionally cross-check Sharpe and drawdown with Apache-2 QuantStats."""
    try:
        qs = importlib.import_module("quantstats")
        pd = importlib.import_module("pandas")
    except ImportError:
        return {}
    series = pd.Series(daily_returns(values))
    result = {
        "sharpe": float(qs.stats.sharpe(series, rf=risk_free_rate, periods=252)),
        "max_drawdown": float(qs.stats.max_drawdown(series)),
    }
    return {key: value for key, value in result.items() if math.isfinite(value)}


def fmt(value: float | int | None, percent: bool = False) -> str:
    if value is None:
        return ""
    return f"{value * 100:.2f}" if percent else f"{value:.3f}"


def signal(row: dict) -> str:
    metrics = [row.get("excess_return_1y"), row.get("momentum_6m"), row.get("sharpe")]
    if any(value is None for value in metrics):
        return "待資料"
    votes = sum((metrics[0] > 0, metrics[1] > 0, metrics[2] > 0))
    return "買進" if votes == 3 else "觀察" if votes == 2 else "賣出"


def score(row: dict) -> float | None:
    """Composite score is published only when every required metric exists."""
    required = ("return_1y", "excess_return_1y", "momentum_6m", "sharpe", "max_drawdown")
    if any(row.get(key) is None for key in required):
        return None
    return (float(row["return_1y"]) + float(row["excess_return_1y"])
            + float(row["momentum_6m"]) + float(row["sharpe"]) / 5
            + float(row["max_drawdown"]) / 2)


def ranking_score(row: dict) -> float:
    def present(key: str, missing: float) -> float:
        value = row.get(key)
        return missing if value is None else float(value)
    values = (present("return_1y", -9), present("excess_return_1y", -9),
              present("momentum_6m", -9), present("sharpe", -9) / 5,
              present("max_drawdown", -1) / 2)
    return sum(values)


def main() -> int:
    api_key = os.environ.get("TWELVE_DATA_API_KEY", "")
    apify_token = os.environ.get("APIFY_API_TOKEN", "")
    allow_pending = os.environ.get("ALLOW_PENDING", "0") == "1"
    if not api_key and not apify_token and not allow_pending:
        print("TWELVE_DATA_API_KEY or APIFY_API_TOKEN is required", file=sys.stderr)
        return 2

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    previous_rows, previous_date = load_previous_rankings()
    categories = {item["id"]: item for item in config["categories"]}
    benchmark_cache: dict[str, list[float]] = {}
    mstarpy_session = None
    enable_mstarpy = os.environ.get("ENABLE_MSTARPY", "0") == "1"
    rows: list[dict] = []

    for fund in config["funds"]:
        category = categories[fund["category"]]
        symbol = fund.get("twelve_data_symbol", "").strip()
        yahoo_fund_id = fund.get("yahoo_fund_id", "").strip()
        morningstar_id = fund.get("morningstar_id", "").strip()
        moneydj_id = fund.get("moneydj_id", "").strip()
        row = {**fund, "category_name": category["name"], "benchmark": category["benchmark_symbol"]}
        fund_values = None
        provider = None
        if morningstar_id and enable_mstarpy:
            try:
                fund_values, mstarpy_session = fetch_mstarpy_nav(morningstar_id, mstarpy_session)
                provider = "Morningstar/MStarpy"
            except Exception as exc:
                row["status"] = f"Morningstar錯誤: {type(exc).__name__}"
        # Yahoo 的公開 chart endpoint 不需要金鑰；即使沒有 Twelve Data／Apify，
        # 仍可爬取明確設定的 ETF 代號，避免分類只留下「待配對」占位資料。
        if fund_values is None and symbol:
            try:
                fund_values, provider = fetch_market_closes(symbol, api_key, apify_token)
            except Exception as exc:
                row["status"] = f"市場API錯誤: {type(exc).__name__}"
        if fund_values is None and yahoo_fund_id:
            try:
                fund_values, provider = fetch_yahoo_fund_closes(yahoo_fund_id, apify_token)
            except Exception as exc:
                row["status"] = f"Yahoo基金錯誤: {type(exc).__name__}"
        if fund_values is None and moneydj_id:
            try:
                row.update(fetch_moneydj_metrics(moneydj_id))
                nav_dates, nav_values = fetch_moneydj_nav_history(moneydj_id)
                save_nav_history(moneydj_id, nav_dates, nav_values)
                industries, holdings = fetch_moneydj_portfolio(moneydj_id)
                save_portfolio(moneydj_id, industries, holdings)
                risk_metrics = quantstats_metrics(nav_values, config["risk_free_rate"])
                row["sharpe"] = risk_metrics.get("sharpe", sharpe(nav_values, config["risk_free_rate"]))
                row["max_drawdown"] = risk_metrics.get("max_drawdown", max_drawdown(nav_values))
                row["recovery_days"] = recovery_days(nav_values)
                row["return_1m"] = pct_change(nav_values, min(21, len(nav_values) - 1))
                row["return_3m"] = pct_change(nav_values, min(63, len(nav_values) - 1))
                row["momentum_6m"] = pct_change(nav_values, min(126, len(nav_values) - 1))
                row["daily_change"] = pct_change(nav_values, 1)
                row["momentum_acceleration"] = momentum_acceleration(row["return_3m"], row["momentum_6m"])
                row["data_date"] = datetime.strptime(nav_dates[-1], "%Y%m%d").date().isoformat()
                row["status"] = "已更新（合庫 MoneyDJ 網頁爬蟲）"
            except Exception as exc:
                row["status"] = f"網頁爬蟲暫無資料: {type(exc).__name__}"
        if fund_values is not None:
            try:
                benchmark_symbol = category["benchmark_symbol"]
                if benchmark_symbol not in benchmark_cache:
                    benchmark_cache[benchmark_symbol], _ = fetch_market_closes(benchmark_symbol, api_key, apify_token)
                benchmark_values = benchmark_cache[benchmark_symbol]
                length = min(len(fund_values), len(benchmark_values))
                fund_values, benchmark_values = fund_values[-length:], benchmark_values[-length:]
                risk_metrics = quantstats_metrics(fund_values[-252:], config["risk_free_rate"])
                row.update({
                    "daily_change": pct_change(fund_values, 1),
                    "return_1m": pct_change(fund_values, min(21, length - 1)),
                    "return_3m": pct_change(fund_values, min(63, length - 1)),
                    "return_1y": pct_change(fund_values, min(252, length - 1)),
                    "benchmark_return_1y": pct_change(benchmark_values, min(252, length - 1)),
                    "momentum_6m": pct_change(fund_values, min(126, length - 1)),
                    "sharpe": risk_metrics.get("sharpe", sharpe(fund_values[-252:], config["risk_free_rate"])),
                    "max_drawdown": risk_metrics.get("max_drawdown", max_drawdown(fund_values)),
                    "recovery_days": recovery_days(fund_values),
                    "status": f"已更新（{provider}）",
                    "data_source": provider,
                    "data_date": date.today().isoformat(),
                })
                row["momentum_acceleration"] = momentum_acceleration(row["return_3m"], row["momentum_6m"])
                if row["return_1y"] is not None and row["benchmark_return_1y"] is not None:
                    row["excess_return_1y"] = row["return_1y"] - row["benchmark_return_1y"]
            except Exception as exc:  # keep one bad instrument from blocking every category
                row["status"] = f"指標錯誤: {type(exc).__name__}"
        elif row.get("return_1y") is not None:
            try:
                if row.get("benchmark_return_1y") is None:
                    benchmark_symbol = category["benchmark_symbol"]
                    if benchmark_symbol not in benchmark_cache:
                        benchmark_cache[benchmark_symbol], _ = fetch_market_closes(benchmark_symbol, api_key, apify_token)
                    benchmark_values = benchmark_cache[benchmark_symbol]
                    row["benchmark_return_1y"] = pct_change(benchmark_values, min(252, len(benchmark_values) - 1))
                if row.get("benchmark_return_1y") is not None:
                    row["excess_return_1y"] = row["return_1y"] - row["benchmark_return_1y"]
            except Exception:
                pass
        else:
            if fund.get("seed_return_1y") is not None:
                row["return_1y"] = fund["seed_return_1y"]
                row["status"] = "MoneyDJ報酬；其餘待API"
                row["data_source"] = "MoneyDJ既有資料"
                row["data_date"] = previous_date or "日期待確認"
            else:
                row["status"] = "待填Morningstar或市場代碼"
        restore_previous_values(row, previous_rows, previous_date)
        row.setdefault("data_source", "尚無可用來源")
        row.setdefault("data_date", previous_date or "日期待確認")
        row["signal"] = signal(row)
        row["score"] = score(row)
        rows.append(row)

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["category"]].append(row)
    for category_id, category in categories.items():
        if not grouped[category_id]:
            grouped[category_id].append({
                "category": category_id,
                "category_name": category["name"],
                "name": "基金清單建置中",
                "moneydj_id": "",
                "twelve_data_symbol": "",
                "benchmark": category["benchmark_symbol"],
                "signal": "待資料",
                "status": "待建立基金清單",
                "data_source": "尚無可用來源",
                "data_date": previous_date or date.today().isoformat(),
                "score": None,
            })
    for category_rows in grouped.values():
        category_rows.sort(key=ranking_score, reverse=True)
        for rank, row in enumerate(category_rows[:10], 1):
            row["rank"] = rank

    DATA.mkdir(exist_ok=True)
    CATEGORY_DATA.mkdir(parents=True, exist_ok=True)
    fields = ["rank", "category_name", "name", "moneydj_id", "twelve_data_symbol", "benchmark", "distribution", "daily_change", "return_1m", "return_3m", "momentum_6m", "return_1y", "momentum_acceleration", "benchmark_return_1y", "excess_return_1y", "sharpe", "max_drawdown", "recovery_days", "score", "signal", "status", "data_source", "data_date"]

    def write_csv(path: Path, selected: list[dict]) -> None:
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for item in selected:
                output = {key: item.get(key, "") for key in fields}
                for key in ("daily_change", "return_1m", "return_3m", "return_1y", "benchmark_return_1y", "excess_return_1y", "momentum_6m", "momentum_acceleration", "max_drawdown"):
                    output[key] = fmt(item.get(key), percent=True)
                output["sharpe"] = fmt(item.get("sharpe"))
                output["score"] = fmt(item.get("score"))
                writer.writerow(output)

    ranked = [row for key in categories for row in grouped.get(key, [])[:10]]
    write_csv(DATA / "rankings.csv", ranked)
    for category_id in categories:
        write_csv(CATEGORY_DATA / f"{category_id}.csv", grouped.get(category_id, [])[:10])

    try:
        etf_flow_rows = update_etf_flows()
        etf_flow_status = "updated"
    except Exception as exc:
        etf_flow_rows = 0
        etf_flow_status = f"preserved: {type(exc).__name__}"

    metadata = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "rows": len(ranked),
        "verified_rows": sum(str(r.get("status", "")).startswith("已更新") for r in ranked),
        "etf_flow_rows": etf_flow_rows,
        "etf_flow_status": etf_flow_status,
    }
    (DATA / "status.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    today = date.today()
    if os.environ.get("MONTH_END") == "1" or (today + timedelta(days=1)).month != today.month:
        snapshots = DATA / "monthly"
        snapshots.mkdir(exist_ok=True)
        write_csv(snapshots / f"{today:%Y-%m}.csv", ranked)
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
