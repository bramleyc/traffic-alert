import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta
import boto3

TOMTOM_API_KEY = os.environ["TOMTOM_API_KEY"]
SSM_CONFIG     = os.environ.get("SSM_CONFIG",  "/traffic-alert/config")
SSM_STATE      = os.environ.get("SSM_STATE",   "/traffic-alert/state")
REGION         = os.environ.get("AWS_REGION",  "eu-west-2")

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

    print(f"  Live: {live_mins}min | Free-flow: {ff_mins}min | Delay: {delay_pct}%")

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
            f"Traffic is {delay_pct:.0f}% above normal ({live_mins}min vs {ff_mins}min free-flow)."
        )
        alerted = True

    if alerted:
        message = "\n".join(alert_reasons) + f"\n\nRoute: {route_name}"
        print(f"  ALERT: {message}")
        notify(topic, f"🚗 Traffic Alert — {route_name}", message, priority="high")
    else:
        # Always send all-clear on every check
        message = f"Traffic is fine. Journey: {live_mins}min (free-flow: {ff_mins}min).\n\nRoute: {route_name}"
        print(f"  ALL CLEAR: {message}")
        notify(topic, f"✅ All Clear — {route_name}", message, priority="default")

    return alerted


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def validate_config(config: dict) -> None:
    for route in config["routes"]:
        seen = set()
        for check in route["checks"]:
            for day in check["days"]:
                key = f"{day}|{check['time_utc']}"
                if key in seen:
                    raise ValueError(
                        f"Duplicate check in route '{route['name']}': {day} at {check['time_utc']}"
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
    
    topic          = config["ntfy_topic"]
    threshold_pct  = float(config.get("alert_threshold_pct", 20))

    # State tracks whether the previous check for each route+check sent an alert
    # Key: "{route_name}|{check_time_utc}"
    state: dict = ssm_get(SSM_STATE) or {}

    for route in config["routes"]:
        for check in route["checks"]:
            if now_day not in check["days"]:
                continue
            if check["time_utc"] != now_time:
                continue

            state_key    = f"{route['name']}|{check['time_utc']}"
            prev_alerted = state.get(state_key, False)

            alerted = evaluate_route(
                route=route,
                check=check,
                now=now,
                threshold_pct=threshold_pct,
                prev_alerted=prev_alerted,
                topic=topic,
            )
            state[state_key] = alerted

    ssm_put(SSM_STATE, state)
    print("Done.")
    return {"status": "ok"}
