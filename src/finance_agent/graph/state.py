# src/finance_agent/graph/state.py
from __future__ import annotations
from pydantic import BaseModel, Field
from finance_agent.signals.models import TechnicalSignals


class NewsItem(BaseModel):
    title: str
    summary: str
    published: str


class StockAnalysis(BaseModel):
    """单只股票的完整分析结果（Pydantic 确保 Agent 间信息无衰减）"""
    ticker: str
    market: str
    signals: TechnicalSignals
    news: list[NewsItem] = Field(default_factory=list)

    # Agent 输出（由 LangGraph 节点填充）
    bull_thesis: str = ""       # 多方论点（DeepSeek）
    bear_thesis: str = ""       # 空方论点（DeepSeek）

    # 最终裁决（Claude）
    recommendation: str = ""    # "买入" | "持有" | "减仓" | "观望" | "卖出"
    confidence: str = ""        # "高" | "中" | "低"
    entry_hint: str = ""        # 进场/止损建议
    key_risk: str = ""          # 最大风险点
    one_line: str = ""          # 一句话结论（飞书卡片用）


class AgentState(BaseModel):
    """LangGraph 全局状态，贯穿整个工作流"""
    date: str = ""
    stocks: list[StockAnalysis] = Field(default_factory=list)
    # 当前正在处理的 ticker 索引（用于节点间传递进度）
    current_index: int = 0
    # 最终报告文本（发飞书用）
    report_text: str = ""
    # 错误记录
    errors: list[str] = Field(default_factory=list)
