# src/finance_agent/notifications/glossary.py
"""
名词解释模块：检测报告内容中出现的金融术语，自动生成「本期术语说明」飞书卡片元素。
只展示本期报告里实际出现的词条，最多 6 条，避免无关噪音。
"""

# 术语库：key = 检测关键词，value = (术语名, 白话解释)
_GLOSSARY: dict[str, tuple[str, str]] = {
    "RSI":       ("RSI（超买超卖指标）", "0-100 的数字，>70 说明涨太多了可能要回调，<30 说明跌太多了可能要反弹"),
    "MACD":      ("MACD（趋势动能）",    "判断上涨或下跌的力度是否在增强，金叉看涨，死叉看跌"),
    "布林带":    ("布林带（波动范围）",   "价格的正常波动区间，碰到上轨说明偏贵，碰到下轨说明偏便宜"),
    "均线":      ("均线（平均价格线）",   "过去 N 天的平均买入价，价格在均线上方通常是上涨趋势"),
    "浮盈":      ("浮盈 / 浮亏",          "按当前价格算的未实现盈亏，卖出才算真的赚到手"),
    "回撤":      ("回撤（从高点跌了多少）","比如「回撤 15%」意思是从最高价跌了 15%"),
    "对冲":      ("对冲（降风险操作）",   "买一些跟主仓反向波动的资产，主仓跌了它涨，整体亏损变小"),
    "仓位":      ("仓位（投了多少比例）",  "某只股票占你总资产的百分比，5% 仓位就是每 100 元投了 5 元"),
    "多头":      ("多头 / 空方",           "多头预期价格上涨，空方预期价格下跌"),
    "空头":      ("多头 / 空方",           "多头预期价格上涨，空方预期价格下跌"),
    "持仓逻辑":  ("持仓逻辑（Thesis）",   "当初买入这只股票的核心理由，如果这个理由不再成立就要考虑卖出"),
    "安全边际":  ("安全边际",              "当前价格比合理估值便宜多少，越大越安全，防止估值算错"),
    "ETF":       ("ETF（指数基金）",       "一篮子股票打包成一个可交易的产品，分散风险，费用低"),
    "宽基":      ("宽基 ETF",              "覆盖大量股票的指数基金，如标普500（VOO）、纳斯达克100（QQQM）"),
    "定投":      ("定投（定期定额投资）",  "每月固定时间投固定金额，无论涨跌都买，均摊成本"),
    "减仓":      ("加仓 / 减仓",           "增加/减少某只股票的持股数量"),
    "加仓":      ("加仓 / 减仓",           "增加/减少某只股票的持股数量"),
    "止损":      ("止损",                  "提前设好一个价格，跌到那个价就卖出，防止亏损无限扩大"),
    "集中度":    ("持仓集中度",            "单一行业占总资产的比例，比例越高风险越集中"),
    "候选标的":  ("候选标的",              "经过初步筛选、值得进一步研究的股票名单"),
}

# 去重：多个 key 可能映射到同一术语名，只展示一次
_SHOWN: set[str] = set()


def detect_terms(text: str, max_terms: int = 6) -> list[tuple[str, str]]:
    """
    在文本中检测出现的术语关键词，返回 [(术语名, 解释), ...] 列表，最多 max_terms 条。
    同一术语名只出现一次（去重）。
    """
    _SHOWN.clear()
    results: list[tuple[str, str]] = []
    for keyword, (term_name, explanation) in _GLOSSARY.items():
        if keyword in text and term_name not in _SHOWN:
            _SHOWN.add(term_name)
            results.append((term_name, explanation))
        if len(results) >= max_terms:
            break
    return results


def build_glossary_element(text: str, max_terms: int = 5) -> dict | None:
    """
    检测文本中的术语，构建飞书卡片 note 元素。
    若无匹配术语则返回 None。
    """
    terms = detect_terms(text, max_terms=max_terms)
    if not terms:
        return None

    lines = ["📖 本期术语说明"]
    for term_name, explanation in terms:
        lines.append(f"· {term_name}：{explanation}")

    return {
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "\n".join(lines)}],
    }
