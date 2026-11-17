"""Job discovery from public ATS job-board APIs (Greenhouse, Lever, Ashby)."""
import html
import re
from dataclasses import dataclass, field

import requests

UA = {"User-Agent": "Mozilla/5.0 (job-search script; personal use)"}


@dataclass
class Job:
    source: str
    company: str
    job_id: str
    title: str
    location: str
    url: str          # public posting / apply page
    description: str  # plain text
    extra: dict = field(default_factory=dict)

    @property
    def key(self):
        return f"{self.source}:{self.company}:{self.job_id}"


def strip_html(s: str) -> str:
    s = s or ""
    for _ in range(3):  # Greenhouse content can be double-escaped
        u = html.unescape(s)
        if u == s:
            break
        s = u
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


def greenhouse(slug):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                     params={"content": "true"}, headers=UA, timeout=30)
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        yield Job("greenhouse", slug, str(j["id"]), j["title"],
                  (j.get("location") or {}).get("name", ""), j["absolute_url"],
                  strip_html(j.get("content", "")))


def lever(slug):
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"},
                     headers=UA, timeout=30)
    r.raise_for_status()
    for j in r.json():
        desc = j.get("descriptionPlain", "") + "\n" + "\n".join(
            f"{l.get('text','')}\n{strip_html(l.get('content',''))}" for l in j.get("lists", []))
        yield Job("lever", slug, j["id"], j["text"],
                  (j.get("categories") or {}).get("location", ""),
                  j.get("applyUrl") or j["hostedUrl"] + "/apply", desc)


def ashby(slug):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                     headers=UA, timeout=30)
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        loc = j.get("location", "") + (" Remote" if j.get("isRemote") else "")
        yield Job("ashby", slug, j["id"], j["title"], loc,
                  j.get("applyUrl") or j.get("jobUrl", ""),
                  j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")))


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


# ---------------------------------------------------------------- big job sites (via aggregators)
BIG_BOARDS = ("linkedin", "indeed", "glassdoor", "ziprecruiter", "monster", "simplyhired", "dice",
              "careerbuilder", "builtin", "wellfound", "talent.com", "jooble", "adzuna", "lensa", "bebee")


NYC_WORDS = {"nyc", "new york", "new york city", "new york ny", "ny", "manhattan", "brooklyn", "queens",
             "bronx", "the bronx", "staten island", "long island city", "lic", "jamaica"}
STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC "
             "ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split())


def parse_locations(text, limit=3):
    """'New york, NY, queens, manhattan, brooklyn, NYC' -> ['New York, NY'].
    'Jersey City, NJ; Remote' -> ['Jersey City, NJ', 'Remote']. Separate places with ; or commas."""
    out = []
    for chunk in re.split(r"[;|/]|\bor\b", text or "", flags=re.I):
        parts = [p.strip() for p in chunk.split(",") if p.strip()]
        merged = []
        for p in parts:   # glue state abbreviations back onto their city: "Jersey City", "NJ"
            if p.upper() in STATES and merged and merged[-1].upper() not in STATES and "," not in merged[-1]:
                merged[-1] = f"{merged[-1]}, {p.upper()}"
            else:
                merged.append(p)
        for m in merged:
            key = re.sub(r"[^a-z ]", "", m.lower()).strip()
            if key in NYC_WORDS or key.replace(" ny", "") in NYC_WORDS:
                m = "New York, NY"
            elif key in ("remote", "anywhere", "work from home", "wfh"):
                m = "Remote"
            if m.lower() not in [o.lower() for o in out]:
                out.append(m)
    return out[:limit] or [""]


# Middleman job sites that wrap the real posting in ads/popups/sign-up walls
SPAMMY = ("adzuna.", "jooble.", "lensa.", "bebee.", "talent.com", "jobrapido.", "careerjet.", "joblist.", "whatjobs.",
          "jobsora.", "learn4good.", "jobilize.", "recruit.net", "tallo.", "jobsearcher.", "jobright.", "salary.com",
          "simplyhired.", "neuvoo.", "jobs2careers.", "myjobhelper.", "jobcase.", "snagajob.", "ladders.", "jobted.",
          "jobleads.", "teal.com", "hiring.cafe", "jobgether.", "builtin.", "glassdoor.com/job-listing/", "monster.",
          "careerbuilder.", "dice.com", "mediabistro.", "jobzmall.", "trabajo.org", "ziprecruiter.")
TRUSTED_BOARDS = ("linkedin.com", "indeed.com", "glassdoor.com", "wellfound.com", "handshake")
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "myworkdaysite.com", "icims.com",
             "smartrecruiters.com", "jobvite.com", "workable.com", "taleo.net", "successfactors.", "bamboohr.com",
             "applytojob.com", "paylocity.com", "ultipro.com", "ukg.net", "adp.com", "oraclecloud.com",
             "dayforcehcm.com", "recruitee.com", "breezy.hr", "jazzhr.com", "pinpointhq.com", "teamtailor.com",
             "rippling.com", "eightfold.ai", "phenompeople.com", "avature.net", "csod.com", "brassring.com")


def _host(url):
    from urllib.parse import urlparse
    try:
        return urlparse(url).netloc.lower() + urlparse(url).path.lower()
    except ValueError:
        return ""


def is_spammy(url):
    h = _host(url)
    return any(s in h for s in SPAMMY) and not any(a in h for a in ATS_HOSTS)


def link_rank(url):
    """0 = application form we can fill, 1 = other employer system / company site,
    2 = LinkedIn / Indeed / Glassdoor, 3 = middleman spam sites."""
    try:
        parse_job_url(url)
        return 0
    except ValueError:
        pass
    h = _host(url)
    if any(a in h for a in ATS_HOSTS):
        return 1
    if is_spammy(url):
        return 3
    if any(b in h for b in TRUSTED_BOARDS):
        return 2
    return 1


def clean_url(url):
    """Drop tracking codes (utm_*, click ids); keep everything a site needs."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
    try:
        p = urlparse(url)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not re.match(r"^(utm_|gclid$|fbclid$|msclkid$|mc_[ce]id$|_hs|trk$|refId$|trackingId$)", k, re.I)]
        return urlunparse(p._replace(query=urlencode(q)))
    except ValueError:
        return url


BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml"}


def _links_in(html, base):
    from urllib.parse import urljoin
    found = []
    for rx in (r'http-equiv=["\']?refresh["\']?[^>]*content=["\'][^"\']*url=([^"\'>]+)',
               r'(?:window\.)?location(?:\.href)?\s*=\s*["\']([^"\']+)',
               r'location\.(?:replace|assign)\(\s*["\']([^"\']+)',
               r'"(?:url|redirectUrl|applyUrl|apply_url|jobUrl|targetUrl|destination)"\s*:\s*"(https?:[^"]+)"',
               r'href=["\'](https?://[^"\'#]+)'):
        for m in re.finditer(rx, html or "", re.I):
            u = urljoin(base, m.group(1).replace("\\/", "/").replace("&amp;", "&").strip())
            if u.startswith("http"):
                found.append(u)
    return found


def resolve_direct(url, timeout=12, _depth=0):
    """Follow a job link past middleman sites to the employer's own posting. Best effort: if nothing
    better turns up, returns the original link (minus tracking codes)."""
    if not url or not is_spammy(url) or _depth > 2:
        return clean_url(url)
    try:
        r = requests.get(url, headers=BROWSER_UA, timeout=timeout, allow_redirects=True)
    except Exception:  # noqa: BLE001
        return clean_url(url)
    if not is_spammy(r.url):
        return clean_url(r.url)
    cands = [u for u in _links_in(r.text[:400000], r.url) if _host(u) != _host(r.url)]
    best = sorted((u for u in cands if not is_spammy(u)), key=link_rank)
    best = [u for u in best if link_rank(u) <= 1 and (link_rank(u) == 0 or re.search(r"job|career|position|opening|apply|req", u, re.I))]
    if best:
        return clean_url(best[0])
    nxt = next((u for u in cands if is_spammy(u) and re.search(r"/(land|redirect|apply|go|out|click)", u, re.I)), None)
    return resolve_direct(nxt, timeout, _depth + 1) if nxt else clean_url(r.url)


def sort_apply_options(options):
    """Direct links first, middleman sites hidden unless they're all there is."""
    opts = [o for o in options if o.get("link")]
    opts.sort(key=lambda o: link_rank(o["link"]))
    good = [o for o in opts if link_rank(o["link"]) < 3]
    return good or opts


def _best_apply(options):
    """From a job's 'Apply on …' links pick: an ATS form we can fill > the company's own site > big boards > middlemen."""
    opts = sort_apply_options(options)
    if not opts:
        return {}, False
    return opts[0], link_rank(opts[0]["link"]) == 0


def _ats_job_from(link, title, company_name, location, description, extra):
    """If an apply link is Greenhouse/Lever/Ashby, make it a job AutoApply can fill out."""
    source, slug, jid = parse_job_url(link)
    url = link if source != "greenhouse" else f"https://job-boards.greenhouse.io/{slug}/jobs/{jid}"
    return Job(source, slug, jid, title, location, url, description, {**extra, "company_name": company_name})


def google_jobs(query, location, api_key, pages=1, log=print):
    """Google Jobs (which lists LinkedIn, Indeed, Glassdoor, ZipRecruiter and company sites) via SerpApi."""
    token = None
    for _ in range(max(1, pages)):
        q = query if not location else (f"{query} remote" if location == "Remote" else f"{query} in {location}")
        params = {"engine": "google_jobs", "q": q, "api_key": api_key, "hl": "en", "gl": "us"}
        if token:
            params["next_page_token"] = token
        r = requests.get("https://serpapi.com/search.json", params=params, headers=UA, timeout=60)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or data.get("error"):
            err = data.get("error") or f"HTTP {r.status_code}"
            if "hasn't returned any results" in err:
                return
            raise RuntimeError(f"Google Jobs: {err}")
        for j in data.get("jobs_results", []):
            options = j.get("apply_options") or []
            best, fillable = _best_apply(options)
            extra = {"via": (j.get("via") or "").replace("via ", ""), "company_name": j.get("company_name", ""),
                     "apply_options": [{"title": o.get("title", ""), "link": clean_url(o.get("link", ""))}
                                       for o in sort_apply_options(options)][:6],
                     "posted": (j.get("detected_extensions") or {}).get("posted_at", "")}
            desc = j.get("description", "")
            if fillable:
                yield _ats_job_from(best["link"], j.get("title", ""), j.get("company_name", ""),
                                    j.get("location", ""), desc, extra)
            else:
                yield Job("google", j.get("company_name", ""), str(j.get("job_id", ""))[:60] or j.get("title", ""),
                          j.get("title", ""), j.get("location", ""),
                          best.get("link") or j.get("share_link", ""), desc, extra)
        token = (data.get("serpapi_pagination") or {}).get("next_page_token")
        if not token:
            return


def adzuna(query, location, app_id, app_key, pages=1, max_days_old=14, log=print):
    """Adzuna job search (collects listings from many US job boards)."""
    for page in range(1, max(1, pages) + 1):
        params = {"app_id": app_id, "app_key": app_key, "what": query + (" remote" if location == "Remote" else ""),
                  "results_per_page": 50, "max_days_old": max_days_old, "content-type": "application/json"}
        if location and location != "Remote":
            params["where"] = location
        r = requests.get(f"https://api.adzuna.com/v1/api/jobs/us/search/{page}", params=params,
                         headers=UA, timeout=60)
        if r.status_code in (401, 403):
            raise RuntimeError("Adzuna rejected the keys. Check the App ID and App Key in Settings "
                               "(new accounts can take a little while to activate).")
        r.raise_for_status()
        results = r.json().get("results", [])
        for j in results:
            yield Job("adzuna", (j.get("company") or {}).get("display_name", "") or "Unknown company",
                      str(j.get("id", "")), strip_html(j.get("title", "")),
                      (j.get("location") or {}).get("display_name", ""), j.get("redirect_url", ""),
                      strip_html(j.get("description", "")),
                      {"via": "Adzuna", "snippet_only": True, "posted": j.get("created", "")[:10],
                       "company_name": (j.get("company") or {}).get("display_name", "")})
        if len(results) < 50:
            return


def full_description(job, log=print):
    """Adzuna only gives a snippet; try to fetch the full posting before tailoring."""
    if not job.extra.get("snippet_only") or not job.url:
        return job
    try:
        direct = resolve_direct(job.url)
        if direct != job.url:
            job.extra["direct_url"] = direct
        r = requests.get(direct, headers=BROWSER_UA, timeout=20, allow_redirects=True)
        # an ATS page we can read properly through its API?
        try:
            better = fetch_job(r.url)
            better.extra = {**job.extra, **better.extra, "snippet_only": False, "company_name": job.company}
            return better
        except Exception:  # noqa: BLE001
            pass
        if not is_spammy(r.url):
            job.url = clean_url(r.url)
        text = strip_html(re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", r.text))
        if len(text) > len(job.description) + 300:
            job.description = text[:15000]
            job.extra["snippet_only"] = False
    except Exception as e:  # noqa: BLE001
        log(f"  (couldn't load the full posting: {e})")
    return job


def _dedupe_key(j):
    return re.sub(r"\W+", "", (j.extra.get("company_name") or j.company).lower()) + "|" + re.sub(r"\W+", "", j.title.lower())


def title_ok(title, cfg):
    t = title.lower()
    if any(x.lower() in t for x in cfg["title_exclude"]):
        return False
    return any(k.lower() in t for k in cfg["title_keywords"])


def location_ok(loc, cfg):
    l = (loc or "").lower()
    return any(x.lower() in l for x in cfg["locations"])


def discover(config, log=print, keys=None):
    keys = keys or {}
    srch = config["search"]
    src = srch.get("sources", {})
    queries = srch.get("queries") or srch["title_keywords"][:3]
    places = parse_locations(srch.get("location", ""))
    if any(places):
        log(f"  Searching in: {', '.join(p or 'anywhere' for p in places)}")
    config.setdefault("_bad_boards", [])
    out = []

    if src.get("google", True) and not keys.get("serpapi_key"):
        log("  Google Jobs (LinkedIn, Indeed, Glassdoor…): skipped, no SerpApi key saved")
    if src.get("adzuna", True) and not (keys.get("adzuna_app_id") and keys.get("adzuna_app_key")):
        log("  Adzuna: skipped, no Adzuna keys saved")
    if src.get("google", True) and keys.get("serpapi_key"):
        for q in queries:
            for where in places:
                tag = f"'{q}'" + (f" in {where}" if where else "")
                try:
                    found = [j for j in google_jobs(q, where, keys["serpapi_key"], srch.get("google_pages", 1), log)
                             if not _excluded(j.title, srch)]
                    log(f"  Google Jobs {tag}: {len(found)} jobs")
                    out.extend(found)
                except Exception as e:  # noqa: BLE001
                    log(f"  ! Google Jobs {tag}: {e}")
    if src.get("adzuna", True) and keys.get("adzuna_app_id") and keys.get("adzuna_app_key"):
        for q in queries:
            for where in places:
                tag = f"'{q}'" + (f" in {where}" if where else "")
                try:
                    found = [j for j in adzuna(q, where, keys["adzuna_app_id"], keys["adzuna_app_key"],
                                               srch.get("adzuna_pages", 1), log=log) if not _excluded(j.title, srch)]
                    log(f"  Adzuna {tag}: {len(found)} jobs")
                    out.extend(found)
                except Exception as e:  # noqa: BLE001
                    log(f"  ! Adzuna {tag}: {e}")
    if not src.get("company_boards", True):
        return _dedupe(out)
    for source, slugs in config["boards"].items():
        for slug in slugs or []:
            if source == "workday":
                try:
                    found = list(workday(slug, queries, places, srch, log=log))
                    wb = workday_board(slug)
                    log(f"  Workday {wb[0]}/{wb[2] if wb else slug}: {len(found)} jobs match your titles and places")
                    out.extend(found)
                except Exception as e:  # noqa: BLE001
                    if "404" in str(e) or "isn't a Workday" in str(e):
                        config["_bad_boards"].append((source, slug))
                        log(f"  ! Workday {slug}: couldn't find that Workday site, so it was removed from your list.")
                    else:
                        log(f"  ! workday/{slug}: {e}")
                continue
            try:
                jobs = list(FETCHERS[source](slug))
            except Exception as e:  # noqa: BLE001
                if "404" in str(e):
                    config["_bad_boards"].append((source, slug))
                    log(f"  ! {source}/{slug}: no {source.title()} job board with that name, so it was removed from your list.")
                else:
                    log(f"  ! {source}/{slug}: {e}")
                continue
            kept = [j for j in jobs if title_ok(j.title, config["search"])
                    and location_ok(j.location, config["search"])]
            log(f"  {source}/{slug}: {len(jobs)} jobs, {len(kept)} match title+location")
            out.extend(kept)
    return _dedupe(out)


def _excluded(title, cfg):
    t = title.lower()
    return any(x.lower() in t for x in cfg["title_exclude"])


def _dedupe(jobs):
    """Same job from several sources: keep the one we can do the most with."""
    rank = {"greenhouse": 0, "lever": 0, "ashby": 0, "google": 1, "adzuna": 2}
    best = {}
    for j in jobs:
        k = _dedupe_key(j)
        if k not in best or rank.get(j.source, 3) < rank.get(best[k].source, 3):
            best[k] = j
    return list(best.values())


# ---------------------------------------------------------------- single job from a URL
URL_PATTERNS = [
    ("greenhouse", r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)"),
    ("greenhouse", r"greenhouse\.io/embed/job_app\?.*?for=([\w-]+).*?token=(\d+)"),
    ("lever", r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})"),
    ("ashby", r"jobs\.ashbyhq\.com/([\w.%-]+)/([0-9a-f-]{36})"),
]


WORKDAY_RX = re.compile(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)/(job|details)/([^?#]+)", re.I)
WORKDAY_SITE_RX = re.compile(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", re.I)


def workday_board(entry):
    """'https://pwc.wd3.myworkdayjobs.com/US_Entry_Level_Careers' or 'pwc.wd3/US_Entry_Level_Careers'
    -> ('pwc', 'wd3', 'US_Entry_Level_Careers'), or None."""
    e = entry.strip()
    m = WORKDAY_SITE_RX.search(e)
    if m:
        return m.group(1), m.group(2).lower(), m.group(3)
    m = re.match(r"^([\w-]+)\.(wd\d+)/([\w-]+)$", e, re.I)
    return (m.group(1), m.group(2).lower(), m.group(3)) if m else None


def _wd_base(tenant, wd, site):
    return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


def parse_job_url(url, slug=None):
    """Return (source, company_slug, job_id) or raise ValueError with advice."""
    m = WORKDAY_RX.search(url)
    if m:
        tenant, wd, site, _kind, path = m.groups()
        return "workday", f"{tenant}.{wd.lower()}/{site}", "/job/" + path.strip("/")
    for source, rx in URL_PATTERNS:
        m = re.search(rx, url, re.I)
        if m:
            return source, m.group(1), m.group(2)
    # Company careers pages that embed Greenhouse use ?gh_jid=<id>
    m = re.search(r"[?&]gh_jid=(\d+)", url)
    if m:
        if not slug:
            raise ValueError(
                "This is a Greenhouse job on the company's own site. Re-run with --slug <company>, "
                "where <company> is the name in boards.greenhouse.io/<company> (try the company name, lowercase).")
        return "greenhouse", slug, m.group(1)
    raise ValueError(
        "Not a Greenhouse/Lever/Ashby link, so I can't fill this form automatically.\n"
        "    Tip: on LinkedIn/Indeed click 'Apply' (not Easy Apply) and copy the URL it opens.\n"
        "    Or paste the description into jd.txt and run:  python main.py tailor jd.txt")


def workday_job(board, external_path, fallback_url=""):
    tenant, wd, site = workday_board(board)
    r = requests.get(_wd_base(tenant, wd, site) + external_path,
                     headers={**UA, "Accept": "application/json"}, timeout=30)
    if r.status_code == 404:
        raise ValueError("Workday says this posting is closed or doesn't exist.")
    r.raise_for_status()
    d = r.json()
    info = d.get("jobPostingInfo") or {}
    org = (d.get("hiringOrganization") or {}).get("name") or tenant.replace("-", " ").title()
    locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
    canonical = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{external_path}"
    ext = info.get("externalUrl") or ""
    return Job("workday", board, external_path, info.get("title", ""), "; ".join(l for l in locs if l),
               ext if ext.startswith("http") else canonical, strip_html(info.get("jobDescription", "")),
               {"company_name": org, "via": "Workday", "posted": info.get("postedOn", ""),
                "deadline": info.get("jobPostingEndDateAsText") or info.get("timeLeftToApply", ""),
                "req_id": info.get("jobReqId", ""), "can_apply_now": info.get("canApply", True)})


def workday(board, queries=(), places=(), search_cfg=None, max_details=30, log=print):
    """Search one company's Workday career site. Board: 'tenant.wdN/Site' or the site's URL."""
    parsed = workday_board(board)
    if not parsed:
        raise ValueError(f"'{board}' isn't a Workday career site link.")
    tenant, wd, site = parsed
    key = f"{tenant}.{wd}/{site}"
    seen, postings = set(), []
    for q in (queries or [""]):
        for offset in (0, 20):
            r = requests.post(_wd_base(tenant, wd, site) + "/jobs",
                              json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q},
                              headers={**UA, "Accept": "application/json"}, timeout=30)
            r.raise_for_status()
            page = r.json().get("jobPostings") or []
            for p in page:
                if p.get("externalPath") and p["externalPath"] not in seen:
                    seen.add(p["externalPath"])
                    postings.append(p)
            if len(page) < 20:
                break
    if search_cfg:   # cheap filter on titles before loading each job's details
        postings = [p for p in postings if not _excluded(p.get("title", ""), search_cfg)]
    for p in postings[:max_details]:
        try:
            j = workday_job(key, p["externalPath"],
                            f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{p['externalPath']}")
        except Exception as e:  # noqa: BLE001
            log(f"    (skipped one {tenant} job: {e})")
            continue
        if places and any(places) and not _place_ok(j.location, places):
            continue
        yield j


def _place_ok(location, places):
    loc = (location or "").lower()
    for p in places:
        if not p:
            return True
        if p == "Remote":
            if "remote" in loc or "virtual" in loc:
                return True
            continue
        city = p.split(",")[0].strip().lower()
        if city and city in loc:
            return True
    return False


def fetch_job(url, slug=None):
    source, company, job_id = parse_job_url(url, slug)
    if source == "workday":
        return workday_job(company, job_id, url.split("?")[0])
    if source == "greenhouse":
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs/{job_id}",
                         headers=UA, timeout=30)
        if r.status_code == 404:
            raise ValueError(f"Greenhouse has no job {job_id} under '{company}' (closed, or wrong --slug).")
        r.raise_for_status()
        j = r.json()
        return Job("greenhouse", company, job_id, j["title"], (j.get("location") or {}).get("name", ""),
                   j["absolute_url"], strip_html(j.get("content", "")))
    if source == "lever":
        r = requests.get(f"https://api.lever.co/v0/postings/{company}/{job_id}", headers=UA, timeout=30)
        if r.status_code == 404:
            raise ValueError("Lever says this posting is closed or doesn't exist.")
        r.raise_for_status()
        j = r.json()
        desc = j.get("descriptionPlain", "") + "\n" + "\n".join(
            f"{l.get('text','')}\n{strip_html(l.get('content',''))}" for l in j.get("lists", []))
        return Job("lever", company, job_id, j["text"], (j.get("categories") or {}).get("location", ""),
                   j.get("applyUrl") or j["hostedUrl"] + "/apply", desc)
    # ashby: no single-job endpoint, so search the board
    for j in ashby(company):
        if j.job_id == job_id:
            return j
    raise ValueError("Ashby posting not found (closed?).")


def manual_job(jd_path=None, company="manual", title="", url="", text=None):
    """Tailor for a job you found anywhere (LinkedIn, Indeed...): a .txt file or pasted text."""
    if text is None:
        text = open(jd_path, encoding="utf-8").read()
    if not title:
        title = text.strip().splitlines()[0][:80]
    import hashlib
    jid = hashlib.md5(text.encode()).hexdigest()[:10]
    return Job("manual", company, jid, title, "", url, text)
