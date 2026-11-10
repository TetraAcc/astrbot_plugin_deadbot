"""Pillow dark-theme war report card renderer for Deadlock matches.

Zero external browser dependency; graceful fallback when Pillow is absent.
Renders high-density, high-resolution (Retina 2x) cards optimized for mobile screens.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
import time
from functools import lru_cache
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None  # type: ignore

try:
    from .statlocker_service import (
        CATEGORY_NAMES,
        hero_name,
        display_num,
        farm_amount,
        format_highlight,
        COMPONENT_ORDER,
        player_components,
    )
except ImportError:
    from statlocker_service import (
        CATEGORY_NAMES,
        hero_name,
        display_num,
        farm_amount,
        format_highlight,
        COMPONENT_ORDER,
        player_components,
    )

# ─── Canvas & Scale Configuration ─────────────────────────
# Narrow layout (560px base) rendered at 2x Retina scale (1120px)
# Ensures perfect 1080p full-screen clarity and crisp QQ chat bubble previews.

SCALE  = 2
BASE_W = 560
W      = BASE_W * SCALE    # 1120 px
PX     = 18 * SCALE        # Outer horizontal padding: 36 px
PY     = 16 * SCALE        # Outer vertical padding: 32 px
R      = 8 * SCALE         # Card corner radius: 16 px
IP     = 14 * SCALE        # Card inner padding: 28 px
ACCENT_W = 4               # Left accent bar base width (px before scale)

BG      = (15, 23, 42)      # Slate 900
CARD_BG = (30, 41, 59)      # Slate 800
CARD_BD = (51, 65, 85)      # Slate 700
DIV     = (40, 53, 72)      # Subtle divider
T1      = (248, 250, 252)   # Slate 50 (primary text)
T2      = (203, 213, 225)   # Slate 300 (secondary text)
T3      = (148, 163, 184)   # Slate 400 (labels / captions)
T_HL    = (170, 185, 205)   # Slightly brighter than T3 for highlights

WIN_C   = (34, 197, 94)     # Green 500
LOSE_C  = (239, 68, 68)     # Red 500

GRADE_CLR = {
    'S+': (255, 215, 0), 'S': (255, 215, 0), 'S-': (245, 208, 50),
    'A+': (34, 197, 94), 'A': (34, 197, 94), 'A-': (74, 222, 128),
    'B+': (59, 130, 246), 'B': (59, 130, 246), 'B-': (96, 165, 250),
    'C+': (226, 232, 240), 'C': (203, 213, 225), 'C-': (203, 213, 225),
    'D+': (249, 115, 22), 'D': (249, 115, 22), 'D-': (251, 146, 60),
    'E+': (239, 68, 68), 'E': (239, 68, 68), 'E-': (248, 113, 113),
    'F': (185, 28, 28),
}

CAT_CLR = {
    'DAMAGE DEALER': (239, 68, 68), 'TANK': (59, 130, 246),
    'SUPPORT': (34, 197, 94), 'EARLY IMPACT': (245, 158, 11),
    'PUSHER': (168, 85, 247), 'TEAM FARM BONUS': (16, 185, 129),
    'DEATHS': (148, 163, 184),
}

CAT_SHORT = {
    'DAMAGE DEALER': '输出',
    'TANK': '承伤',
    'SUPPORT': '辅助',
    'PUSHER': '推进',
    'EARLY IMPACT': '前期',
    'TEAM FARM BONUS': '经济',
    'DEATHS': '死亡',
}


# ─── Font Discovery ───────────────────────────────────────

_FONT = Path(__file__).resolve().parent / "assets" / "font.ttf"


@lru_cache(maxsize=64)
def _F(sz: int, bold: bool = False):
    """Load CJK font with cross-platform fallback."""
    if _FONT.exists():
        try:
            return ImageFont.truetype(str(_FONT), sz)
        except Exception:
            pass
    try:
        sibling = Path(__file__).resolve().parent.parent / "astrbot_plugin_parser" / "core" / "resources" / "HYSongYunLangHeiW-1.ttf"
        if sibling.exists():
            return ImageFont.truetype(str(sibling), sz)
    except Exception:
        pass
    for p, i in [
        ("C:/Windows/Fonts/" + ("msyhbd.ttc" if bold else "msyh.ttc"), 0),
        ("C:/Windows/Fonts/simhei.ttf", 0),
        ("/System/Library/Fonts/PingFang.ttc", 0),
        ("/usr/share/fonts/noto-cjk/NotoSansCJK-" + ("Bold" if bold else "Regular") + ".ttc", 0),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-" + ("Bold" if bold else "Regular") + ".ttc", 0),
        ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
        ("/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc", 0),
    ]:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, sz, index=i)
            except Exception:
                continue
    return ImageFont.load_default()


# ─── Text Helpers ─────────────────────────────────────────

_EMOJI_RE = re.compile(
    r"[\U00010000-\U0010ffff"
    r"\u2600-\u26ff\u2700-\u27bf\u2300-\u23ff"
    r"\u2b50\u2b55\u200d\ufe0f\ufe0e"
    r"\u2190-\u21ff\u25a0-\u25ff\u2b00-\u2bff]"
)


def _strip_emoji(s: str) -> str:
    if not s:
        return ""
    clean = _EMOJI_RE.sub("", s)
    return re.sub(r"^[ ·:：—\s-]+", "", clean).strip()


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines = []
    for para in text.splitlines():
        cur = ""
        for ch in para:
            test = cur + ch
            if draw.textlength(test, font=font) <= max_w:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
    return lines or [""]


def _n(v) -> str:
    if type(v) in (int, float) and math.isfinite(v) and v >= 0:
        return f"{v:,.0f}" if int(v) == v else f"{v:,.1f}"
    return "—"


# ─── Height Calculator ────────────────────────────────────

def _player_h(md, acc: str, sl: dict, api: dict, data, info: dict, fhl, max_tw: int) -> int:
    """Calculate scaled pixel height for one player card with high density."""
    ps, pa = sl.get(acc, {}), api.get(acc, {})
    hl = data.get('highlights', {}).get(acc) if data else None
    comp = player_components(ps, hl) if ps else {}
    facts = [text for f in (hl.get('facts', []) if hl else []) if (text := format_highlight(f))]
    farm = data.get('farm_impact', {}).get(acc) if data else None

    # Base card: top pad (10) + hero row (22) + score & kda row (28) + divider (8)
    h = 10 + 22 + 28 + 8

    # Symmetrical 4-row combat stats
    if pa:
        h += 4 * 18 + 4

    # Single-line 6/7-dimension breakdown
    if comp:
        h += 6 + 17 + 16
    elif ps and data:
        h += 6 + 17

    # Highlights (only when genuine highlights exist)
    if facts:
        h += 6 + 17
        for text in facts:
            clean_text = _strip_emoji(text)
            lines = _wrap(md, "◆ " + clean_text, fhl, max_tw)
            h += 15 * len(lines) + 2

    # Farm impact
    if farm:
        h += 6 + 17

    h += 10  # bottom pad
    return int(h * SCALE)


# ─── Public API ────────────────────────────────────────────

def render_report_image(data, account, raw_match=None, roster=None, stat_error=None):
    """Render a Deadlock war report as a dark-theme PNG card.

    Returns:
        File path to the generated PNG, or None if rendering fails.
    """
    if Image is None:
        return None
    try:
        return _render(data, account, raw_match, roster, stat_error)
    except Exception:
        return None


# ─── Implementation ────────────────────────────────────────

def _render(data, account, raw_match, roster, stat_error):
    account = str(account)
    roster = roster or {}

    info = raw_match.get("match_info", {}) if isinstance(raw_match, dict) else {}
    sl = {str(p["account_id"]): p for p in data["players"]} if data and "players" in data else {}
    mid = str(info.get("match_id", data.get("match_id", "?") if data else "?"))

    if not isinstance(info, dict) or (data and str(info.get("match_id")) != str(data["match_id"])):
        info = {}
    api = {str(p["account_id"]): p for p in info.get("players", [])
           if isinstance(p, dict) and p.get("account_id") is not None}

    if sl and account not in sl:
        return None
    if not sl and api and account not in api:
        return None
    if sl:
        targets = [account] + [a for a in sl if a != account and a in roster]
    elif api:
        targets = [account] + [a for a in api if a != account and a in roster]
    else:
        return None

    duration = info.get("duration_s")

    # Scaled font definitions for 2x Retina rendering
    S = SCALE
    ft   = _F(int(20 * S), True)
    fs   = _F(int(12 * S))
    fh   = _F(int(16 * S), True)
    fn   = _F(int(13 * S))
    fsc  = _F(int(20 * S), True)   # Score number
    fg   = _F(int(20 * S), True)   # Grade letter (same size as score)
    fr   = _F(int(13 * S))
    fl   = _F(int(12 * S))
    fsec = _F(int(13 * S), True)
    fhl  = _F(int(11.5 * S))
    fft  = _F(int(11 * S))

    _m = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    cx = PX
    cw = W - 2 * PX
    mx = cx + cw // 2
    max_tw = cw - 2 * IP - int(16 * S)

    # Calculate heights
    p_heights = [_player_h(_m, a, sl, api, data, info, fhl, max_tw) for a in targets]

    sub_parts = []
    if type(duration) is int and duration >= 0:
        sub_parts.append(f"时长 {duration // 60:02d}:{duration % 60:02d}")
    if len(targets) > 1:
        sub_parts.append(f"群友 {len(targets)}人")
    if stat_error:
        sub_parts.append(str(stat_error))

    hdr_h = int((30 + (16 if sub_parts else 0) + 10) * S)
    total_h = PY + hdr_h + sum(h + int(10 * S) for h in p_heights) + int(24 * S) + PY

    # Canvas
    img = Image.new("RGB", (W, total_h), BG)
    draw = ImageDraw.Draw(img)

    y = PY

    # Match outcome (single unified display in header)
    target_pa = api.get(account, {})
    t_team, winner = target_pa.get('team'), info.get('winning_team')
    match_win = (t_team == winner) if t_team in (0, 1) and winner in (0, 1) else None

    # Header
    title = f"StatLocker 战报 · {mid}" if data else f"Deadlock 战报 · {mid}"
    draw.text((cx, y + int(2 * S)), title, font=ft, fill=T1)

    if match_win is not None:
        lbl = "胜 利" if match_win else "失 败"
        clr = WIN_C if match_win else LOSE_C
        bg_clr = (20, 50, 35) if match_win else (60, 25, 25)
        lw = draw.textlength(lbl, font=fs)
        pad_h = int(10 * S)  # horizontal padding
        badge_h = int(20 * S)
        bx2 = cx + cw
        bx1 = bx2 - lw - pad_h * 2
        by1 = y + int(4 * S)
        by2 = by1 + badge_h
        draw.rounded_rectangle([(bx1, by1), (bx2, by2)],
                               radius=int(4 * S), fill=bg_clr, outline=clr, width=max(1, S))
        # True center: use bbox for precise vertical centering
        bbox = draw.textbbox((0, 0), lbl, font=fs)
        th = bbox[3] - bbox[1]
        tx = bx1 + (bx2 - bx1 - lw) // 2
        ty = by1 + (badge_h - th) // 2 - bbox[1]
        draw.text((tx, ty), lbl, font=fs, fill=clr)

    y += int(30 * S)
    if sub_parts:
        draw.text((cx, y), " · ".join(sub_parts), font=fs, fill=T3)
        y += int(16 * S)
    draw.line([(cx, y + int(2 * S)), (cx + cw, y + int(2 * S))], fill=DIV, width=max(1, S))
    y += int(10 * S)

    # Draw player cards
    for idx, acc in enumerate(targets):
        ps, pa = sl.get(acc, {}), api.get(acc, {})
        card_h = p_heights[idx]
        grade = ps.get('grade', '')
        accent = GRADE_CLR.get(grade, T3)

        # Card container with colored left accent bar
        draw.rounded_rectangle([(cx, y), (cx + cw, y + card_h)], radius=R,
                               fill=CARD_BG, outline=CARD_BD, width=max(1, S))
        bar_w = ACCENT_W * S
        bar_margin = int(6 * S)
        draw.rounded_rectangle(
            [(cx, y + bar_margin), (cx + bar_w, y + card_h - bar_margin)],
            radius=int(2 * S), fill=accent)
        cy = y + int(10 * S)

        # 1. Hero & Name (Left)
        name = str(roster.get(acc, ps.get('name', f"玩家 {acc}"))).replace("\n", " ")[:30]
        h_name = hero_name(pa['hero_id']) if pa.get('hero_id') else ps.get('hero', '未知英雄')

        draw.text((cx + IP + int(4 * S), cy), h_name, font=fh, fill=T1)
        hn_w = draw.textlength(h_name, font=fh)
        draw.text((cx + IP + int(4 * S) + hn_w + int(8 * S), cy + int(2 * S)), name, font=fn, fill=T2)
        cy += int(22 * S)

        # 2. Score & Rank (Left), KDA & Participation (Right) on SAME ROW
        if ps:
            sc_str = f"{ps['score']:.1f}"
            g_clr = GRADE_CLR.get(grade, T1)
            draw.text((cx + IP + int(4 * S), cy), sc_str, font=fsc, fill=g_clr)
            sw = draw.textlength(sc_str, font=fsc)
            draw.text((cx + IP + int(4 * S) + sw + int(6 * S), cy), grade, font=fg, fill=g_clr)
            gw = draw.textlength(grade, font=fg)
            rank = re.match(r"^\s*(\d+)", str(ps.get('rank', '')))
            rank_str = f"全场第 {rank[1]}/12" if rank else ""
            draw.text((cx + IP + int(4 * S) + sw + gw + int(12 * S), cy + int(8 * S)), rank_str, font=fl, fill=T3)
        else:
            draw.text((cx + IP + int(4 * S), cy + int(4 * S)), "评分暂未就绪", font=fn, fill=T3)

        if pa:
            k, d, a = (pa.get(x) for x in ('kills', 'deaths', 'assists'))
            if all(type(v) is int for v in (k, d, a)):
                teammates = [p for p in api.values() if p.get('team') == pa.get('team')]
                kills = [p.get('kills') for p in teammates]
                kp_str = ""
                if len(teammates) == 6 and sum(kills) > 0:
                    kp_str = f" · 参团 {(k + a) / sum(kills):.0%}"
                kda_text = f"{_n(k)} / {_n(d)} / {_n(a)}{kp_str}"
                kw = draw.textlength(kda_text, font=fn)
                draw.text((cx + cw - IP - kw, cy + int(6 * S)), kda_text, font=fn, fill=T1)
        cy += int(28 * S)

        # Divider
        draw.line([(cx + IP, cy), (cx + cw - IP, cy)], fill=DIV, width=max(1, S))
        cy += int(8 * S)

        # 3. Combat Stats Grid (4 Symmetrical Rows x 2 Balanced Columns)
        if pa:
            snaps = [v for v in (pa.get('stats') or [])
                     if isinstance(v, dict) and type(v.get('time_stamp_s')) in (int, float)]
            last = max(snaps, key=lambda v: v['time_stamp_s']) if snaps else {}

            mit = [last.get('damage_mitigated'), last.get('damage_absorbed')]
            mit_sum = sum(mit) if all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in mit) else 0

            heal = last.get('teammate_healing')
            barrier = last.get('teammate_barriering')
            has_heal = type(heal) in (int, float) and heal > 0
            has_barrier = type(barrier) in (int, float) and barrier > 0

            if has_heal and has_barrier:
                sup_str = f"{_n(heal)} / {_n(barrier)}"
            elif has_heal:
                sup_str = f"{_n(heal)}"
            elif has_barrier:
                sup_str = f"盾 {_n(barrier)}"
            else:
                sup_str = "—"

            left_x = cx + IP + int(4 * S)
            right_x = mx + int(2 * S)

            def draw_stat(x, y, label, val):
                draw.text((x, y), label, font=fl, fill=T3)
                lw = draw.textlength(label, font=fl)
                draw.text((x + lw, y), val, font=fr, fill=T1)

            # Row 1: 英雄伤害 vs 承受伤害
            draw_stat(left_x, cy, "英雄伤害 ", _n(last.get('player_damage')))
            draw_stat(right_x, cy, "承受伤害 ", _n(last.get('player_damage_taken')))
            cy += int(18 * S)

            # Row 2: 灵魂 vs 补刀 / 反补
            draw_stat(left_x, cy, "灵魂 ", _n(pa.get('net_worth')))
            draw_stat(right_x, cy, "补刀/反补 ", f"{_n(pa.get('last_hits'))} / {_n(pa.get('denies'))}")
            cy += int(18 * S)

            # Row 3: 建筑伤害 vs 击杀小兵
            draw_stat(left_x, cy, "建筑伤害 ", _n(last.get('boss_damage')))
            draw_stat(right_x, cy, "击杀小兵 ", _n(last.get('creep_kills')))
            cy += int(18 * S)

            # Row 4: 减免/吸收 vs 治疗/护盾
            draw_stat(left_x, cy, "减免/吸收 ", _n(mit_sum) if mit_sum > 0 else "—")
            draw_stat(right_x, cy, "治疗/护盾 ", sup_str)
            cy += int(18 * S) + int(4 * S)

        # 4. Six-Dimensional Score Breakdown (SINGLE ROW)
        hl = data.get('highlights', {}).get(acc) if data else None
        comp = player_components(ps, hl) if ps else {}
        if comp:
            draw.line([(cx + IP, cy), (cx + cw - IP, cy)], fill=DIV, width=max(1, S))
            cy += int(5 * S)
            draw.text((cx + IP + int(4 * S), cy), "评分构成", font=fsec, fill=T2)
            cy += int(17 * S)

            rx = cx + IP + int(4 * S)
            ordered_comp = sorted(
                comp.items(),
                key=lambda x: COMPONENT_ORDER.index(x[0]) if x[0] in COMPONENT_ORDER else 99
            )
            dot_r = int(4 * S)  # Larger dots for better visibility
            for cat, val in ordered_comp:
                short_name = CAT_SHORT.get(cat, CATEGORY_NAMES.get(cat, cat))
                clr = CAT_CLR.get(cat, T3)
                dot_y = cy + int(4 * S)
                draw.ellipse((rx, dot_y, rx + dot_r, dot_y + dot_r), fill=clr)
                rx += dot_r + int(3 * S)
                txt = f"{short_name} {val:+.1f}"
                draw.text((rx, cy), txt, font=fl, fill=T2)
                rx += draw.textlength(txt, font=fl) + int(7 * S)
            cy += int(16 * S)
        elif ps and data:
            draw.line([(cx + IP, cy), (cx + cw - IP, cy)], fill=DIV, width=max(1, S))
            cy += int(5 * S)
            draw.text((cx + IP + int(4 * S), cy), "评分构成：暂未获取", font=fsec, fill=T3)
            cy += int(17 * S)

        # 5. Highlights (only genuine clean highlights)
        facts = [text for f in (hl.get('facts', []) if hl else []) if (text := format_highlight(f))]
        if facts:
            draw.line([(cx + IP, cy), (cx + cw - IP, cy)], fill=DIV, width=max(1, S))
            cy += int(5 * S)
            draw.text((cx + IP + int(4 * S), cy), "本局亮点", font=fsec, fill=T2)
            cy += int(17 * S)
            bullet_clr = accent  # Use card accent color for bullets
            for text in facts:
                clean_text = _strip_emoji(text)
                wrapped = _wrap(draw, "◆ " + clean_text, fhl, max_tw)
                for i, ln in enumerate(wrapped):
                    if i == 0:
                        # Draw bullet in accent color, rest in brighter T_HL
                        draw.text((cx + IP + int(4 * S), cy), "◆", font=fhl, fill=bullet_clr)
                        bw = draw.textlength("◆ ", font=fhl)
                        draw.text((cx + IP + int(4 * S) + bw, cy), ln[2:], font=fhl, fill=T_HL)
                    else:
                        draw.text((cx + IP + int(4 * S), cy), ln, font=fhl, fill=T_HL)
                    cy += int(15 * S)
                cy += int(2 * S)

        # 6. Farm impact
        farm_data = data.get('farm_impact', {}).get(acc) if data else None
        if farm_data:
            draw.line([(cx + IP, cy), (cx + cw - IP, cy)], fill=DIV, width=max(1, S))
            cy += int(5 * S)
            FL = {'LOW FARM': '低资源', 'MEDIUM FARM': '中等资源',
                  'HIGH FARM': '高资源', 'HIGHEST FARM': '最高资源'}
            farm_label = FL.get(farm_data['label'], farm_data['label'])
            farm_rest = (
                " · 发育 " + farm_amount(farm_data['farm']) + "灵魂"
                + f" · 奖励 {farm_data['bonus']}"
                + f" · {farm_data['before_grade']} → {ps.get('grade', '?')}"
            )
            # Farm label in a tinted color based on tier
            farm_colors = {
                'LOW FARM': (148, 163, 184), 'MEDIUM FARM': (203, 213, 225),
                'HIGH FARM': (34, 197, 94), 'HIGHEST FARM': (245, 158, 11),
            }
            f_clr = farm_colors.get(farm_data['label'], T3)
            draw.text((cx + IP + int(4 * S), cy), farm_label, font=fl, fill=f_clr)
            fl_w = draw.textlength(farm_label, font=fl)
            draw.text((cx + IP + int(4 * S) + fl_w, cy), farm_rest, font=fl, fill=T3)
            cy += int(16 * S)

        y += card_h + int(10 * S)

    # Footer (centered)
    src = "数据来源: Deadlock API + Statlocker.gg  ·  Powered by Statlocker"
    src_w = draw.textlength(src, font=fft)
    draw.text((cx + (cw - src_w) // 2, y + int(2 * S)), src, font=fft, fill=T3)

    # Save to temp file
    fd, path = tempfile.mkstemp(prefix="deadbot_report_", suffix=".png")
    os.close(fd)
    img.save(path, format="PNG", optimize=True)
    return path
