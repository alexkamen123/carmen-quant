"""_clip_md：日报卡 📈/🐂/🐻 摘要的截断 + Markdown 加粗自愈。

06-15 bug：截断刚好落在 **加粗** 中间时，闭合符被砍掉，
飞书把残留的裸 * 当普通星号渲染（如 `类现金工具*…`）。
"""
from finance_agent.graph.workflow import _clip_md


def test_short_text_untouched():
    assert _clip_md("短文本", limit=45) == "短文本"


def test_plain_truncation_adds_ellipsis():
    out = _clip_md("a" * 50, limit=10)
    assert out == "a" * 10 + "…"


def test_cut_inside_bold_repairs_closing():
    # 砍在 **类现金工具** 的闭合符之前 → 应补回 ** 再加省略号
    text = "本质是**类现金工具**信用风险几乎为零"
    out = _clip_md(text, limit=len("本质是**类现金工具"))
    assert out == "本质是**类现金工具**…"
    assert out.count("**") % 2 == 0  # 加粗成对，飞书能正常渲染


def test_dangling_single_star_stripped():
    # 砍点正好落在闭合 ** 的中间，残留一个裸 *
    text = "本质是**类现金工具**xyz"
    out = _clip_md(text, limit=len("本质是**类现金工具*"))
    assert "工具*…" not in out          # 不再漏裸星号
    assert out.count("**") % 2 == 0


def test_balanced_bold_not_overclosed():
    # 截断点之前加粗已成对闭合 → 不应再多补 **
    text = "**重点**之后还有很长很长很长很长很长很长的尾巴内容"
    out = _clip_md(text, limit=len("**重点**之后还有"))
    assert out == "**重点**之后还有…"
    assert out.count("**") == 2
