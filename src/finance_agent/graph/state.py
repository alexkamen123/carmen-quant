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
    fundamental_view: str = ""                 # Claude 的基本面一段话判断


class StockAnalysis(BaseModel):
    """单只股票的完整分析结果"""
    ticker: str
    market: str
    signals: TechnicalSignals
    news: list[NewsItem] = Field(default_factory=list)
    earnings: EarningsSummary = Field(default_factory=EarningsSummary)

    # Technical debate (DeepSeek)
    bull_thesis: str = ""
    bear_thesis: str = ""

    # Final decision (Claude)
    recommendation: str = ""
    confidence: str = ""
    entry_hint: str = ""
    key_risk: str = ""
    one_line: str = ""


class AgentState(BaseModel):
    """LangGraph 全局状态"""
    date: str = ""
    stocks: list[StockAnalysis] = Field(default_factory=list)
    current_index: int = 0
    macro_summary: str = ""      # 宏观背景一行文字，注入 PM prompt
    report_text: str = ""
    report_card: dict = Field(default_factory=dict)   # 飞书卡片 JSON
    errors: list[str] = Field(default_factory=list)
