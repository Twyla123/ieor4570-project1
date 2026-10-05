"""Free-events helper (Usman): free events, pop-ups, giveaways, wait estimates and day itineraries.

Exports EVENTS_TOOLS (schemas the model sees) and EVENTS_TOOL_MAP (name -> function); tools.py merges them.
Errors come back as {"error": ...} with a hint the model can act on instead of raising.
"""

import html as html_lib
import json
import math
import re
import time
from zoneinfo import ZoneInfo
from datetime import date, datetime, timedelta

import requests

# --- Shared constants ---

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PARKS_EVENTS_URL = "https://www.nycgovparks.org/xml/events_300_rss.json"

# Parks feed park ids start with a borough letter.
BOROUGH_PREFIX = {"M": "Manhattan", "B": "Brooklyn", "Q": "Queens", "X": "Bronx", "R": "Staten Island"}
NYC_LAT, NYC_LON = 40.7128, -74.0060


NYC_TZ = ZoneInfo("America/New_York")


def _now() -> datetime:
    return datetime.now(NYC_TZ)


def _today() -> date:
    return _now().date()


def _err(message: str) -> str:
    return json.dumps({"error": message})


def _parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    return datetime.strptime(value, "%Y-%m-%d").date()


def _next_weekend(today: date) -> tuple[date, date]:
    """Saturday and Sunday of the coming weekend (this weekend if it has started)."""
    if today.weekday() == 6:  # Sunday
        return today, today
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    if today.weekday() == 5:
        saturday = today
    return saturday, saturday + timedelta(days=1)


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _forecast(target: date) -> dict:
    """Daily forecast for NYC on one date. Raises requests.RequestException on failure."""
    data = requests.get(
        FORECAST_URL,
        params={
            "latitude": NYC_LAT,
            "longitude": NYC_LON,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "temperature_unit": "fahrenheit",
            "timezone": "America/New_York",
            "start_date": target.isoformat(),
            "end_date": target.isoformat(),
        },
        timeout=10,
    ).json()
    if "daily" not in data:
        raise requests.RequestException(data.get("reason", "no forecast returned for that date"))
    daily = data["daily"]
    return {
        "date": target.isoformat(),
        "high_f": daily["temperature_2m_max"][0],
        "low_f": daily["temperature_2m_min"][0],
        "rain_chance_pct": daily["precipitation_probability_max"][0],
    }


# --- Tool 1 (external data): free events from NYC Parks ---


def find_free_events(
    start_date: str | None = None,
    end_date: str | None = None,
    borough: str | None = None,
    keyword: str | None = None,
    limit: int = 12,
) -> str:
    """Free NYC Parks events in a date range, optionally filtered by borough or keyword."""
    today = _today()
    sat, sun = _next_weekend(today)
    try:
        start = _parse_date(start_date, sat)
        end = _parse_date(end_date, sun)
    except ValueError:
        return _err("Dates must look like YYYY-MM-DD, e.g. 2026-10-03.")

    try:
        events = requests.get(PARKS_EVENTS_URL, timeout=15).json()
    except (requests.RequestException, ValueError) as e:
        return _err(f"NYC Parks events feed failed: {e}. Tell the user and offer to try again.")

    wanted_borough = borough.strip().lower() if borough else None
    wanted_keyword = keyword.strip().lower() if keyword else None
    results = []
    for ev in events:
        try:
            ev_date = datetime.strptime(ev["startdate"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if not (start <= ev_date <= end):
            continue

        if "cancel" in (ev.get("title") or "").lower():
            continue

        park_ids = (ev.get("parkids") or "").split(",")
        ev_borough = BOROUGH_PREFIX.get(park_ids[0].strip()[:1].upper(), "Unknown")
        if wanted_borough and wanted_borough not in ev_borough.lower():
            continue

        haystack = " ".join(str(ev.get(k) or "") for k in ("title", "description", "categories")).lower()
        if wanted_keyword and wanted_keyword not in haystack:
            continue

        coords = (ev.get("coordinates") or "").split(",")
        try:
            lat, lon = float(coords[0]), float(coords[1])
        except (ValueError, IndexError):
            lat = lon = None

        results.append({
            "title": html_lib.unescape(ev.get("title") or ""),
            "date": ev["startdate"],
            "time": f"{ev.get('starttime', '?')} - {ev.get('endtime', '?')}",
            "borough": ev_borough,
            "park": ev.get("parknames"),
            "location": ev.get("location"),
            "categories": (ev.get("categories") or "").replace("HEXIST_ERR_UNKNOWN", "").strip(" |"),
            "lat": lat,
            "lon": lon,
            "link": ev.get("link"),
            "registration": ev.get("registration_description"),
        })

    results.sort(key=lambda r: (r["date"], r["time"]))
    limit = max(1, min(int(limit), 25))
    return json.dumps({
        "searched": f"{start} to {end}",
        "total_matches": len(results),
        "showing": min(limit, len(results)),
        "note": "NYC Parks events are free to attend unless the listing says registration or a fee applies.",
        "events": results[:limit],
    })


# --- Tool 1b (external data): pop-ups, giveaways and freebies from NYC for FREE ---

NYCFORFREE_URL = "https://www.nycforfree.co"
NYCFORFREE_HEADERS = {"User-Agent": "StoopNYC-class-project/1.0 (Columbia University course project)"}
_freebies_cache: dict = {"fetched_at": 0.0, "rows": []}
CACHE_SECONDS = 30 * 60  # one request per half hour at most


def _load_freebies() -> list[dict]:
    """Parse the public events listing at nycforfree.co. Cached; raises requests.RequestException."""
    if _freebies_cache["rows"] and time.time() - _freebies_cache["fetched_at"] < CACHE_SECONDS:
        return _freebies_cache["rows"]

    page = requests.get(f"{NYCFORFREE_URL}/events", headers=NYCFORFREE_HEADERS, timeout=15)
    page.raise_for_status()

    rows, seen = [], set()
    for tag in re.finditer(r'<div\b((?:[^>"]|"[^"]*")*)>', page.text):
        attrs = {k: html_lib.unescape(v) for k, v in re.findall(r'([\w-]+)="([^"]*)"', tag.group(1))}
        slug = attrs.get("slug")
        if not slug or slug in seen or "cancel" in attrs.get("title", "").lower():
            continue
        try:
            start = datetime.strptime(attrs["start-date"], "%B %d, %Y").date()
            end = datetime.strptime(attrs.get("end-date") or attrs["start-date"], "%B %d, %Y").date()
        except (KeyError, ValueError):
            continue
        try:
            lat, lon = float(attrs["lat"]), float(attrs["long"])
        except (KeyError, ValueError):
            lat = lon = None
        seen.add(slug)
        rows.append({
            "title": attrs.get("title", slug),
            "start": start,
            "end": max(start, end),
            "time": f"{attrs.get('start-time', '?')} - {attrs.get('end-time', '?')}",
            "borough": attrs.get("borough") or "Unknown",
            "address": attrs.get("address"),
            "category": attrs.get("category"),
            "description": attrs.get("description"),
            "frequency": attrs.get("frequency"),
            "lat": lat,
            "lon": lon,
            "link": f"{NYCFORFREE_URL}/events/{slug}",
        })

    _freebies_cache.update(fetched_at=time.time(), rows=rows)
    return rows


def find_freebies(
    start_date: str | None = None,
    end_date: str | None = None,
    borough: str | None = None,
    category: str | None = None,
    keyword: str | None = None,
    limit: int = 10,
) -> str:
    """Free pop-ups, giveaways, sampling events and brand activations from NYC for FREE."""
    sat, sun = _next_weekend(_today())
    try:
        start = _parse_date(start_date, sat)
        end = _parse_date(end_date, sun)
    except ValueError:
        return _err("Dates must look like YYYY-MM-DD, e.g. 2026-10-03.")

    try:
        rows = _load_freebies()
    except requests.RequestException as e:
        return _err(f"nycforfree.co could not be reached: {e}. Fall back to find_free_events (NYC Parks) and tell the user.")

    wanted_borough = borough.strip().lower().replace("the ", "") if borough else None
    wanted_category = category.strip().lower() if category else None
    wanted_keyword = keyword.strip().lower() if keyword else None

    results = []
    for r in rows:
        if r["end"] < start or r["start"] > end:
            continue
        if wanted_borough and wanted_borough not in r["borough"].lower():
            continue
        if wanted_category and wanted_category not in (r["category"] or "").lower():
            continue
        haystack = f"{r['title']} {r['description'] or ''} {r['category'] or ''}".lower()
        if wanted_keyword and wanted_keyword not in haystack:
            continue
        first_day = max(r["start"], start)  # a day the user can actually go
        results.append({
            "title": r["title"],
            "date": first_day.isoformat(),
            "runs": f"{r['start']:%b %d} - {r['end']:%b %d}" if r["end"] != r["start"] else f"{r['start']:%b %d}",
            "time": r["time"],
            "borough": r["borough"],
            "address": r["address"],
            "category": r["category"],
            "description": r["description"],
            "lat": r["lat"],
            "lon": r["lon"],
            "link": r["link"],
        })

    results.sort(key=lambda r: (r["date"], r["title"]))
    limit = max(1, min(int(limit), 20))
    return json.dumps({
        "searched": f"{start} to {end}",
        "total_matches": len(results),
        "showing": min(limit, len(results)),
        "source": "NYC for FREE (nycforfree.co). Credit them and include the link when sharing results.",
        "events": results[:limit],
    })


# --- Tool 2 (external data): forecast for a date ---


def get_forecast(date_str: str | None = None) -> str:
    """Forecast for New York City on one date (defaults to today)."""
    try:
        target = _parse_date(date_str, _today())
    except ValueError:
        return _err("date must look like YYYY-MM-DD, e.g. 2026-10-03.")
    try:
        return json.dumps(_forecast(target))
    except requests.RequestException as e:
        return _err(f"Weather service failed: {e}. Forecasts only reach about 16 days ahead.")


# --- Tool 3 (original): curbside freebie score ---

# 1-10: how reliably a neighborhood has stoops, curb piles and dense renter turnover.
NEIGHBORHOODS = {
    "upper west side": 9, "upper east side": 9, "park slope": 9, "brooklyn heights": 8,
    "carroll gardens": 8, "cobble hill": 8, "prospect heights": 8, "fort greene": 8,
    "williamsburg": 7, "greenpoint": 7, "bushwick": 7, "astoria": 7, "long island city": 6,
    "harlem": 7, "washington heights": 6, "west village": 7, "east village": 7,
    "chelsea": 6, "hoboken": 6, "jackson heights": 6, "sunnyside": 6, "bed-stuy": 6,
}


def curb_hunt_score(neighborhood: str, date_str: str | None = None) -> str:
    """0-100 score for how good a day is for hunting curbside freebies, with reasons."""
    try:
        target = _parse_date(date_str, _next_weekend(_today())[0])
    except ValueError:
        return _err("date must look like YYYY-MM-DD, e.g. 2026-10-03.")

    key = neighborhood.strip().lower()
    hood = NEIGHBORHOODS.get(key)
    reasons, score = [], 30

    if hood is None:
        hood = 5
        reasons.append(f"'{neighborhood}' isn't in my neighborhood list, so I assumed an average area.")
    else:
        score += hood * 3
        reasons.append(f"{neighborhood.title()} has dense stoop/curb culture (rated {hood}/10).")

    dom, wd = target.day, target.weekday()
    days_in_month = (date(target.year + (target.month == 12), target.month % 12 + 1, 1) - timedelta(days=1)).day
    if dom >= days_in_month - 2 or dom <= 2:
        score += 20
        reasons.append("Month-end/start is peak move-out time, so furniture hits the curb.")
    elif dom >= days_in_month - 6:
        score += 8
        reasons.append("Approaching month-end: early movers are starting to purge.")
    if wd in (5, 6):
        score += 10
        reasons.append("Weekends are when people clear out and when you have time to look.")
    if wd == 6:
        score += 5
        reasons.append("Sunday evening is trash-night eve, so good pieces get put out.")

    weather = None
    try:
        weather = _forecast(target)
        rain = weather["rain_chance_pct"] or 0
        if rain >= 60:
            score -= 20
            reasons.append(f"{rain}% chance of rain: items get soaked and ruined, and fewer people put things out.")
        elif rain >= 30:
            score -= 8
            reasons.append(f"{rain}% chance of rain: bring a tarp for fabric items.")
        else:
            reasons.append("Dry forecast, so items on the curb stay in good shape.")
    except requests.RequestException:
        reasons.append("Couldn't fetch the forecast, so weather isn't factored in.")

    score = max(0, min(100, score))
    label = "great" if score >= 75 else "good" if score >= 55 else "so-so" if score >= 35 else "poor"
    return json.dumps({
        "neighborhood": neighborhood,
        "date": target.isoformat(),
        "score": score,
        "verdict": label,
        "reasons": reasons,
        "weather": weather,
        "known_neighborhoods": sorted(NEIGHBORHOODS) if hood == 5 and key not in NEIGHBORHOODS else None,
    })


# --- Tool 4 (original): estimated wait time ---

WITLF_URL = "https://www.whatisthislinefor.nyc/"

# (keywords, typical queue minutes, why). The highest-matching group wins.
WAIT_PROFILES = [
    (("free food", "tasting", "sample", "giveaway", "free stuff", "swag", "pop-up", "popup", "sample sale", "opening"),
     35, "free food/giveaways and pop-ups draw lines"),
    (("concert", "festival", "fair", "market", "parade", "fireworks", "block party", "street fest"),
     25, "big public events have entry and food lines"),
    (("movie", "film", "cinema", "screening"),
     20, "outdoor movies fill up, and good spots go early"),
    (("museum", "tour", "exhibit", "garden"),
     15, "popular attractions can have an entry queue"),
    (("yoga", "fitness", "class", "workshop", "storytime", "kids", "walk", "hike", "dance"),
     5, "classes and workshops rarely have a queue"),
]


def _parse_clock(text: str | None) -> int | None:
    """'8:00 am' -> minutes since midnight."""
    text = (text or "").lower()
    if "noon" in text:
        return 720
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", text)
    if not m:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3) == "p" else 0)
    return hour * 60 + int(m.group(2) or 0)


def _fmt_clock(minutes: float) -> str:
    minutes = int(round(minutes))
    hour, mins = divmod(minutes, 60)
    return f"{(hour % 12) or 12}:{mins:02d} {'am' if hour % 24 < 12 else 'pm'}"


def _estimate_wait(text: str, target: date, start_min: int | None, rain_pct: float | None) -> dict:
    """Heuristic queue estimate. This is a guess, not live data."""
    text = text.lower()
    base, why = 8, "no strong draw detected, so a short wait is typical"
    for keywords, minutes, reason in WAIT_PROFILES:
        if any(k in text for k in keywords) and minutes > base:
            base, why = minutes, reason
    reasons, factor = [why], 1.0

    if target.weekday() >= 5:
        factor *= 1.25
        reasons.append("weekend crowds (+25%)")
    if start_min is not None:
        if 660 <= start_min <= 840 or 1020 <= start_min <= 1200:
            factor *= 1.3
            reasons.append("starts at a peak time, around lunch or after work (+30%)")
        elif start_min < 600:
            factor *= 0.7
            reasons.append("early start, so smaller crowds (-30%)")
    if rain_pct is not None and rain_pct >= 60:
        factor *= 0.75
        reasons.append(f"{int(rain_pct)}% chance of rain thins outdoor crowds (-25%)")

    minutes = max(0, int(round(base * factor / 5)) * 5)
    label = "no wait" if minutes == 0 else "short" if minutes <= 10 else "moderate" if minutes <= 25 else "long"
    return {"minutes": minutes, "label": label, "reasons": reasons}


def estimate_wait_time(name: str, kind: str | None = None, date_str: str | None = None, time_str: str | None = None) -> str:
    """Estimated queue length for an event or place, from type, day, time and weather."""
    try:
        target = _parse_date(date_str, _next_weekend(_today())[0])
    except ValueError:
        return _err("date must look like YYYY-MM-DD, e.g. 2026-10-03.")

    rain = None
    try:
        rain = _forecast(target)["rain_chance_pct"]
    except requests.RequestException:
        pass

    est = _estimate_wait(f"{name} {kind or ''}", target, _parse_clock(time_str), rain)
    return json.dumps({
        "name": name,
        "date": target.isoformat(),
        "estimated_wait_minutes": est["minutes"],
        "level": est["label"],
        "why": est["reasons"],
        "disclaimer": "This is an estimate from event type, day, time and weather, not live data.",
        "live_reports": f"For real-time crowd reports from other New Yorkers, check {WITLF_URL}",
    })


# --- Tool 5 (original): timed itinerary ---


LANDMARKS = {
    "columbia university": (40.8075, -73.9626), "nyu": (40.7295, -73.9965), "washington square": (40.7308, -73.9973),
    "times square": (40.7580, -73.9855), "union square": (40.7359, -73.9911), "grand central": (40.7527, -73.9772),
    "penn station": (40.7506, -73.9935), "central park": (40.7829, -73.9654), "brooklyn bridge": (40.7061, -73.9969),
    "downtown brooklyn": (40.6928, -73.9903), "barclays center": (40.6826, -73.9754), "williamsburg": (40.7081, -73.9571),
    "park slope": (40.6710, -73.9777), "astoria": (40.7644, -73.9235), "long island city": (40.7447, -73.9485),
    "flushing": (40.7675, -73.8330), "harlem": (40.8116, -73.9465), "jackson heights": (40.7557, -73.8831),
    "world trade center": (40.7127, -74.0134), "staten island ferry": (40.7013, -74.0132), "bronx zoo": (40.8506, -73.8770),
    "yankee stadium": (40.8296, -73.9262), "fordham": (40.8610, -73.8890), "prospect park": (40.6602, -73.9690),
    "brooklyn museum": (40.6712, -73.9636), "lower east side": (40.7150, -73.9843), "east village": (40.7265, -73.9815),
    "west village": (40.7358, -74.0036), "soho": (40.7233, -74.0030), "chelsea": (40.7465, -74.0014),
    "midtown": (40.7549, -73.9840), "upper west side": (40.7870, -73.9754), "upper east side": (40.7736, -73.9566),
}
MAX_LEG_MIN = 45  # don't send people across the city for one stop


def _resolve_place(place: str) -> tuple[float, float, str] | None:
    """Turn a place name into (lat, lon, name): known NYC landmarks first, then Open-Meteo geocoding."""
    key = place.strip().lower()
    for name, (lat, lon) in LANDMARKS.items():
        if name in key or key in name:
            return lat, lon, name.title()
    try:
        results = requests.get(GEOCODE_URL, params={"name": place, "count": 5}, timeout=10).json().get("results") or []
    except (requests.RequestException, ValueError):
        return None
    for r in results:  # only accept places inside the NYC area
        if 40.4 <= r["latitude"] <= 41.0 and -74.3 <= r["longitude"] <= -73.6:
            return r["latitude"], r["longitude"], r["name"]
    return None


def _travel(a: tuple | None, b: tuple | None) -> tuple[int, str]:
    """(minutes, mode) between two (lat, lon) points; a guess when coordinates are missing."""
    if not a or not b:
        return 20, "unknown route"
    km = _haversine_km(a[0], a[1], b[0], b[1])
    if km < 1.5:
        return max(1, round(km / 4.8 * 60)), "walk"
    return round(12 + km / 20 * 60), "subway/bus"


def build_itinerary(
    events: list[dict],
    date_str: str | None = None,
    start_place: str | None = None,
    start_lat: float | None = None,
    start_lon: float | None = None,
    max_stops: int = 5,
    day_start: str | None = None,
    day_end: str = "9:00 pm",
) -> str:
    """Turn events from find_free_events / find_freebies into a timed, geographically sensible plan.

    Short events (<= 2.5 hours) are fixed: be there when they start. Longer or all-day events are
    flexible: visit for about an hour at any time inside their window. Each next stop is the one
    that is closest and fits, so the route does not zigzag across the city.
    """
    if not events:
        return _err("Pass the events from find_free_events / find_freebies as 'events'. Call one of them first.")
    try:
        target = _parse_date(date_str, _next_weekend(_today())[0])
    except ValueError:
        return _err("date must look like YYYY-MM-DD, e.g. 2026-10-03.")

    if day_start:
        earliest_day = _parse_clock(day_start) or 540
    elif target == _today():  # "now": next quarter hour
        now = _now()
        earliest_day = -(-(now.hour * 60 + now.minute) // 15) * 15
    else:
        earliest_day = 540
    latest_day = _parse_clock(day_end) or 1260

    notes, start_name = [], None
    if (start_lat is None or start_lon is None) and start_place:
        found = _resolve_place(start_place)
        if found:
            start_lat, start_lon, start_name = found
        else:
            notes.append(f"Couldn't locate '{start_place}', so travel to the first stop is a guess. Ask for a neighborhood or landmark.")
    pos = (start_lat, start_lon) if start_lat is not None and start_lon is not None else None

    rain = None
    try:
        rain = _forecast(target)["rain_chance_pct"]
    except requests.RequestException:
        pass

    candidates, dropped = [], []
    for ev in events:
        title = html_lib.unescape(str(ev.get("title") or "Untitled event"))
        if "cancel" in title.lower():
            dropped.append({"title": title, "reason": "cancelled"})
            continue
        if ev.get("date") and ev["date"] != target.isoformat():
            dropped.append({"title": title, "reason": f"it's on {ev['date']}, not {target.isoformat()}"})
            continue
        start_text, _, end_text = str(ev.get("time") or "").partition(" - ")
        start = _parse_clock(ev.get("start_time") or start_text)
        if start is None:
            dropped.append({"title": title, "reason": "no usable start time"})
            continue
        if start < 300:  # feeds use 0:00 am for "no start time given"
            start = 540
        end = _parse_clock(ev.get("end_time") or end_text)
        if end is None or end <= start:
            end = start + 90
        try:
            point = (float(ev["lat"]), float(ev["lon"]))
        except (KeyError, TypeError, ValueError):
            point = None
        if end <= earliest_day:
            dropped.append({"title": title, "reason": "already over by the time the plan starts"})
            continue
        flexible = (end - start) > 150
        text = f"{title} {ev.get('categories') or ev.get('category') or ''} {ev.get('park') or ev.get('address') or ''}"
        stay = 60 if flexible else end - start
        if flexible and any(k in text.lower() for k in ("festival", "fair", "market", "museum", "movie", "parade")):
            stay = 90
        candidates.append({"title": title, "start": start, "end": end, "point": point, "ev": ev,
                           "flexible": flexible, "stay": stay, "text": text})

    stops, clock = [], earliest_day
    while candidates and len(stops) < max(1, int(max_stops)):
        best = None
        for c in candidates:
            travel_min, mode = _travel(pos, c["point"]) if pos else (0, "start here")
            if pos and travel_min > MAX_LEG_MIN:
                continue
            wait = _estimate_wait(c["text"], target, c["start"], rain)
            earliest = clock + travel_min
            if c["flexible"]:
                arrive = max(earliest, c["start"])
                depart = arrive + wait["minutes"] + c["stay"]
                fits = depart <= c["end"] + wait["minutes"] and depart <= latest_day
            else:
                arrive = max(earliest, c["start"] - wait["minutes"], earliest_day)
                depart = c["end"]
                fits = earliest <= c["start"] + 10 and depart <= latest_day
            if not fits:
                continue
            score = (travel_min + 0.5 * max(0, arrive - earliest), c["start"])  # close first, little idle time
            if best is None or score < best[0]:
                best = (score, c, travel_min, mode, wait, arrive, depart)
        if best is None:
            break
        _, c, travel_min, mode, wait, arrive, depart = best
        ev = c["ev"]
        stops.append({
            "order": len(stops) + 1,
            "title": c["title"],
            "where": ev.get("park") or ev.get("address") or ev.get("location"),
            "arrive": _fmt_clock(arrive),
            "leave_by": _fmt_clock(depart),
            "event_window": f"{_fmt_clock(c['start'])} - {_fmt_clock(c['end'])}",
            "type": "flexible visit" if c["flexible"] else "fixed time",
            "travel_from_previous_min": travel_min,
            "getting_there": mode,
            "estimated_wait_min": wait["minutes"],
            "wait_level": wait["label"],
            "wait_why": wait["reasons"],
            "link": ev.get("link"),
        })
        candidates.remove(c)
        clock, pos = depart, (c["point"] or pos)

    for c in candidates:
        dropped.append({"title": c["title"],
                        "reason": f"doesn't fit: too far from the other stops (over {MAX_LEG_MIN} min), clashes, or no time left"})

    return json.dumps({
        "date": target.isoformat(),
        "starting_from": start_name or start_place or "not given",
        "plan_starts_at": _fmt_clock(earliest_day),
        "rain_chance_pct": rain,
        "stops": stops,
        "skipped": dropped,
        "notes": notes,
        "disclaimer": "Waits are estimates, not live data. For real-time crowd reports see " + WITLF_URL,
    }, ensure_ascii=False)


# --- Tool 6 (original): hunting checklist ---

CHECKLISTS = {
    "furniture": {
        "bring": ["work gloves", "tape measure", "a friend with a car or a dolly", "flashlight"],
        "check": ["wobbly joints", "water stains", "smell (smoke/mildew)"],
        "avoid": ["upholstered items with any sign of bed bugs (dark specks in seams)", "mattresses"],
    },
    "books": {
        "bring": ["tote bag", "hand sanitizer"],
        "check": ["mold or damp smell", "missing pages"],
        "avoid": ["boxes left in the rain"],
    },
    "clothes": {
        "bring": ["tote bags", "hand sanitizer"],
        "check": ["stains", "odors", "zippers"],
        "avoid": ["anything that smells musty; wash everything on hot before wearing"],
    },
    "electronics": {
        "bring": ["a portable charger", "an adapter kit"],
        "check": ["frayed cords", "cracked screens", "missing power supply"],
        "avoid": ["CRT TVs (heavy and costly to recycle)", "anything with a swollen battery"],
    },
    "plants": {
        "bring": ["a bag or box", "a small trowel"],
        "check": ["pests on leaves and soil", "root rot"],
        "avoid": ["plants with visible bugs, since they can infest your other plants"],
    },
}


def freebie_checklist(kind: str) -> str:
    """Practical hunting checklist for one kind of item."""
    key = kind.strip().lower()
    for name, data in CHECKLISTS.items():
        if key == name or key.rstrip("s") == name.rstrip("s"):
            return json.dumps({"kind": name, **data})
    return _err(f"No checklist for '{kind}'. Available kinds: {sorted(CHECKLISTS)}")


# --- What the model sees ---

EVENTS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_free_events",
            "description": (
                "Search free NYC Parks events (fitness classes, concerts, movies, workshops, tours) "
                "in a date range. Defaults to the coming Saturday-Sunday. Returns titles, times, "
                "boroughs, parks, coordinates and links."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "First day, YYYY-MM-DD. Omit for this weekend."},
                    "end_date": {"type": "string", "description": "Last day, YYYY-MM-DD. Omit for this weekend."},
                    "borough": {"type": "string", "description": "Manhattan, Brooklyn, Queens, Bronx or Staten Island. Omit for all."},
                    "keyword": {"type": "string", "description": "Word to match in title/description/category, e.g. 'yoga', 'movie', 'kids'."},
                    "limit": {"type": "integer", "description": "Max events to return (1-25, default 12)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_freebies",
            "description": (
                "Find free pop-ups, giveaways, sampling events, brand activations, free food and drink, and "
                "other freebies in NYC from the NYC for FREE listings. Defaults to the coming Saturday-Sunday. "
                "Use this for 'free stuff', giveaways and pop-ups; use find_free_events for parks, classes "
                "and community events. Returns title, dates, time, borough, address, coordinates and a link."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "First day, YYYY-MM-DD. Omit for this weekend."},
                    "end_date": {"type": "string", "description": "Last day, YYYY-MM-DD. Omit for this weekend."},
                    "borough": {"type": "string", "description": "Manhattan, Brooklyn, Queens, Bronx or Staten Island. Omit for all."},
                    "category": {"type": "string", "description": "e.g. Food, Beauty, Fashion, Drink, Music, Kids, Art, Wellness, TV/Movies."},
                    "keyword": {"type": "string", "description": "Word to match in title/description, e.g. 'coffee', 'tote'."},
                    "limit": {"type": "integer", "description": "Max results (1-20, default 10)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_forecast",
            "description": "Forecast (high/low in F, chance of rain) for New York City on one date, up to about 16 days ahead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_str": {"type": "string", "description": "Date as YYYY-MM-DD. Omit for today."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "curb_hunt_score",
            "description": (
                "Score 0-100 how good a given day is for hunting free curbside items (furniture, books, etc.) "
                "in a NYC neighborhood, using move-out timing, day of week, neighborhood and forecast. "
                "Use this for questions about free items or giveaways, not events."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "neighborhood": {"type": "string", "description": "NYC neighborhood, e.g. 'Park Slope'."},
                    "date_str": {"type": "string", "description": "Date as YYYY-MM-DD. Omit for this Saturday."},
                },
                "required": ["neighborhood"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_wait_time",
            "description": (
                "Estimate how long the line or wait will be at an event or place, based on its type, the day, "
                "the start time and the weather. It is a heuristic estimate, not live data. Use it when the "
                "user asks about lines, crowds or how early to arrive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Event or place name, e.g. 'Brooklyn Flea market'."},
                    "kind": {"type": "string", "description": "What it is, e.g. 'free food', 'concert', 'yoga class'. Optional."},
                    "date_str": {"type": "string", "description": "Date as YYYY-MM-DD. Omit for this Saturday."},
                    "time_str": {"type": "string", "description": "Start time, e.g. '2:00 pm'. Optional."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_itinerary",
            "description": (
                "Build a timed one-day plan from events found with find_free_events: it orders them by time, "
                "adds travel time between stops, estimates the wait at each, and skips events that clash or "
                "can't be reached in time. Pass the events exactly as returned by find_free_events."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "events": {
                        "type": "array",
                        "description": "Candidate events from find_free_events.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "date": {"type": "string", "description": "YYYY-MM-DD"},
                                "time": {"type": "string", "description": "e.g. '8:00 am - 10:00 am'"},
                                "park": {"type": "string"},
                                "categories": {"type": "string"},
                                "lat": {"type": "number"},
                                "lon": {"type": "number"},
                                "link": {"type": "string"},
                            },
                            "required": ["title", "time"],
                        },
                    },
                    "date_str": {"type": "string", "description": "Day to plan, YYYY-MM-DD. Omit for this Saturday."},
                    "start_place": {"type": "string", "description": "Where the user starts, as a place name, e.g. 'Columbia University', 'Williamsburg'. Preferred over lat/lon."},
                    "start_lat": {"type": "number", "description": "Starting latitude. Only if no start_place."},
                    "start_lon": {"type": "number", "description": "Starting longitude. Only if no start_place."},
                    "max_stops": {"type": "integer", "description": "Maximum stops (default 5)."},
                    "day_start": {"type": "string", "description": "When the day begins, e.g. '10:00 am'. Omit to start now when planning today, else 9:00 am."},
                    "day_end": {"type": "string", "description": "When the user wants to be done, e.g. '6:00 pm'. Default 9:00 pm."},
                },
                "required": ["events"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "freebie_checklist",
            "description": "What to bring, check and avoid when picking up free items of one kind.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "One of: furniture, books, clothes, electronics, plants."},
                },
                "required": ["kind"],
            },
        },
    },
]

EVENTS_TOOL_MAP = {
    "find_free_events": find_free_events,
    "find_freebies": find_freebies,
    "get_forecast": get_forecast,
    "curb_hunt_score": curb_hunt_score,
    "estimate_wait_time": estimate_wait_time,
    "build_itinerary": build_itinerary,
    "freebie_checklist": freebie_checklist,
}
