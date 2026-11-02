"""DeadBot command reference and shared message-size handling."""

HELP = """📖 DeadBot 帮助

🔗 /绑定 AccountID或Steam数字主页 — 首次绑定
🏆 /战报 — 自己最新一局，也可加比赛ID或链接
🕘 /最近 — 最近5场比赛
📊 /分析 — 近期表现与练习建议

群聊先@机器人，私聊直接发送。
/帮助 战报 — 查看具体用法
/帮助 详细 — 更多功能
/帮助 管理 — 管理员维护说明"""

HELP_DETAILED = """📖 DeadBot 完整用法

比赛ID、群友称呼需换成实际信息。
也可用AccountID指定群友。

🔄 更新与绑定
/绑定 Account ID、SteamID64或Steam数字主页链接
无需群友名单；也支持名单称呼和[U:1:数字]。
暂不支持/id/自定义短链接；绑定不验证账号归属。
/解绑
仅解除自己的聊天身份绑定，保留比赛记录。
/同步
管理员更新全部群友的历史。
同步后自动补抓近期缺失详情，每轮最多25场。
/同步 群友称呼
管理员只更新这位群友。
/补全
管理员继续补齐近期标准、排位详情，已有比赛不重复抓取。
/补全 群友称呼
只补这位群友；接口暂未收录的比赛稍后再试。

🏆 最新与战报
/最新
按需刷新自己的比赛历史，2分钟内复用缓存。
接口异常时明确提示使用本地记录；进行中或未收录的比赛不会显示。
/最新 群友称呼
查看指定玩家的个人战报，也可填写Account ID。
/战报
不填参数查自己最新一局；/最新 保留兼容。
/最近 [群友称呼或Account ID]
按需刷新并列出最近5场，不批量抓取评分。
/战报 比赛ID
查询自己的原站评分，也支持StatLocker单局链接。
缓存24小时；同场合并、不同场排队；失败不切换本地评分。
/战报 比赛ID 刷新
重新获取这一局的数据。

📊 打法与英雄
/日报
今天全部群友的本地总结。
/日报 昨天
查看昨天，也可填写 YYYY-MM-DD。
/日报 群友称呼 昨天
查看这位群友指定日期的总结。
按中国时间的比赛结束日期统计，标准和排位分开。
/建议
兼容入口，输出与 /分析 相同。
/建议 群友称呼 排位
只看这位群友的排位练习方向。
结合自己的同英雄、同模式历史观察，数据不足时会说明。
六维用于复盘参考；发育与承伤高不等于打得好。
日报与建议只读本地数据，无需AI模型。
/分析
自己最近50场表现，并附个人练习建议；两部分分别注明数据范围。
/分析 排位
只看自己的排位。
/分析 群友称呼 标准
只看这位群友的标准比赛。
/英雄统计 群友称呼
查看这位群友的英雄胜率。
/英雄统计 群友称呼 英雄名 天数 模式
指定英雄、天数和游戏模式。
依次填写：称呼、英雄、天数、模式。
天数0查全部历史；模式填「全部」则不限制模式。
/群友画像 群友称呼
查看历史数量与最近比赛。
/群友名单
查看当前名单。
名单中的 Account ID 是 Steam 账号的短数字账号编号；[U:1:12345678] 取12345678，不能直接填写17位SteamID64。

🎖 成就与规则
/成就 比赛ID
自研成就评定已暂停。
/成就 比赛ID 刷新
保留旧指令，提示评定已暂停。
/成就规则
查看历史成就门槛（当前不评定）。
/成就规则 史诗
只看这一类，也可填稀有、少见、常见、整活。
/成就规则 灵魂收割机
查看这一项的条件。
/评分规则
查看 StatLocker 网页评分与缓存说明。

🔁 同义指令
/同步 = /同步历史 = /刷新
/最新 = /最新数据 = /最新比赛
/分析 = /打法分析
/建议 = /打法建议
/日报 = /每日总结
/补全 = /补全详情
/成就 = /成就详情
/战报 = /评分详情（旧命令兼容别名，输出相同）
/群友名单 = /名单
也支持 /群有名单、/查看群友名单
/帮助 = /指令
也支持 /deadbot帮助、/deadbothelp
这些帮助指令后加「详细」可打开本页。

💡 小提示
/最新、/分析、/建议 查自己前要先绑定。
新比赛没找到？先 /同步。
乱斗不参与历史分析，自研评分与成就评定已暂停。
其他插件与系统指令：/help"""


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
    '管理': '🛠️ 管理员维护\n/同步 [群友称呼或全部]：更新历史\n/补全 [群友称呼或全部]：补齐详情\n群友名单在插件配置中修改，可留空。',
}
