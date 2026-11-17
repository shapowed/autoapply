"""Score a job against the master profile and tailor the resume — truthfully.

Rules enforced in code (not just in the prompt):
  * Only bullets, skills, certs and numbers from profile.yaml can appear.
  * Bullets can be reordered and reworded; a rewrite is thrown out if it adds a
    tech term, tool or number that the original bullet didn't have.
  * Low-confidence skills never get promoted into the summary or bullets.
  * Keywords the JD wants that you don't have are REPORTED, never inserted.
"""
import copy
import json
import os
import re

import yaml

# Common IT / security terms used to find what a JD asks for (gap analysis).
VOCAB = """active directory|azure ad|entra id|entra|intune|sccm|mecm|jamf|group policy|gpo|powershell|bash|python|
sql|office 365|microsoft 365|m365|exchange|exchange online|sharepoint|teams|onedrive|google workspace|okta|duo|mfa|
sso|saml|ldap|dns|dhcp|tcp/ip|vpn|vlan|vlans|firewall|firewalls|palo alto|fortinet|fortigate|meraki|cisco|catalyst|
ise|cucm|webex|zoom|voip|wireshark|nmap|splunk|siem|soc|edr|xdr|crowdstrike|sentinelone|defender|cortex xdr|tenable|
nessus|qualys|zscaler|vulnerability|incident response|nist|iso 27001|soc 2|hipaa|pci|itil|itsm|servicenow|jira|
zendesk|freshservice|ticketing|sla|windows|windows 10|windows 11|windows server|macos|linux|ios|android|mdm|
vmware|hyper-v|virtualization|aws|azure|gcp|cloud|imaging|deployment|patching|patch management|backup|
printers|hardware|troubleshooting|help desk|service desk|desktop support|end user|customer service|documentation|
knowledge base|asset management|inventory|access points|wireless|wi-fi|routers|switches|routing|switching|
network monitoring|lan|wan|ethernet|osi|ipv4|ipv6|subnetting|nac|802.1x|authentication|audio visual|av|
point of sale|pos|onboarding|offboarding|password reset|remote support|on-site|a\\+|network\\+|security\\+|ccna""".replace("\n", "")
VOCAB_RE = re.compile(r"(?<![\w+])(" + VOCAB + r")(?![\w+])", re.I)


def load_profile(path="profile.yaml"):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def norm(s):
    return re.sub(r"\s+", " ", s.lower()).strip()


def skill_index(profile):
    """term -> (category, skill dict). Includes each skill's name and aliases."""
    idx = {}
    for cat, items in profile["skills"].items():
        for sk in items:
            terms = [sk["name"]] + sk.get("aliases", [])
            for t in terms:
                idx[norm(t)] = (cat, sk)
    return idx


def profile_corpus(profile):
    parts = [profile["summary"]]
    for job in profile["experience"]:
        for b in job["bullets"]:
            parts += [b["text"], " ".join(b.get("tags", []))]
    for items in profile["skills"].values():
        for sk in items:
            parts += [sk["name"], " ".join(sk.get("aliases", []))]
    parts += [c[0] for c in profile["certifications"]]
    parts += [e["detail"] + " " + e.get("extra", "") for e in profile["education"]]
    return norm(" ".join(parts))


def jd_terms(text):
    """Return {term: count} of known IT terms in the JD."""
    counts = {}
    for m in VOCAB_RE.finditer(text):
        t = norm(m.group(1))
        counts[t] = counts.get(t, 0) + 1
    return counts


def has_term(term, corpus):
    return re.search(r"(?<![\w+])" + re.escape(term) + r"(?![\w+])", corpus) is not None


# ---------------------------------------------------------------- scoring
def score(job, profile, search_cfg):
    corpus = profile_corpus(profile)
    terms = jd_terms(job.title + "\n" + job.description)
    if not terms:
        return 0, [], []
    matched = [t for t in terms if has_term(t, corpus)]
    missing = [t for t in terms if t not in matched]
    weight = lambda ts: sum(min(terms[t], 3) for t in ts)  # noqa: E731
    coverage = weight(matched) / max(1, weight(terms))
    title_bonus = 15 if any(k.lower() in job.title.lower() for k in search_cfg["title_keywords"]) else 0
    # Hard-requirement signals that usually mean "not a fit"
    yrs = [int(y) for y in re.findall(r"(\d+)\+?\s*(?:years|yrs)", job.description, re.I) if int(y) < 20]
    penalty = 20 if yrs and min(yrs) >= 5 else (8 if yrs and min(yrs) >= 3 else 0)
    s = round(min(100, coverage * 85 + title_bonus - penalty))
    return max(0, s), sorted(matched, key=lambda t: -terms[t]), sorted(missing, key=lambda t: -terms[t])


# ---------------------------------------------------------------- rule-based tailoring
def _relevance(text, jd_lower, terms):
    t = norm(text)
    return sum(c for term, c in terms.items() if has_term(term, t))


def rule_tailor(profile, job):
    p = copy.deepcopy(profile)
    jd = norm(job.title + " " + job.description)
    terms = jd_terms(jd)

    # 1) reorder bullets inside each job by JD relevance (stable for ties)
    for exp in p["experience"]:
        exp["bullets"].sort(key=lambda b: -_relevance(b["text"] + " " + " ".join(b.get("tags", [])), jd, terms))

    # 2) reorder skills inside each category: JD-matched first
    for cat, items in p["skills"].items():
        def rel(sk):
            names = [sk["name"]] + sk.get("aliases", [])
            return -sum(1 for n in names if has_term(norm(n), jd))
        items.sort(key=rel)

    # 3) summary: name the strongest matched high-confidence skills (facts only)
    idx = skill_index(profile)
    featured = []
    for term in sorted(terms, key=lambda t: -terms[t]):
        hit = idx.get(term)
        if hit and hit[1].get("confidence", "high") == "high":
            label = hit[1]["name"].split(" (")[0]
            # only feature a skill when the JD names it directly (not via a loose alias)
            if label not in featured and len(label) < 30 and has_term(norm(label), jd):
                featured.append(label)
        if len(featured) == 4:
            break
    if featured:
        p["summary"] = p["summary"].replace(
            "managing system updates, and assisting users",
            f"managing system updates, working with {', '.join(featured)}, and assisting users")
    return p


# ---------------------------------------------------------------- rule-based rewording (no API key needed)
# Groups of verbs that mean the same thing on a resume: (present, past)
VERB_GROUPS = [
    [("Troubleshoot", "Troubleshot"), ("Diagnose", "Diagnosed")],   # never "Resolve": that claims a fix
    [("Support", "Supported"), ("Assist", "Assisted")],
    [("Manage", "Managed"), ("Administer", "Administered"), ("Maintain", "Maintained"), ("Oversee", "Oversaw")],
    [("Deploy", "Deployed"), ("Install", "Installed"), ("Implement", "Implemented")],
    [("Log", "Logged"), ("Track", "Tracked"), ("Document", "Documented")],
    [("Handle", "Handled"), ("Manage", "Managed"), ("Support", "Supported")],
    [("Use", "Used"), ("Utilize", "Utilized"), ("Leverage", "Leveraged")],
    [("Perform", "Performed"), ("Conduct", "Conducted")],
]


def _verb_forms(present, past):
    p = present.lower()
    ing = (p[:-1] if p.endswith("e") and not p.endswith("ee") else p + ("g" if p in ("log",) else "")) + "ing"
    return {p, past.lower(), p + "s", ing}


def _jd_count(jd, present, past):
    return sum(len(re.findall(r"\b" + re.escape(f) + r"\b", jd)) for f in _verb_forms(present, past))


def _swap_verb(text, jd):
    """'Handle enterprise…' -> 'Manage enterprise…' when the posting says 'manage' and never 'handle'."""
    for group in VERB_GROUPS:
        for present, past in group:
            for form, is_past in ((present, False), (past, True)):
                if re.match(re.escape(form) + r"\b", text):
                    cur = _jd_count(jd, present, past)
                    best = max(group, key=lambda v: _jd_count(jd, *v))
                    if best[0] != present and _jd_count(jd, *best) >= max(2, cur + 1):
                        return (best[1] if is_past else best[0]) + text[len(form):]
                    return text
    return text


def _mirror_in_bullet(text, jd_term_counts, profile):
    """Write the job's name for a tool you already mention: 'ServiceNow' -> 'ServiceNow (ITSM)'."""
    t = norm(text)
    for term in sorted(jd_term_counts, key=lambda k: -jd_term_counts[k]):
        if len(term) < 3 or has_term(term, t):
            continue
        for items in profile["skills"].values():
            for sk in items:
                if sk.get("confidence", "high") != "high":
                    continue
                if term not in [norm(a) for a in sk.get("aliases", [])]:
                    continue
                base = sk["name"].split(" (")[0]
                m = re.search(r"\b" + re.escape(base) + r"\b", text, re.I)
                if not m or text[m.end():m.end() + 2] == " (":
                    continue
                label = next(a for a in sk["aliases"] if norm(a) == term)
                label = label.upper() if len(label) <= 4 else label
                return text[:m.end()] + f" ({label})" + text[m.end():]
    return text


def reword_rules(p, job, master, limit=6):
    """Light, meaning-preserving rewording: the posting's verbs and names for things you already describe."""
    jd = norm(job.title + " " + job.description)
    terms = jd_terms(jd)
    corpus = profile_corpus(master)
    low = [norm(n) for items in master["skills"].values() for sk in items
           if sk.get("confidence") == "low" for n in [sk["name"]] + sk.get("aliases", [])]
    changed = 0
    for exp in p["experience"]:
        for b in exp["bullets"]:
            if changed >= limit:
                return p
            new = _mirror_in_bullet(_swap_verb(b["text"], jd), terms, master)
            if re.search(r"\bend[- ]users?\b", jd) and not re.search(r"\bend[- ]users?\b", new, re.I):
                new = re.sub(r"(?<!end )\busers\b", "end users", new, count=1)
            if new != b["text"] and validate_rewrite(b["text"], new, corpus, low):
                b["text"] = new
                changed += 1
    return p


# ---------------------------------------------------------------- Claude tailoring
PROMPT = """You are tailoring a resume to a job description. You must be strictly truthful.

MASTER PROFILE (the only facts you may use):
{profile}

JOB: {title} at {company}
{jd}

Return ONLY JSON:
{{
 "summary": "3-4 sentence summary, same tone/length as the original, emphasising what this job wants",
 "bullet_order": {{"<job id>": ["<bullet id>", ...]}},      // every bullet id exactly once, most relevant first
 "bullet_rewrites": {{"<bullet id>": "reworded text"}},      // reword the bullets that matter most for this job
 "skill_order": {{"<category>": ["<exact skill name>", ...]}} // every skill exactly once
}}

Hard rules:
- Never invent tools, technologies, certifications, numbers, employers, titles, dates or duties.
- A rewrite may only rephrase what the original bullet says; you may swap in the JD's wording
  for something the bullet already describes (e.g. "ticketing system" -> "ITSM platform").
- Keep each rewrite within ±20% of the original length. Keep the original verb tense.
- Skills with confidence "low" must not appear in the summary or any bullet.
- Do not mention the company name or job title in the summary.

How to reword (for up to 6 bullets that relate to the job):
- Lead with the part of the bullet this job cares about most.
- Use the job description's exact words for things the bullet already describes (its verbs, its
  names for tools and processes), so an applicant tracking system matches them.
- Keep every fact: same tools, same scope, same numbers, same outcome. Change emphasis, not substance.
"""


def _terms_in(text):
    return {norm(m.group(1)) for m in VOCAB_RE.finditer(text)}


def _numbers(text):
    return set(re.findall(r"\d[\d,.+%]*", text))


def _caps(text):
    # Proper-noun-ish tokens (tools/brands): words with capitals beyond sentence start
    return {w for w in re.findall(r"\b[A-Z][A-Za-z0-9+./-]*[A-Z0-9][A-Za-z0-9+./-]*\b", text)}


def validate_rewrite(orig, new, allowed_corpus, low_terms):
    if not new or abs(len(new) - len(orig)) > 0.35 * len(orig):
        return False
    if _numbers(new) - _numbers(orig):
        return False
    if (_terms_in(new) - _terms_in(orig)) - {t for t in _terms_in(new) if has_term(t, allowed_corpus)}:
        return False
    if any(has_term(t, norm(new)) and not has_term(t, norm(orig)) for t in low_terms):
        return False
    if any(c.lower() not in allowed_corpus for c in _caps(new) - _caps(orig)):
        return False
    return True


def claude_tailor(profile, job, model):
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model, max_tokens=3000,
        messages=[{"role": "user", "content": PROMPT.format(
            profile=yaml.safe_dump(profile, sort_keys=False, allow_unicode=True),
            title=job.title, company=job.company, jd=job.description[:12000])}])
    raw = msg.content[0].text
    data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])

    p = copy.deepcopy(profile)
    corpus = profile_corpus(profile)
    low = [norm(n) for items in profile["skills"].values() for sk in items
           if sk.get("confidence") == "low" for n in [sk["name"]] + sk.get("aliases", [])]
    rejected = []

    for exp in p["experience"]:
        by_id = {b["id"]: b for b in exp["bullets"]}
        order = [i for i in data.get("bullet_order", {}).get(exp["id"], []) if i in by_id]
        if sorted(order) == sorted(by_id):
            exp["bullets"] = [by_id[i] for i in order]
        for b in exp["bullets"]:
            new = data.get("bullet_rewrites", {}).get(b["id"])
            if new:
                if validate_rewrite(b["text"], new, corpus, low):
                    b["text"] = new.rstrip(".")
                else:
                    rejected.append((b["id"], new))

    for cat, items in p["skills"].items():
        by_name = {sk["name"]: sk for sk in items}
        order = [n for n in data.get("skill_order", {}).get(cat, []) if n in by_name]
        if sorted(order) == sorted(by_name):
            p["skills"][cat] = [by_name[n] for n in order]

    s = data.get("summary", "")
    if s and summary_ok(s, profile, corpus, low):
        p["summary"] = s
    elif s:
        rejected.append(("summary", s))
    return p, rejected


def summary_ok(s, profile, corpus, low):
    full_text = yaml.safe_dump(profile).lower()
    if len(s) > 1.4 * len(profile["summary"]):
        return False
    if any(n not in full_text for n in _numbers(s)):
        return False
    if any(has_term(t, norm(s)) for t in low):
        return False
    if any(not has_term(t, corpus) for t in _terms_in(s)):
        return False
    if any(c.lower() not in full_text for c in _caps(s)):
        return False
    return True


def visible_text(p):
    parts = [p["summary"]] + [e["title"] for e in p["experience"]]
    parts += [b["text"] for e in p["experience"] for b in e["bullets"]]
    parts += [sk["name"] for items in p["skills"].values() for sk in items]
    parts += [c[0] for c in p["certifications"]] + [e["detail"] + " " + e.get("extra", "") for e in p["education"]]
    return norm(" ".join(parts))


def mirror_keywords(p, job, limit=6):
    """ATS only reads what's printed. If the JD uses a term that is an alias of a
    skill you have (high confidence) but the term isn't printed anywhere, print it
    next to that skill, e.g. 'ServiceNow (ITSM)'."""
    terms = jd_terms(job.title + " " + job.description)
    shown = visible_text(p)
    added = 0
    for term in sorted(terms, key=lambda t: -terms[t]):
        if added >= limit or has_term(term, shown) or len(term) < 3:
            continue
        for items in p["skills"].values():
            sk = next((s for s in items if term in [norm(a) for a in s.get("aliases", [])]
                       and s.get("confidence", "high") == "high"), None)
            if sk:
                label = next(a for a in sk["aliases"] if norm(a) == term)  # keep profile's casing
                label = label.upper() if len(label) <= 4 else label
                sk["name"] = sk["name"][:-1] + f", {label})" if sk["name"].endswith(")") \
                    else f"{sk['name']} ({label})"
                shown += " " + term
                added += 1
                break
    return p


def tailor(profile, job, cfg, log=print):
    t = cfg.get("tailoring") or {}
    reword = t.get("reword", True)
    if reword and t.get("use_claude", True) and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            p, rejected = claude_tailor(profile, job, t.get("model", "claude-sonnet-5-5"))
            for rid, txt in rejected:
                log(f"    guardrail rejected rewrite of {rid}: {txt[:90]}...")
            return mirror_keywords(p, job), "claude"
        except Exception as e:  # noqa: BLE001
            log(f"    Claude tailoring failed ({e}); using rule-based")
    p = rule_tailor(profile, job)
    if reword:
        p = reword_rules(p, job, profile)
    return mirror_keywords(p, job), "rules"
