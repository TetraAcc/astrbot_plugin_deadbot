"""DeadBot command reference and shared message-size handling."""

HELP = """📖 DeadBot 指令

⚡ 常用
/最新 → 自己最新一局
/分析 → 最近50场表现
/建议 → 六维观察与练习方向
/日报 → 今天的群友总结
/同步 → 更新群友历史
/补全 → 补齐近期详情

👤 群友
/绑定 群友称呼 → 绑定自己
/群友名单 → 查看名单
/群友画像 群友称呼 → 最近战绩
/英雄统计 群友称呼 → 英雄胜率

🏆 战报
/战报 比赛ID → 战报与评分
/成就 比赛ID → 本局成就
/评分规则 → 评分怎么算
/成就规则 → 成就怎么拿

💡 比赛ID、群友称呼需换成实际信息。
先绑定，再用 /最新、/分析、/建议。
群聊先@机器人，私聊直接发。

/帮助 详细 → 完整用法与同义词
/指令 也能打开本页。"""

HELP_DETAILED = """📖 DeadBot 完整用法

比赛ID、群友称呼需换成实际信息。
也可用AccountID指定群友。

🔄 更新与绑定
/绑定 群友称呼
把自己的聊天账号绑定到名单群友。
/同步
更新全部群友的历史。
同步后自动补抓近期缺失详情，每轮最多25场。
/同步 群友称呼
只更新这位群友。
/补全
继续补齐近期标准、排位详情，已有比赛不重复抓取。
/补全 群友称呼
只补这位群友；接口暂未收录的比赛稍后再试。

🏆 最新与战报
/最新
查看自己在本地最新参与的一局。
/最新 群友称呼
查看这位群友的最新一局。
/战报 比赛ID
查询指定比赛，也支持StatLocker单局链接。
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
自己的输出、发育、承伤、辅助、前期、推进六维观察。
/建议 群友称呼 排位
只看这位群友的排位练习方向。
六维指数对比最近10场和此前40场的同英雄历史。
同英雄历史至少5场，近期至少5场能比较才展示指数。
50是自己的基准，不是MVP分数；发育与承伤高不等于打得好。
日报与建议只读本地数据，无需AI模型。
/分析
自己最近50场，标准和排位分别统计。
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

🎖 成就与规则
/成就 比赛ID
查看本局全部个人成就。
/成就 比赛ID 刷新
先更新比赛数据，再查看成就。
/成就规则
查看全部成就门槛。
/成就规则 史诗
只看这一类，也可填稀有、少见、常见、整活。
/成就规则 灵魂收割机
查看这一项的条件。
/评分规则
查看MVP评分规则。

🔁 同义指令
/同步 = /同步历史 = /刷新
/最新 = /最新数据 = /最新比赛
/分析 = /打法分析
/建议 = /打法建议
/日报 = /每日总结
/补全 = /补全详情
/成就 = /成就详情
/战报 = /评分详情
/群友名单 = /名单
也支持 /群有名单、/查看群友名单
/帮助 = /指令
也支持 /deadbot帮助、/deadbothelp
这些帮助指令后加「详细」可打开本页。

💡 小提示
/最新、/分析、/建议 查自己前要先绑定。
新比赛没找到？先 /同步。
乱斗不参与分析，成就不影响MVP。
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
