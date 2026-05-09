from pybaseball import statcast
from datetime import date
import statsapi
from types import SimpleNamespace
import argparse
import json
import math

KEEP = {
    # Identification
    'batter', 'pitcher', 'player_name',
    'fielder_2', 'fielder_3', 'fielder_4', 'fielder_5',
    'fielder_6', 'fielder_7', 'fielder_8', 'fielder_9',
    'home_team', 'away_team', 'game_pk', 'game_date',
    # Pitch context
    'pitch_type', 'pitch_name', 'release_speed', 'description', 'type',
    'zone', 'balls', 'strikes', 'pitch_number', 'at_bat_number',
    'stand', 'p_throws',
    # Event / outcome
    'events', 'des', 'hit_location', 'bb_type',
    # Game state (pre-pitch)
    'on_1b', 'on_2b', 'on_3b', 'outs_when_up',
    'inning', 'inning_topbot',
    # Score
    'home_score', 'away_score', 'bat_score', 'fld_score',
    'post_home_score', 'post_away_score', 'post_bat_score', 'post_fld_score',
}


def getMlbData(game_pk):
    gameData = statsapi.boxscore_data(game_pk)
    away = gameData['teamInfo']['away']['abbreviation']
    home = gameData['teamInfo']['home']['abbreviation']
    gameInfo = {item['label']: item.get('value') for item in gameData['gameBoxInfo']}
    venue = f"{gameInfo['Venue'].rstrip('.')}"
    weather = f"{gameInfo['Weather'].rstrip('.')}; wind {gameInfo['Wind'].rstrip('.')}"
    firstPitch = f"{gameInfo['First pitch'].rstrip('.')}"
    return {
        "teams": [away, home],
        "venue": venue,
        "weather": weather,
        "firstPitch": firstPitch,
        "players": {str(p['id']): p for p in gameData['playerInfo'].values()},
    }


def replayGame(awayData, homeData):
    awayData = awayData[[c for c in awayData.columns if c in KEEP]]
    homeData = homeData[[c for c in homeData.columns if c in KEEP]]
    lastInning = max(int(awayData.inning.head(1).values[0]), int(homeData.inning.head(1).values[0]))
    game_pk = int(homeData.game_pk.values[0])
    metaData = getMlbData(game_pk)
    top = { inning: [] for inning in range(1, lastInning+1) }
    bottom = { inning: [] for inning in range(1, lastInning+1) }
    for index, row in awayData[::-1].iterrows():
        inning = int(row.inning)
        top[inning].append(row.to_dict())
    for index, row in homeData[::-1].iterrows():
        inning = int(row.inning)
        bottom[inning].append(row.to_dict())
    return top, bottom, metaData


def main(game_date, home_team, away_team):
    game_date = str(game_date)
    home_team = str(home_team)
    away_team = str(away_team)
    home = statcast(start_dt=game_date, team=home_team)
    away = statcast(start_dt=game_date, team=away_team)
    return replayGame(home, away)

def _sanitize(obj):
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj

def writeGameFile(top, bottom, meta, game_date, home_team, away_team, outfile=None):
    gameData = _sanitize({
        "meta": meta,
        "top": top,
        "bottom": bottom
    })
    if not outfile:
        outfile = "games/%s-%s-%s.json" %(str(game_date), str(home_team), str(away_team))
    try:
        with open(outfile, "w") as f:
            json.dump(gameData, f)
    except Exception as e:
        print(f"Error writing file: {e}")
        return False
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser("What game do you want to replay?")
    parser.add_argument("game_date", help="The date of the game in YYYY-MM-DD format (with the hyphens)")
    parser.add_argument("home", help="Three-letter code for the home team")
    parser.add_argument("away", help="Three-letter code for the away team")
    args = parser.parse_args()
    top, bottom, metaData = main(args.game_date, args.home, args.away)
    writeGameFile(top, bottom, metaData, args.game_date, args.home, args.away)
