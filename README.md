# New in NYC

AI helpers for students who just landed in New York. Pick a helper, ask one focused question, and see which records it checked.

**Live app:** https://ieor4570-project1-git-770026520963.us-east1.run.app
(Sign in with a Columbia Google account if prompted.)

A three-student IEOR 4570 project (Project 1: A Tool-Calling Agent). Authors: usm2104, xz2843, sz3410.

## Why we built this

We are three students who came to New York and are slowly becoming New Yorkers, and each helper comes from something one of us actually cares about:

- **A good apartment.** One of us wants a place that is good quality: no heat problems, no pests, a landlord who takes care of the building. The city publishes all of that, but it is scattered across datasets nobody reads before signing a lease.
- **Hunting for freebies.** One of us loves finding free things in her spare time, from free events and pop-up giveaways to furniture left on the curb. A friend took her hunting for free items one weekend, it was a blast, and she wanted a way to plan it.
- **Discounts and free stuff.** One of us loves a good deal, and wants furniture that fits both the budget and the room, because a bargain that does not fit through the door is not one.

So New in NYC is built from our own first-month experience: finding a place, getting stuff for it, and having fun without spending much.

## The idea

Moving to a new city means opening twenty tabs: find a place, fill it, find something to do. New in NYC puts that first month behind one chat page with a helper for each part. Each helper is a Gemini agent that decides which tools to call, reads real data, and answers only with what the tools returned. The page shows every tool call (name, arguments, result) above the answer.

| Helper | Owner | What it does | Status |
|---|---|---|---|
| Apartments | xz2843 | Checks a building before you sign: housing violations, tenant complaints, rats, bedbugs, the landlord, noise and the subway. Compares several addresses and ranks them by what matters to you. | Live |
| Free events | usm2104 | Finds free events, pop-ups and giveaways, estimates the wait, and builds a timed day plan from wherever you start. | Live |
| Furnish | sz3410 | Finds furniture on Amazon that fits your budget and your space: it searches, then checks each product's listed size against your measurements and says whether it fits, may fit, or is unknown. | Live |

## Sample queries to try

1. **Apartments:** `Compare 600 W 113th St and 543 W 125th St in Manhattan.`
2. **Free events:** `Plan my Saturday from Columbia University. I want free stuff and not too much walking.`
3. **Free events (wait times):** `How long will the line be at a free coffee pop-up on Saturday at noon?`

4. **Furnish:** `I need a standing desk under $200. My wall space is 48 inches wide and 24 inches deep.`

(The Furnish helper needs a SerpApi key on the server. If it answers that product search is unavailable, the key is missing or its quota is used up.)

## Tools

All tools live in one agent (`app.py`); `tools.py` merges each helper's tools. Every tool returns errors as a plain message with a hint (for example "No building record found, check the address") instead of raising, so the model can recover.

### Apartments (`apartment.py`)

There are many apartment searching websites and apps, but they do not tell you everything about the apartment, like whether the building has rats, heat problems or a landlord with a bad record. Those hidden red flags can change your decision. To use it, give the agent one or more NYC addresses and what matters to you, and it checks city records for each one and ranks them. Everything comes from free NYC and New York State open data, with no API key.

- `check_building`: open housing-code violations by class with the worst ones described, problems tenants reported in the last 12 months (heat, pests, leaks), the last three rat inspections, the latest bedbug report, and basic facts like year built and number of units.
- `check_landlord`: the owner on the building's housing registration and when that registration ended. If the owner is a company, it also counts every building registered under the same name and their open violations.
- `check_neighborhood`: 311 noise complaints within about a block in the last 12 months, and subway stations within about a 10-minute walk with their lines.

If an address does not match a real NYC building, the tool says what it matched and the agent asks you to confirm instead of guessing.

### Free events (`events.py`)

Free things to do in NYC are spread across city, park and brand listings, and a list of events does not tell you how to spend a day. Tell the agent when you want to go and where you are starting, and it finds free events, pop-ups and giveaways, estimates the wait at each, and builds a timed plan with travel between stops.

- `find_free_events`: free NYC Parks events by date, borough and keyword *(external data: NYC Parks events feed)*
- `find_freebies`: pop-ups, giveaways, sampling and free food from the [NYC for FREE](https://www.nycforfree.co) listings *(external data; we read their public events page lightly, cache it for 30 minutes, and credit and link back)*
- `get_forecast`: the forecast for a date *(external data: Open-Meteo)*
- `curb_hunt_score`: our own 0-100 score for how good a day is for finding free curbside items in a neighborhood, from move-out timing, weekday, neighborhood and rain. A heuristic, not data.
- `estimate_wait_time`: our own estimate of how long a line will be, from event type, day, start time and weather. An estimate, not live data. For real-time crowd reports we link to [What is this line for?](https://www.whatisthislinefor.nyc/).
- `build_itinerary`: turns events into a timed plan. Short events are fixed-time, long or all-day ones are flexible visits, each next stop is the closest one that fits, and travel time and expected wait are added.
- `freebie_checklist`: what to bring, check and avoid for free furniture, books, clothes, electronics and plants

### Furnish (`shopping.py`)

The shopping helper finds furniture and other home items on Amazon that fit both the user's budget and the space they have. The user says what they want, how much they can spend, and optionally how wide, deep or tall the item can be. It is meant to give New Yorkers a smoother move-in.

- `search_products_that_fit`:
  1. Searches Amazon through SerpApi's [Amazon Search API](https://serpapi.com/amazon-search-api) and keeps only results at or under the budget.
  2. Reads each product's dimensions from its title, or from its product page when the title does not list them, and converts centimeters to inches.
  3. Compares the dimensions with the user's size limits and gives each product a verdict: fits, does not fit (left out), maybe, or unknown.
  4. Returns the products with price, rating, dimensions, verdict and link, plus a one-line summary of how many were checked and how many fit.

*Setup:* the tool needs a SerpApi key in the `SERPAPI_API_KEY` environment variable.

### Shared

- `get_weather`: current weather *(external data: Open-Meteo)*

## How it works

- `app.py`: FastAPI server and the agent loop. It calls Gemini through LiteLLM, runs any tools the model asks for, and repeats until the model answers. Sessions are kept in memory by `session_id`, and the page keeps a separate session per helper.
- `POST /chat` takes `{message, session_id}` and returns `{response, session_id, tool_calls}`, where each tool call has its `name`, `args` and `result`.
- `index.html`: the whole frontend. It lists the helpers, shows example questions and priority chips, renders markdown answers, and draws a card for each tool call.
- The prompt includes today's date and time in New York so "this weekend" and "now" work.

## Things we were careful about

- **Sources.** Each answer comes from tool results. The prompts tell the models not to invent events, buildings or stops.
- **Scraping.** We did not scrape sites whose terms forbid it. What is this line for? bans automated access, so we estimate waits ourselves and link to it.
- **Estimates are labeled.** Wait times and the curb score are heuristics, and the agent says so.

## Run it locally

1. A GCP project with billing and the Vertex AI API enabled.
2. `gcloud auth application-default login`
3. Optional, for the Furnish helper: `export SERPAPI_API_KEY=your-key` (never commit the key)
4. `uv run app.py`, then open http://localhost:8000

## Deployment

Google Cloud Run, built from the `Dockerfile`, with continuous deploy from the `main` branch of this repo. Every push to `main` redeploys in about three minutes. The service needs the `SERPAPI_API_KEY` environment variable for Furnish, and its service account needs the Vertex AI User role for Gemini.
