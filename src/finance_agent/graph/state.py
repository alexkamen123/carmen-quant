# src/finance_agent/graph/state.py
from __future__ import annotations
from pydantic import BaseModel, Field
from finance_agent.signals.models import TechnicalSignals


class NewsItem(BaseModel):
    title: str
    summary: str
    published: str


class EarningsSummary(BaseModel):
    """基本面数据摘要（由 Fundamental Analyst 填充）"""
    revenue_growth_yoy: float | None = None   # 营收同比增速 %
    gross_margin: float | None = None          # 毛利率 %
    pe_ratio: float | None = None              # 市盈率
    ps_ratio: float | None = None              # 市销率
    debt_to_equity: float | None = None        # 资产负债率
    next_earnings_date: str = ""               # 下次财报日期 "YYYY-MM-DD"，空字符串表示未知
    fundamental_view: str = ""                 # Claude 的基本面一段话判断


class StockAnalysis(BaseModel):
    """单只股票的完整分析结果"""
    ticker: str
    market: str
    signals: TechnicalSignals
    news: list[NewsItem] = Field(default_factory=list)
    peer_news: list[NewsItem] = Field(default_factory=list)  # 竞争对手新闻
    earnings: EarningsSummary = Field(default_factory=EarningsSummary)

    # Portfolio metadata
    shares: float = 0.0              # 持仓股数
    sector: str = ""                 # 行业标签（来自 portfolio.yaml）
    is_etf: bool = False             # True = 宽基/股息 ETF，跳过辩论和基本面分析
    cost_basis: float = 0.0          # 买入均价（来自 portfolio.yaml）
    signal_weight: str = "normal"    # "low" = 技术信号回测无效，PM 应以基本面/thesis 为主
    unrealized_pnl_pct: float | None = None   # 浮动盈亏 %（运行时计算）

    # 持仓逻辑（从 DB 加载，注入 PM prompt）
    thesis: str = ""             # 原始持仓理由（Claude 生成，持久化在 theses 表）
    thesis_stale: bool = False   # True = 超过 30 天未更新，已触发自动重生成

    # 历史决策记忆（mempal 查询结果，注入 PM prompt）
    memory_context: str = ""

    # 当前触发的量化策略信号（signal_lookup 计算，注入 PM prompt）
    strategy_evidence: str = ""

    # Technical debate (DeepSeek)
    bull_thesis: str = ""
    bear_thesis: str = ""

    # Final decision (Claude)
    recommendation: str = ""       # 长期（1-3月）建议
    short_term_action: str = ""    # 短期（本周）执行建议，与长期不同时才有意义
    confidence: str = ""
    entry_hint: str = ""
    key_risk: str = ""
    one_line: str = ""
    position_change: str = ""   # 仓位调整建议：减仓 / 维持 / 小加 / 大加
    key_assumption: str = ""    # 本次裁决成立的关键假设（逻辑止损触发器）
    stop_loss_hint: str = ""    # 价格止损位或逻辑止损条件


class AgentState(BaseModel):
    """LangGraph 全局状态"""
    date: str = ""
    stocks: list[StockAnalysis] = Field(default_factory=list)
    current_index: int = 0
    macro_summary: str = ""      # 宏观背景一行文字，注入 PM prompt
    exposure_posture: str = "NEW_ENTRY_ALLOWED"   # NEW_ENTRY_ALLOWED / REDUCE_ONLY / CASH_PRIORITY
    report_text: str = ""
    report_card: dict = Field(default_factory=dict)   # 飞书卡片 JSON
    errors: list[str] = Field(default_factory=list)
