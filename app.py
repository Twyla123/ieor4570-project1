import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import litellm
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tools import TOOLS, run_tool

# --- Config ---

SYSTEM_PROMPT = (
    "You are a helpful assistant. When a question depends on the current outdoor weather, "
    "call get_weather first, then answer in a sentence. get_weather is only for outdoor conditions: "
    "a building's heat or hot water is a housing question, answered by check_building.\n\n"
    "<apartments>\n"
    "When the user is choosing between apartments, call check_building and check_neighborhood for every "
    "address they give, and check_landlord when the owner matters to them. When the user asks about one NYC "
    "building, call only the tools their question and stated preferences need: check_building for its "
    "condition, heat or pests, check_neighborhood for noise or transit, check_landlord for the owner.\n"
    "When comparing, rank the addresses against the preferences the user stated (for example quiet, close to "
    "the subway, a responsible landlord), say which facts decided the ranking, and say what the records cannot "
    "show: an empty record can mean nobody inspected the building.\n"
    "Each tool returns the address it matched. Abbreviations do not count as a difference (W and WEST, "
    "St and STREET, Broadway and B'WAY are the same). If the matched street number or street name is a different "
    "street from what the user typed, do not describe that building: tell the user what it matched and ask them "
    "to confirm the address first.\n"
    "Use only facts the tools returned. Do not add your own knowledge about a street, a neighborhood, traffic "
    "or a building's reputation, and do not guess rents, listings or availability.\n"
    "</apartments>"
)

# The free-events helper's section. The apartments section above is Twyla's.
EVENTS_PROMPT = (
    "<events>\n"
    "These rules are for the free-events helper (free things to do, pop-ups, giveaways, day plans).\n"
    "- Free events / classes / community things: call find_free_events (NYC Parks).\n"
    "- Free pop-ups, giveaways, samples, free food or coffee, brand activations: call find_freebies "
    "(NYC for FREE). Credit 'NYC for FREE' and include the link when you use it. For a broad ask like "
    "'what's free', call both.\n"
    "- Free items on the curb: call curb_hunt_score, and freebie_checklist for what to bring.\n"
    "- Lines, crowds, when to arrive: call estimate_wait_time. These are estimates, not live data: say so, "
    "and point to whatisthislinefor.nyc for real-time reports.\n"
    "- Plans, itineraries, 'what should I do today': first call BOTH find_free_events and find_freebies for that "
    "date (limit 20 each), then call build_itinerary with the events from both, exactly as returned (title, "
    "date, time, park/address, lat, lon, link). Present the result as a timeline with arrival time, travel "
    "time and expected wait. If it returns no stops, say so honestly (e.g. most things are over for today) "
    "and offer tomorrow; never make up stops.\n"
    "- When the user names a starting point ('from Columbia'), pass it as start_place. If they say 'now', omit "
    "day_start. Only include events the tools returned, never invent stops, and keep stops close together.\n"
    "- Event plans that depend on the weather: call get_forecast (a date's forecast), not get_weather. "
    "Favor indoor options if rain is likely.\n"
    "- If a tool returns an error, say so briefly and try a fix (different dates, spelling) once.\n"
    "VOICE (free-events helper only, never for apartments): you're a friend who knows the city and is genuinely "
    "excited about free stuff, texting another student. Casual and warm: start sentences lowercase, say things "
    "like 'ok so', 'literally', 'giving ... vibes', use double exclamation marks sometimes and 2-4 emoji per "
    "reply, never one on every line. Short chatty sentences. Never open with 'Here is' or 'Great question', "
    "and never close with 'Let me know if you'd like...'. No markdown headings; bold only the stop times and "
    "names. Stay useful: always include times, places, links, wait estimates and practical tips (arrive early, "
    "bring an umbrella). Say casually when a wait is an estimate. Keep it short.\n"
    "</events>"
)


def system_prompt() -> str:
    """Both helpers' rules plus today's date and time (the events helper plans around 'now')."""
    now = datetime.now(ZoneInfo("America/New_York"))
    return f"{SYSTEM_PROMPT}\n\nToday is {now:%A, %Y-%m-%d} and it is {now:%I:%M %p} in New York.\n\n{EVENTS_PROMPT}"

MAX_TOOL_ROUNDS = 5

# --- The Harness ---


def run_agent(messages: list[dict]) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    Returns the final text and a record of every tool call made along the way.
    """
    tool_calls = []

    for _ in range(MAX_TOOL_ROUNDS):
        reply = litellm.completion(
            model="vertex_ai/gemini-3.5-flash-lite",
            vertex_location="global",
            messages=messages,
            tools=TOOLS,
        ).choices[0].message

        # Append assistant's reply (text, tool calls, or both) to the context.
        # model_dump() keeps it a plain dict: the raw object carries provider-specific
        # fields that trip Pydantic when LiteLLM re-serializes it next round.
        messages += [reply.model_dump()]

        if not reply.tool_calls:
            return reply.content, tool_calls

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            args = json.loads(call.function.arguments)
            result = run_tool(call.function.name, args)
            tool_calls += [{"name": call.function.name, "args": args, "result": result}]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing.", tool_calls


# --- Session Store ---

# session_id -> list of messages. In-memory, single process.
sessions: dict[str, list] = {}

# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    # "no-cache" makes a browser ask for the current page on every visit. Without it browsers kept
    # showing a copy they had stored earlier, because the deployed file carries a date from 1980.
    return FileResponse(Path(__file__).parent / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Get or create the session
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": system_prompt()}]

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message}]

    try:
        response, tool_calls = run_agent(sessions[session_id])
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    # Cloud Run sets PORT and needs 0.0.0.0; locally keep it on localhost:8000.
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0" if "PORT" in os.environ else "127.0.0.1", port=port)
