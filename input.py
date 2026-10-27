import re
from urllib.parse import urlsplit


def parse_match_id(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[0-9]{1,15}", value):
        url = urlsplit(value)
        if url.scheme != "https" or url.netloc.lower() != "statlocker.gg":
            raise ValueError("请填写数字比赛 ID 或 https://statlocker.gg 的单局链接。")
        match = re.fullmatch(r"/profile/[0-9]+/matches/([0-9]{1,15})(?:/summary)?/?", url.path)
        if not match:
            raise ValueError("链接中没有有效比赛 ID，请复制 StatLocker 单局 summary 链接。")
        value = match.group(1)
    if int(value) <= 0:
        raise ValueError("比赛 ID 必须大于 0。")
    return str(int(value))
