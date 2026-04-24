import json
import os
import re
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
import boto3
import google.auth.transport.requests
from google.oauth2 import service_account

TOMTOM_API_KEY   = os.environ["TOMTOM_API_KEY"]
SSM_CONFIG       = os.environ.get("SSM_CONFIG",       "/traffic-alert/config")
SSM_STATE        = os.environ.get("SSM_STATE",        "/traffic-alert/state")
SSM_GOOGLE_CREDS = os.environ.get("SSM_GOOGLE_CREDS", "/traffic-alert/google-credentials")
REGION           = os.environ.get("AWS_REGION",       "eu-west-2")

ssm = boto3.client("ssm", region_name=REGION)


# ---------------------------------------------------------------------------
# SSM helpers
# ---------------------------------------------------------------------------

def ssm_get(name: str) -> dict | list | None:
    try:
        return json.loads(ssm.get_parameter(Name=name)["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        return None


def ssm_put(name: str, value: dict | list) -> None:
    ssm.put_parameter(Name=name, Value=json.dumps(value), Type="String", Overwrite=True)


# ---------------------------------------------------------------------------
# TomTom
# ---------------------------------------------------------------------------

def build_route_url(origin: str, destination: str, waypoints: list[str], traffic: bool) -> str:
    stops = ":".join([origin] + waypoints + [destination])
    return (
        f"https://api.tomtom.com/routing/1/calculateRoute/{stops}/json"
        f"?key={TOMTOM_API_KEY}&travelMode=car&traffic={'true' if traffic else 'false'}"
    )


def fetch_travel_time(url: str) -> int:
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    return data["routes"][0]["summary"]["travelTimeInSeconds"]


def get_times(route: dict) -> tuple[int, int]:
    """Returns (live_secs, free_flow_secs)."""
    origin      = route["origin"]
    destination = route["destination"]
    waypoints   = route.get("waypoints", [])
    live = fetch_travel_time(build_route_url(origin, destination, waypoints, traffic=True))
    ff   = fetch_travel_time(build_route_url(origin, destination, waypoints, traffic=False))
    return live, ff


def _nominatim(query: str) -> str | None:
    url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(query)}&format=json&limit=1"
    req = urllib.request.Request(url, headers={"User-Agent": "traffic-alert/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if data:
            return f"{data[0]['lat']},{data[0]['lon']}"
    except Exception as e:
        print(f"  Geocode error for '{query}': {e}")
    return None


def geocode(address: str) -> str | None:
    """Return 'lat,lon' for a plain-text address using Nominatim (OpenStreetMap).
    Falls back to just the UK postcode if the full address returns no results."""
    result = _nominatim(address)
    if result:
        return result

    postcode_match = re.search(r"\b[A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2}\b", address, re.IGNORECASE)
    if postcode_match:
        postcode = postcode_match.group()
        print(f"  Geocode: no results for full address, trying postcode '{postcode}'")
        result = _nominatim(postcode)
        if result:
            return result

    print(f"  Geocode: no results for '{address}'")
    return None


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------

def fetch_google_calendar_events(calendar_id: str, creds_dict: dict, today: str) -> list[dict]:
    """
    Fetch timed events that have a location set from Google Calendar for today (UTC).
    All-day events and events without a location are skipped.
    Returns a list of dicts: {name, location, date, time_utc}.
    """
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    credentials.refresh(google.auth.transport.requests.Request())

    time_min = urllib.parse.quote(f"{today}T00:00:00Z")
    time_max = urllib.parse.quote(f"{today}T23:59:59Z")
    cal_id   = urllib.parse.quote(calendar_id, safe="")
    url = (
        f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events"
        f"?timeMin={time_min}&timeMax={time_max}&singleEvents=true&orderBy=startTime"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {credentials.token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())

    events = []
    for item in data.get("items", []):
        dt_str   = item.get("start", {}).get("dateTime")
        if not dt_str:
            continue  # skip all-day events
        location = item.get("location", "").strip()
        if not location:
            continue  # no destination to drive to

        dt = datetime.fromisoformat(dt_str).astimezone(timezone.utc)
        events.append({
            "name":     item.get("summary", "Unnamed event"),
            "location": location,
            "date":     dt.strftime("%Y-%m-%d"),
            "time_utc": dt.strftime("%H:%M"),
        })
    return events


# ---------------------------------------------------------------------------
# ntfy
# ---------------------------------------------------------------------------

def notify(topic: str, title: str, message: str, priority: str = "default") -> None:
    payload = message.encode("utf-8")
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=payload,
        headers={
            "Title":       title.encode("utf-8"),
            "Priority":    priority,
            "Tags":        "car",
            "Content-Type": "text/plain; charset=utf-8",
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)


# ---------------------------------------------------------------------------
# Alert logic
# ---------------------------------------------------------------------------

def evaluate_route(
    route: dict,
    check: dict,
    now: datetime,
    threshold_pct: float,
    prev_alerted: bool,
    topic: str,
    alert_only: bool = False,
) -> bool:
    route_name = route["name"]
    print(f"  Checking '{route_name}'...")

    try:
        live_secs, ff_secs = get_times(route)
    except Exception as e:
        print(f"  TomTom error for '{route_name}': {e}")
        return prev_alerted

    live_mins = round(live_secs / 60, 1)
    ff_mins   = round(ff_secs / 60, 1)
    delay_pct = round((live_secs - ff_secs) / ff_secs * 100, 1) if ff_secs > 0 else 0

    print(f"  Live: {live_mins}min | Usual: {ff_mins}min | Delay: {delay_pct}%")

    alerted = False
    alert_reasons = []

    # Condition 1: will miss target arrival time
    target_arrival_utc = check.get("target_arrival_utc")
    if target_arrival_utc:
        t_hour, t_min = map(int, target_arrival_utc.split(":"))
        target_dt = now.replace(hour=t_hour, minute=t_min, second=0, microsecond=0)
        if target_dt < now:
            target_dt += timedelta(days=1)
        eta = now + timedelta(seconds=live_secs)
        if eta > target_dt:
            miss_mins = round((eta - target_dt).total_seconds() / 60)
            eta_str   = eta.strftime("%H:%M")
            alert_reasons.append(
                f"ETA {eta_str} — you'll miss your {target_arrival_utc} target by {miss_mins}min."
            )
            alerted = True

    # Condition 2: significantly above free-flow
    if delay_pct >= threshold_pct:
        alert_reasons.append(
            f"Traffic is {delay_pct:.0f}% above normal ({live_mins}min vs {ff_mins}min usual)."
        )
        alerted = True

    if alerted:
        message = "\n".join(alert_reasons) + f"\n\nRoute: {route_name}"
        print(f"  ALERT: {message}")
        notify(topic, f"🚗 Traffic Alert — {route_name}", message, priority="high")
    elif not alert_only:
        message = f"Traffic is fine. Journey: {live_mins}min (usual: {ff_mins}min).\n\nRoute: {route_name}"
        print(f"  ALL CLEAR: {message}")
        notify(topic, f"✅ All Clear — {route_name}", message, priority="default")

    return alerted


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def _round_to_5(mins: float) -> int:
    """Round a duration to the nearest 5 minutes (minimum 5)."""
    return max(5, round(mins / 5) * 5)


def _time_minus_minutes(time_utc: str, minutes: int) -> str | None:
    """Subtract minutes from a HH:MM string. Returns None if result crosses midnight."""
    h, m = map(int, time_utc.split(":"))
    total = h * 60 + m - minutes
    if total < 0:
        return None
    return f"{total // 60:02d}:{total % 60:02d}"


def handle_calendar(
    profiles_by_name: dict,
    calendar_events: list,
    state: dict,
    now: datetime,
    now_time: str,
) -> None:
    """
    For each calendar event matching today:
      - On first encounter: geocode the location (if needed), fetch free-flow time,
        and compute check windows (2× and 1.5× free-flow before the event, rounded to 5 min).
      - At each computed check time: run a traffic check from home to the event.

    State keys:
      cal_init|{profile}|{name}|{date}  →  {destination, check_times_utc, free_flow_mins}
      cal|{profile}|{name}|{date}|{HH:MM}  →  True/False (last alerted)
    """
    today = now.strftime("%Y-%m-%d")

    for cal_event in calendar_events:
        if cal_event["date"] != today:
            continue

        profile_name = cal_event["profile"]
        profile = profiles_by_name.get(profile_name)
        if not profile:
            print(f"  Calendar: unknown profile '{profile_name}', skipping '{cal_event['name']}'")
            continue

        home = profile.get("home")
        if not home:
            print(f"  Calendar: profile '{profile_name}' has no 'home' configured, skipping")
            continue

        event_name = cal_event["name"]
        event_date = cal_event["date"]
        event_time = cal_event["time_utc"]
        init_key   = f"cal_init|{profile_name}|{event_name}|{event_date}"

        # --- Initialise on first encounter ---
        if init_key not in state:
            print(f"  Calendar: initialising '{event_name}' for {profile_name}...")

            # Resolve lat/lon destination
            destination = geocode(cal_event["location"])
            if not destination:
                state[init_key] = {"error": "geocode failed"}
                continue

            # Free-flow time from home
            try:
                _, ff_secs = get_times({"origin": home, "destination": destination, "waypoints": []})
            except Exception as e:
                print(f"  Calendar: TomTom error initialising '{event_name}': {e}")
                state[init_key] = {"error": "tomtom failed"}
                continue

            ff_mins = ff_secs / 60
            offsets = sorted(
                {_round_to_5(ff_mins * 2), _round_to_5(ff_mins * 1.5)},
                reverse=True,
            )
            check_times = [
                t for t in (_time_minus_minutes(event_time, o) for o in offsets)
                if t is not None
            ]
            state[init_key] = {
                "destination":     destination,
                "check_times_utc": check_times,
                "free_flow_mins":  round(ff_mins, 1),
            }
            print(
                f"  Calendar: '{event_name}' checks at {check_times} "
                f"(free-flow: {round(ff_mins)}min, event at {event_time} UTC)"
            )

        init_data = state[init_key]
        if "error" in init_data:
            print(f"  Calendar: skipping '{event_name}' — init previously failed ({init_data['error']})")
            continue

        check_times = init_data["check_times_utc"]

        if now_time not in check_times:
            continue

        # --- Run traffic check ---
        print(f"  Calendar: running check for '{event_name}' ({profile_name})")
        route = {
            "name":        event_name,
            "origin":      home,
            "destination": init_data["destination"],
            "waypoints":   [],
        }
        check        = {"time_utc": now_time, "target_arrival_utc": event_time}
        state_key    = f"cal|{profile_name}|{event_name}|{event_date}|{now_time}"
        prev_alerted = state.get(state_key, False)

        alerted = evaluate_route(
            route=route,
            check=check,
            now=now,
            threshold_pct=float(profile.get("alert_threshold_pct", 20)),
            prev_alerted=prev_alerted,
            topic=profile["ntfy_topic"],
            alert_only=profile.get("notify_mode", {}).get("calendar", "always") == "alert_only",
        )
        state[state_key] = alerted


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def validate_config(config: dict) -> None:
    for profile in config["profiles"]:
        for route in profile["routes"]:
            seen = set()
            for check in route["checks"]:
                for day in check["days"]:
                    key = f"{day}|{check['time_utc']}"
                    if key in seen:
                        raise ValueError(
                            f"Duplicate check in profile '{profile['name']}', "
                            f"route '{route['name']}': {day} at {check['time_utc']}"
                        )
                    seen.add(key)

# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    # print(f"Event: {event}")
    # Allow time override for testing: {"test_time_utc": "07:15", "test_day": "MON"}
    if "test_time_utc" in event:
        now = datetime.now(timezone.utc)
        h, m = map(int, event["test_time_utc"].split(":"))
        now = now.replace(hour=h, minute=m, second=0, microsecond=0)
        now_time = event["test_time_utc"]
        now_day  = event.get("test_day", now.strftime("%a").upper())
    else:
        now = datetime.now(timezone.utc)
        now_time = now.strftime("%H:%M")
        now_day  = now.strftime("%a").upper()

    print(f"Running at {now.isoformat()} ({now_day} {now_time} UTC)")

    config = ssm_get(SSM_CONFIG)
    if not config:
        raise RuntimeError(f"No config found at SSM path {SSM_CONFIG}")

    validate_config(config)

    # State tracks alert history for both static and calendar checks
    state: dict = ssm_get(SSM_STATE) or {}

    # --- Static route checks ---
    for profile in config["profiles"]:
        topic         = profile["ntfy_topic"]
        threshold_pct = float(profile.get("alert_threshold_pct", 20))
        profile_name  = profile["name"]

        print(f"Profile: {profile_name}")
        for route in profile["routes"]:
            for check in route["checks"]:
                if now_day not in check["days"]:
                    continue
                if check["time_utc"] != now_time:
                    continue

                state_key    = f"{profile_name}|{route['name']}|{check['time_utc']}"
                prev_alerted = state.get(state_key, False)
                alert_only   = profile.get("notify_mode", {}).get("routes", "always") == "alert_only"

                alerted = evaluate_route(
                    route=route,
                    check=check,
                    now=now,
                    threshold_pct=threshold_pct,
                    prev_alerted=prev_alerted,
                    topic=topic,
                    alert_only=alert_only,
                )
                state[state_key] = alerted

    # --- Calendar checks ---
    google_creds = ssm_get(SSM_GOOGLE_CREDS)
    if google_creds:
        today             = now.strftime("%Y-%m-%d")
        profiles_by_name  = {p["name"]: p for p in config["profiles"]}
        cal_events: list  = []

        for profile in config["profiles"]:
            calendar_id = profile.get("calendar_id")
            if not calendar_id:
                continue
            try:
                events = fetch_google_calendar_events(calendar_id, google_creds, today)
                for e in events:
                    e["profile"] = profile["name"]
                cal_events.extend(events)
                print(f"  Google Calendar: {len(events)} event(s) today for {profile['name']}")
            except Exception as e:
                print(f"  Google Calendar error for '{profile['name']}': {e}")

        if cal_events:
            handle_calendar(profiles_by_name, cal_events, state, now, now_time)

    ssm_put(SSM_STATE, state)
    print("Done.")
    return {"status": "ok"}
