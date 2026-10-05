"""The tools the harness can run, and the JSON that describes them to the model."""

import json

import requests

from apartment import APARTMENT_TOOL_MAP, APARTMENT_TOOLS
from events import EVENTS_TOOL_MAP, EVENTS_TOOLS

# Open-Meteo is free and needs no API key.
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def get_weather(location: str) -> str:
    """Get the current weather for a location."""
    try:
        places = requests.get(GEOCODE_URL, params={"name": location, "count": 1}, timeout=10).json()
        if not places.get("results"):
            return json.dumps({"error": f"City '{location}' was not found."})
        place = places["results"][0]

        current = requests.get(
            FORECAST_URL,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
            },
            timeout=10,
        ).json()["current"]

        return json.dumps({
            "location": place["name"],
            "temp_f": current["temperature_2m"],
            "humidity": current["relative_humidity_2m"],
            "wind_mph": current["wind_speed_10m"],
        })
    except (requests.RequestException, KeyError, ValueError, TypeError) as e:
        # The model cannot see an exception. Return something it can reason about.
        # KeyError and the others cover an error reply from the weather service that lacks the expected fields.
        return json.dumps({"error": f"Weather service failed: {type(e).__name__}: {e}"})


# What the model sees: the "set notes" in the screenplay.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather (temperature, humidity, wind) for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name, e.g. 'New York'"},
                },
                "required": ["location"],
            },
        },
    },
    *APARTMENT_TOOLS,  # check_building, check_landlord, check_neighborhood (apartment.py)
    *EVENTS_TOOLS,  # find_free_events, find_freebies, get_forecast, curb_hunt_score, estimate_wait_time, build_itinerary, freebie_checklist (events.py)
]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {"get_weather": get_weather, **APARTMENT_TOOL_MAP, **EVENTS_TOOL_MAP}
assert len(TOOL_MAP) == len(TOOLS), "two tools share a name"


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}"})
