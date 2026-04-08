"""
Backtrader engine for OpenTrader strategy backtesting.

This module runs inside a ProcessPoolExecutor worker — entirely synchronous,
no FastAPI / asyncio dependencies.

Entry point:  run_backtest(params: dict) -> dict
"""
import base64
import io
import math

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import backtrader as bt
import pandas as pd
import yfinance as yf


# ── Direction normaliser ───────────────────────────────────────────────────────

def _norm_direction(raw: str) -> str:
    r = (raw or "long").lower().strip()
    if "both" in r or ("long" in r and "short" in r):
        return "both"
    if r in ("sell", "close", "exit"):
        return "sell"
    if r in ("short", "sell_short"):
        return "short"
    return "long"


# ── Strategy ───────────────────────────────────────────────────────────────────

class OpenTraderStrategy(bt.Strategy):
    """
    EMA 10/21 crossover with percentage-based stop-loss and take-profit.
    Uses notify_trade for clean P&L logging (Backtrader handles netting).

    direction:
      "long"  — long entries only
      "short" — short entries only
      "both"  — long and short
      "sell"  — exit-only (no new entries)
    """

    params = dict(
        fast_period=10,
        slow_period=21,
        stop_pct=1.5,
        tp_pct=3.0,
        confidence=0.70,
        max_pos=500.0,
        direction="long",
    )

    def __init__(self):
        self.fast_ema  = bt.ind.EMA(period=self.p.fast_period)
        self.slow_ema  = bt.ind.EMA(period=self.p.slow_period)
        self.crossup   = bt.ind.CrossUp(self.fast_ema,  self.slow_ema)
        self.crossdown = bt.ind.CrossDown(self.fast_ema, self.slow_ema)
        self.trade_log: list[dict] = []
        self._pending   = False
        self._entry_px  = None
        self._entry_dt  = None
        self._entry_dir = None   # "long" | "short"
        self._entry_qty = 0

    def notify_order(self, order):
        if order.status in (order.Submitted, order.Accepted):
            return
        if order.status in (order.Canceled, order.Margin, order.Rejected):
            self._pending = False
            return
        if order.status == order.Completed:
            self._pending = False
            # Record entry only when transitioning from flat to in-position
            if self.position.size != 0 and self._entry_px is None:
                self._entry_px  = order.executed.price
                self._entry_dt  = self.data.datetime.date(0).isoformat()
                self._entry_dir = "long" if order.isbuy() else "short"
                self._entry_qty = int(abs(order.executed.size))

    def notify_trade(self, trade):
        """Called when a trade is closed — record to trade_log."""
        if not trade.isclosed:
            return
        qty      = self._entry_qty or 1
        entry_px = self._entry_px or trade.price
        pnl_pct  = trade.pnl / max(abs(entry_px * qty), 1e-8) * 100
        self.trade_log.append({
            "entry_date":  self._entry_dt or "",
            "exit_date":   self.data.datetime.date(0).isoformat(),
            "ticker":      self.data._name,
            "direction":   self._entry_dir or "long",
            "entry_price": round(entry_px, 4),
            "exit_price":  round(self.data.close[0], 4),
            "qty":         qty,
            "pnl":         round(trade.pnl, 2),
            "pnl_pct":     round(pnl_pct, 2),
            "exit_reason": getattr(self, "_last_exit_reason", "signal"),
        })
        self._entry_px  = None
        self._entry_dt  = None
        self._entry_dir = None
        self._entry_qty = 0

    def _size_for(self, price: float) -> int:
        return max(1, int(self.p.max_pos * self.p.confidence / price))

    def next(self):
        if self._pending:
            return

        price     = self.data.close[0]
        pos       = self.position.size
        direction = self.p.direction
        stop      = self.p.stop_pct / 100
        tp        = self.p.tp_pct   / 100

        # ── Exit: manage open long ─────────────────────────────────────────
        if pos > 0 and self._entry_px is not None:
            if price <= self._entry_px * (1 - stop):
                self._last_exit_reason = "stop_loss"
                self.close(); self._pending = True; return
            if price >= self._entry_px * (1 + tp):
                self._last_exit_reason = "take_profit"
                self.close(); self._pending = True; return
            if self.crossdown[0] and direction in ("long", "both"):
                self._last_exit_reason = "signal_exit"
                self.close(); self._pending = True; return

        # ── Exit: manage open short ────────────────────────────────────────
        elif pos < 0 and self._entry_px is not None:
            if price >= self._entry_px * (1 + stop):
                self._last_exit_reason = "stop_loss"
                self.close(); self._pending = True; return
            if price <= self._entry_px * (1 - tp):
                self._last_exit_reason = "take_profit"
                self.close(); self._pending = True; return
            if self.crossup[0] and direction in ("short", "both"):
                self._last_exit_reason = "signal_exit"
                self.close(); self._pending = True; return

        # ── Entry: only when flat ──────────────────────────────────────────
        if pos == 0:
            size = self._size_for(price)
            if direction in ("long", "both") and self.crossup[0]:
                self._last_exit_reason = "signal"
                self.buy(size=size); self._pending = True
            elif direction in ("short", "both") and self.crossdown[0]:
                self._last_exit_reason = "signal"
                self.sell(size=size); self._pending = True


# ── Portfolio value analyzer ───────────────────────────────────────────────────

class _PortfolioValue(bt.Analyzer):
    def start(self):
        self.vals: list[float] = []

    def next(self):
        self.vals.append(round(self.strategy.broker.getvalue(), 2))

    def get_analysis(self):
        return self.vals


# ── OHLCV fetch ────────────────────────────────────────────────────────────────

def _fetch_ohlcv(ticker: str, period: str = "2y") -> pd.DataFrame:
    df = yf.download(ticker, period=period, interval="1d", auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"No OHLCV data returned for {ticker!r}")
    df.index = pd.to_datetime(df.index)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    return df[["open", "high", "low", "close", "volume"]].dropna()


# ── Chart capture ──────────────────────────────────────────────────────────────

BG   = "#0f172a"
SURF = "#1e293b"
FG   = "#e2e8f0"
MUT  = "#64748b"
GRN  = "#4ade80"
RED  = "#f87171"
BLU  = "#60a5fa"
YEL  = "#fbbf24"


def _build_chart(df: pd.DataFrame, trade_log: list, equity_curve: list, ticker: str) -> str:
    """Build a custom chart from OHLCV + trade log + equity curve. Returns base64 PNG."""
    try:
        import matplotlib.gridspec as gridspec
        from matplotlib.lines import Line2D

        fast_ema = df["close"].ewm(span=10, adjust=False).mean()
        slow_ema = df["close"].ewm(span=21, adjust=False).mean()

        fig = plt.figure(figsize=(14, 8), facecolor=BG)
        gs  = gridspec.GridSpec(3, 1, figure=fig, hspace=0.06,
                                height_ratios=[3, 1, 1.5])

        # ── Price + EMA panel ──────────────────────────────────────────────
        ax1 = fig.add_subplot(gs[0])
        ax1.set_facecolor(SURF)
        ax1.plot(df.index, df["close"], color=FG,   linewidth=1.0, label="Close")
        ax1.plot(df.index, fast_ema,    color=BLU,  linewidth=0.8, label="EMA 10", alpha=0.85)
        ax1.plot(df.index, slow_ema,    color=YEL,  linewidth=0.8, label="EMA 21", alpha=0.85)

        # Trade entry / exit markers
        for t in trade_log:
            try:
                ed  = pd.to_datetime(t["entry_date"])
                xd  = pd.to_datetime(t["exit_date"])
                ep  = t["entry_price"]
                xp  = t["exit_price"]
                col = GRN if (t.get("pnl", 0) or 0) >= 0 else RED
                marker_e = "^" if t.get("direction", "long") == "long" else "v"
                ax1.scatter(ed, ep, marker=marker_e, color=GRN, s=60, zorder=5, linewidths=0)
                ax1.scatter(xd, xp, marker="x",      color=col,  s=60, zorder=5, linewidths=1.5)
                ax1.plot([ed, xd], [ep, xp], color=col, linewidth=0.6, alpha=0.4, linestyle="--")
            except Exception:
                pass

        ax1.set_ylabel("Price", color=MUT, fontsize=9)
        ax1.tick_params(colors=MUT, labelsize=8)
        ax1.set_title(f"{ticker} — Backtest (EMA 10/21 Crossover)", color=FG,
                      fontsize=11, pad=8)
        for spine in ax1.spines.values():
            spine.set_edgecolor(MUT)
        ax1.set_xticklabels([])
        ax1.legend(loc="upper left", fontsize=8, facecolor=SURF,
                   labelcolor=FG, edgecolor=MUT, framealpha=0.7)

        # ── Volume panel ───────────────────────────────────────────────────
        ax2 = fig.add_subplot(gs[1], sharex=ax1)
        ax2.set_facecolor(SURF)
        colors = [GRN if df["close"].iloc[i] >= df["close"].iloc[i-1] else RED
                  for i in range(len(df))]
        ax2.bar(df.index, df["volume"], color=colors, alpha=0.6, width=1.0)
        ax2.set_ylabel("Volume", color=MUT, fontsize=8)
        ax2.tick_params(colors=MUT, labelsize=7)
        for spine in ax2.spines.values():
            spine.set_edgecolor(MUT)
        ax2.set_xticklabels([])

        # ── Equity curve panel ─────────────────────────────────────────────
        ax3 = fig.add_subplot(gs[2])
        ax3.set_facecolor(SURF)
        if equity_curve:
            xs = range(len(equity_curve))
            ax3.plot(xs, equity_curve, color=BLU, linewidth=1.2)
            ax3.fill_between(xs, equity_curve, equity_curve[0],
                             alpha=0.15,
                             color=GRN if equity_curve[-1] >= equity_curve[0] else RED)
        ax3.set_ylabel("Portfolio ($)", color=MUT, fontsize=8)
        ax3.tick_params(colors=MUT, labelsize=7)
        ax3.set_xlabel("Time", color=MUT, fontsize=8)
        for spine in ax3.spines.values():
            spine.set_edgecolor(MUT)

        plt.tight_layout(pad=0.4)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=90, bbox_inches="tight",
                    facecolor=BG, edgecolor="none")
        plt.close("all")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        plt.close("all")
        return ""


# ── Main entry point ───────────────────────────────────────────────────────────

def run_backtest(params: dict) -> dict:
    ticker          = params["ticker"].upper().strip()
    period          = params.get("period", "2y")
    stop_pct        = float(params.get("stop_pct",    1.5))
    tp_pct          = float(params.get("tp_pct",      3.0))
    confidence      = float(params.get("confidence",  0.70))
    direction       = _norm_direction(params.get("direction", "long"))
    max_pos         = float(params.get("max_pos",     500))
    initial_capital = float(params.get("initial_capital", 10_000))

    df = _fetch_ohlcv(ticker, period)
    start_date = df.index[0].strftime("%Y-%m-%d")
    end_date   = df.index[-1].strftime("%Y-%m-%d")

    cerebro = bt.Cerebro(stdstats=True)
    cerebro.broker.setcash(initial_capital)
    cerebro.broker.setcommission(commission=0.001)
    cerebro.broker.set_slippage_perc(0.001)

    cerebro.adddata(bt.feeds.PandasData(dataname=df, name=ticker))
    cerebro.addstrategy(
        OpenTraderStrategy,
        stop_pct=stop_pct, tp_pct=tp_pct,
        confidence=confidence, max_pos=max_pos, direction=direction,
    )
    cerebro.addanalyzer(bt.analyzers.SharpeRatio,  _name="sharpe",
                        riskfreerate=0.05, annualize=True,
                        timeframe=bt.TimeFrame.Days)
    cerebro.addanalyzer(bt.analyzers.DrawDown,      _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(_PortfolioValue,            _name="portfolio")

    results = cerebro.run()
    strat   = results[0]

    final_cap = cerebro.broker.getvalue()
    total_ret = (final_cap - initial_capital) / initial_capital * 100

    sharpe_raw = strat.analyzers.sharpe.get_analysis()
    sharpe     = sharpe_raw.get("sharperatio") or 0.0
    if sharpe is None or (isinstance(sharpe, float) and math.isnan(sharpe)):
        sharpe = 0.0

    dd_raw = strat.analyzers.drawdown.get_analysis()
    max_dd = dd_raw.get("max", {}).get("drawdown", 0.0) or 0.0

    ta      = strat.analyzers.trades.get_analysis()
    total_t = int(ta.get("total", {}).get("closed", 0) or 0)
    won_t   = int(ta.get("won",   {}).get("total",  0) or 0)
    win_rate = round(won_t / total_t * 100, 1) if total_t else 0.0

    portfolio_vals = strat.analyzers.portfolio.get_analysis() or [initial_capital]
    if len(portfolio_vals) > 260:
        step = len(portfolio_vals) // 260
        equity_curve = portfolio_vals[::step]
        if equity_curve[-1] != portfolio_vals[-1]:
            equity_curve.append(portfolio_vals[-1])
    else:
        equity_curve = portfolio_vals

    n_years    = max(len(df) / 252, 0.01)
    annualized = round(((1 + total_ret / 100) ** (1 / n_years) - 1) * 100, 2)

    monthly_returns = _build_monthly_returns(df)
    chart_png_b64   = _build_chart(df, strat.trade_log, equity_curve, ticker)

    return {
        "engine":            "backtrader",
        "ticker":            ticker,
        "period":            f"{start_date} to {end_date}",
        "initial_cap":       initial_capital,
        "final_cap":         round(final_cap, 2),
        "total_return":      round(total_ret, 2),
        "annualized_return": annualized,
        "sharpe":            round(float(sharpe), 3),
        "max_drawdown":      round(float(max_dd), 2),
        "win_rate":          win_rate,
        "total_trades":      total_t,
        "direction":         direction,
        "stop_pct":          stop_pct,
        "tp_pct":            tp_pct,
        "confidence":        confidence,
        "equity_curve":      equity_curve,
        "monthly_returns":   monthly_returns,
        "trade_log":         strat.trade_log,
        "chart_png_b64":     chart_png_b64,
    }


def _build_monthly_returns(df: pd.DataFrame) -> list[float]:
    try:
        monthly = df["close"].resample("ME").last().pct_change().dropna()
        return [round(v * 100, 2) for v in monthly.tolist()]
    except Exception:
        return []


def _bt_run_in_process(params: dict) -> dict:
    return run_backtest(params)
