"""DeadBot command reference and shared message-size handling."""

HELP = """📖 DeadBot 帮助手册

🎮 开黑核心
🔗 /绑定 <AccountID或Steam主页> — 首次绑定身份
🏆 /战报 [比赛ID] — 默认查自己最新一局（高清卡片）
🕘 /最近 [群友] — 查最近 5 场对局速览（高清卡片）
📊 /分析 [群友] [模式] — 近 50 场表现与打法体检

📈 战况与英雄
📰 /日报 [昨天/日期] — 当日或指定日期战况汇总
🦸 /英雄 [群友] [英雄名] — 常用英雄与胜率统计

群聊先@机器人，私聊直接发送。
/帮助 战报 — 查看战报用法与刷新规则
/帮助 详细 — 查看完整指令与参数
/帮助 管理 — 管理员维护说明"""

HELP_DETAILED = """📖 DeadBot 完整指令指南

比赛ID、群友称呼需换成实际信息。
也可用 Account ID 指定群友。

🎮 开黑核心指令
/绑定 <Account ID / SteamID64 / Steam数字个人主页链接>
将聊天账号与游戏账号绑定。无需名单，支持数字主页自动换算。
/解绑
解除本人的聊天身份绑定，历史比赛数据完整保留。
/战报 [比赛ID] [刷新]
不带参数默认查询自己最新一局战报（带高颜值暗色卡片）。
带比赛ID可查指定对局；加「刷新」跳过24小时缓存重新采集。
（旧指令 /最新 保留兼容）
/最近 [群友称呼或Account ID]
生成最近 5 场对局的高清战绩速览卡片，包含胜负、KDA、时长与对局ID。
默认查自己，也可查指定群友。
/分析 [群友称呼或Account ID] [排位/标准/全部]
基于最近 50 场有效对局，综合分析打法偏好与针对性练习建议。
默认查自己全部对局；（旧指令 /建议 保留兼容）。

📈 战况与英雄统计
/日报 [昨天/日期]
汇总本地记录的单日战况与胜负概览；支持「昨天」或具体日期（YYYY-MM-DD）。
/英雄 [群友称呼] [英雄名] [天数] [模式]
查询玩家常用英雄池、出场场次、胜率及分钟级对线数据。
默认查自己；（旧指令 /英雄统计、/群友画像 保留兼容）。
/群友名单
查看后台配置的群友名单（名单为可选功能）。

🛠️ 管理员维护指令
/同步 [群友称呼或全部]
从 Deadlock API 刷新玩家比赛历史记录。
/补全 [群友称呼或全部]
补齐近期未抓取详情的比赛战斗元数据（每轮最多25场）。
/评分规则
查看 StatLocker 原站评分与缓存机制说明。

💡 使用小贴士
1. 查自己前请先发送 /绑定 完成一次性身份关联。
2. 在 /最近 卡片中查得比赛ID后，复制 ID 发送 /战报 <ID> 可深入复盘。
3. 比赛刚打完通常有数分钟入库延迟，若未刷新到最新场次，可稍后重试。"""


def text_pages(text, title, markdown=False):
    """Split reports and references into conservative QQ message sizes.

    Args:
        text: Full rendered text.
        title: Continuation-page label.
        markdown: WebUI supports a single full Markdown message.

    Returns:
        A list of messages, reserving space for continuation labels.
    """
    if markdown:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines():
        if current and len((current + "\n" + line).encode("utf-8")) > 3500:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    return [
        (f"{title}续页 {index + 1}/{len(chunks)}\n" if len(chunks) > 1 else "") + chunk
        for index, chunk in enumerate(chunks)
    ]


HELP_TOPICS = {
    '战报': '🏆 /战报：自己最新一局（需绑定）\n/战报 比赛ID或StatLocker链接：指定比赛\n/战报 比赛ID 刷新：重新采集\n/最新 [群友称呼或Account ID]：查询最新比赛。',
    '最近': '🕘 /最近 [群友称呼或Account ID]：最近5场已收录比赛；默认自己。复制比赛ID后用 /战报 查询。',
    '绑定': '🔗 /绑定 Account ID、SteamID64或Steam数字主页\n/解绑：解除本人绑定，保留比赛记录。',
    '分析': '📊 /分析 [群友称呼或Account ID] [标准或排位或全部]\n合并近期表现与练习建议，使用已存数据；/建议 保留兼容。',
    '英雄': '🦸 /英雄 [群友称呼或Account ID] [英雄名] [天数] [模式]\n常用英雄池与胜率表现；默认查自己。兼容 /英雄统计、/群友画像、/常用英雄。',
    '管理': '🛠️ 管理员维护\n/同步 [群友称呼或全部]：更新历史\n/补全 [群友称呼或全部]：补齐详情\n群友名单在插件配置中修改，可留空。',
}
