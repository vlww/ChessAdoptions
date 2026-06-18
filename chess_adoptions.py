import requests
import time
import re
from datetime import datetime, timedelta

USERNAME = input("Chess.com username: ").strip()

HEADERS = {"User-Agent": "ChessAdoptionTracker/1.0"}
BASE = f"https://api.chess.com/pub/player/{USERNAME}"


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_archives():
    r = requests.get(f"{BASE}/games/archives", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()["archives"]


def download_month_pgn(archive_url):
    r = requests.get(archive_url + "/pgn", headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.text


def parse_pgn_games(pgn_text):
    """
    Yield dicts with the fields we need from each game header.
    Returns: white, black, result, time_control, utc_datetime
    """
    # Split on blank lines before [Event
    blocks = re.split(r'\n(?=\[Event )', pgn_text.strip())

    for block in blocks:
        if not block.strip():
            continue

        def tag(name):
            m = re.search(rf'\[{name} "([^"]*)"\]', block)
            return m.group(1) if m else None

        white = tag("White")
        black = tag("Black")
        result = tag("Result")
        tc = tag("TimeControl")
        utc_date = tag("UTCDate")   # e.g. 2024.03.15
        utc_time = tag("UTCTime")   # e.g. 23:58:42

        if not all([white, black, result, tc, utc_date]):
            continue

        try:
            dt_str = f"{utc_date} {utc_time or '00:00:00'}"
            dt = datetime.strptime(dt_str, "%Y.%m.%d %H:%M:%S")
        except ValueError:
            continue

        yield {
            "white": white.lower(),
            "black": black.lower(),
            "result": result,        # "1-0", "0-1", "1/2-1/2"
            "time_control": tc,
            "dt": dt,
        }


def format_tc(tc):
    """Convert raw time control like '180+2' to a human-readable string."""
    m = re.match(r'^(\d+)(?:\+(\d+))?$', tc)
    if not m:
        return tc
    base = int(m.group(1))
    inc = int(m.group(2)) if m.group(2) else 0

    def fmt_secs(s):
        if s >= 60 and s % 60 == 0:
            return f"{s // 60}min"
        elif s >= 60:
            return f"{s // 60}min{s % 60}sec"
        else:
            return f"{s}sec"

    base_str = fmt_secs(base)
    return f"{base_str}+{inc}sec" if inc else base_str


# ── Core logic ─────────────────────────────────────────────────────────────────

def find_adoptions(games, username):
    """
    Walk the full global game timeline in order.

    A "sitting" is a contiguous run of games against the same opponent at the
    same time control, with no other opponents interleaved and no 24h+ gap.

    Within a sitting, losses/draws reset the current win streak but do NOT
    end the sitting. At the end of each sitting we record only the single
    best (longest) win streak — if it's >= 10, it counts as one adoption.

    Sitting breaks on:
      - any game against a DIFFERENT opponent
      - a different time control vs the same opponent
      - a gap of more than 24 hours between consecutive games in the sitting
    """
    username = username.lower()

    timeline = []
    for g in games:
        if g["white"] == username:
            opponent = g["black"]
            if g["result"] == "1-0":
                outcome = "win"
            elif g["result"] == "0-1":
                outcome = "loss"
            else:
                outcome = "draw"
        elif g["black"] == username:
            opponent = g["white"]
            if g["result"] == "0-1":
                outcome = "win"
            elif g["result"] == "1-0":
                outcome = "loss"
            else:
                outcome = "draw"
        else:
            continue

        timeline.append({
            "opponent": opponent,
            "time_control": g["time_control"],
            "outcome": outcome,
            "dt": g["dt"],
        })

    timeline.sort(key=lambda x: x["dt"])

    adoptions = []

    # Sitting state
    cur_opponent = None
    cur_tc = None
    sitting_last_dt = None

    # Within the sitting: current win streak and best win streak so far
    cur_streak = []
    best_streak = []

    def close_sitting():
        if len(best_streak) >= 10:
            adoptions.append({
                "opponent": cur_opponent,
                "time_control": cur_tc,
                "streak": len(best_streak),
                "date_start": best_streak[0]["dt"],
                "date_end": best_streak[-1]["dt"],
            })

    def reset_sitting(g=None):
        nonlocal cur_opponent, cur_tc, sitting_last_dt, cur_streak, best_streak
        if g is not None:
            cur_opponent = g["opponent"]
            cur_tc = g["time_control"]
            sitting_last_dt = g["dt"]
            cur_streak = [g] if g["outcome"] == "win" else []
            best_streak = list(cur_streak)
        else:
            cur_opponent = None
            cur_tc = None
            sitting_last_dt = None
            cur_streak = []
            best_streak = []

    for g in timeline:
        gap_broken = (
            sitting_last_dt is not None and
            (g["dt"] - sitting_last_dt) > timedelta(hours=24)
        )
        same_key = (g["opponent"] == cur_opponent and
                    g["time_control"] == cur_tc)

        if cur_opponent is None:
            # No active sitting
            if g["outcome"] == "win":
                reset_sitting(g)
            else:
                sitting_last_dt = g["dt"]

        elif gap_broken or not same_key:
            # Sitting ends
            close_sitting()
            if g["outcome"] == "win":
                reset_sitting(g)
            else:
                reset_sitting()
                sitting_last_dt = g["dt"]

        elif g["outcome"] == "win":
            # Continue win streak within sitting
            cur_streak.append(g)
            if len(cur_streak) > len(best_streak):
                best_streak = list(cur_streak)
            sitting_last_dt = g["dt"]

        else:
            # Loss or draw: reset win streak, sitting continues
            cur_streak = []
            sitting_last_dt = g["dt"]

    close_sitting()

    adoptions.sort(key=lambda x: x["date_start"])
    return adoptions



# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"\nFetching archives for {USERNAME}...")
    archives = get_archives()
    print(f"Found {len(archives)} monthly archives\n")

    all_games = []

    for i, archive in enumerate(archives, 1):
        try:
            print(f"[{i}/{len(archives)}] {archive.split('/')[-2]}/{archive.split('/')[-1]}", end="  ")
            pgn = download_month_pgn(archive)
            games = list(parse_pgn_games(pgn))
            all_games.extend(games)
            print(f"({len(games)} games)")
            time.sleep(0.1)
        except Exception as e:
            print(f"\n  ⚠ Skipped: {e}")

    print(f"\nTotal games parsed: {len(all_games):,}")
    print("Scanning for adoptions (10+ consecutive wins)...\n")

    adoptions = find_adoptions(all_games, USERNAME)

    if not adoptions:
        print("No adoptions found.")
        return

    print(f"{'#':<4} {'Opponent':<25} {'Time Control':<18} {'Streak':>6}  {'Date'}")
    print("-" * 75)

    for n, a in enumerate(adoptions, 1):
        date_str = a["date_start"].strftime("%Y-%m-%d")
        if a["date_start"].date() != a["date_end"].date():
            date_str += " → " + a["date_end"].strftime("%Y-%m-%d")
        tc_human = format_tc(a["time_control"])
        print(f"{n:<4} {a['opponent']:<25} {tc_human:<18} {a['streak']:>6}x  {date_str}")

    print(f"\nTotal adoptions: {len(adoptions)}")


if __name__ == "__main__":
    main()
