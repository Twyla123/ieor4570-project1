"""Apartment check tools: what a listing won't tell you about an NYC apartment building.

Three tools for the agent, all built on free NYC and New York State open data:
- check_building: the building's age, size and type, its open housing-code violations, the problems tenants
  reported in the last 12 months, its last rat inspections and its latest bedbug report.
- check_landlord: who owns the building, and the open violations across all buildings that owner registers.
- check_neighborhood: noise complaints on the block and the subway stations within a short walk.
"""

# --- Setup: imports, web addresses, datasets and settings ---

import json
import math
import time
from datetime import date, timedelta

import requests

ADDRESS_SERVICE = "https://geosearch.planninglabs.nyc/v2/search"
CITY_DATA = "https://data.cityofnewyork.us/resource/{}.json"
STATE_DATA = "https://data.ny.gov/resource/{}.json"

# Open data datasets, by their IDs (city datasets on data.cityofnewyork.us, the subway list on data.ny.gov)
VIOLATIONS = "wvxf-dwi5"        # Housing Maintenance Code Violations
RAT_INSPECTIONS = "p937-wjvj"   # Rodent Inspection
BEDBUG_REPORTS = "wz6d-d3jb"    # Bedbug Reporting
REGISTRATIONS = "tesw-yqqr"     # Multiple Dwelling Registrations
CONTACTS = "feu5-w2e2"          # Registration Contacts
COMPLAINTS = "ygpa-z7cr"        # Housing Maintenance Code Complaints and Problems
TAX_LOTS = "64uk-42ks"          # Primary Land Use Tax Lot Output (PLUTO): one row of facts per tax lot
COMPLAINTS_311 = "erm2-nwe9"    # 311 Service Requests from 2010 to Present
SUBWAY_STATIONS = "39hk-dx4f"   # MTA Subway Stations (New York State data)

NOISE_RADIUS_METERS = 150       # about one block
SUBWAY_RADIUS_METERS = 800      # about a 10-minute walk
WALKING_METERS_PER_MINUTE = 80
EARTH_RADIUS_METERS = 6_371_000
IDS_PER_QUERY = 200             # keeps each query's web address short enough for the city's server
VIOLATIONS_SHOWN = 8            # how many open violations to describe, most serious first
RAT_INSPECTIONS_SHOWN = 3
DESCRIPTION_LENGTH = 220        # violation descriptions are long legal text; keep the start

# The first letter of the city's building class code says what kind of building it is.
BUILDING_TYPES = {
    "A": "one-family home", "B": "two-family home", "C": "walk-up apartment building",
    "D": "elevator apartment building", "E": "warehouse", "F": "factory or industrial building",
    "G": "garage", "H": "hotel", "I": "hospital or health facility", "J": "theatre", "K": "store building",
    "L": "loft building", "M": "religious building", "N": "group home or institution", "O": "office building",
    "P": "cultural or public assembly building", "Q": "outdoor recreation facility", "R": "condominium",
    "S": "mixed residential and store building", "T": "transportation facility", "U": "utility property",
    "V": "vacant land", "W": "school or educational building", "Y": "government building", "Z": "other",
}

# The city's own wording for the four classes (from the violations dataset's description).
VIOLATION_CLASSES = (
    "Class A is non-hazardous, class B is hazardous and class C is immediately hazardous. Class I is an "
    "information order, generally issued administratively rather than after an inspection; it is not a hazard level."
)

# The city files addresses that have no building number of their own under one shared ID per borough.
# Looking records up by a shared ID would return the records of every such address in the borough.
SHARED_BUILDING_IDS = {"1000000", "2000000", "3000000", "4000000", "5000000"}

# The errors Python raises when a reply does not have the shape the code expects (a missing field, a wrong type).
UNEXPECTED_SHAPE = (KeyError, ValueError, TypeError, AttributeError)

# What a tool returns when it cannot build its report: what happened, and what to do next.
DATA_DOWN = {"error": "NYC's open data did not respond. Try again in a minute."}
DATA_UNREADABLE = {"error": "NYC's open data sent back records in a form that could not be read. Try again in a minute."}
NO_BUILDING_RECORD = {"error": (
    "The city has no building number on file for this address, so its housing records cannot be looked up "
    "reliably. Ask the user to check the address or to give the building's main street address."
)}


# --- Plumbing: asking the open data sites ---


def city_data(dataset: str, params: dict, site: str = CITY_DATA) -> list:
    """Run one query against an open data dataset (NYC's site unless another site is given).

    The servers sometimes answer with an error, so this tries up to 3 times, waiting 2 and then
    4 seconds in between, before passing the error on to the tool that asked. Rows always come
    as a list, so any other reply is passed on as unreadable.
    """
    for attempt in range(3):
        try:
            # a normal query answers in about a second; 8 seconds keeps a hanging dataset from holding the user for long
            reply = requests.get(site.format(dataset), params=params, timeout=8)
            reply.raise_for_status()
            rows = reply.json()
            break
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    if not isinstance(rows, list):  # rows come as a list; a single object is an error message from the site
        raise ValueError("the open data site did not send a list of rows")
    return rows


def one_year_ago() -> str:
    """The date 365 days before today, as text such as "2025-10-03"."""
    return (date.today() - timedelta(days=365)).isoformat()


def counts_by(rows: list, field: str) -> dict:
    """Add up the count column "n" of grouped query rows, one total per value of the given field."""
    counts = {}
    for row in rows:
        counts[row[field]] = counts.get(row[field], 0) + int(row["n"])
    return counts


def number_or_none(text: str | None) -> int | None:
    """The open data sites send numbers as text ("294"); turn one into a number, or None if it is missing."""
    if text in (None, ""):
        return None
    return int(float(text))


# --- Finding the building ---


def lookup_address(address: str) -> dict:
    """Turn a street address into the IDs the city's datasets are searched by.

    The address service guesses rather than saying "no match" (it turned "1600 Pennsylvania Ave
    Washington DC" into a Brooklyn address), so the street number it found must equal the first
    word typed. A different street with the same number can still slip through, which is why the
    matched address is always returned for the agent to compare with what the user typed.
    """
    if not isinstance(address, str) or not address.strip():  # the model can send a number or nothing at all
        return {"error": "No address was given. Ask the user for a street number, street name and borough."}
    try:
        reply = requests.get(ADDRESS_SERVICE, params={"text": address, "size": 1}, timeout=10)
        reply.raise_for_status()
        matches = reply.json()["features"]
    except requests.RequestException:
        return {"error": "The NYC address service is not responding. Try again in a minute."}
    except UNEXPECTED_SHAPE:
        return {"error": "The NYC address service sent back an answer that could not be read. Try again in a minute."}
    if not matches:
        return {"error": f"No NYC address matched '{address}'. Ask the user for a street number, street name and borough."}

    try:
        best = matches[0]
        props = best["properties"]
        if address.split()[0] != props.get("housenumber"):
            return {"error": f"'{address}' did not match an NYC building exactly; the closest is '{props.get('label')}'. Ask the user to confirm the street number and street."}
        longitude, latitude = best["geometry"]["coordinates"]  # the service lists longitude first
        return {
            "address": props["label"],
            "bin": props["addendum"]["pad"]["bin"],    # building ID
            "bbl": props["addendum"]["pad"]["bbl"],    # tax-lot ID (a lot can hold more than one building)
            "latitude": latitude,
            "longitude": longitude,
        }
    except UNEXPECTED_SHAPE:
        return {"error": f"'{address}' matched a place without a building record the city's datasets can be searched by. Ask the user for a full street address."}


# --- Building facts ---


def open_violations(bin_id: str) -> dict:
    """Count a building's open (not yet corrected) housing-code violations by class (A, B, C, and I for information orders)."""
    rows = city_data(VIOLATIONS, {
        "$select": "class, count(*) AS n",
        "$where": f"bin = '{bin_id}' AND violationstatus = 'Open'",
        "$group": "class",
        "$order": "class",  # always A, B, C, I, so the same building reads the same on every run
    })
    return counts_by(rows, "class")


def open_violation_details(bin_id: str) -> list:
    """What the open violations are: up to 8 of them, most serious class first, newest first within a class."""
    details = []
    for violation_class in ("C", "B", "A"):
        still_needed = VIOLATIONS_SHOWN - len(details)
        if still_needed == 0:
            break
        rows = city_data(VIOLATIONS, {
            "$select": "inspectiondate, apartment, novdescription",
            "$where": f"bin = '{bin_id}' AND violationstatus = 'Open' AND class = '{violation_class}'",
            # the ID breaks ties between violations inspected on the same day, so every run returns the same ones
            "$order": "inspectiondate DESC, violationid DESC",
            "$limit": still_needed,
        })
        for row in rows:
            description = " ".join(row.get("novdescription", "").split())  # collapse line breaks and double spaces
            details.append({
                "class": violation_class,
                "inspected": row.get("inspectiondate", "")[:10],
                "apartment": row.get("apartment"),
                "description": description[:DESCRIPTION_LENGTH],
            })
    return details


def tenant_complaints(bin_id: str) -> dict:
    """Problems tenants reported to the city's housing agency in the last 12 months, by category (heat, pests...)."""
    rows = city_data(COMPLAINTS, {
        "$select": "major_category, count(*) AS n",
        "$where": f"bin = {int(bin_id)} AND received_date > '{one_year_ago()}'",
        "$group": "major_category",
        "$order": "n DESC",
    })
    by_category = counts_by(rows, "major_category")
    return {"total_problems_reported": sum(by_category.values()), "by_category": by_category}


def building_facts(bbl: str) -> dict | None:
    """Basic facts about the tax lot's building from city tax records: age, size, type and owner."""
    rows = city_data(TAX_LOTS, {
        "$select": "yearbuilt, numfloors, unitsres, unitstotal, bldgclass, ownername",
        "$where": f"bbl = {int(bbl)}",
    })
    if not rows:
        return None
    row = rows[0]
    class_code = row.get("bldgclass") or ""
    return {
        "year_built": number_or_none(row.get("yearbuilt")) or None,  # the city records an unknown year as 0
        "floors": number_or_none(row.get("numfloors")),
        "residential_units": number_or_none(row.get("unitsres")),
        "total_units": number_or_none(row.get("unitstotal")),
        "building_type": BUILDING_TYPES.get(class_code[:1], "unknown"),
        "owner_on_tax_record": row.get("ownername"),
    }


def recent_rat_inspections(bbl: str) -> list:
    """The lot's last 3 rat inspections, newest first: date, kind of inspection and result."""
    rows = city_data(RAT_INSPECTIONS, {
        "$where": f"bbl = {int(bbl)} AND inspection_date IS NOT NULL",
        "$order": "inspection_date DESC",
        "$limit": RAT_INSPECTIONS_SHOWN,
    })
    inspections = []
    for row in rows:
        inspections.append({
            "date": row["inspection_date"][:10],
            "type": row.get("inspection_type"),
            "result": row.get("result"),
        })
    return inspections


def latest_bedbug_report(bin_id: str) -> dict | None:
    """The building's most recent annual bedbug filing: infested units out of all units."""
    rows = city_data(BEDBUG_REPORTS, {
        "$where": f"bin = '{bin_id}' AND filing_date IS NOT NULL",
        "$order": "filing_date DESC",
        "$limit": 1,
    })
    if not rows:
        return None
    row = rows[0]
    return {
        "filed": row["filing_date"][:10],
        "infested_units": number_or_none(row.get("infested_dwelling_unit_count")),
        "total_units": number_or_none(row.get("of_dwelling_units")),
    }


# --- Landlord facts ---


def building_owner(bin_id: str) -> dict | None:
    """The owner named on the building's latest registration with the city, or None if it has none.

    Owners register every year, and some stop: the newest registration is used and its end date is
    returned, so the tool can say when the owner shown may be out of date. Buildings with no
    registration carry registration ID 0, a placeholder the city shares across all of them, so 0 is
    treated as "no registration".
    """
    rows = city_data(REGISTRATIONS, {"$select": "registrationid, registrationenddate", "$where": f"bin = {int(bin_id)}"})
    registrations = [row for row in rows if row.get("registrationid") not in (None, "0")]
    if not registrations:
        return None
    # a few buildings carry several registrations: take the one that ends last (the ID breaks ties)
    registrations.sort(key=lambda row: (row.get("registrationenddate", ""), int(row["registrationid"])), reverse=True)
    latest = registrations[0]
    end_date = latest.get("registrationenddate", "")[:10] or None

    contacts = city_data(CONTACTS, {
        "$select": "type, corporationname, firstname, lastname",
        "$where": f"registrationid = {latest['registrationid']}",
    })
    for contact in contacts:
        if contact["type"] == "CorporateOwner" and contact.get("corporationname"):
            return {"name": contact["corporationname"], "kind": "company", "registration_end_date": end_date}
    for contact in contacts:
        if contact["type"] in ("IndividualOwner", "JointOwner"):
            name = f"{contact.get('firstname', '')} {contact.get('lastname', '')}".strip()
            if name:
                return {"name": name, "kind": "person", "registration_end_date": end_date}
    return None


def owner_violations(company: str) -> dict:
    """How many buildings are registered under this exact company name, and their open violations by class.

    One registration usually covers one building, but some cover a whole complex of dozens of
    buildings, so buildings are counted by their building IDs, not by registrations.
    """
    safe_name = company.replace("'", "''")  # a ' inside a name would otherwise end the query's text early
    rows = city_data(CONTACTS, {
        "$select": "registrationid",
        "$where": f"type = 'CorporateOwner' AND corporationname = '{safe_name}'",
        "$limit": 5000,
    })
    unique_ids = {row["registrationid"] for row in rows}
    registration_ids = sorted(unique_ids - {"0"})  # drop the shared "no registration" placeholder

    building_ids = set()
    violation_rows = []
    for start in range(0, len(registration_ids), IDS_PER_QUERY):
        id_list = ",".join(registration_ids[start:start + IDS_PER_QUERY])
        for row in city_data(REGISTRATIONS, {
            "$select": "bin",
            "$where": f"registrationid in ({id_list})",
            "$limit": 50000,
        }):
            if row.get("bin") not in (None, "0"):
                building_ids.add(row["bin"])
        violation_rows += city_data(VIOLATIONS, {
            "$select": "class, count(*) AS n",
            "$where": f"registrationid in ({id_list}) AND violationstatus = 'Open'",
            "$group": "class",
            "$order": "class",
        })
    counts = counts_by(violation_rows, "class")
    return {
        "buildings_registered": len(building_ids),
        "open_violations_total": sum(counts.values()),
        "open_violations_by_class": counts,
    }


# --- Neighborhood facts ---


def noise_complaints(latitude: float, longitude: float) -> dict:
    """311 noise complaints from the last 12 months within about a block, by type, most common first."""
    rows = city_data(COMPLAINTS_311, {
        "$select": "complaint_type, count(*) AS n",
        "$where": (f"within_circle(location, {latitude}, {longitude}, {NOISE_RADIUS_METERS}) "
                   f"AND created_date > '{one_year_ago()}' AND complaint_type like 'Noise%'"),
        "$group": "complaint_type",
        "$order": "n DESC",
    })
    by_type = counts_by(rows, "complaint_type")
    return {"within_meters": NOISE_RADIUS_METERS, "total": sum(by_type.values()), "by_type": by_type}


def straight_line_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance between two points on the earth, in meters (the haversine formula)."""
    lat_change = math.radians(lat2 - lat1)
    lon_change = math.radians(lon2 - lon1)
    a = (math.sin(lat_change / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(lon_change / 2) ** 2)
    return EARTH_RADIUS_METERS * 2 * math.asin(math.sqrt(a))


def nearby_stations(latitude: float, longitude: float) -> list:
    """Subway stations within about a 10-minute walk, closest first, with their daytime lines."""
    rows = city_data(SUBWAY_STATIONS, {
        "$select": "stop_name, daytime_routes, gtfs_latitude, gtfs_longitude",
        "$where": f"within_circle(georeference, {latitude}, {longitude}, {SUBWAY_RADIUS_METERS})",
    }, site=STATE_DATA)
    stations_with_meters = []
    for row in rows:
        meters = straight_line_meters(latitude, longitude, float(row["gtfs_latitude"]), float(row["gtfs_longitude"]))
        station = {
            "station": row["stop_name"],
            "lines": row["daytime_routes"],
            "walk_minutes": round(meters / WALKING_METERS_PER_MINUTE),
        }
        stations_with_meters.append((meters, station))
    stations_with_meters.sort(key=lambda pair: pair[0])  # closest first, by exact distance, not rounded minutes
    return [station for meters, station in stations_with_meters]


# --- The three tools the agent calls ---


def run_check(address: str, make_report, needs_building_number: bool = True) -> str:
    """The steps all three tools share, around the one step that differs (`make_report`).

    Find the address, build the tool's report, and turn anything that goes wrong into a short
    message the model can act on, so a failure never reaches the user as a crash.
    """
    building = lookup_address(address)
    if "error" in building:
        return json.dumps(building)
    if needs_building_number and building["bin"] in SHARED_BUILDING_IDS:
        return json.dumps(NO_BUILDING_RECORD)
    try:
        return json.dumps(make_report(building))
    except requests.RequestException:
        return json.dumps(DATA_DOWN)
    except UNEXPECTED_SHAPE:
        return json.dumps(DATA_UNREADABLE)


def building_report(building: dict) -> dict:
    """Everything check_building reports: the building's facts, violations, tenant problems, rats and bedbugs."""
    facts = building_facts(building["bbl"])
    violation_counts = open_violations(building["bin"])
    report = {
        "address": building["address"],
        "building": facts or "no tax record found",
        "open_violations_total": sum(violation_counts.values()),  # added up here so the model does not have to
        "open_violations_by_class": violation_counts,
        "violation_classes": VIOLATION_CLASSES,
        # only ask for the details when there is something open
        "open_violation_details": open_violation_details(building["bin"]) if violation_counts else "none",
        "tenant_complaints_last_12_months": tenant_complaints(building["bin"]),
        "recent_rat_inspections_on_tax_lot": recent_rat_inspections(building["bbl"]) or "none on record",
        "latest_bedbug_report": latest_bedbug_report(building["bin"]) or "none on record",
    }
    if facts and facts["residential_units"] == 0:
        report["note"] = (
            f"City tax records list no residential units at this address (building type: {facts['building_type']}), "
            "so housing records such as violations and bedbug reports may not apply. Tell the user this may not "
            "be an apartment building."
        )
    return report


def landlord_report(building: dict) -> dict:
    """Everything check_landlord reports: the owner, and a company owner's record across its buildings."""
    owner = building_owner(building["bin"])
    facts = building_facts(building["bbl"])
    owner_on_tax_record = facts["owner_on_tax_record"] if facts else None  # can be a different company
    if owner is None:
        return {
            "address": building["address"],
            "owner": "not registered with NYC's housing agency",
            "owner_on_tax_record": owner_on_tax_record,
            "note": ("This building has no current registration with NYC's housing agency, so the owner's other "
                     "buildings cannot be looked up. The owner named on the city's tax record is shown instead."),
        }

    registration_end = owner["registration_end_date"]
    report = {
        "address": building["address"],
        "owner": owner["name"],  # from the latest housing registration
        "registration_end_date": registration_end,
        "owner_on_tax_record": owner_on_tax_record,
    }
    if registration_end and registration_end < one_year_ago():
        report["registration_note"] = (
            f"This registration ended on {registration_end} and has not been renewed in the city's records, so the "
            "owner shown may be out of date; compare it with the owner on the tax record."
        )
    if owner["kind"] == "company":
        report.update(owner_violations(owner["name"]))
        report["violation_classes"] = VIOLATION_CLASSES
        report["note"] = ("Counts cover buildings registered under this exact company name. Landlords often use a "
                          "different company name for each building, so the true total can be higher. The list can "
                          "include registrations that have lapsed, so a few of these buildings may no longer belong "
                          "to this owner.")
    else:
        report["note"] = "The owner is a person, so their other buildings are not looked up: personal names are not unique."
    return report


def neighborhood_report(building: dict) -> dict:
    """Everything check_neighborhood reports: noise on the block and the subway stations nearby."""
    stations = nearby_stations(building["latitude"], building["longitude"])
    return {
        "address": building["address"],
        "noise_complaints_last_12_months": noise_complaints(building["latitude"], building["longitude"]),
        "subway_stations_nearby": stations or "none within about a 10-minute walk",
        "note": "Walking minutes are straight-line estimates; real walks take a little longer.",
    }


def check_building(address: str) -> str:
    """Tool: what the city's records say about one NYC apartment building."""
    return run_check(address, building_report)


def check_landlord(address: str) -> str:
    """Tool: who owns one NYC apartment building, and their record across the buildings they register."""
    return run_check(address, landlord_report)


def check_neighborhood(address: str) -> str:
    """Tool: noise on the block and nearby subway stations for one NYC address."""
    return run_check(address, neighborhood_report, needs_building_number=False)  # it only uses the coordinates


# --- What the model sees, and what the harness runs ---

ADDRESS_ARGUMENT = {
    "address": {
        "type": "string",
        "description": "NYC street address starting with the building number, e.g. '600 W 113th St, Manhattan'",
    },
}

APARTMENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_building",
            "description": (
                "Look up what NYC city records say about one apartment building: its age, size and type; open "
                "housing-code violations by seriousness, with descriptions of the most serious ones; problems tenants "
                "reported in the last 12 months, such as heat or pests; the last rat inspections on the building's tax "
                "lot (a lot can include a neighboring building); and the latest bedbug report. Use it when the user "
                "asks whether a specific NYC building is in good condition or worth renting."
            ),
            "parameters": {"type": "object", "properties": ADDRESS_ARGUMENT, "required": ["address"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_landlord",
            "description": (
                "Find who owns one NYC apartment building, from its latest registration with the city's housing "
                "agency, and when that registration ended. When the owner is a company, also count the buildings "
                "registered under that company name and the open housing-code violations across all of them; owners "
                "who are people get no totals. Use it when the user asks about the landlord or owner, or how the owner "
                "treats their buildings. Can take about 20 seconds for large owners."
            ),
            "parameters": {"type": "object", "properties": ADDRESS_ARGUMENT, "required": ["address"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_neighborhood",
            "description": (
                "Describe the block around one NYC address: 311 noise complaints from the last 12 months within "
                "about a block, by type, and the subway stations within about a 10-minute walk, with their lines "
                "and walking minutes. Use it when the user cares about noise, quiet, commute or transit."
            ),
            "parameters": {"type": "object", "properties": ADDRESS_ARGUMENT, "required": ["address"]},
        },
    },
]

APARTMENT_TOOL_MAP = {
    "check_building": check_building,
    "check_landlord": check_landlord,
    "check_neighborhood": check_neighborhood,
}


# --- Quick test: run this file directly to try every tool on three addresses ---

if __name__ == "__main__":
    for address in ["600 W 113th St, Manhattan", "2880 Broadway, Manhattan", "1600 Pennsylvania Ave Washington DC"]:
        for tool in (check_building, check_landlord, check_neighborhood):
            print(f"{tool.__name__}({address!r})")
            print("  ", tool(address))
