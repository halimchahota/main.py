def render_chart(df: pd.DataFrame, plan: Plan) -> bytes:
    d = df.tail(120).copy()
    d["EMA_FAST"] = ema(d["Close"], EMA_FAST)
    d["EMA_SLOW"] = ema(d["Close"], EMA_SLOW)

    x = list(range(len(d)))
    o = d["Open"].values
    h = d["High"].values
    l = d["Low"].values
    c = d["Close"].values

    fig = plt.figure(figsize=(12, 7), dpi=150)
    ax = plt.gca()

    # Candles
    for i in range(len(d)):
        ax.plot([x[i], x[i]], [l[i], h[i]], linewidth=1)
        y0 = min(o[i], c[i])
        y1 = max(o[i], c[i])
        rect = plt.Rectangle((x[i] - 0.32, y0), 0.64, max(y1 - y0, 1e-9), fill=False, linewidth=1)
        ax.add_patch(rect)

    # EMA
    ax.plot(x, d["EMA_FAST"].values, linewidth=1.2, label=f"EMA{EMA_FAST}")
    ax.plot(x, d["EMA_SLOW"].values, linewidth=1.2, label=f"EMA{EMA_SLOW}")

    # Zone
    if plan.zone_low is not None and plan.zone_high is not None:
        ax.axhspan(plan.zone_low, plan.zone_high, alpha=0.15)

    # Levels
    ax.axhline(plan.entry, linewidth=1.2, linestyle="--")
    ax.axhline(plan.sl, linewidth=1.2, linestyle="--")
    ax.axhline(plan.tp1, linewidth=1.0, linestyle=":")
    ax.axhline(plan.tp2, linewidth=1.0, linestyle=":")
    ax.axhline(plan.tp3, linewidth=1.0, linestyle=":")

    title_side = "BUY" if plan.side == "BUY" else "SELL"
    ax.set_title(f"{plan.label} ({plan.symbol}) | {plan.timeframe} | {title_side} {plan.entry_type}")
    ax.grid(True, alpha=0.2)
    ax.legend(loc="upper left", fontsize=9)

    # X labels
    idx = d.index
    step = max(1, len(d) // 6)
    ticks = list(range(0, len(d), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(idx[i])[:16] for i in ticks], rotation=15, ha="right", fontsize=8)

    # Technical analysis box
    zone_txt = "None"
    if plan.zone_low is not None and plan.zone_high is not None:
        zone_txt = f"{plan.zone_name} [{fmt_price(plan.label, plan.zone_low, plan.entry)} - {fmt_price(plan.label, plan.zone_high, plan.entry)}]"

    liq_txt = plan.liq_side if plan.liq_side else "NO"
    bos_txt = plan.bos_side if plan.bos_side else "NO"

    analysis_text = (
        f"Analysis\n"
        f"D1/H4: {plan.trend_d1:+d}/{plan.trend_h4:+d} | Overall: {plan.overall:+d}\n"
        f"Liq: {liq_txt} | BOS: {bos_txt}\n"
        f"Zone: {zone_txt}\n"
        f"Entry: {fmt_price(plan.label, plan.entry, plan.entry)}\n"
        f"SL: {fmt_price(plan.label, plan.sl, plan.entry)}\n"
        f"TP1: {fmt_price(plan.label, plan.tp1, plan.entry)}\n"
        f"Conf: {plan.confidence}/10"
    )

    ax.text(
        0.01, 0.98, analysis_text,
        transform=ax.transAxes,
        fontsize=9,
        verticalalignment="top",
        bbox=dict(boxstyle="round", alpha=0.15)
    )

    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
