"""Practicum stock portfolio tracker (Streamlit)."""

from __future__ import annotations

import os
import sqlite3
import threading
import time as time_module
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st


APP_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("PORTFOLIO_DB", APP_DIR / "portfolio.db"))
NY = ZoneInfo("America/New_York")
TEAMS = [f"{g}-{i}" for g in ("A", "B") for i in range(1, 6)] + ["Prof Balch"]
GROUPS = ["Group A", "Group B", "Reference"]
DEFAULT_COLUMNS = ["group_name", "team", "ticker", "side", "shares", "entry_price", "entry_date"]
STARTING_POSITIONS_PATH = APP_DIR / "starting_positions.csv"
YAHOO_SYMBOLS = {"BF.B": "BF-B", "RHM": "RHM.DE", "SOX": "^SOX", "VW": "VWAGY"}
EUR_TICKERS = {"RHM"}
REFRESH_LOCK = threading.Lock()

st.set_page_config(page_title="EagleTrack - Portfolio Tracker", layout="wide")
st.markdown(
    """<style>
    [data-testid="stCaptionContainer"] {
        color: #454A3B !important;
        font-size: 1rem !important;
        line-height: 1.55 !important;
    }
    [data-testid="stCaptionContainer"] p {
        color: inherit !important;
        font-size: inherit !important;
        line-height: inherit !important;
    }
    </style>""",
    unsafe_allow_html=True,
)


def style_holdings_table(view: pd.DataFrame) -> pd.io.formats.style.Styler:
    """Use higher-contrast zebra rows and a clear header for holdings tables."""
    def row_style(row: pd.Series) -> list[str]:
        background = "#F8F6F0" if int(row.name) % 2 == 0 else "#E9E5D9"
        return [
            f"background-color: {background}; color: #292D24; border-bottom: 1px solid #C8C4B7"
            for _ in row
        ]

    return view.style.apply(row_style, axis=1).set_table_styles([
        {"selector": "th", "props": [
            ("background-color", "#DCD8C9"),
            ("color", "#292D24"),
            ("font-weight", "700"),
            ("border-bottom", "2px solid #B8B4A5"),
        ]},
    ], overwrite=False)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    with connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT NOT NULL, team TEXT NOT NULL, ticker TEXT NOT NULL,
            side TEXT NOT NULL, shares REAL NOT NULL, entry_price REAL NOT NULL,
            entry_date TEXT NOT NULL
        )""")
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("""CREATE TABLE IF NOT EXISTS price_snapshots (
            ticker TEXT NOT NULL, quote_date TEXT NOT NULL, price_usd REAL NOT NULL,
            fetched_at TEXT NOT NULL, PRIMARY KEY (ticker, quote_date))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            as_of TEXT NOT NULL, group_name TEXT NOT NULL, team TEXT NOT NULL,
            equity REAL NOT NULL, pnl REAL NOT NULL)""")
        # Seed a fresh local/cloud database once from the version-controlled
        # starting file. The flag prevents intentionally cleared positions from
        # being silently restored on every Streamlit rerun.
        seed_marker = conn.execute(
            "SELECT value FROM settings WHERE key='starting_positions_seeded'"
        ).fetchone()
        if seed_marker is None:
            position_count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
            if position_count == 0 and STARTING_POSITIONS_PATH.exists():
                starting_positions = pd.read_csv(STARTING_POSITIONS_PATH)
                missing_columns = set(DEFAULT_COLUMNS) - set(starting_positions.columns)
                if missing_columns:
                    raise ValueError(
                        "starting_positions.csv is missing columns: "
                        + ", ".join(sorted(missing_columns))
                    )
                if not starting_positions.empty:
                    starting_positions[DEFAULT_COLUMNS].to_sql(
                        "positions", conn, if_exists="append", index=False
                    )
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("starting_positions_seeded", "1"),
            )


def get_positions() -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query("SELECT * FROM positions ORDER BY group_name, team, ticker", conn)


def save_positions(df: pd.DataFrame) -> None:
    cleaned = df[DEFAULT_COLUMNS].copy()
    cleaned["ticker"] = cleaned["ticker"].astype(str).str.strip().str.upper()
    cleaned["side"] = cleaned["side"].astype(str).str.strip().str.title()
    cleaned["entry_date"] = pd.to_datetime(cleaned["entry_date"]).dt.strftime("%Y-%m-%d")
    if (cleaned["ticker"] == "").any() or not cleaned["side"].isin(["Long", "Short"]).all():
        raise ValueError("Each row needs a ticker and side must be Long or Short.")
    if (cleaned[["shares", "entry_price"]] <= 0).any().any():
        raise ValueError("Shares and entry price must be greater than zero.")
    if not cleaned["group_name"].isin(GROUPS).all() or not cleaned["team"].isin(TEAMS).all():
        raise ValueError("Choose a valid group and team for every row.")
    with connect() as conn:
        conn.execute("DELETE FROM positions")
        cleaned.to_sql("positions", conn, if_exists="append", index=False)


ENTRY_DATE = "2026-10-27"


def import_class_allocations(prices: dict[str, float]) -> pd.DataFrame:
    allocations = pd.read_csv(APP_DIR / "allocations.csv")
    # Teams with two portfolios split their $20,000 equally; Prof Balch is a separate reference.
    counts_by_team = allocations.groupby("team")["portfolio"].nunique().to_dict()
    rows = []
    for item in allocations.itertuples(index=False):
        price = prices.get(item.ticker)
        if not price or item.allocation_pct == 0:
            continue
        capital = 20_000 / counts_by_team[item.team]
        notional = capital * abs(item.allocation_pct) / 100
        rows.append({"group_name": item.group_name, "team": item.team, "ticker": item.ticker,
                     "side": "Long" if item.allocation_pct > 0 else "Short",
                     "shares": notional / price, "entry_price": price, "entry_date": ENTRY_DATE})
    return pd.DataFrame(rows, columns=DEFAULT_COLUMNS)


def _close_table(data: pd.DataFrame) -> pd.DataFrame:
    if data is None or data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        close_level = next((level for level in range(data.columns.nlevels)
                            if "Close" in data.columns.get_level_values(level)), None)
        return data.xs("Close", axis=1, level=close_level) if close_level is not None else pd.DataFrame()
    return data[["Close"]].rename(columns={"Close": data.columns.name or "Close"}) if "Close" in data.columns else pd.DataFrame()


def download_daily_closes(tickers: list[str], start: str | None = None,
                          end: str | None = None) -> dict[str, pd.Series]:
    """Download yfinance adjusted daily closes, keyed by the class spreadsheet ticker."""
    import yfinance as yf

    yahoo = {ticker: YAHOO_SYMBOLS.get(ticker, ticker) for ticker in tickers}
    fx_needed = any(t in EUR_TICKERS for t in tickers)
    symbols = list(dict.fromkeys([*yahoo.values(), *( ["EURUSD=X"] if fx_needed else [] )]))
    kwargs = {"tickers": symbols, "interval": "1d", "auto_adjust": True,
              "progress": False, "group_by": "ticker", "threads": False}
    if start and end:
        kwargs.update(start=start, end=end)
    else:
        kwargs["period"] = "10d"
    closes = _close_table(yf.download(**kwargs))
    result: dict[str, pd.Series] = {}
    for ticker, symbol in yahoo.items():
        try:
            result[ticker] = closes[symbol].dropna() if isinstance(closes, pd.DataFrame) else closes.dropna()
        except (KeyError, TypeError):
            result[ticker] = pd.Series(dtype=float)
    if fx_needed:
        try:
            result["__EURUSD__"] = closes["EURUSD=X"].dropna()
        except (KeyError, TypeError):
            result["__EURUSD__"] = pd.Series(dtype=float)
    return result


def usd_price(ticker: str, quote_day: date, value: float,
              closes: dict[str, pd.Series]) -> float | None:
    if ticker not in EUR_TICKERS:
        return value
    fx = closes.get("__EURUSD__", pd.Series(dtype=float))
    for index, rate in fx.items():
        fx_day = pd.Timestamp(index).date()
        if fx_day == quote_day:
            return value * float(rate)
    prior = [(pd.Timestamp(index).date(), float(rate)) for index, rate in fx.items()
             if pd.Timestamp(index).date() <= quote_day]
    return value * prior[-1][1] if prior else None


def get_price_data(tickers: list[str], as_of: str | None = None) -> tuple[dict[str, float], str]:
    """Fetch adjusted closes, selecting the exact date or the last completed session."""
    try:
        now_day = datetime.now(NY).date()
        start = as_of or (now_day - timedelta(days=12)).isoformat()
        closes = download_daily_closes(tickers, start=start, end=(now_day + timedelta(days=1)).isoformat())
        prices: dict[str, float] = {}
        for ticker in tickers:
            series = closes.get(ticker, pd.Series(dtype=float))
            valid = []
            for idx, val in series.items():
                quote_day = pd.Timestamp(idx).date()
                if as_of:
                    if quote_day == pd.Timestamp(as_of).date():
                        valid.append((quote_day, float(val)))
                elif quote_day < now_day:
                    valid.append((quote_day, float(val)))
            if valid:
                quote_day, value = valid[-1]
                converted = usd_price(ticker, quote_day, value, closes)
                if converted is not None:
                    prices[ticker] = converted
        return prices, as_of or "Last completed market session"
    except Exception as exc:
        return {}, f"Unavailable: {exc}"


def get_saved_prices(tickers: list[str]) -> tuple[dict[str, float], dict[str, str], str, str]:
    if not tickers:
        return {}, {}, "No positions", "Not refreshed"
    marks, quote_dates = {}, {}
    with connect() as conn:
        rows = conn.execute("""SELECT p.ticker, p.quote_date, p.price_usd FROM price_snapshots p
            JOIN (SELECT ticker, MAX(quote_date) AS quote_date FROM price_snapshots GROUP BY ticker) latest
            ON p.ticker=latest.ticker AND p.quote_date=latest.quote_date""").fetchall()
        settings = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    for ticker, quote_date, price in rows:
        if ticker in tickers:
            marks[ticker] = float(price)
            quote_dates[ticker] = quote_date
    return marks, quote_dates, settings.get("last_refresh_date", "Unknown date"), settings.get("last_refresh_status", "No refresh yet")


def refresh_daily_prices(force: bool = False) -> tuple[int, str]:
    """Persist the latest completed daily close once each New York morning."""
    with REFRESH_LOCK:
        today = datetime.now(NY).date()
        with connect() as conn:
            last = conn.execute("SELECT value FROM settings WHERE key='last_refresh_date'").fetchone()
            if not force and last and last[0] == today.isoformat():
                return 0, "Already refreshed today"
        allocations = pd.read_csv(APP_DIR / "allocations.csv")
        tickers = sorted(set(allocations["ticker"].tolist()) | set(get_positions().get("ticker", pd.Series(dtype=str)).tolist()))
        try:
            closes = download_daily_closes(tickers, start=(today - timedelta(days=12)).isoformat(),
                                           end=(today + timedelta(days=1)).isoformat())
            fetched_at = datetime.now(NY).isoformat(timespec="seconds")
            stored = 0
            with connect() as conn:
                for ticker in tickers:
                    series = closes.get(ticker, pd.Series(dtype=float))
                    dated = [(pd.Timestamp(idx).date(), float(val)) for idx, val in series.items()
                             if pd.Timestamp(idx).date() < today]
                    if not dated:
                        continue
                    quote_day, value = dated[-1]
                    value_usd = usd_price(ticker, quote_day, value, closes)
                    if value_usd is None:
                        continue
                    conn.execute("INSERT OR REPLACE INTO price_snapshots VALUES (?, ?, ?, ?)",
                                 (ticker, quote_day.isoformat(), value_usd, fetched_at))
                    stored += 1
                if stored == 0:
                    raise RuntimeError("yfinance returned no usable prices")
                status = f"Updated {stored}/{len(tickers)} tickers"
                conn.execute("INSERT OR REPLACE INTO settings VALUES ('last_refresh_date', ?)", (today.isoformat(),))
                conn.execute("INSERT OR REPLACE INTO settings VALUES ('last_refresh_time', ?)", (fetched_at,))
                conn.execute("INSERT OR REPLACE INTO settings VALUES ('last_refresh_status', ?)", (status,))
            return stored, status
        except Exception as exc:
            with connect() as conn:
                conn.execute("INSERT OR REPLACE INTO settings VALUES ('last_refresh_status', ?)", (f"Refresh failed: {exc}",))
            return 0, f"Refresh failed: {exc}"


def _daily_refresh_worker() -> None:
    while True:
        now = datetime.now(NY)
        run_at = datetime.combine(now.date(), wall_time(8, 0), tzinfo=NY)
        if now >= run_at:
            _, status = refresh_daily_prices()
            if status.startswith("Refresh failed"):
                time_module.sleep(30 * 60)
                continue
            run_at += timedelta(days=1)
        delay = max(60, (run_at - datetime.now(NY)).total_seconds())
        time_module.sleep(delay)


@st.cache_resource
def start_daily_refresh_worker():
    worker = threading.Thread(target=_daily_refresh_worker, name="daily-yfinance-refresh", daemon=True)
    worker.start()
    return worker


def calculate_positions(raw: pd.DataFrame, prices: dict[str, float]) -> pd.DataFrame:
    if raw.empty:
        return raw.assign(current_price=pd.Series(dtype=float), market_value=pd.Series(dtype=float),
                          unrealized_pnl=pd.Series(dtype=float))
    df = raw.copy()
    df["current_price"] = df["ticker"].map(prices)
    direction = df["side"].map({"Long": 1, "Short": -1})
    df["market_value"] = df["shares"] * df["current_price"]
    df["unrealized_pnl"] = (df["current_price"] - df["entry_price"]) * df["shares"] * direction
    df["signed_exposure"] = df["market_value"] * direction
    df["price_available"] = df["current_price"].notna()
    return df


def admin_unlocked() -> bool:
    configured = st.secrets.get("admin_password", os.environ.get("PORTFOLIO_ADMIN_PASSWORD", ""))
    if not configured:
        st.sidebar.warning("Set an admin password before enabling edits. See README.md.")
        return False
    entered = st.sidebar.text_input("Admin password", type="password")
    if entered and entered == configured:
        st.sidebar.success("Admin editing enabled")
        return True
    return False


init_db()
start_daily_refresh_worker()
st.title("EagleTrack - Portfolio Tracker")
st.caption("Read-only portfolio dashboard • Contact Sam for any changes.")
raw = get_positions()
all_tickers = sorted(set(pd.read_csv(APP_DIR / "allocations.csv")["ticker"].tolist()) |
                     set(raw["ticker"].tolist() if not raw.empty else []))
last_refresh_date = ""
with connect() as conn:
    row = conn.execute("SELECT value FROM settings WHERE key='last_refresh_date'").fetchone()
    last_refresh_date = row[0] if row else ""
if not get_saved_prices(all_tickers)[0] or (
    datetime.now(NY).time() >= wall_time(8, 0) and last_refresh_date != datetime.now(NY).date().isoformat()
):
    refresh_daily_prices()
prices, quote_dates, refresh_date, refresh_status = get_saved_prices(all_tickers)
df = calculate_positions(raw, prices)
if not df.empty:
    df["quote_date"] = df["ticker"].map(quote_dates)
allocations = pd.read_csv(APP_DIR / "allocations.csv")
allocation_map = allocations[["group_name", "team", "ticker", "portfolio", "allocation_pct"]].drop_duplicates()
if not df.empty:
    df = df.merge(allocation_map, on=["group_name", "team", "ticker"], how="left")
    df["portfolio"] = df["portfolio"].fillna("Portfolio")

# Keep zero-weight positions visible as flat holdings without creating shares
# or affecting portfolio P/L.
present_positions = set(zip(raw.get("group_name", []), raw.get("team", []), raw.get("ticker", [])))
zero_allocations = allocations[allocations["allocation_pct"] == 0].copy()
zero_allocations = zero_allocations[
    ~zero_allocations.apply(lambda row: (row["group_name"], row["team"], row["ticker"]) in present_positions, axis=1)
]
if not zero_allocations.empty:
    zero_rows = zero_allocations[["group_name", "team", "ticker", "portfolio", "allocation_pct"]].copy()
    zero_rows["side"] = "Flat"
    zero_rows["shares"] = 0.0
    zero_rows["entry_price"] = float("nan")
    zero_rows["entry_date"] = ENTRY_DATE
    zero_rows["current_price"] = zero_rows["ticker"].map(prices)
    zero_rows["market_value"] = 0.0
    zero_rows["unrealized_pnl"] = 0.0
    zero_rows["signed_exposure"] = 0.0
    zero_rows["price_available"] = zero_rows["current_price"].notna()
    zero_rows["quote_date"] = zero_rows["ticker"].map(quote_dates)
    df = pd.concat([df, zero_rows], ignore_index=True, sort=False)

with st.sidebar:
    st.subheader("Market data")
    st.write(f"Last refresh: **{refresh_date}**")
    st.caption(refresh_status)
    if st.button("Refresh prices"):
        refresh_daily_prices(force=True)
        st.rerun()
    st.caption("Automatically refreshes at 8:00 AM Eastern while this app is running. Prices show the latest completed daily close available then; foreign-currency quotes are converted to USD.")
    is_admin = admin_unlocked()

if df.empty:
    st.info("No positions yet. The instructor can add them from the Admin panel in the sidebar.")
else:
    group_df = df[df["group_name"].isin(["Group A", "Group B"])]
    missing_quotes = sorted(df.loc[df["current_price"].isna() & (df["shares"] != 0), "ticker"].unique())
    if missing_quotes:
        st.warning("Latest quotes are unavailable for: " + ", ".join(missing_quotes) +
                   ". Those positions are excluded from P/L until a quote is available.")
    overall_pnl = group_df["unrealized_pnl"].sum()
    group_a_pnl = df.loc[df["group_name"] == "Group A", "unrealized_pnl"].sum()
    group_b_pnl = df.loc[df["group_name"] == "Group B", "unrealized_pnl"].sum()
    reference_pnl = df.loc[df["group_name"] == "Reference", "unrealized_pnl"].sum()
    ranked_summaries = [
        ("Group A", group_a_pnl / 100_000),
        ("Group B", group_b_pnl / 100_000),
        ("Prof Balch", reference_pnl / 20_000),
    ]
    ranked_summaries.sort(key=lambda item: item[1], reverse=True)
    summary_badges = {name: (("🥇", "1st"), ("🥈", "2nd"), ("🥚", "3rd"))[rank]
                      for rank, (name, _) in enumerate(ranked_summaries)}
    tiles = st.columns(4, border=True)
    def pnl_display(value: float) -> str:
        color = "green" if value >= 0 else "red"
        return f":{color}[${value:,.2f}]"

    def render_summary_tile(container, title: str, pnl: float, capital: float, badge=None):
        value_area, medal_area = container.columns([5, 1])
        value_area.metric(
            title,
            pnl_display(pnl),
            f"{pnl / capital * 100:+.2f}% on ${capital:,.0f}",
        )
        if badge:
            medal_area.markdown(
                f'<div style="text-align:center; padding-top:26px;">'
                f'<div style="font-size:38px; line-height:1.0;">{badge[0]}</div>'
                f'<div style="font-size:13px; font-weight:700;">{badge[1]}</div></div>',
            unsafe_allow_html=True,
            )

    render_summary_tile(tiles[0], "Overall portfolio performance", overall_pnl, 200_000)
    render_summary_tile(tiles[1], "Group A performance", group_a_pnl, 100_000, summary_badges["Group A"])
    render_summary_tile(tiles[2], "Group B performance", group_b_pnl, 100_000, summary_badges["Group B"])
    render_summary_tile(tiles[3], "Prof Balch P/L", reference_pnl, 20_000, summary_badges["Prof Balch"])
    st.caption(f"P/L since October 27, 2026 close • Price data refreshed {refresh_date}. Reference portfolio is excluded from group and overall totals.")

    for group in ("Group A", "Group B"):
        st.header(group)
        part = df[df["group_name"] == group]
        for team in sorted(part["team"].unique()):
            team_df = part[part["team"] == team]
            team_pnl = team_df["unrealized_pnl"].sum()
            with st.expander(f"{team} · P/L ${team_pnl:,.2f}", expanded=True):
                for portfolio in team_df["portfolio"].dropna().unique():
                    holdings = team_df[team_df["portfolio"] == portfolio]
                    st.subheader(f"{portfolio} portfolio")
                    view = holdings[["ticker", "side", "allocation_pct", "shares", "entry_price", "current_price", "quote_date", "unrealized_pnl"]].copy().reset_index(drop=True)
                    view = view.rename(columns={"ticker": "Asset", "side": "Position", "allocation_pct": "Allocation (%)", "shares": "Shares",
                        "entry_price": "Entry price (Oct 27 close)", "current_price": "Latest price (USD)",
                        "quote_date": "Price date", "unrealized_pnl": "P/L (USD)"})
                    st.dataframe(style_holdings_table(view), hide_index=True, row_height=44, column_config={
                        "Allocation (%)": st.column_config.NumberColumn(format="%g%%"),
                        "Shares": st.column_config.NumberColumn(format="%.4f"),
                        "Entry price (Oct 27 close)": st.column_config.NumberColumn(format="$%.2f"),
                        "Latest price (USD)": st.column_config.NumberColumn(format="$%.2f"),
                        "P/L (USD)": st.column_config.NumberColumn(format="$%.2f")})

    reference = df[df["group_name"] == "Reference"]
    if not reference.empty:
        st.header("Prof Balch")
        for portfolio in reference["portfolio"].dropna().unique():
            holdings = reference[reference["portfolio"] == portfolio]
            portfolio_pnl = holdings["unrealized_pnl"].sum()
            with st.expander(f"{portfolio} portfolio · P/L ${portfolio_pnl:,.2f}", expanded=True):
                view = holdings[["ticker", "side", "allocation_pct", "shares", "entry_price", "current_price", "quote_date", "unrealized_pnl"]].reset_index(drop=True).rename(columns={
                    "ticker": "Asset", "side": "Position", "allocation_pct": "Allocation (%)", "shares": "Shares", "entry_price": "Entry price (Oct 27 close)",
                    "current_price": "Latest price (USD)", "quote_date": "Price date", "unrealized_pnl": "P/L (USD)"})
                st.dataframe(style_holdings_table(view), hide_index=True, row_height=44, column_config={
                    "Allocation (%)": st.column_config.NumberColumn(format="%g%%"),
                    "Shares": st.column_config.NumberColumn(format="%.4f"),
                    "Entry price (Oct 27 close)": st.column_config.NumberColumn(format="$%.2f"),
                    "Latest price (USD)": st.column_config.NumberColumn(format="$%.2f"),
                    "P/L (USD)": st.column_config.NumberColumn(format="$%.2f")})

if is_admin:
    st.sidebar.divider()
    st.sidebar.subheader("Admin: manage positions")
    st.sidebar.caption("Initial class allocations are provided as position weights. Loading them uses the October 27, 2026 adjusted close as the entry price.")
    if st.sidebar.button("Load allocations at Oct 27 close prices"):
        allocations = pd.read_csv(APP_DIR / "allocations.csv")
        seed_prices, _ = get_price_data(sorted(allocations["ticker"].unique().tolist()), as_of=ENTRY_DATE)
        seeded = import_class_allocations(seed_prices)
        if seeded.empty:
            st.sidebar.error("No October 27 quotes were returned. Try again later or enter positions manually.")
        else:
            save_positions(seeded)
            st.sidebar.success(f"Loaded {len(seeded)} positions using October 27 adjusted closes.")
            st.rerun()
    st.sidebar.caption("Upload a CSV template or edit the current positions below. Saving replaces the full positions list.")
    template = pd.DataFrame(columns=DEFAULT_COLUMNS)
    st.sidebar.download_button("Download CSV template", template.to_csv(index=False), "positions_template.csv", "text/csv")
    uploaded = st.sidebar.file_uploader("Upload positions CSV", type="csv")
    initial = pd.read_csv(uploaded) if uploaded else raw[DEFAULT_COLUMNS].copy()
    if initial.empty:
        initial = pd.DataFrame([{ "group_name": GROUPS[0], "team": TEAMS[0], "ticker": "AAPL", "side": "Long",
                                  "shares": 10.0, "entry_price": 200.0, "entry_date": date.today().isoformat()}])
    edited = st.sidebar.data_editor(initial, num_rows="dynamic", use_container_width=True,
        column_config={"group_name": st.column_config.SelectboxColumn(options=GROUPS),
                       "team": st.column_config.SelectboxColumn(options=TEAMS),
                       "side": st.column_config.SelectboxColumn(options=["Long", "Short"]),
                       "shares": st.column_config.NumberColumn(min_value=0.0001),
                       "entry_price": st.column_config.NumberColumn(format="$%.2f", min_value=0.0001)},
        key="positions_editor")
    if st.sidebar.button("Save positions", type="primary"):
        try:
            save_positions(edited)
            st.sidebar.success("Positions saved.")
            st.rerun()
        except Exception as exc:
            st.sidebar.error(str(exc))


@st.fragment(run_every="10m")
def rerun_dashboard_after_daily_sync():
    """Pick up the background 8 AM quote refresh without requiring a manual browser reload."""
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='last_refresh_date'").fetchone()
    revision = row[0] if row else ""
    if "shown_refresh_date" not in st.session_state:
        st.session_state.shown_refresh_date = revision
    elif revision and revision != st.session_state.shown_refresh_date:
        st.session_state.shown_refresh_date = revision
        st.rerun(scope="app")


rerun_dashboard_after_daily_sync()
