"""Account input and adapter-scoped identity; no ownership verification."""
import re
from urllib.parse import urlsplit

STEAM_BASE = 76561197960265728


def parse_account(value):
    value = value.strip()
    if value.startswith("https://"):
        url = urlsplit(value)
        if url.netloc.lower() != "steamcommunity.com":
            raise ValueError("请使用 steamcommunity.com 的数字个人主页链接。")
        match = re.fullmatch(r"/profiles/([0-9]+)/?", url.path)
        if not match:
            raise ValueError("暂不解析 /id/ 自定义短链接，请使用 SteamID64、Account ID 或 /profiles/数字 链接。")
        value = match[1]
    match = re.fullmatch(r"\[U:1:([0-9]+)\]", value)
    if match:
        value = match[1]
    if not re.fullmatch(r"[0-9]{1,17}", value):
        raise ValueError("请填写 Account ID、SteamID64 或 Steam 数字个人主页链接。")
    number = int(value)
    if number > STEAM_BASE:
        number -= STEAM_BASE
    if not 0 < number <= 4294967295:
        raise ValueError("Steam 账号编号不在有效范围内。")
    return str(number)


def identity(event):
    platform = str(getattr(event, "get_platform_id", lambda: "")() or "")
    sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
    if not platform or not sender:
        raise ValueError("无法识别发送者，请在群聊或私聊中重试。")
    return platform, sender
