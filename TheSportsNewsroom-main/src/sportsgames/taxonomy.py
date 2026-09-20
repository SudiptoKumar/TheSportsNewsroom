from __future__ import annotations

# The taxonomy is intentionally wider than professional sports.
SPORTS = [
    "football", "cricket", "basketball", "tennis", "badminton", "table tennis",
    "volleyball", "baseball", "rugby", "golf", "boxing", "mma", "formula 1",
    "formula e", "motogp", "athletics", "swimming", "cycling", "gymnastics",
    "wrestling", "fencing", "archery", "shooting", "rowing", "canoeing",
    "kayaking", "sailing", "surfing", "skateboarding", "bmx", "sport climbing",
    "water polo", "hockey", "field hockey", "ice hockey", "snooker", "billiards",
    "darts", "squash", "bowling", "handball", "netball", "kabaddi", "kho kho",
    "sepak takraw", "sumo", "judo", "karate", "taekwondo", "weightlifting",
    "triathlon", "modern pentathlon", "equestrian", "curling", "biathlon", "bobsleigh",
    "luge", "skeleton", "triathlon", "wheelchair basketball", "para athletics",
    "roller sports", "futsal", "beach volleyball", "beach soccer", "lacrosse", "hurling",
    "gaelic football", "shinty", "pelota", "buzkashi", "muay thai", "kendo",
]

BOARD_GAMES = [
    "chess", "go", "shogi", "xiangqi", "backgammon", "monopoly", "catan", "carrom",
    "mahjong", "scrabble", "risk", "ticket to ride", "snakes and ladders", "pachisi",
    "mancala", "checkers", "draughts", "othello", "connect four", "cluedo", "reversi",
]

CARD_GAMES = [
    "uno", "rummy", "hearts", "spades", "bridge", "cribbage", "canasta", "gin rummy",
    "exploding kittens", "phase 10", "skip-bo", "dos", "presidents", "durak",
    "traditional playing cards",
]

PARTY_GAMES = [
    "mafia", "werewolf", "codenames", "pictionary", "charades", "just one", "taboo",
    "pit", "twister",
]

TRADITIONAL_GAMES = [
    "ludo", "pachisi", "carrom", "kabaddi", "kho kho", "sepak takraw", "mancala",
    "bao", "surakarta", "sungka", "congkak", "senet", "patolli", "gungi",
]

MIND_GAMES = [
    "chess", "go", "shogi", "xiangqi", "sudoku", "rubik's cube", "nonogram",
    "crossword", "logic puzzles", "solitaire",
]

EXPLICIT_VIDEO_GAME_TERMS = {
    "video game", "video games", "videogame", "gaming", "esports", "e-sports",
    "playstation", "xbox", "nintendo switch", "steam", "epic games store", "dlc",
    "patch notes", "game patch", "console", "ps5", "ps4", "xbox series", "pc gaming",
    "mobile gaming", "game trailer", "game studio", "game developer", "publisher",
    "gpu", "game engine", "playstation 5", "xbox one", "xbox series x", "xbox series s",
}

# Discovery source hierarchy. Unknown domains are conservative by default.
PRIMARY_DOMAINS = [
    "fifa.com", "uefa.com", "icc-cricket.com", "worldathletics.org", "fide.com",
    "itftennis.com", "bwfbadminton.com", "world.rugby", "olympics.com", "paralympic.org",
    "formula1.com", "fia.com", "worldboxing.org", "ijf.org", "worldarchery.sport",
    "worldrowing.com", "worldaquatics.com", "uci.org", "fiba.basketball", "worlddarts.com",
    "worldsnooker.com", "worldsquash.org", "worldtaekwondo.org", "worldkarate-federation.com",
    "fivb.com", "ihf.info", "wtt.com", "itftennis.com", "wtatennis.com", "atptour.com",
]

SECONDARY_DOMAINS = [
    "reuters.com", "apnews.com", "bbc.com", "espn.com", "skysports.com", "theguardian.com",
    "nbcsports.com", "cbssports.com", "si.com", "npr.org", "dw.com", "france24.com",
    "boardgamegeek.com", "dicebreaker.com", "tabletopgaming.co.uk", "polygon.com",
]

REFERENCE_DOMAINS = [
    "wikipedia.org", "britannica.com", "guinnessworldrecords.com", "atlasobscura.com",
    "merriam-webster.com", "dictionary.com",
]

LEAD_ONLY_DOMAINS = ["reddit.com", "quora.com", "x.com", "twitter.com", "facebook.com"]

# Angles become the main anti-repeat dimension.
ANGLES = [
    "origin", "etymology", "rule", "house_rule", "myth", "number", "record", "accident",
    "banned", "weird", "design", "symbol", "tradition", "equipment", "measurement",
    "terminology", "scoring", "strategy", "geography", "culture", "evolution", "first",
    "last", "only", "oldest", "youngest", "rare", "historical_connection", "new_mechanic",
    "rules_change", "discovery", "timeline", "then_vs_now", "forgotten", "inventor",
    "how_to_play", "common_mistake", "why", "record_context", "game_anatomy",
]

CATEGORIES = [
    "evergreen_fact", "game_discovery", "new_board_game", "new_card_game", "new_tabletop_game",
    "new_sport", "rule_check", "how_to_play", "game_history", "sport_history", "on_this_date",
    "century_ago", "why_explained", "first_last_only", "then_vs_now", "forgotten_game",
    "forgotten_sport", "sport_discovery", "sports_daily_next", "sports_daily_past",
    "myth_vs_fact", "game_anatomy", "interesting_number", "game_origin", "sport_origin",
]

FORMAT_ORDER = [
    "game_discovery", "fact", "rule_check", "how_to_play", "history", "on_this_date",
    "century_ago", "why", "first_last_only", "then_vs_now", "forgotten", "number",
    "daily_next", "daily_past",
]

SURPRISE_PATTERNS = [
    "why is it called",
    "origin of",
    "oldest known",
    "first ever",
    "only time ever",
    "never been broken",
    "invented by accident",
    "originally called",
    "originally meant",
    "rule most people get wrong",
    "official rule",
    "house rule",
    "myth about",
    "banned in",
    "strange rule",
    "unusual tradition",
    "forgotten history",
    "why does it use",
    "where did it come from",
    "what changed",
    "then vs now",
    "first to",
    "only to",
    "what does the number mean",
]

# RSS is only a supplementary current-sports source. Exa remains the broad discovery layer.
RSS_FEEDS = [
    {"name": "BBC Sport", "url": "https://feeds.bbci.co.uk/sport/rss.xml"},
    {"name": "ESPN", "url": "https://www.espn.com/espn/rss/news"},
    {"name": "The Guardian Sport", "url": "https://www.theguardian.com/uk/sport/rss"},
    {"name": "Sky Sports", "url": "https://www.skysports.com/rss/12040"},
]

VIDEO_GAME_QUERY_PATTERNS = [
    "video game", "videogame", "playstation", "xbox", "steam", "nintendo switch",
    "esports", "e-sports", "console gaming", "pc gaming", "mobile gaming", "dlc",
    "patch notes", "game patch", "season pass", "game trailer",
]
