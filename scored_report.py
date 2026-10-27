from .achievements import earned_achievements, relative_achievements
from .deadlock import display, hero_name

CATEGORIES = {
    "early": "前期",
    "late": "输出",
    "push": "推进",
    "tank": "承伤",
    "support": "辅助",
}
SEPARATOR = "➖" * 11


def grade(value):
    """Return the display grade for a rounded contribution score.

    Args:
        value: Contribution score from zero to one hundred.

    Returns:
        A grade label independent of the scoring calculation.
    """
    for threshold, label in (
        (90, "S"),
        (80, "A+"),
        (70, "A"),
        (60, "B"),
        (50, "C"),
        (40, "D"),
    ):
        if round(value, 1) >= threshold:
            return label
    return "E"


def make_scored_report(payload, roster, detailed=False, markdown=False):
    """Render compact match summaries and player blocks with visual anchors.

    Args:
        payload: Normalized match and cached scoring result.
        roster: Account IDs mapped to current display names.
        detailed: Retained for command compatibility; both commands use this layout.
        markdown: Enable emphasis for clients that render Markdown.

    Returns:
        Chinese report containing only roster players, with positive optional stats.
    """
    marker = "**" if markdown else ""
    scoring = payload["scoring"]
    friends = [p for p in payload["players"] if p["account_id"] in roster]
    scored = {str(p["account_id"]): p for p in scoring.get("players", [])}
    teams = {team["team"]: team for team in scoring.get("teams", [])}
    groups = {}
    for p in friends:
        groups.setdefault(p.get("team"), []).append(p)
    lines = [f"🏆 {marker}DeadBot 战报 · {payload['matchId']}{marker}"]
    summary = []
    duration = payload.get("duration_s")
    if duration is not None:
        summary.append(f"⏱️ 时长：{duration // 60:02d}:{duration % 60:02d}")
    summary.append(f"👥 群友：{len(friends)}/{len(roster)}")
    outcomes = {p.get("won") for p in friends}
    if len(outcomes) == 1:
        won = next(iter(outcomes))
        result = "胜利" if won is True else "失败" if won is False else "未知"
        summary.append(f"🏁 结果：{result}")
    lines.append(" | ".join(summary))
    if not friends:
        return "\n".join(lines + ["本局没有名单内群友。", "数据来源：Deadlock API"])
    if len(outcomes) > 1:
        for group in groups.values():
            won = group[0].get("won")
            result = "胜利" if won is True else "失败" if won is False else "未知"
            lines.append(
                "🏁 结果："
                + "、".join(roster[p["account_id"]] for p in group)
                + f" ({result})"
            )
    mvp_accounts = set()
    for team_id, group in groups.items():
        players = [scored.get(p["account_id"], {}) for p in group]
        if (
            not teams.get(team_id, {}).get("eligible")
            or any(p.get("score") is None for p in players)
            or len(players) < 2
        ):
            continue
        values = [round(p["score"], 1) for p in players]
        if min(values) == max(values):
            continue
        winners = [
            str(p["account_id"]) for p in players if round(p["score"], 1) == max(values)
        ]
        mvp_accounts.update(winners)
        top = "、".join(roster[a] for a in winners)
        bottom = "、".join(
            roster[str(p["account_id"])]
            for p in players
            if round(p["score"], 1) == min(values)
        )
        lines.append(
            f"🏅 MVP：{top} ({max(values):.1f}/{grade(max(values))}) | 📉 最低分：{bottom} ({min(values):.1f}/{grade(min(values))})"
        )
    relative = relative_achievements(payload, roster)
    for p in friends:
        lines.extend(["", SEPARATOR, ""])
        score = scored.get(p["account_id"], {})
        metrics = score.get("metrics", {})
        fun = score.get("fun", {})
        value = score.get("score")
        label = f"{value:.1f}/{grade(value)}" if value is not None else "未知"
        badge = " 🏅" if p["account_id"] in mvp_accounts else ""
        lines.append(
            f"👤 {marker}{roster[p['account_id']]}{marker} 🕹️ {hero_name(p['hero_id'])} | 综合评分：{marker}{label}{marker}{badge}"
        )
        kda = "/".join(
            str(p[k]) if p[k] is not None else "?"
            for k in ("kills", "deaths", "assists")
        )
        kp = fun.get("kill_participation")
        participation = f"{kp:.0%}" if kp is not None else "未知"
        lines.append(
            f"⚔️ {marker}{kda}{marker} (参团 {participation} | 前期助攻 {display(metrics.get('early_assists'))}) | 💰 灵魂 {marker}{display(p['net_worth'])}{marker}"
        )
        achievements = relative.get(p["account_id"], []) + earned_achievements(
            p, score, duration
        )
        if achievements:
            lines.append(
                "🏆 本场成就：" + " | ".join(a["name"] for a in achievements[:3])
            )
        lines.append("")
        if value is not None:
            lines.append(
                "📊 雷达表现："
                + " / ".join(
                    f"{name} {score['category_scores'][key]:.1f}"
                    for key, name in CATEGORIES.items()
                )
            )
            lines.append(
                f"📉 额外增减：死亡 -{score['death_penalty']:.1f} / 经济效率 +{score['economy_bonus']:.1f}"
            )
        lines.append(
            f"💥 伤害输出：{marker}{display(p.get('player_damage'))}{marker} (前期 {display(metrics.get('early_damage'))} / 后期 {display(metrics.get('late_damage'))})"
        )
        shot, head = fun.get("hero_accuracy"), fun.get("hero_headshot_rate")
        accuracy = f"{shot:.1%}" if shot is not None else "未知"
        headshots = f"{head:.1%}" if head is not None else "未知"
        lines.append(f"🎯 射击准度：命中 {accuracy} / 爆头 {headshots}")
        defence = []
        support = []
        for key, name, output in (
            ("damage_mitigated", "减免", defence),
            ("damage_absorbed", "吸收", defence),
            ("teammate_healing", "队友治疗", support),
            ("teammate_barriering", "队友护盾", support),
            ("heal_prevented", "阻止治疗", support),
        ):
            amount = p.get(key) if p.get(key) is not None else metrics.get(key)
            if type(amount) in (int, float) and amount > 0:
                output.append(f"{name} {display(amount)}")
        taken = p.get("player_damage_taken")
        taken = taken if taken is not None else metrics.get("player_damage_taken")
        suffix = " (" + " / ".join(defence) + ")" if defence else ""
        lines.append(f"🛡️ 承伤减免：{display(taken)}{suffix}")
        building = p.get("boss_damage")
        building = building if building is not None else metrics.get("boss_damage")
        creeps = p.get("creep_kills")
        creeps = creeps if creeps is not None else metrics.get("creep_kills")
        lines.append(
            f"🚩 运营推进：建筑伤害 {display(building)} | 清兵 {display(creeps)}"
        )
        if support:
            lines.append("💚 辅助贡献：" + " | ".join(support))
        lines.append(
            f"✨ 趣味高光：招架 {display(fun.get('parry_success'))}成/{display(fun.get('parry_miss'))}败 | 反补 {display(fun.get('denies', p.get('denies')))}"
        )
    lines.extend(["", SEPARATOR, "数据来源：Deadlock API"])
    return "\n".join(lines)
