# tests/test_db/test_playbook.py
"""P2a Shadow Account 操作复盘：user_actions 的 BUY/SELL/TRIM 用 FIFO 配对成「已平仓交易」
算真实盈亏，3 条可解释、样本门控的规则挖行为模式，中文注入月报折叠面板。
不改建议、不改交易，只递镜子（纯观测默认 on）。
诚实铁律：卖出量超剩余/无配对买入 → 丢弃并计数呈现，绝不臆造成本价；
桶内 <5 笔或胜率差 <15pp → 规则沉默；已平仓 <5 笔 → 整个剧本闭嘴返 None。
关键取舍：FIFO 而非聚类——几十笔样本上用 KMeans/决策树=给噪声找形状，属「绝不碰」。"""
from finance_agent.db import tracker
from finance_agent.monthly import playbook as pb


def _a(date, ticker, action, shares, price):
    return {"date": date, "ticker": ticker, "action": action,
            "shares": shares, "price": price}


# ── FIFO 配对（TDD 主战场）─────────────────────────────────────────────────

def test_fifo_simple_round_trip():
    """整笔买卖：BUY 10@100 → SELL 10@110 → 1 笔已平仓，+10%，持有天数正确。"""
    trades, dropped = pb.pair_fifo_trades([
        _a("2026-01-01", "NVDA", "BUY", 10, 100.0),
        _a("2026-01-31", "NVDA", "SELL", 10, 110.0),
    ])
    assert dropped == 0 and len(trades) == 1
    t = trades[0]
    assert t["ticker"] == "NVDA" and t["shares"] == 10
    assert t["pnl_pct"] == 10.0 and t["holding_days"] == 30
    assert t["exit_type"] == "SELL"


def test_fifo_partial_trim_leaves_queue():
    """部分减仓：BUY 10@100 → TRIM 4@110 → 只产出 4 股的已平仓，剩 6 股在队列不计盈亏。"""
    trades, dropped = pb.pair_fifo_trades([
        _a("2026-01-01", "NVDA", "BUY", 10, 100.0),
        _a("2026-01-10", "NVDA", "TRIM", 4, 110.0),
    ])
    assert dropped == 0 and len(trades) == 1
    assert trades[0]["shares"] == 4 and trades[0]["exit_type"] == "TRIM"


def test_fifo_sell_crosses_lots():
    """跨批次：BUY 5@100 + BUY 5@120 → SELL 8@130 → 拆成两笔(5股@100、3股@120)，先进先出。"""
    trades, dropped = pb.pair_fifo_trades([
        _a("2026-01-01", "NVDA", "BUY", 5, 100.0),
        _a("2026-01-05", "NVDA", "BUY", 5, 120.0),
        _a("2026-01-20", "NVDA", "SELL", 8, 130.0),
    ])
    assert dropped == 0 and len(trades) == 2
    assert (trades[0]["shares"], trades[0]["buy_price"]) == (5, 100.0)
    assert (trades[1]["shares"], trades[1]["buy_price"]) == (3, 120.0)
    assert trades[0]["pnl_pct"] == 30.0 and trades[1]["pnl_pct"] == round((130 / 120 - 1) * 100, 1)


def test_fifo_oversell_dropped_not_fabricated():
    """卖出量超剩余：只配对到剩余部分，超出股数丢弃并计数——绝不臆造成本价。"""
    trades, dropped = pb.pair_fifo_trades([
        _a("2026-01-01", "NVDA", "BUY", 4, 100.0),
        _a("2026-01-20", "NVDA", "SELL", 10, 110.0),
    ])
    assert len(trades) == 1 and trades[0]["shares"] == 4
    assert dropped == 1   # 该笔卖出有未配对残量


def test_fifo_sell_without_buy_fully_dropped():
    """追踪开始前买的票被卖：无任何配对买入 → 整笔丢弃计数，产出 0 笔。"""
    trades, dropped = pb.pair_fifo_trades([
        _a("2026-01-20", "AAPL", "SELL", 10, 110.0),
    ])
    assert trades == [] and dropped == 1


def test_fifo_priceless_sell_consumes_queue_keeps_alignment():
    """对抗审查 CONFIRMED：缺价卖出若被静默过滤，后续卖出会配到「其实已卖掉」的批次
    报出自信错误的盈亏。正确语义：缺价卖出照常消耗队头（不产出盈亏、计 dropped），保住对齐。"""
    trades, dropped = pb.pair_fifo_trades([
        _a("2026-01-01", "NVDA", "BUY", 10, 100.0),
        _a("2026-02-01", "NVDA", "SELL", 10, None),    # auto 检测取价失败 → price=None
        _a("2026-03-01", "NVDA", "BUY", 10, 200.0),
        _a("2026-04-01", "NVDA", "SELL", 10, 210.0),
    ])
    assert len(trades) == 1 and dropped == 1
    assert trades[0]["buy_price"] == 200.0 and trades[0]["pnl_pct"] == 5.0   # 绝不配到 100 批次


def test_fifo_priceless_buy_lot_no_trade_but_alignment():
    """无价买入批次入队占位：被卖出消耗时不产出盈亏（计 dropped），后续批次配对不错位。"""
    trades, dropped = pb.pair_fifo_trades([
        _a("2026-01-01", "NVDA", "BUY", 10, None),
        _a("2026-01-20", "NVDA", "SELL", 10, 110.0),
        _a("2026-02-01", "NVDA", "BUY", 10, 100.0),
        _a("2026-02-20", "NVDA", "SELL", 10, 120.0),
    ])
    assert len(trades) == 1 and dropped == 1
    assert trades[0]["pnl_pct"] == 20.0


def test_fifo_missing_shares_pollutes_ticker():
    """缺股数没法维持队列对齐 → 该票从此整票停记（宁缺勿假），后续卖出计 dropped；
    其他票不受影响。"""
    trades, dropped = pb.pair_fifo_trades([
        _a("2026-01-01", "NVDA", "BUY", None, 100.0),   # 缺股数 → NVDA 污染
        _a("2026-01-05", "NVDA", "BUY", 10, 100.0),
        _a("2026-01-20", "NVDA", "SELL", 10, 110.0),
        _a("2026-01-02", "AMD", "BUY", 5, 50.0),
        _a("2026-01-25", "AMD", "SELL", 5, 60.0),
    ])
    assert {t["ticker"] for t in trades} == {"AMD"} and dropped == 1


def test_fifo_tickers_isolated():
    """不同票各自独立队列：NVDA 的卖出绝不消耗 AMD 的买入。"""
    trades, _ = pb.pair_fifo_trades([
        _a("2026-01-01", "AMD", "BUY", 10, 100.0),
        _a("2026-01-20", "NVDA", "SELL", 5, 110.0),
        _a("2026-01-25", "AMD", "SELL", 10, 120.0),
    ])
    assert len(trades) == 1 and trades[0]["ticker"] == "AMD"


# ── 3 条规则：样本门控（桶 <5 笔或差 <15pp → 沉默）──────────────────────────

def _mk_trades(n_win_short, n_lose_short, n_win_long, n_lose_long):
    """构造持有期两桶交易：短持 <30 天、长持 ≥30 天。"""
    ts = []
    for i in range(n_win_short):
        ts.append({"ticker": "A", "shares": 1, "buy_price": 100, "sell_price": 110,
                   "pnl_pct": 10.0, "holding_days": 5, "exit_type": "SELL"})
    for i in range(n_lose_short):
        ts.append({"ticker": "A", "shares": 1, "buy_price": 100, "sell_price": 90,
                   "pnl_pct": -10.0, "holding_days": 5, "exit_type": "SELL"})
    for i in range(n_win_long):
        ts.append({"ticker": "A", "shares": 1, "buy_price": 100, "sell_price": 110,
                   "pnl_pct": 10.0, "holding_days": 60, "exit_type": "SELL"})
    for i in range(n_lose_long):
        ts.append({"ticker": "A", "shares": 1, "buy_price": 100, "sell_price": 90,
                   "pnl_pct": -10.0, "holding_days": 60, "exit_type": "SELL"})
    return ts


def test_rule_holding_period_fires_on_big_gap():
    """短持 1/6 胜 vs 长持 5/6 胜（差>15pp、两桶各≥5）→ 出一条持有期发现。"""
    findings = pb.rule_findings(_mk_trades(1, 5, 5, 1))
    assert any("持有" in f for f in findings)


def test_rule_silent_when_bucket_too_small():
    """任一桶 <5 笔 → 该规则沉默（样本门控保险丝）。"""
    findings = pb.rule_findings(_mk_trades(1, 3, 5, 1))   # 短持桶只有 4 笔
    assert not any("持有" in f for f in findings)


def test_rule_silent_when_diff_below_15pp():
    """两桶胜率差 <15pp → 沉默（不显著不下结论）。"""
    findings = pb.rule_findings(_mk_trades(3, 3, 3, 3))   # 均 50% 胜率
    assert findings == []


def test_rule_findings_descriptive_not_prescriptive():
    """措辞递镜子不说教：描述统计事实，不出现「必须/应该立即」类命令式。"""
    findings = pb.rule_findings(_mk_trades(1, 5, 5, 1))
    joined = "".join(findings)
    assert "必须" not in joined and "应该立即" not in joined


# ── 剧本与面板：整体闭嘴门 + 诚实呈现丢弃计数 ────────────────────────────────

def test_build_playbook_none_when_too_few_closed():
    """已平仓 <5 次退出 → 整个剧本返 None（月报不出面板，不硬凑）。"""
    actions = [
        _a("2026-01-01", "NVDA", "BUY", 10, 100.0),
        _a("2026-01-31", "NVDA", "SELL", 10, 110.0),
    ]
    assert pb.build_shadow_playbook(actions) is None


def test_build_gate_counts_physical_exits_not_slices():
    """审查 PLAUSIBLE 采纳：定投 5 个建仓批次一次性卖出=1 次物理退出（虽拆 5 个切片），
    不得凑过闭嘴门——单次决策不能解锁面板。"""
    actions = [_a(f"2026-01-0{i + 1}", "NVDA", "BUY", 5, 100.0 + 4 * i) for i in range(5)]
    actions.append(_a("2026-02-01", "NVDA", "SELL", 25, 110.0))
    assert pb.build_shadow_playbook(actions) is None


def test_rule_position_size_fx_normalized():
    """审查 CONFIRMED 采纳：港股名义额按 7.8 折美元再分桶（CLAUDE.md 同口径），
    币种量级不得伪装成仓位大小效应。构造：港股 raw 7800=1000 USD 全胜、美股 1000 USD 全负——
    折算后名义额全相等 → 无法分桶 → 仓位规则必须沉默；若未折算则会按币种劈桶并谎报 100pp 差。"""
    trades = []
    for i in range(5):   # 港股：20股@390 HKD = raw 7800 = 1000 USD
        trades.append({"ticker": "00700", "shares": 20, "buy_price": 390.0,
                       "sell_price": 429.0, "pnl_pct": 10.0, "holding_days": 10,
                       "exit_type": "SELL"})
    for i in range(5):   # 美股：10股@100 USD = 1000 USD
        trades.append({"ticker": "NVDA", "shares": 10, "buy_price": 100.0,
                       "sell_price": 90.0, "pnl_pct": -10.0, "holding_days": 10,
                       "exit_type": "SELL"})
    findings = pb.rule_findings(trades)
    assert not any("仓位" in f for f in findings)


def test_build_playbook_summary_and_panel():
    """≥5 笔已平仓 → 剧本含胜率/均值/规则发现；面板含丢弃计数的诚实注脚。"""
    actions = []
    for i in range(6):
        actions.append(_a(f"2026-01-0{i+1}", f"T{i}", "BUY", 10, 100.0))
        actions.append(_a(f"2026-02-0{i+1}", f"T{i}", "SELL", 10, 110.0 if i < 4 else 90.0))
    actions.append(_a("2026-02-20", "GHOST", "SELL", 5, 50.0))   # 无配对 → 丢弃
    play = pb.build_shadow_playbook(actions)
    assert play["n_closed"] == 6 and play["n_exits"] == 6
    assert play["win_rate"] == round(4 / 6 * 100, 1)
    assert play["dropped"] == 1
    panel = pb.playbook_panel(play)
    assert panel["tag"] == "collapsible_panel"
    body = str(panel)
    assert "未计入" in body or "无配对" in body   # 丢弃必须呈现，不许静默吞
    assert "非模拟" not in body and "显著" not in body   # 审查措辞修正：不夸大成交真实性/统计功效
    assert "近似价" in body   # auto 检测行用检测日近似价，须明示


def test_playbook_panel_none_passthrough():
    assert pb.playbook_panel(None) is None


# ── 取数：BUY/SELL/TRIM 全量（含缺股数/缺价行·由配对层诚实处置），按时间序 ────

def test_get_actions_for_pairing_filters_and_orders(tmp_path):
    """审查 CONFIRMED 采纳：缺价/缺股数行不许在 SQL 层静默过滤（会打乱 FIFO 队列对齐），
    必须交给 pair_fifo_trades 做污染标记/占位消耗。只排除非交易动作。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        con.executescript(tracker._CREATE_ACTIONS_SQL)
        for d, tk, act, sh, px in [
            ("2026-01-05", "NVDA", "SELL", 5, 110.0),
            ("2026-01-01", "NVDA", "BUY", 10, 100.0),
            ("2026-01-03", "NVDA", "HOLD", None, None),    # 非交易动作 → 排除
            ("2026-01-04", "AMD", "BUY", None, 50.0),      # 缺股数 → 保留，交配对层污染处置
            ("2026-01-06", "AMD", "SELL", 5, None),        # 缺价 → 保留，交配对层占位消耗
        ]:
            con.execute(
                "INSERT INTO user_actions(date,ticker,action,shares,price) VALUES(?,?,?,?,?)",
                (d, tk, act, sh, px))
    rows = tracker.get_actions_for_pairing(db_path=db)
    assert [r["action"] for r in rows] == ["BUY", "BUY", "SELL", "SELL"]   # 时间升序·只滤 HOLD
    assert rows[0]["date"] == "2026-01-01"
