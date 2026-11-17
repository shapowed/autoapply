"""Distances for 'within N miles of …' searches, offline (US ZIP/city coordinates from the zipcodes package)."""
import math
import re
from functools import lru_cache

STATE_ABBR = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
              "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "washington dc": "DC", "florida": "FL",
              "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
              "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
              "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
              "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
              "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
              "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
              "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
              "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY"}
ABBRS = set(STATE_ABBR.values())
# Names people use that aren't USPS city names
ALIASES = {("nyc", None): ("new york", "NY"), ("new york city", None): ("new york", "NY"),
           ("manhattan", "NY"): ("new york", "NY"), ("queens", "NY"): ("queens county", "NY"),
           ("the bronx", "NY"): ("bronx", "NY"), ("sf", "CA"): ("san francisco", "CA"),
           ("la", "CA"): ("los angeles", "CA"), ("dc", None): ("washington", "DC")}


@lru_cache(maxsize=1)
def _index():
    import zipcodes
    by_zip, by_city, by_county, by_name = {}, {}, {}, {}
    for z in zipcodes.list_all():
        try:
            lat, lon = float(z["lat"]), float(z["long"])
        except (TypeError, ValueError, KeyError):
            continue
        st = z.get("state", "")
        by_zip[z["zip_code"]] = (lat, lon)
        for name in [z.get("city", "")] + list(z.get("acceptable_cities") or []):
            if name:
                by_city.setdefault((name.lower(), st), []).append((lat, lon))
                by_name.setdefault(name.lower(), set()).add(st)
        if z.get("county"):
            by_county.setdefault((z["county"].lower(), st), []).append((lat, lon))
    avg = lambda pts: (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))  # noqa: E731
    return (by_zip, {k: avg(v) for k, v in by_city.items()}, {k: avg(v) for k, v in by_county.items()}, by_name)


def miles(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    d = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(d))


def _city(name, st):
    by_zip, by_city, by_county, by_name = _index()
    name = re.sub(r"\s+", " ", name.lower().replace(".", "")).strip(" -,")
    name = re.sub(r"^(greater|downtown|metro)\s+", "", name)
    name = re.sub(r"\s+(area|metro|metropolitan area|city area)$", "", name)
    key = ALIASES.get((name, st)) or ALIASES.get((name, None))
    if key:
        name, st = key
    if st and (name, st) in by_city:
        return by_city[(name, st)]
    if st and (name, st) in by_county:
        return by_county[(name, st)]
    if st and (name + " county", st) in by_county:
        return by_county[(name + " county", st)]
    if not st and name in by_name and len(by_name[name]) == 1:   # unambiguous city name without a state
        return by_city[(name, next(iter(by_name[name])))]
    return None


@lru_cache(maxsize=5000)
def locate(text):
    """'Jamaica, NY 11435' / 'New York, NY' / 'NY-New York' / 'Brooklyn, New York, United States' / '11435'
    -> (lat, lon), or None if it can't tell (e.g. 'United States', 'Multiple locations')."""
    if not text:
        return None
    t = str(text)
    by_zip = _index()[0]
    z = re.search(r"\b(\d{5})(?:-\d{4})?\b", t)
    if z and z.group(1) in by_zip:
        return by_zip[z.group(1)]
    t = re.sub(r"(?i)\b(remote|hybrid|on-?site|in office|united states( of america)?|usa|us)\b", " ", t)
    for part in re.split(r"[;|•/]|\s+or\s+|\s+-\s+(?=[A-Z][a-z])", t):
        part = part.strip(" ,-()")
        if not part:
            continue
        m = re.match(r"^([A-Z]{2})-(.+)$", part)                      # Workday style: NY-New York
        if m and m.group(1) in ABBRS:
            hit = _city(m.group(2), m.group(1))
            if hit:
                return hit
        bits = [b.strip() for b in part.split(",") if b.strip()]
        for i in range(len(bits) - 1, 0, -1):                          # City, ST  /  City, State
            st = bits[i].upper() if bits[i].upper() in ABBRS else STATE_ABBR.get(bits[i].lower())
            if st:
                hit = _city(bits[i - 1], st)
                if hit:
                    return hit
        m = re.match(r"^(.+?)\s+([A-Z]{2})$", part)                     # City ST
        if m and m.group(2) in ABBRS:
            hit = _city(m.group(1), m.group(2))
            if hit:
                return hit
        if len(bits) == 1:
            hit = _city(bits[0], None)
            if hit:
                return hit
    return None


def center(text):
    """What the person typed ('11435', 'Jamaica, NY', 'Hoboken NJ') -> (lat, lon, 'City, ST'), or None."""
    t = (text or "").strip()
    pt = locate(t)
    if not pt:
        return None
    z = re.search(r"\b(\d{5})\b", t)
    if z:   # show and search a ZIP as its town name
        import zipcodes
        hit = zipcodes.matching(z.group(1))
        if hit:
            return pt[0], pt[1], f"{hit[0]['city'].title()}, {hit[0]['state']}"
    return pt[0], pt[1], t


def is_remote(location, title=""):
    return bool(re.search(r"\bremote\b|work from home|\bwfh\b|anywhere in the u", f"{location} {title}", re.I))
