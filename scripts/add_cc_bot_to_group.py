#!/usr/bin/env python3
"""
将 Claude Code 飞书 bot (cli_aa9c839de1f95cdd) 拉入"粥卡门持仓分析"群，
并更新 lark-channel workspaces.json 映射。
全程本地运行，凭据不出机器。
"""
import json, subprocess, sys, os
from pathlib import Path
import urllib.request, urllib.error

APP_ID   = "cli_aa9c839de1f95cdd"
CHAT_ID  = "oc_25e108f486a9cf3a7eeb051a1f1b6fc0"
CHAT_CWD = "/Users/zhouyihao/Projects/personal/finance-agent"

SECRETS_GETTER = Path.home() / ".lark-channel" / "secrets-getter"
WORKSPACES     = Path.home() / ".lark-channel" / "workspaces.json"

FEISHU_BASE = "https://open.feishu.cn/open-apis"

# ── 代理（跟随 shell 环境变量）─────────────────────────────────────────────
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
if PROXY:
    import urllib.request as _ur
    _ur.install_opener(_ur.build_opener(_ur.ProxyHandler({"https": PROXY, "http": PROXY})))


def feishu_post(path: str, body: dict, token: str | None = None) -> dict:
    url = FEISHU_BASE + path
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def get_app_secret() -> str:
    """从 lark-channel bridge 获取 app secret。"""
    request_json = json.dumps({
        "protocolVersion": 1,
        "keys": [f"app-{APP_ID}"]
    }).encode()
    result = subprocess.run(
        [str(SECRETS_GETTER)],
        input=request_json,
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        print(f"[ERROR] secrets-getter 退出码 {result.returncode}")
        print(result.stderr.decode())
        sys.exit(1)
    resp = json.loads(result.stdout)
    values = resp.get("values", {})
    if not values:
        print("[ERROR] secrets-getter 返回空 values，请检查 key 格式")
        print("完整响应：", resp)
        sys.exit(1)
    secret = list(values.values())[0]
    print(f"[OK] 已从 bridge 获取 app secret（长度 {len(secret)} 字符）")
    return secret


def get_app_token(secret: str) -> str:
    """用 app_id + secret 换取 app_access_token。"""
    resp = feishu_post("/auth/v3/app_access_token/internal", {
        "app_id": APP_ID,
        "app_secret": secret,
    })
    if resp.get("code") != 0:
        print(f"[ERROR] 获取 app token 失败：{resp}")
        sys.exit(1)
    token = resp["app_access_token"]
    print(f"[OK] 已获取 app_access_token（有效期 {resp.get('expire',0)//60} 分钟）")
    return token


def add_bot_to_chat(token: str) -> None:
    """将 CC bot 自身拉入目标群。"""
    resp = feishu_post(
        f"/im/v1/chats/{CHAT_ID}/members?member_id_type=app_id",
        {"id_list": [APP_ID]},
        token=token,
    )
    code = resp.get("code", -1)
    if code == 0:
        print(f"[OK] bot 已成功加入群 {CHAT_ID}")
    elif code == 232012:
        print(f"[INFO] bot 已在群内，无需重复加入（code 232012）")
    else:
        print(f"[ERROR] 加入群失败：{resp}")
        sys.exit(1)


def update_workspaces() -> None:
    """在 lark-channel workspaces.json 中添加新群映射。"""
    ws = json.loads(WORKSPACES.read_text()) if WORKSPACES.exists() else {}
    chats = ws.setdefault("chats", {})
    if CHAT_ID in chats:
        print(f"[INFO] workspaces.json 已有该群映射，跳过")
    else:
        chats[CHAT_ID] = {"cwd": CHAT_CWD}
        named = ws.setdefault("named", {})
        named.setdefault("karmen", CHAT_CWD)   # 快捷名 karmen
        WORKSPACES.write_text(json.dumps(ws, ensure_ascii=False, indent=2))
        print(f"[OK] workspaces.json 已更新：{CHAT_ID} → {CHAT_CWD}")


if __name__ == "__main__":
    print("=== Claude Code bot → 粥卡门持仓分析 群 ===\n")
    secret = get_app_secret()
    token  = get_app_token(secret)
    add_bot_to_chat(token)
    update_workspaces()
    print("\n完成！在飞书群里 @ Claude Code 即可使用。")
