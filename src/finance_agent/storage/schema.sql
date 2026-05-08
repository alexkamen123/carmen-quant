-- src/finance_agent/storage/schema.sql
CREATE TABLE IF NOT EXISTS daily_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    market          TEXT NOT NULL,
    close_price     REAL,
    composite_score REAL,
    recommendation  TEXT,
    confidence      TEXT,
    one_line        TEXT,
    -- 次日收盘价（由 backtest engine 在次日回填）
    next_day_close  REAL,
    next_day_change_pct REAL,
    -- 信号是否正确（recommendation 为买入/持有且次日上涨 > 1%，或减仓/卖出且次日下跌 > 1%）
    signal_correct  INTEGER,  -- 1=正确, 0=错误, NULL=未回填
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_signals_date_ticker ON daily_signals(date, ticker);

CREATE TABLE IF NOT EXISTS win_rate_stats (
    ticker          TEXT PRIMARY KEY,
    total_signals   INTEGER DEFAULT 0,
    correct_signals INTEGER DEFAULT 0,
    win_rate        REAL DEFAULT 0.0,
    last_updated    TEXT
);
