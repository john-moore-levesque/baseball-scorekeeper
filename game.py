from pybaseball import statcast

class Game():
    def __init__(self, teams: tuple, date: str, location: str, weather: str, starters: tuple[str, str], game_pk: int):
        self.teams = teams
        self.date = date
        self.location = location
        self.weather = weather
        self.starters = starters
        self.score = [(0,0,0), (0,0,0)]
        self.innings = [] # insert as (top, bottom) tuple
        self.lineups = [ [] , [] ]
        self.game_data = self.from_statcast(game_pk)
    
    def from_statcast(self, game_pk):
        return statcast