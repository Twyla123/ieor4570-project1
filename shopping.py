"""search_products_that_fit: Amazon search filtered by budget and by the space the user has.

How it plugs into the starter (three lines in tools.py, after TOOLS and TOOL_MAP are defined):

    from shopping_tool import SHOPPING_TOOL, search_products_that_fit
    TOOLS.append(SHOPPING_TOOL)
    TOOL_MAP["search_products_that_fit"] = search_products_that_fit

Data comes from SerpApi's Amazon engines (https://serpapi.com/amazon-search-api).
Put your key in the SERPAPI_API_KEY environment variable.

API cost per tool call: 1 search, plus up to MAX_DETAIL_LOOKUPS product lookups when the
user gave size limits and a listing's title does not state its dimensions.
"""

import json
import os
import re
from itertools import permutations

import requests

SERPAPI_URL = "https://serpapi.com/search"
MAX_DETAIL_LOOKUPS = 4   # product-page lookups per tool call (each one uses a SerpApi credit)
MAX_CANDIDATES = 12      # under-budget results examined per tool call
AXES = ("width", "depth", "height")

# asin -> parsed dimensions (or None). Saves credits when the same product comes up again.
_details_cache: dict[str, dict | None] = {}


class ToolError(Exception):
    """A problem the model should hear about. The message is written for the model to act on."""


# --- Talking to SerpApi ---


def _serpapi(params: dict) -> dict:
    key = os.environ.get("SERPAPI_API_KEY")
    if not key:
        raise ToolError(
            "Product search is not configured (SERPAPI_API_KEY is missing on the server). "
            "Do not retry. Tell the user product search is unavailable right now."
        )
    try:
        resp = requests.get(SERPAPI_URL, params={**params, "api_key": key}, timeout=25)
    except requests.RequestException as e:
        # Only the exception type: the full message contains the request URL, which includes the key.
        raise ToolError(
            f"Could not reach the product search service ({type(e).__name__}). "
            "Retry once. If it fails again, tell the user the search is temporarily down."
        )
    if resp.status_code == 401:
        raise ToolError("The product search API key was rejected. Do not retry. Tell the user search is unavailable.")
    if resp.status_code == 429:
        raise ToolError(
            "The product search quota or rate limit has been reached. Do not retry now. "
            "Tell the user to try again later."
        )
    try:
        data = resp.json()
    except ValueError:
        raise ToolError(f"The product search service returned an unreadable response (HTTP {resp.status_code}). Retry once.")
    if not resp.ok and "error" not in data:
        raise ToolError(f"The product search service returned HTTP {resp.status_code}. Retry once.")
    return data


# --- Reading dimensions out of listing text ---

_NUM = r"\d+(?:\.\d+)?"
_UNIT = r"\"|”|″|''|inches|inch|in\b\.?|cm\b"
# A side label (W, D, H, L) right after the number. "L" in "L-Shaped" / "L Shaped" is not a label.
_LABEL = r"[WDHL]\b(?![\s-]*shap)"
_PART = rf"{_NUM}(?:\s*(?:{_UNIT}))?(?:\s?{_LABEL})?"
_SPAN_RE = re.compile(rf"{_PART}(?:\s*[x×]\s*{_PART}){{1,2}}", re.IGNORECASE)
_PART_RE = re.compile(rf"({_NUM})(?:\s*({_UNIT}))?(?:\s?({_LABEL}))?", re.IGNORECASE)


def _parse_dimensions(text: str | None) -> dict | None:
    """Find the first 'A x B' or 'A x B x C' size in text and return it in inches.

    Returns {"width": .., "depth": .., "height": ..} (any subset) when the listing labels its
    sides, {"unlabeled": [..]} when it does not, or None when no size is found.
    Examples it understands: '47.2"W x 23.6"D x 28.7"H', '48 x 24 Inch', '120 x 60 x 75 cm'.
    """
    if not text:
        return None
    for span in _SPAN_RE.finditer(text):
        parts = _PART_RE.findall(span.group(0))
        units = [u.lower() for _, u, _ in parts if u]
        labels = [l.upper() for _, _, l in parts if l]
        if not units and not labels:
            continue  # bare "2 x 4" is more likely a pack count than a size
        scale = 1 / 2.54 if any(u.startswith("cm") for u in units) else 1.0
        values = [round(float(n) * scale, 1) for n, _, _ in parts]
        if any(v <= 0 or v > 200 for v in values):
            continue
        if labels:
            length_is_width = not ("L" in labels and "W" in labels)
            dims = {}
            for (_, _, label), value in zip(parts, values):
                label = label.upper()
                if label == "H":
                    dims["height"] = value
                elif label == "D":
                    dims["depth"] = value
                elif label == "L":
                    dims["width"] = value          # length is the long side
                elif label == "W":
                    dims["width" if length_is_width else "depth"] = value
            if dims:
                return dims
        else:
            return {"unlabeled": values}
    return None


def _lookup_dimensions(asin: str) -> dict | None:
    """Fetch one product page and pull the item dimensions from its spec tables."""
    if asin in _details_cache:
        return _details_cache[asin]
    data = _serpapi({"engine": "amazon_product", "asin": asin, "amazon_domain": "amazon.com"})
    candidates = []
    for section in ("item_specifications", "product_details"):
        for key, value in (data.get(section) or {}).items():
            if isinstance(value, str) and "dimension" in key and "package" not in key:
                candidates.append(value)
    candidates.append((data.get("product_results") or {}).get("title"))
    candidates += [b for b in (data.get("about_item") or []) if isinstance(b, str)]
    dims = next((d for d in map(_parse_dimensions, candidates) if d), None)
    _details_cache[asin] = dims
    return dims


# --- Does it fit? ---


def _check_fit(dims: dict | None, limits: dict) -> tuple[str, str]:
    """Compare a product's size with the user's limits. Returns (verdict, note).

    Verdicts: "yes", "no", "maybe" (not enough information to be sure), "unknown" (no size listed).
    """
    if not dims:
        return "unknown", "The listing does not state its dimensions."
    active = {axis: limit for axis, limit in limits.items() if limit is not None}

    if "unlabeled" not in dims:
        over = [f"{a} is {dims[a]:g} in, limit is {active[a]:g} in" for a in active if a in dims and dims[a] > active[a]]
        if over:
            return "no", "; ".join(over)
        missing = [a for a in active if a not in dims]
        if missing:
            return "maybe", f"Fits on the sides it lists, but the listing does not give its {' or '.join(missing)}."
        return "yes", "Within every limit."

    # Sides are not labeled, so try each way the numbers could map onto width/depth/height.
    values = dims["unlabeled"]
    if len(values) == 2:
        # Two numbers are a footprint, and desks, shelves and sofas list the long side as the width.
        verdict, note = _check_fit({"width": max(values), "depth": min(values)}, limits)
        return verdict, note + " (Assumed the longer listed side is the width.)"
    axes = AXES
    outcomes = []
    for order in permutations(axes, len(values)):
        assigned = dict(zip(order, values))
        fits = all(assigned[a] <= active[a] for a in active if a in assigned)
        complete = all(a in assigned for a in active)
        outcomes.append((fits, complete))
    if not any(fits for fits, _ in outcomes):
        return "no", "No orientation of the listed size fits within the limits."
    if all(fits and complete for fits, complete in outcomes):
        return "yes", "Within every limit whichever side is which."
    return "maybe", "The listing does not say which side is which (or omits one); it fits in some orientations only."


# --- The tool ---


def _number(value, name: str, required: bool = False) -> float | None:
    """Models sometimes send numbers as strings ("200", "$200"). Accept those; reject nonsense."""
    if value is None or value == "":
        if required:
            raise ToolError(f"'{name}' is required. Ask the user for it, then call again.")
        return None
    try:
        number = float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        raise ToolError(f"'{name}' must be a number, got {value!r}. Call again with a plain number.")
    if number <= 0:
        raise ToolError(f"'{name}' must be greater than 0, got {value!r}.")
    return number


def _search(query, max_price, max_width_in, max_depth_in, max_height_in, max_results) -> dict:
    query = (query or "").strip()
    if not query:
        raise ToolError("'query' is empty. Call again with what the user wants to buy, e.g. 'standing desk'.")
    max_price = _number(max_price, "max_price", required=True)
    limits = {
        "width": _number(max_width_in, "max_width_in"),
        "depth": _number(max_depth_in, "max_depth_in"),
        "height": _number(max_height_in, "max_height_in"),
    }
    max_results = int(min(max(_number(max_results, "max_results") or 5, 1), 10))
    checking_size = any(v is not None for v in limits.values())

    data = _serpapi({"engine": "amazon", "k": query, "amazon_domain": "amazon.com"})
    error = data.get("error")
    if error and "any results" not in error.lower():
        raise ToolError(f"Product search failed: {error}")

    seen, priced = set(), []
    for item in data.get("organic_results") or []:
        asin, price = item.get("asin"), item.get("extracted_price")
        if asin and asin not in seen and isinstance(price, (int, float)):
            seen.add(asin)
            priced.append(item)
    if not priced:
        raise ToolError(f"No Amazon results for '{query}'. Try a shorter or more general query (e.g. 'desk' instead of a full sentence).")

    affordable = [p for p in priced if p["extracted_price"] <= max_price]
    if not affordable:
        cheapest = min(priced, key=lambda p: p["extracted_price"])
        return {
            "query": query,
            "max_price": max_price,
            "products": [],
            "summary": (
                f"Nothing at or under ${max_price:g}. The cheapest match is ${cheapest['extracted_price']:g} "
                f"({cheapest['title'][:80]}). Ask the user whether to raise the budget or change the search."
            ),
        }

    by_verdict = {"yes": [], "maybe": [], "unknown": [], "no": [], "not_checked": []}
    lookups = 0
    for item in affordable[:MAX_CANDIDATES]:
        entry = {
            "title": item["title"][:120],
            "price": item["extracted_price"],
            "rating": item.get("rating"),
            "reviews": item.get("reviews"),
            "url": item.get("link_clean") or f"https://www.amazon.com/dp/{item['asin']}/",
        }
        if not checking_size:
            entry["fits"] = "not_checked"
            by_verdict["not_checked"].append(entry)
            continue

        dims = _parse_dimensions(item["title"])
        if dims is None and lookups < MAX_DETAIL_LOOKUPS:
            lookups += 1
            try:
                dims = _lookup_dimensions(item["asin"])
            except ToolError:
                lookups = MAX_DETAIL_LOOKUPS  # service trouble: stop looking things up, keep what we have
        verdict, note = _check_fit(dims, limits)
        entry["dimensions_in"] = dims
        entry["fits"] = verdict
        entry["fit_note"] = note
        by_verdict[verdict].append(entry)
        if len(by_verdict["yes"]) >= max_results:
            break

    if not checking_size:
        products = by_verdict["not_checked"][:max_results]
        summary = (
            f"{len(affordable)} results at or under ${max_price:g}; showing {len(products)}. "
            "Size was not checked because no measurements were given. Offer to check fit if the user shares their space."
        )
    else:
        products = (by_verdict["yes"] + by_verdict["maybe"] + by_verdict["unknown"])[:max_results]
        checked = sum(len(v) for v in by_verdict.values())
        summary = (
            f"Checked {checked} products at or under ${max_price:g}: {len(by_verdict['yes'])} fit, "
            f"{len(by_verdict['no'])} do not fit (left out), {len(by_verdict['maybe'])} uncertain, "
            f"{len(by_verdict['unknown'])} with no listed dimensions."
        )
        if not by_verdict["yes"]:
            summary += " None is a confirmed fit; say so, and suggest relaxing a measurement or the budget."

    return {
        "query": query,
        "max_price": max_price,
        "limits_in": {a: v for a, v in limits.items() if v is not None},
        "summary": summary + " Prices are list prices before tax.",
        "products": products,
    }


def search_products_that_fit(
    query: str,
    max_price: float,
    max_width_in: float | None = None,
    max_depth_in: float | None = None,
    max_height_in: float | None = None,
    max_results: int = 5,
) -> str:
    """Search Amazon for products within a budget that fit in the user's space."""
    try:
        return json.dumps(_search(query, max_price, max_width_in, max_depth_in, max_height_in, max_results))
    except ToolError as e:
        # The model cannot see an exception. Return something it can reason about.
        return json.dumps({"error": str(e)})


# What the model sees.
SHOPPING_TOOL = {
    "type": "function",
    "function": {
        "name": "search_products_that_fit",
        "description": (
            "Search Amazon for a physical product (furniture, appliances, storage, decor) within the user's "
            "budget, and check each result against the space they have. Returns products at or under max_price "
            "with price, rating, link, listed dimensions in inches, and a 'fits' verdict: yes, no (left out), "
            "maybe, or unknown. Call it when the user wants to buy or compare something for their home. "
            "If the user has not given a budget, ask for one first. Pass the size limits only when the user "
            "gave measurements; without them, results are filtered by price only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for, as short keywords, e.g. 'standing desk' or 'narrow bookshelf'. No prices or sizes here.",
                },
                "max_price": {
                    "type": "number",
                    "description": "The user's budget for one item in US dollars, e.g. 200.",
                },
                "max_width_in": {
                    "type": "number",
                    "description": "Widest the item can be, in inches (the side along the wall). Convert feet to inches: 4 ft = 48. Omit if unknown.",
                },
                "max_depth_in": {
                    "type": "number",
                    "description": "Deepest the item can be, in inches (how far it sticks out from the wall). Omit if unknown.",
                },
                "max_height_in": {
                    "type": "number",
                    "description": "Tallest the item can be, in inches (e.g. to fit under a window or shelf). Omit if unknown.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "How many products to return, 1 to 10. Default 5.",
                },
            },
            "required": ["query", "max_price"],
        },
    },
}
