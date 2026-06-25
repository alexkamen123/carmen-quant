# src/finance_agent/notifications/cards.py
"""飞书卡片共享构件——折叠面板（schema 2.0）单一真相源。
日报/周报/月报/价值体检统一「主屏留结论、明细折叠」范式时复用，避免各处重复 schema。"""


def collapsible_panel(title: str, content: str, expanded: bool = False) -> dict:
    """schema 2.0 折叠面板：主屏只显 title，点开看 content（C 端瘦身减冗余）。"""
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "markdown", "content": title},
            "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined",
                     "size": "16px 16px"},
            "icon_position": "right",
            "icon_expanded_angle": 180,
        },
        "elements": [{"tag": "markdown", "content": content}],
    }
