"""
Wind Alerts — notify ~1 hour before any NFL or FBS college football game in an
OUTDOOR stadium where the wind is forecast at 15+ mph at kickoff.

HOW IT WORKS (one pass; you run it on a schedule, e.g. every 20 min):
  1. Pull this week's NFL + CFB games from ESPN's free API (schedule, venue, kickoff).
  2. Drop domes / retractable-roof stadiums (wind doesn't matter there).
  3. For each outdoor game kicking off within the next LEAD_MINUTES, look up the
     stadium's coordinates and get the wind forecast AT kickoff (Open-Meteo, free).
  4. If wind >= THRESHOLD, push a notification to your phone via ntfy.sh, once per game.

SETUP (5 min):
  1. Install the free "ntfy" app on your phone (iOS/Android). Open it, subscribe to a
     topic you invent, e.g.  wind-alerts-8H3k9   (make it random so nobody else guesses it).
  2. Put that same topic in NTFY_TOPIC below (or set env var NTFY_TOPIC).
  3. pip install requests
  4. Run it once to test:  python wind_alerts.py --test
  5. Schedule it every ~20 min (see cron / GitHub Actions instructions in the README).

No API keys needed anywhere.
"""

import os, sys, json, datetime as dt
import requests

# ---------------- CONFIG ----------------
NTFY_TOPIC   = os.environ.get("NTFY_TOPIC", "PUT-YOUR-RANDOM-TOPIC-HERE")
THRESHOLD_MPH = float(os.environ.get("WIND_THRESHOLD", "15"))
LEAD_MINUTES  = int(os.environ.get("LEAD_MINUTES", "60"))   # alert within this many min before kickoff
STATE_FILE    = os.environ.get("STATE_FILE", "alerted.json")
TIMEOUT = 20

# Venues to always skip: fixed domes + retractable roofs (roof state unknown pre-game).
# Matched as case-insensitive substrings of the ESPN venue name. Remove any you WANT checked.
INDOOR_VENUES = [
    # NFL fixed domes / covered
    "ford field", "u.s. bank", "us bank", "caesars superdome", "superdome",
    "allegiant", "sofi",
    # NFL retractable (roof may be open, but unknown ahead of time -> skip to avoid false alerts)
    "at&t stadium", "nrg stadium", "state farm stadium", "mercedes-benz", "lucas oil",
    # College domes / indoor
    "fargodome", "jma wireless", "carrier dome", "uni-dome", "unidome",
    "kibbie", "alamodome", "dome",
]

# ---------------- ESPN SCHEDULE ----------------
ESPN = {
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "CFB": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&limit=400",
}

def fetch_games(league):
    out = []
    try:
        r = requests.get(ESPN[league], timeout=TIMEOUT); r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[{league}] schedule fetch failed: {e}"); return out
    for ev in data.get("events", []):
        try:
            comp = ev["competitions"][0]
            venue = comp.get("venue", {}) or {}
            addr = venue.get("address", {}) or {}
            # home team
            home = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "home"), {})
            out.append({
                "league": league,
                "id": ev.get("id"),
                "name": ev.get("shortName") or ev.get("name"),
                "kickoff": dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00")),
                "venue": venue.get("fullName", ""),
                "city": addr.get("city", ""),
                "state": addr.get("state", ""),
                "indoor": bool(venue.get("indoor", False)),
                "home": (home.get("team", {}) or {}).get("displayName", ""),
            })
        except Exception:
            continue
    return out

def is_outdoor(g):
    if g["indoor"]:
        return False
    name = (g["venue"] or "").lower()
    return not any(k in name for k in INDOOR_VENUES)

# ---------------- OPEN-METEO (geocode + wind) ----------------
_geo_cache = {}
def geocode(city, state):
    key = f"{city},{state}"
    if key in _geo_cache:
        return _geo_cache[key]
    try:
        q = city + (f", {state}" if state else "")
        r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": city, "count": 5, "country": "US"}, timeout=TIMEOUT)
        res = r.json().get("results", []) or []
        # prefer a result whose admin1 matches the state
        pick = None
        for cand in res:
            if state and str(cand.get("admin1_code", "")).upper() == state.upper():
                pick = cand; break
            if state and state.lower() in str(cand.get("admin1", "")).lower():
                pick = cand; break
        pick = pick or (res[0] if res else None)
        latlon = (pick["latitude"], pick["longitude"]) if pick else None
    except Exception as e:
        print(f"  geocode failed for {key}: {e}"); latlon = None
    _geo_cache[key] = latlon
    return latlon

def wind_at(lat, lon, kickoff_utc):
    """Return (wind_mph, gust_mph) at the hour nearest kickoff, or (None, None)."""
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon,
            "hourly": "wind_speed_10m,wind_gusts_10m",
            "wind_speed_unit": "mph", "timezone": "UTC", "forecast_days": 3,
        }, timeout=TIMEOUT)
        h = r.json()["hourly"]
        target = kickoff_utc.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:00")
        times = h["time"]
        if target in times:
            i = times.index(target)
        else:  # nearest hour
            tk = kickoff_utc.timestamp()
            i = min(range(len(times)),
                    key=lambda j: abs(dt.datetime.fromisoformat(times[j]).replace(
                        tzinfo=dt.timezone.utc).timestamp() - tk))
        return h["wind_speed_10m"][i], h["wind_gusts_10m"][i]
    except Exception as e:
        print(f"  wind fetch failed: {e}"); return None, None

# ---------------- NOTIFY ----------------
def notify(title, message):
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",
                      data=message.encode("utf-8"),
                      headers={"Title": title, "Priority": "high", "Tags": "dash,football"},
                      timeout=TIMEOUT)
        print(f"  NOTIFIED: {title} — {message}")
    except Exception as e:
        print(f"  notify failed: {e}")

# ---------------- STATE (dedupe) ----------------
def load_state():
    try:
        with open(STATE_FILE) as f: return set(json.load(f))
    except Exception: return set()
def save_state(s):
    try:
        with open(STATE_FILE, "w") as f: json.dump(sorted(s), f)
    except Exception as e: print("  state save failed:", e)

# ---------------- MAIN ----------------
def main(test=False):
    now = dt.datetime.now(dt.timezone.utc)
    alerted = load_state()
    games = fetch_games("NFL") + fetch_games("CFB")
    print(f"{now:%Y-%m-%d %H:%M UTC} — {len(games)} games pulled")

    for g in games:
        mins = (g["kickoff"] - now).total_seconds() / 60
        in_window = 0 < mins <= LEAD_MINUTES
        if not (test or in_window):        # only games in the hour before kickoff
            continue
        if not is_outdoor(g):
            continue
        if g["id"] in alerted:
            continue
        ll = geocode(g["city"], g["state"])
        if not ll:
            continue
        wind, gust = wind_at(ll[0], ll[1], g["kickoff"])
        if wind is None:
            continue
        print(f"  {g['league']} {g['name']} @ {g['venue']} ({g['city']}): "
              f"wind {wind:.0f} mph, gust {gust:.0f}, kickoff in {mins:.0f} min")
        if wind >= THRESHOLD_MPH:
            kt = g["kickoff"].strftime("%-I:%M %p UTC") if os.name != "nt" else g["kickoff"].strftime("%H:%M UTC")
            notify(
                f"WIND {wind:.0f} mph — {g['name']}",
                f"{g['league']}: {g['name']} at {g['venue']} ({g['city']}, {g['state']}). "
                f"Forecast wind {wind:.0f} mph (gusts {gust:.0f}) at kickoff. Outdoor. "
                f"Kickoff ~{int(mins)} min.",
            )
            if not test:
                alerted.add(g["id"])

    if not test:
        save_state(alerted)

if __name__ == "__main__":
    test = "--test" in sys.argv     # --test: check ALL games now (ignores the 1-hour window), no dedupe
    if NTFY_TOPIC.startswith("PUT-YOUR"):
        print("Set NTFY_TOPIC (in the file or as an env var) before running."); sys.exit(1)
    main(test=test)
