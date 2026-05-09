import json
import re
import os
import argparse
from dataclasses import dataclass, field

# ── Position name → number mapping for des parsing ──

POS_TEXT_MAP = [
    ('first baseman', 3), ('second baseman', 4), ('third baseman', 5),
    ('shortstop', 6), ('left fielder', 7), ('center fielder', 8),
    ('right fielder', 9), ('catcher', 2), ('pitcher', 1),
]

PITCH_DESC_MAP = {
    'called_strike': 'S', 'swinging_strike': 'S', 'swinging_strike_blocked': 'S',
    'missed_bunt': 'S',
    'ball': 'B', 'blocked_ball': 'B', 'automatic_ball': 'B',
    'intent_ball': 'B', 'pitchout': 'B',
    'foul': 'F', 'foul_tip': 'F', 'foul_bunt': 'F', 'bunt_foul_tip': 'F',
    'hit_into_play': '✕', 'hit_into_play_no_out': '✕',
    'hit_into_play_score': '✕', 'hit_by_pitch': '✕',
}

HIT_EVENTS = {'single', 'double', 'triple', 'home_run'}
NO_AB_EVENTS = {'walk', 'intent_walk', 'hit_by_pitch', 'sac_fly',
                'sac_bunt', 'catcher_interf', 'sac_fly_double_play',
                'sac_bunt_double_play'}

POS_NUM_TO_ABBR = {
    1: 'P', 2: 'C', 3: '1B', 4: '2B', 5: '3B',
    6: 'SS', 7: 'LF', 8: 'CF', 9: 'RF',
}


@dataclass
class PA:
    at_bat_number: int
    batter_id: int
    pitcher_id: int
    inning: int
    pitches: list
    event: str
    notation: str
    category: str
    rbi: int
    pitch_sequence: str
    diamond_type: str


# ═══════════════════════════════════════════════════════════
#  Loading
# ═══════════════════════════════════════════════════════════

def load_game(filepath):
    raw = open(filepath).read()
    raw = re.sub(r'\bNaN\b', 'null', raw)
    return json.loads(raw)


def resolve_sides(data):
    top_pitches = data['top']
    bottom_pitches = data['bottom']
    sample = None
    for inn in top_pitches.values():
        if inn:
            sample = inn[0]
            break
    if sample and sample.get('inning_topbot') == 'Top':
        return top_pitches, bottom_pitches
    return bottom_pitches, top_pitches


# ═══════════════════════════════════════════════════════════
#  PA Logic
# ═══════════════════════════════════════════════════════════

def group_plate_appearances(all_half_innings):
    pas = []
    for inn_num in sorted(all_half_innings.keys(), key=lambda k: int(k)):
        pitches = all_half_innings[inn_num]
        if not pitches:
            continue
        current_ab = None
        current_pitches = []
        for p in pitches:
            ab = p['at_bat_number']
            if ab != current_ab:
                if current_pitches:
                    pas.append(_build_pa(current_pitches))
                current_ab = ab
                current_pitches = [p]
            else:
                current_pitches.append(p)
        if current_pitches:
            pas.append(_build_pa(current_pitches))
    return pas


def _build_pa(pitches):
    ending = None
    for p in reversed(pitches):
        if p.get('events') is not None:
            ending = p
            break
    event = ending['events'] if ending else None
    notation, category = derive_notation(pitches, ending)
    rbi = 0
    if ending:
        pre = ending.get('bat_score') or 0
        post = ending.get('post_bat_score') or 0
        rbi = max(0, int(post - pre))
    return PA(
        at_bat_number=pitches[0]['at_bat_number'],
        batter_id=int(pitches[0]['batter']),
        pitcher_id=int(pitches[0]['pitcher']),
        inning=int(pitches[0]['inning']),
        pitches=pitches,
        event=event,
        notation=notation,
        category=category,
        rbi=rbi,
        pitch_sequence=build_pitch_sequence(pitches),
        diamond_type=_diamond_type(event),
    )


def derive_notation(pa_pitches, ending):
    if ending is None:
        return ('?', 'unknown')
    event = ending['events']
    bb_type = ending.get('bb_type')
    hit_loc = ending.get('hit_location')
    des = ending.get('des') or ''

    if event == 'strikeout':
        last_desc = pa_pitches[-1].get('description', '')
        return ('ꓘ', 'out') if last_desc == 'called_strike' else ('K', 'out')

    if event == 'strikeout_double_play':
        last_desc = pa_pitches[-1].get('description', '')
        code = 'ꓘ' if last_desc == 'called_strike' else 'K'
        return (f'{code} DP', 'out')

    if event == 'field_out':
        loc = _int_loc(hit_loc)
        if bb_type == 'fly_ball':
            return (f'F{loc}', 'out') if loc else ('F', 'out')
        if bb_type == 'line_drive':
            return (f'L{loc}', 'out') if loc else ('L', 'out')
        if bb_type == 'popup':
            return (f'P{loc}', 'out') if loc else ('P', 'out')
        if bb_type == 'ground_ball':
            return (parse_fielding_sequence(des, event), 'out')
        return (f'F{loc}' if loc else '?', 'out')

    if event == 'grounded_into_double_play':
        return (parse_fielding_sequence(des, event), 'out')

    if event == 'double_play':
        if bb_type == 'line_drive':
            loc = _int_loc(hit_loc)
            return (f'L{loc} DP' if loc else 'DP', 'out')
        if bb_type == 'fly_ball':
            loc = _int_loc(hit_loc)
            return (f'F{loc} DP' if loc else 'DP', 'out')
        return (parse_fielding_sequence(des, event), 'out')

    if event == 'sac_fly_double_play':
        loc = _int_loc(hit_loc)
        return (f'SF DP', 'sacrifice')

    if event == 'sac_bunt_double_play':
        return ('SAC DP', 'sacrifice')

    if event == 'triple_play':
        return (parse_fielding_sequence(des, event), 'out')

    if event == 'force_out':
        return ('FC', 'out')

    if event == 'single':
        loc = _int_loc(hit_loc)
        return (f'1B{loc}' if loc else '1B', 'hit')
    if event == 'double':
        loc = _int_loc(hit_loc)
        return (f'2B{loc}' if loc else '2B', 'hit')
    if event == 'triple':
        loc = _int_loc(hit_loc)
        return (f'3B{loc}' if loc else '3B', 'hit')
    if event == 'home_run':
        return ('HR', 'hit')

    if event == 'walk':
        return ('BB', 'walk')
    if event == 'intent_walk':
        return ('IBB', 'walk')
    if event == 'hit_by_pitch':
        return ('HBP', 'hbp')

    if event == 'sac_fly':
        return ('SF', 'sacrifice')
    if event == 'sac_bunt':
        return ('SAC', 'sacrifice')

    if event in ('fielders_choice', 'fielders_choice_out'):
        return ('FC', 'fc')

    if event == 'field_error':
        pos = _parse_error_position(des)
        return (f'E{pos}', 'error')

    if event == 'catcher_interf':
        return ('CI', 'walk')

    if event and event.startswith('caught_stealing'):
        return ('CS', 'out')
    if event and event.startswith('pickoff'):
        return ('PO', 'out')

    return (event or '?', 'unknown')


def _int_loc(hit_loc):
    if hit_loc is None:
        return ''
    try:
        return str(int(hit_loc))
    except (ValueError, TypeError):
        return ''


def parse_fielding_sequence(des, event):
    first_sentence = des.split('.')[0].lower() if des else ''
    positions = []
    checked = set()
    for pos_text, pos_num in POS_TEXT_MAP:
        idx = first_sentence.find(pos_text)
        while idx != -1:
            if idx not in checked:
                positions.append((idx, pos_num))
                checked.add(idx)
            idx = first_sentence.find(pos_text, idx + 1)
    positions.sort()
    if not positions:
        return '?'
    seq = '-'.join(str(p[1]) for p in positions)
    if len(positions) == 1 and event == 'field_out':
        return f'{positions[0][1]}U'
    if event in ('grounded_into_double_play', 'double_play', 'triple_play',
                 'sac_fly_double_play', 'sac_bunt_double_play'):
        return f'{seq} DP'
    return seq


def _parse_error_position(des):
    if not des:
        return '?'
    lower = des.lower()
    for pos_text, pos_num in POS_TEXT_MAP:
        if pos_text in lower:
            return pos_num
    return '?'


def build_pitch_sequence(pitches):
    chars = []
    for p in pitches:
        desc = p.get('description', '')
        ch = PITCH_DESC_MAP.get(desc, '')
        if ch:
            chars.append(ch)
    return ' '.join(chars)


def _diamond_type(event):
    if event == 'single':
        return 'single'
    if event == 'double':
        return 'double'
    if event == 'triple':
        return 'triple'
    if event == 'home_run':
        return 'hr'
    if event in ('walk', 'intent_walk', 'hit_by_pitch', 'catcher_interf'):
        return 'walk'
    return 'out'


# ═══════════════════════════════════════════════════════════
#  Lineup Detection
# ═══════════════════════════════════════════════════════════

def detect_lineup(half_innings, players):
    all_pas = group_plate_appearances(half_innings)
    slots = [[] for _ in range(9)]
    current_holders = [None] * 9
    slot_idx = 0
    for pa in all_pas:
        if current_holders[slot_idx] is None:
            current_holders[slot_idx] = pa.batter_id
            slots[slot_idx].append({'batter_id': pa.batter_id, 'pas': [pa]})
        elif current_holders[slot_idx] == pa.batter_id:
            slots[slot_idx][-1]['pas'].append(pa)
        else:
            current_holders[slot_idx] = pa.batter_id
            slots[slot_idx].append({'batter_id': pa.batter_id, 'pas': [pa]})
        slot_idx = (slot_idx + 1) % 9
    return slots


def _lookup_name(player_id, players):
    key = str(int(player_id))
    p = players.get(key)
    if p:
        return p.get('boxscoreName') or p.get('fullName', f'#{key}')
    return f'#{key}'


def detect_positions(fielding_half_innings):
    positions = {}
    for inn_num in sorted(fielding_half_innings.keys(), key=lambda k: int(k)):
        pitches = fielding_half_innings[inn_num]
        if not pitches:
            continue
        for p in pitches:
            for pos_num in range(2, 10):
                fid = p.get(f'fielder_{pos_num}')
                if fid is not None:
                    fid = int(fid)
                    if fid not in positions:
                        positions[fid] = (POS_NUM_TO_ABBR[pos_num], pos_num)
            pid = int(p['pitcher'])
            if pid not in positions:
                positions[pid] = ('P', 1)
    return positions


# ═══════════════════════════════════════════════════════════
#  Line Score
# ═══════════════════════════════════════════════════════════

def compute_line_score(half_innings):
    scores = []
    for inn_num in sorted(half_innings.keys(), key=lambda k: int(k)):
        pitches = half_innings[inn_num]
        if not pitches:
            scores.append((0, 0, 0))
            continue
        bat_scores = [p.get('bat_score', 0) or 0 for p in pitches]
        post_scores = [p.get('post_bat_score', 0) or 0 for p in pitches]
        runs = int(max(post_scores) - min(bat_scores))
        pas = group_plate_appearances({inn_num: pitches})
        hits = sum(1 for pa in pas if pa.event in HIT_EVENTS)
        errors = sum(1 for pa in pas if pa.event == 'field_error')
        scores.append((runs, hits, errors))
    return scores


# ═══════════════════════════════════════════════════════════
#  Pitcher Stints
# ═══════════════════════════════════════════════════════════

def compute_pitcher_stints(opposing_half_innings, players):
    all_pas = group_plate_appearances(opposing_half_innings)
    stints = []
    current_pitcher = None
    current = None
    for pa in all_pas:
        pid = pa.pitcher_id
        if pid != current_pitcher:
            if current:
                stints.append(current)
            current_pitcher = pid
            current = {'pitcher_id': pid, 'name': _lookup_name(pid, players),
                       'outs': 0, 'hits': 0, 'runs': 0, 'bb': 0, 'k': 0, 'pitches': 0}
        current['pitches'] += len(pa.pitches)
        if pa.category == 'hit':
            current['hits'] += 1
        if pa.event in ('strikeout', 'strikeout_double_play'):
            current['k'] += 1
        if pa.event in ('walk', 'intent_walk'):
            current['bb'] += 1
        ending = pa.pitches[-1]
        r = max(0, int((ending.get('post_bat_score') or 0) - (ending.get('bat_score') or 0)))
        current['runs'] += r
        if pa.category in ('out', 'fc'):
            if 'DP' in pa.notation:
                current['outs'] += 2
            else:
                current['outs'] += 1
        elif pa.category == 'sacrifice':
            current['outs'] += 1
    if current:
        stints.append(current)
    for s in stints:
        full, part = divmod(s['outs'], 3)
        s['ip'] = f'{full}.{part}'
    return stints


# ═══════════════════════════════════════════════════════════
#  Batter Stats
# ═══════════════════════════════════════════════════════════

def compute_batter_stats(stints):
    ab = 0
    h = 0
    rbi = 0
    for stint in stints:
        for pa in stint['pas']:
            if pa.event not in NO_AB_EVENTS and pa.event is not None:
                ab += 1
            if pa.category == 'hit':
                h += 1
            rbi += pa.rbi
    return {'ab': ab, 'h': h, 'rbi': rbi}


# ═══════════════════════════════════════════════════════════
#  Diamond SVG
# ═══════════════════════════════════════════════════════════

_BASE = '<polygon points="15,3 27,15 15,27 3,15" class="base-empty"/>'
_L1B = '<line x1="15" y1="27" x2="27" y2="15" stroke="{color}" stroke-width="2.5" stroke-linecap="round"/>'
_L2B = '<line x1="3" y1="15" x2="15" y2="27" stroke="{color}" stroke-width="2.5" stroke-linecap="round"/>'
_L3B = '<line x1="15" y1="3" x2="3" y2="15" stroke="{color}" stroke-width="2.5" stroke-linecap="round"/>'
RED = '#b8342e'
GREEN = '#2d6a2e'

def diamond_svg(diamond_type):
    if diamond_type == 'hr':
        return ('<svg class="diamond-wrap" viewBox="0 0 30 30">'
                '<polygon points="15,3 27,15 15,27 3,15" class="base-reached"/>'
                '<circle cx="15" cy="15" r="3" fill="#faf6ed"/></svg>')
    lines = ''
    if diamond_type == 'single':
        lines = _L1B.format(color=RED)
    elif diamond_type == 'double':
        lines = _L2B.format(color=RED) + _L1B.format(color=RED)
    elif diamond_type == 'triple':
        lines = _L3B.format(color=RED) + _L2B.format(color=RED) + _L1B.format(color=RED)
    elif diamond_type == 'walk':
        lines = _L1B.format(color=GREEN)
    return f'<svg class="diamond-wrap" viewBox="0 0 30 30">{_BASE}{lines}</svg>'


# ═══════════════════════════════════════════════════════════
#  HTML Rendering
# ═══════════════════════════════════════════════════════════

def render_cell(pa, is_sub=False):
    sub = ' sub-row' if is_sub else ''
    if pa is None:
        return f'<div class="cell cell-inning{sub}"></div>'
    css_hit = ' has-hit' if pa.category == 'hit' else ''
    code_class = 'hit' if pa.category == 'hit' else 'walk' if pa.category in ('walk', 'hbp') else 'out'
    rbi_html = f'<div class="rbi-badge">{pa.rbi}</div>' if pa.rbi > 0 else ''
    return (f'<div class="cell cell-inning{css_hit}{sub}">'
            f'{rbi_html}'
            f'<div class="result-code {code_class}">{pa.notation}</div>'
            f'{diamond_svg(pa.diamond_type)}'
            f'<div class="pitch-seq">{pa.pitch_sequence}</div>'
            f'</div>')


def render_team_grid(team_label, lineup, num_innings, players, pos_map):
    col_template = f'var(--name-w) repeat({num_innings}, var(--cell-w)) repeat(3, 36px)'
    rows = []
    header_cells = ['<div class="cell cell-name" style="background:#eee8d8; min-height:auto; '
                    'border-bottom:2px solid var(--grid-heavy);">'
                    '<span style="font-family:\'JetBrains Mono\',monospace; font-size:11px; '
                    'color:#6b6152; font-weight:500;">BATTING ORDER</span></div>']
    for i in range(1, num_innings + 1):
        header_cells.append(f'<div class="cell cell-inning" style="background:#eee8d8; min-height:auto; '
                           f'border-bottom:2px solid var(--grid-heavy); font-family:\'JetBrains Mono\',monospace; '
                           f'font-size:11px; color:#6b6152; font-weight:500;">{i}</div>')
    for stat in ['AB', 'H', 'RBI']:
        header_cells.append(f'<div class="cell cell-stat stat-header">{stat}</div>')
    rows.append('<div class="grid-header">' + '\n'.join(header_cells) + '</div>')

    for slot_idx, stints in enumerate(lineup):
        for stint_idx, stint in enumerate(stints):
            bid = stint['batter_id']
            name = _lookup_name(bid, players)
            pos_abbr, pos_num = pos_map.get(bid, ('DH', ''))
            pos_label = f'{pos_abbr} · {pos_num}' if pos_num else 'DH'
            stats = compute_batter_stats([stint])
            is_sub = stint_idx > 0
            sub_cls = ' sub-row' if is_sub else ''
            order_num = '' if is_sub else f'<span class="order-num">{slot_idx+1}</span> '
            pa_map = {}
            for pa in stint['pas']:
                pa_map.setdefault(pa.inning, []).append(pa)
            name_cell = (f'<div class="cell cell-name{sub_cls}">'
                        f'<div>{order_num}<span class="player-name">{name}</span></div>'
                        f'<div class="player-detail">{pos_label}</div>'
                        f'</div>')
            inn_cells = []
            for inn in range(1, num_innings + 1):
                pa_list = pa_map.get(inn, [])
                if pa_list:
                    inn_cells.append(render_cell(pa_list[0], is_sub))
                else:
                    inn_cells.append(f'<div class="cell cell-inning{sub_cls}"></div>')
            stat_cells = (f'<div class="cell cell-stat{sub_cls}">{stats["ab"]}</div>'
                         f'<div class="cell cell-stat{sub_cls}">{stats["h"]}</div>'
                         f'<div class="cell cell-stat{sub_cls}">{stats["rbi"]}</div>')
            rows.append(name_cell + '\n'.join(inn_cells) + stat_cells)

    return (f'<div class="team-section">'
            f'<div class="team-label">{team_label}</div>'
            f'<div class="grid" style="grid-template-columns: {col_template};">'
            + '\n'.join(rows) +
            f'</div></div>')


def render_line_score(away_abbr, home_abbr, away_ls, home_ls, num_innings):
    header = '<div class="ls-cell ls-team ls-header"></div>'
    for i in range(1, num_innings + 1):
        header += f'<div class="ls-cell ls-header">{i}</div>'
    header += '<div class="ls-cell ls-header ls-total">R</div>'
    header += '<div class="ls-cell ls-header ls-total">H</div>'
    header += '<div class="ls-cell ls-header ls-total">E</div>'

    def _team_row(abbr, ls):
        row = f'<div class="ls-cell ls-team">{abbr}</div>'
        tr, th, te = 0, 0, 0
        for i in range(num_innings):
            if i < len(ls):
                r, h, e = ls[i]
                tr += r
                th += h
                te += e
                row += f'<div class="ls-cell">{r}</div>'
            else:
                row += '<div class="ls-cell"></div>'
        row += f'<div class="ls-cell ls-total">{tr}</div>'
        row += f'<div class="ls-cell ls-total">{th}</div>'
        row += f'<div class="ls-cell ls-total">{te}</div>'
        return row

    return (f'<div class="line-score">'
            f'<div class="ls-row">{header}</div>'
            f'<div class="ls-row">{_team_row(away_abbr, away_ls)}</div>'
            f'<div class="ls-row">{_team_row(home_abbr, home_ls)}</div>'
            f'</div>')


def render_pitching_summary(away_stints, home_stints, away_abbr, home_abbr):
    def _table(stints):
        rows = '<tr><th></th><th>IP</th><th>H</th><th>R</th><th>BB</th><th>K</th><th>P</th></tr>'
        for s in stints:
            rows += (f'<tr><td class="pitcher-name">{s["name"]}</td>'
                    f'<td>{s["ip"]}</td><td>{s["hits"]}</td><td>{s["runs"]}</td>'
                    f'<td>{s["bb"]}</td><td>{s["k"]}</td><td>{s["pitches"]}</td></tr>')
        return f'<table>{rows}</table>'
    return (f'<div class="pitching-summary" style="display:flex; gap:48px; margin-top:20px;">'
            f'<div><h3>{away_abbr} Pitching</h3>{_table(away_stints)}</div>'
            f'<div><h3>{home_abbr} Pitching</h3>{_table(home_stints)}</div>'
            f'</div>')


def render_scorecard(data):
    meta = data['meta']
    players = meta['players']
    away_abbr, home_abbr = meta['teams']
    top_half, bottom_half = resolve_sides(data)

    num_innings = max(
        max((int(k) for k in top_half.keys()), default=9),
        max((int(k) for k in bottom_half.keys()), default=9),
    )

    away_lineup = detect_lineup(top_half, players)
    home_lineup = detect_lineup(bottom_half, players)

    away_batting_ls = compute_line_score(top_half)
    home_batting_ls = compute_line_score(bottom_half)

    away_pitching = compute_pitcher_stints(bottom_half, players)
    home_pitching = compute_pitcher_stints(top_half, players)

    game_date = meta.get('firstPitch', '')
    raw_date = ''
    for inn in top_half.values():
        if inn:
            raw_date = inn[0].get('game_date', '')
            break

    header_html = (f'<div class="header">'
                   f'<h1>{away_abbr} <span class="at">at</span> {home_abbr}</h1>'
                   f'<div class="meta-line">{raw_date} &middot; {meta.get("venue","")} '
                   f'&middot; {meta.get("weather","")} &middot; First pitch {meta.get("firstPitch","")}</div>'
                   f'</div>')

    away_pos = detect_positions(bottom_half)
    home_pos = detect_positions(top_half)

    away_grid = render_team_grid(f'{away_abbr} — Batting', away_lineup, num_innings, players, away_pos)
    home_grid = render_team_grid(f'{home_abbr} — Batting', home_lineup, num_innings, players, home_pos)
    ls_html = render_line_score(away_abbr, home_abbr, away_batting_ls, home_batting_ls, num_innings)
    pitch_html = render_pitching_summary(away_pitching, home_pitching, away_abbr, home_abbr)

    legend_html = SCORECARD_LEGEND

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scorecard &mdash; {away_abbr} at {home_abbr}</title>
<style>
{CSS_TEMPLATE}
</style>
</head>
<body>
<div class="scorecard">
{header_html}
{away_grid}
{home_grid}
{ls_html}
{pitch_html}
{legend_html}
</div>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════
#  Scorecard Legend
# ═══════════════════════════════════════════════════════════

SCORECARD_LEGEND = """
<div class="legend">
  <div class="legend-title">How to Read This Scorecard</div>
  <div class="legend-columns">
    <div class="legend-col">
      <div class="legend-section">Each Box</div>
      <div class="legend-item"><span class="legend-label">Top</span> Result of the plate appearance</div>
      <div class="legend-item"><span class="legend-label">Middle</span> Diamond showing bases reached by the batter</div>
      <div class="legend-item"><span class="legend-label">Bottom</span> Pitch sequence for the at-bat</div>
      <div class="legend-item"><span class="legend-swatch swatch-hit"></span> Shaded box = hit</div>
      <div class="legend-item"><span class="legend-badge-sample">2</span> RBI count (top-right corner)</div>
    </div>
    <div class="legend-col">
      <div class="legend-section">Pitch Sequence</div>
      <div class="legend-item"><span class="legend-code">S</span> Strike (swinging or called)</div>
      <div class="legend-item"><span class="legend-code">B</span> Ball</div>
      <div class="legend-item"><span class="legend-code">F</span> Foul ball</div>
      <div class="legend-item"><span class="legend-code">✕</span> Ball put in play / HBP</div>
    </div>
    <div class="legend-col">
      <div class="legend-section">Result Codes</div>
      <div class="legend-item"><span class="legend-code">K</span> Strikeout swinging</div>
      <div class="legend-item"><span class="legend-code">ꓘ</span> Strikeout looking</div>
      <div class="legend-item"><span class="legend-code">BB</span> Walk</div>
      <div class="legend-item"><span class="legend-code">HBP</span> Hit by pitch</div>
      <div class="legend-item"><span class="legend-code">1B 2B 3B HR</span> Hit (+ fielder position)</div>
      <div class="legend-item"><span class="legend-code">F L P</span> Fly / Line / Pop out (+ position)</div>
      <div class="legend-item"><span class="legend-code">6-3</span> Groundout (fielding sequence)</div>
      <div class="legend-item"><span class="legend-code">DP</span> Double play</div>
      <div class="legend-item"><span class="legend-code">FC</span> Fielder's choice</div>
      <div class="legend-item"><span class="legend-code">E</span> Error (+ position)</div>
      <div class="legend-item"><span class="legend-code">SF</span> Sacrifice fly &nbsp; <span class="legend-code">SAC</span> Sac bunt</div>
    </div>
    <div class="legend-col">
      <div class="legend-section">Diamond</div>
      <div class="legend-diamond-row">
        <svg viewBox="0 0 30 30" width="24" height="24"><polygon points="15,3 27,15 15,27 3,15" fill="none" stroke="#bfb7a4" stroke-width="1.5"/></svg>
        <span>No bases reached (out)</span>
      </div>
      <div class="legend-diamond-row">
        <svg viewBox="0 0 30 30" width="24" height="24"><polygon points="15,3 27,15 15,27 3,15" fill="none" stroke="#bfb7a4" stroke-width="1.5"/><line x1="15" y1="27" x2="27" y2="15" stroke="#b8342e" stroke-width="2.5"/></svg>
        <span>Single</span>
      </div>
      <div class="legend-diamond-row">
        <svg viewBox="0 0 30 30" width="24" height="24"><polygon points="15,3 27,15 15,27 3,15" fill="none" stroke="#bfb7a4" stroke-width="1.5"/><line x1="3" y1="15" x2="15" y2="27" stroke="#b8342e" stroke-width="2.5"/><line x1="15" y1="27" x2="27" y2="15" stroke="#b8342e" stroke-width="2.5"/></svg>
        <span>Double</span>
      </div>
      <div class="legend-diamond-row">
        <svg viewBox="0 0 30 30" width="24" height="24"><polygon points="15,3 27,15 15,27 3,15" fill="none" stroke="#bfb7a4" stroke-width="1.5"/><line x1="15" y1="3" x2="3" y2="15" stroke="#b8342e" stroke-width="2.5"/><line x1="3" y1="15" x2="15" y2="27" stroke="#b8342e" stroke-width="2.5"/><line x1="15" y1="27" x2="27" y2="15" stroke="#b8342e" stroke-width="2.5"/></svg>
        <span>Triple</span>
      </div>
      <div class="legend-diamond-row">
        <svg viewBox="0 0 30 30" width="24" height="24"><polygon points="15,3 27,15 15,27 3,15" fill="#b8342e" stroke="#b8342e" stroke-width="1.5"/><circle cx="15" cy="15" r="3" fill="#faf6ed"/></svg>
        <span>Home run</span>
      </div>
      <div class="legend-diamond-row">
        <svg viewBox="0 0 30 30" width="24" height="24"><polygon points="15,3 27,15 15,27 3,15" fill="none" stroke="#bfb7a4" stroke-width="1.5"/><line x1="15" y1="27" x2="27" y2="15" stroke="#2d6a2e" stroke-width="2.5"/></svg>
        <span>Walk / HBP</span>
      </div>
    </div>
  </div>
</div>
"""


# ═══════════════════════════════════════════════════════════
#  CSS (from mockup.html)
# ═══════════════════════════════════════════════════════════

CSS_TEMPLATE = """
@import url('https://fonts.googleapis.com/css2?family=Crimson+Pro:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
  --paper: #faf6ed;
  --ink: #2c2416;
  --grid: #bfb7a4;
  --grid-heavy: #8a8172;
  --accent: #b8342e;
  --hit-bg: rgba(184, 52, 46, 0.07);
  --header-bg: #2c2416;
  --header-fg: #faf6ed;
  --cell-w: 82px;
  --cell-h: 72px;
  --name-w: 200px;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  background: #e8e0d0;
  font-family: 'Crimson Pro', Georgia, serif;
  color: var(--ink);
  padding: 32px;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.scorecard {
  background: var(--paper);
  border: 2px solid var(--grid-heavy);
  box-shadow: 0 4px 24px rgba(0,0,0,0.15);
  padding: 28px 32px 24px;
  max-width: 1160px;
  width: 100%;
}

.header { text-align: center; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 2px solid var(--grid-heavy); }
.header h1 { font-size: 28px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 4px; }
.header h1 .at { font-weight: 400; text-transform: lowercase; font-size: 20px; margin: 0 6px; }
.header .meta-line { font-size: 14px; color: #6b6152; font-weight: 400; }

.team-section { margin-bottom: 20px; }
.team-label {
  background: var(--header-bg); color: var(--header-fg);
  font-size: 14px; font-weight: 600; letter-spacing: 2px; text-transform: uppercase;
  padding: 5px 12px; display: inline-block; margin-bottom: 0;
}

.grid { display: grid; border: 1.5px solid var(--grid-heavy); width: fit-content; }
.grid-header { display: contents; }
.grid-header .cell {
  background: #eee8d8;
  font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 500;
  text-align: center; padding: 4px 2px;
  border-bottom: 2px solid var(--grid-heavy); color: #6b6152;
}

.cell {
  border-right: 1px solid var(--grid); border-bottom: 1px solid var(--grid);
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  min-height: var(--cell-h); position: relative;
}
.cell:last-child { border-right: none; }

.cell-name {
  min-width: var(--name-w); max-width: var(--name-w);
  padding: 4px 8px; align-items: flex-start; justify-content: center;
  border-right: 2px solid var(--grid-heavy); line-height: 1.2;
}
.cell-name .player-name { font-size: 14px; font-weight: 600; }
.cell-name .player-detail { font-size: 11px; color: #8a8172; }
.cell-name .order-num {
  font-family: 'JetBrains Mono', monospace; font-size: 11px; color: #8a8172; margin-right: 4px;
}

.cell-inning { min-width: var(--cell-w); max-width: var(--cell-w); padding: 4px; gap: 2px; }
.cell-inning.has-hit { background: var(--hit-bg); }

.cell-stat {
  min-width: 36px; max-width: 36px;
  font-family: 'JetBrains Mono', monospace; font-size: 12px;
  text-align: center; padding: 4px 2px;
}
.cell-stat.stat-header {
  background: #eee8d8; font-size: 10px; font-weight: 500; color: #6b6152;
  min-height: auto; border-bottom: 2px solid var(--grid-heavy);
}

.result-code {
  font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 500; letter-spacing: 0.5px;
}
.result-code.hit { color: var(--accent); font-weight: 700; }
.result-code.walk { color: #2d6a2e; }
.result-code.out { color: var(--ink); }

.diamond-wrap { width: 30px; height: 30px; position: relative; margin: 1px 0; }
.diamond-wrap svg { width: 100%; height: 100%; }
.base-empty { fill: none; stroke: var(--grid); stroke-width: 1.5; }
.base-reached { fill: var(--accent); stroke: var(--accent); stroke-width: 1.5; }
.diamond-dot { fill: var(--accent); }

.pitch-seq {
  font-family: 'JetBrains Mono', monospace; font-size: 9px; color: #8a8172; letter-spacing: 1px;
}

.rbi-badge {
  position: absolute; top: 3px; right: 4px;
  font-family: 'JetBrains Mono', monospace; font-size: 9px;
  color: var(--accent); font-weight: 700;
}

.line-score { margin-top: 20px; border: 1.5px solid var(--grid-heavy); width: fit-content; }
.line-score .ls-row { display: flex; }
.line-score .ls-cell {
  font-family: 'JetBrains Mono', monospace; font-size: 13px;
  text-align: center; padding: 5px 0;
  border-right: 1px solid var(--grid); border-bottom: 1px solid var(--grid);
  width: 36px;
}
.line-score .ls-cell:last-child { border-right: none; }
.line-score .ls-row:last-child .ls-cell { border-bottom: none; }
.line-score .ls-team {
  width: 56px; font-family: 'Crimson Pro', serif; font-weight: 700; font-size: 14px;
  text-align: left; padding-left: 8px; border-right: 2px solid var(--grid-heavy);
}
.line-score .ls-header {
  background: #eee8d8; font-size: 10px; font-weight: 500; color: #6b6152;
}
.line-score .ls-total { font-weight: 700; border-left: 2px solid var(--grid-heavy); }

.pitching-summary { margin-top: 16px; font-size: 13px; color: #6b6152; }
.pitching-summary h3 {
  font-size: 12px; text-transform: uppercase; letter-spacing: 1.5px; color: var(--ink); margin-bottom: 6px;
}
.pitching-summary table { border-collapse: collapse; font-size: 12px; }
.pitching-summary td, .pitching-summary th { padding: 2px 10px 2px 0; text-align: left; }
.pitching-summary th {
  font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #8a8172; font-weight: 500;
}
.pitcher-name { font-weight: 600; color: var(--ink); }

.cell-name.sub-row {
  border-top: 1px dashed var(--grid); min-height: auto; padding: 2px 8px 2px 28px;
}
.cell-name.sub-row .player-name { font-size: 12px; font-weight: 400; font-style: italic; }
.cell-inning.sub-row { border-top: 1px dashed var(--grid); min-height: auto; }
.cell-stat.sub-row { border-top: 1px dashed var(--grid); min-height: auto; }

.legend {
  margin-top: 24px; padding-top: 16px; border-top: 2px solid var(--grid-heavy);
}
.legend-title {
  font-size: 14px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
  margin-bottom: 12px; color: var(--ink);
}
.legend-columns {
  display: flex; gap: 32px; flex-wrap: wrap;
}
.legend-col { min-width: 180px; flex: 1; }
.legend-section {
  font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 1px; color: var(--ink);
  margin-bottom: 6px; padding-bottom: 3px; border-bottom: 1px solid var(--grid);
}
.legend-item {
  font-size: 12px; color: #6b6152; margin-bottom: 3px; line-height: 1.4;
  display: flex; align-items: center; gap: 6px;
}
.legend-code {
  font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 600;
  color: var(--ink); min-width: 18px;
}
.legend-label {
  font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 500;
  color: #8a8172; min-width: 46px;
}
.legend-swatch {
  display: inline-block; width: 14px; height: 14px; border: 1px solid var(--grid);
  vertical-align: middle;
}
.swatch-hit { background: var(--hit-bg); }
.legend-badge-sample {
  font-family: 'JetBrains Mono', monospace; font-size: 9px; font-weight: 700;
  color: var(--accent);
}
.legend-diamond-row {
  display: flex; align-items: center; gap: 8px;
  font-size: 12px; color: #6b6152; margin-bottom: 4px;
}
"""


# ═══════════════════════════════════════════════════════════
#  CLI Entry Point
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Render a baseball scorecard from game JSON')
    parser.add_argument('game_file', help='Path to game JSON (e.g., games/2026-05-07-WSH-MIN.json)')
    args = parser.parse_args()

    data = load_game(args.game_file)
    html = render_scorecard(data)

    basename = os.path.splitext(os.path.basename(args.game_file))[0]
    os.makedirs('scorecards', exist_ok=True)
    outpath = f'scorecards/{basename}.html'
    with open(outpath, 'w') as f:
        f.write(html)
    print(f'Scorecard written to {outpath}')


if __name__ == '__main__':
    main()
