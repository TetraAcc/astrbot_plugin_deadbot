import math


def parse_roster(entries):
    roster = {}
    for entry in entries:
        account, separator, name = str(entry).partition("=")
        account, name = account.strip(), name.strip()
        if (
            not separator
            or not account.isascii()
            or not account.isdigit()
            or not 0 < int(account) <= 4294967295
            or not name
        ):
            raise ValueError(
                "名单格式错误，请使用：AccountID=群友称呼（填写 Account ID，不是 SteamID64）。"
            )
        account = str(int(account))
        if account in roster:
            raise ValueError("名单中存在重复账号，请在插件配置中去重。")
        roster[account] = name.replace("\n", " ").replace("\r", " ")[:40]
    if not roster:
        raise ValueError("名单为空，请先在插件配置中添加群友。")
    return roster


def make_report(payload, roster):
    players = payload.get("players", [])
    if not isinstance(players, list) or any(not isinstance(p, dict) for p in players):
        return "比赛数据格式异常，暂不评选。"
    friends = [p for p in players if str(p.get("account_id")) in roster]
    lines = [f"DeadBot 战报 · {payload.get('matchId', '?')}"]
    if not friends:
        lines.append("本局没有名单内的群友，不评选。")
    else:
        teams = {}
        for player in friends:
            team = player.get("team")
            if not isinstance(team, (str, int)):
                return "比赛缺少有效队伍信息，暂不评选。"
            teams.setdefault(str(team), []).append(player)
        for team, members in teams.items():
            lines.append(f"\n队伍：{team}")
            scored = []
            seen = set()
            for player in members:
                account = str(player["account_id"])
                if account in seen:
                    return "比赛包含重复账号数据，暂不评选。"
                seen.add(account)
                score = player.get("mvpScore")
                valid = type(score) in (int, float) and math.isfinite(score)
                if valid:
                    scored.append((score, roster[account]))
                kda = "/".join(
                    str(player.get(k)) if player.get(k) is not None else "?"
                    for k in ("kills", "deaths", "assists")
                )
                score_text = f"{score:.4g}" if valid else "未就绪"
                lines.append(f"{roster[account]}：K/D/A {kda}｜单局评分 {score_text}")
            if payload.get("matchWasAbandoned") is True:
                lines.append("本局被标记为中途放弃，暂不评选。")
            elif len(members) == 1:
                lines.append("本队仅一名群友，只展示表现，不评选 MVP／背锅位。")
            elif len(scored) != len(members):
                lines.append("群友评分不完整，暂不评选。")
            else:
                scored.sort(reverse=True)
                top, bottom = scored[0][0], scored[-1][0]
                if math.isclose(top, bottom, rel_tol=1e-6, abs_tol=1e-9):
                    lines.append("群友评分相同，本局不区分 MVP／背锅候选。")
                else:
                    winners = [
                        name
                        for score, name in scored
                        if math.isclose(score, top, rel_tol=1e-6, abs_tol=1e-9)
                    ]
                    losers = [
                        name
                        for score, name in scored
                        if math.isclose(score, bottom, rel_tol=1e-6, abs_tol=1e-9)
                    ]
                    lines.append("群友 MVP：" + "、".join(winners))
                    lines.append("背锅候选（群友最低分）：" + "、".join(losers))
                    lines.append(
                        "仅表示本局评分排序，不代表输局责任；明显分差标准待实测校准。"
                    )
    lines.append("\nPowered by Statlocker")
    return "\n".join(lines)
