# Practicum Portfolio Tracker

A Streamlit portfolio dashboard for 10 teams (five in Group A and five in Group B). Viewers can inspect performance; only someone with the configured instructor password can edit positions.

## Run locally in VS Code

1. Open this folder in VS Code.
2. Open **Terminal → New Terminal**.
3. Create and activate a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Create `.streamlit/secrets.toml` (copy `.streamlit/secrets.toml.example`) and set a private password. Keep this file private; it is ignored by Git.
5. Start the app:

   ```bash
   streamlit run app.py --server.port 8502 --server.fileWatcherType poll
   ```

Streamlit prints a local URL, usually `http://localhost:8502` with the command above. You can also open the folder in VS Code and use its integrated terminal for these commands.

## Enter positions

In the app's sidebar, enter the instructor password. Use the editor to add or change positions, then select **Save positions**. You can download the CSV template, fill it, then upload it. Uploading and saving replaces the complete positions list.

CSV columns: `group_name, team, ticker, side, shares, entry_price, entry_date`. Groups must be `Group A`, `Group B`, or `Reference`; teams must match the portfolio labels (A-1 through A-5, B-1 through B-5, or Prof Balch); side is `Long` or `Short`. Record a short's share count and entry price as positive numbers and set its side to `Short`.

The supplied `starting_positions.csv` contains the saved class starting positions, including share counts and entry prices. A fresh app database loads these positions automatically. The supplied `allocations.csv` contains the original portfolio weights; an administrator can still select **Load allocations at Oct 27 close prices** to recreate positions from those weights. A team with two portfolios gets $10,000 per portfolio. Prof Balch is listed as a separate Reference portfolio and is not included in Group A or Group B totals. Assets marked TBD were left out until their tickers are known.

## Sharing with students

The app is read-only for anyone without the instructor password. Keep the GitHub repository private and set `admin_password` in Streamlit Community Cloud's secret settings. The starting positions are reproducible from `starting_positions.csv`, but SQLite writes on Community Cloud are not guaranteed to persist. If the hosted app resets, it reloads the starting positions; edits made only in its admin panel may be lost. To make a position change durable in this simple setup, update `starting_positions.csv` in VS Code, commit, and push it to GitHub. A persistent hosted database is needed if admin-panel edits must persist without GitHub updates. Anyone who knows the admin password can edit, so keep it instructor-only.

## Price and performance notes

The app refreshes daily adjusted closes through `yfinance` at 8:00 AM Eastern while the Streamlit server is running, and saves dated price snapshots in SQLite. The browser checks for a new snapshot every 10 minutes. Quotes are not guaranteed real time and may be delayed; the displayed quote date is the date of the last completed market session. RHM is looked up as RHM.DE and converted from EUR to USD; VW uses VWAGY and SOX uses ^SOX. When a quote is unavailable, the latest-price and P/L cells remain blank and a warning identifies affected symbols. Returns currently mean unrealized P&L divided by position entry notional. This version does not model cash, realized trades, commissions, dividends, or splits.

The local daily scheduler only runs while the Streamlit server process is running. If you stop the app or shut down your computer, it will refresh on the next start instead.
