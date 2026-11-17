"""Fill application forms in a real browser with Playwright.

Default: fills everything it can, uploads the tailored resume, then WAITS for you
to review, answer anything left, solve any CAPTCHA, and click Submit yourself.
Set apply.auto_submit: true in config.yaml to submit automatically when no
required field is left empty (CAPTCHA-protected forms will still need you).
"""
import re

from playwright.sync_api import sync_playwright

# label regex -> applicant config key
FIELD_MAP = [
    (r"^first\s*name|given name", "first_name"),
    (r"^last\s*name|surname|family name", "last_name"),
    (r"^(full\s*)?name\b(?!.*(first|last|company|preferred))", "full_name"),
    (r"e-?mail", "email"),
    (r"phone device type|phone type|device type", "phone_type"),
    (r"phone|mobile", "phone"),
    (r"linkedin", "linkedin"),
    (r"website|portfolio|github", "website"),
    (r"^(current\s*)?(city|location)\b", "city"),
    (r"current (company|employer)|^company$|^org", "current_company"),
    (r"salary|compensation expectation", "salary_expectation"),
    (r"start date|earliest.*start|notice", "earliest_start"),
    (r"authori[sz]ed to work|legally (authorized|eligible)|right to work", "work_authorized_us"),
    (r"sponsorship|visa", "needs_sponsorship"),
    (r"postal|zip", "zip"),
    (r"^state\b|province|^region\b", "state"),
    (r"^country\b", "country"),
    (r"^date\b|today'?s date|^signature date", "today"),
    # Voluntary self-identification (EEO). Only filled if you set an answer in Settings.
    (r"hispanic|latin[oax]", "eeo_hispanic"),
    (r"\bgender\b|\bsex\b", "eeo_gender"),
    (r"\brace\b|ethnicity", "eeo_race"),
    (r"veteran", "eeo_veteran"),
    (r"disabilit", "eeo_disability"),
]

# How each saved answer may be worded on different sites, most specific first.
DECLINE = ["Decline", "I don't wish", "I do not wish", "Prefer not", "I do not want", "I don't want"]
SYNONYMS = {
    "eeo_gender": {"male": ["Male", "Man"], "female": ["Female", "Woman"], "decline": DECLINE},
    "eeo_hispanic": {"no": ["No"], "yes": ["Yes"], "decline": DECLINE},
    "eeo_disability": {"no": ["No, I do not have a disability", "No, I don't have a disability", "No"],
                       "yes": ["Yes, I have a disability", "Yes"], "decline": DECLINE},
    "eeo_veteran": {"no": ["I am not a protected veteran", "not a protected veteran", "No"],
                    "yes": ["I identify as one or more", "protected veteran", "Yes"], "decline": DECLINE},
}


def preferences(key, value):
    """Turn a saved answer into the list of option wordings to look for."""
    v = str(value).strip()
    syn = SYNONYMS.get(key, {}).get(v.lower())
    if syn:
        return syn
    if key == "country" and v.lower().startswith("united states"):
        return ["United States of America", "United States", "USA", "US"]
    if key == "phone_type":
        return ["Mobile", "Cell", "Cellular", "Personal"]
    if key == "eeo_race":   # e.g. "Filipino, Asian" -> try Filipino first, then Asian
        return [p.strip() for p in v.split(",") if p.strip()] + (DECLINE if v.lower() == "decline" else [])
    return [v]


def pick_option(options, prefs):
    """Index of the best option for the preferences, or None. 'Male' never matches 'Female'."""
    clean = [re.sub(r"\s+", " ", o).strip().lower() for o in options]
    for p in prefs:
        p = p.lower()
        for test in (lambda o: o == p,
                     lambda o: o.startswith(p) and (len(o) == len(p) or not o[len(p)].isalnum()),
                     lambda o: re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", o)):
            for i, o in enumerate(clean):
                if o and test(o):
                    return i
    return None


def apply_url(job):
    """The application form itself, built from the job's ids (links from Google Jobs etc. carry
    tracking codes, and some open the description page instead of the form)."""
    if job.source == "greenhouse":   # bypass company iframes, go to the hosted form
        return f"https://job-boards.greenhouse.io/{job.company}/jobs/{job.job_id}"
    if job.source == "ashby":
        return f"https://jobs.ashbyhq.com/{job.company}/{job.job_id}/application"
    if job.source == "lever":
        return f"https://jobs.lever.co/{job.company}/{job.job_id}/apply"
    return job.url


STATE_NAMES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
               "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
               "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
               "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
               "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
               "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
               "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
               "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
               "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
               "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}


def _answers(cfg, profile):
    import datetime as _dt
    a = dict(cfg["applicant"])
    a["full_name"] = f"{a.get('first_name', '')} {a.get('last_name', '')}".strip()
    exp = profile.get("experience") or [{}]
    a["current_company"] = exp[0].get("company", "")
    # address pieces from "Jamaica, NY" / "Jamaica, NY, 11435"
    loc = " ".join([a.get("city", ""), (profile.get("contact") or {}).get("location", "")])
    zm = re.search(r"\b\d{5}\b", loc)
    if zm:
        a["zip"] = zm.group(0)
    sm = re.search(r",\s*([A-Z]{2})\b", loc)
    if sm and sm.group(1) in STATE_NAMES:
        a["state"] = STATE_NAMES[sm.group(1)]
        a["country"] = "United States of America"
    a["city_only"] = (a.get("city") or "").split(",")[0].strip()
    a["phone_type"] = "Mobile"
    a["today"] = _dt.date.today().strftime("%m/%d/%Y")
    return {k: v for k, v in a.items() if v}


def _fill_control(page, ctl, value, key=""):
    tag = ctl.evaluate("e => e.tagName.toLowerCase()")
    typ = (ctl.get_attribute("type") or "").lower()
    prefs = preferences(key, value)
    if tag == "select":
        opts = ctl.evaluate("e => [...e.options].map(o => o.text)")
        i = pick_option(opts, prefs)
        if i is None:
            return False
        ctl.select_option(index=i)
        return True
    if typ in ("checkbox", "radio", "file", "hidden", "submit"):
        return False
    if tag == "button" and (ctl.get_attribute("aria-haspopup") or "") == "listbox":   # Workday dropdowns
        current = (ctl.inner_text() or "").strip().lower()
        if current and current not in ("select one", "select", "--"):
            return True
        ctl.click()
        page.wait_for_timeout(500)
        opts = page.locator("[role=listbox] [role=option], [role=option]")
        i = pick_option(opts.all_inner_texts(), prefs)
        if i is None:
            ctl.press("Escape")
            return False
        opts.nth(i).click()
        page.wait_for_timeout(300)
        return True
    if tag == "button":
        return False
    if ctl.get_attribute("role") == "combobox":   # dropdowns on Greenhouse/Ashby
        ctl.click()
        page.wait_for_timeout(400)
        for attempt in range(2):
            opts = page.locator("[role=option]")
            i = pick_option(opts.all_inner_texts(), prefs)
            if i is not None:
                opts.nth(i).click()
                return True
            if attempt == 0:
                ctl.fill(prefs[0])
                page.wait_for_timeout(600)
        ctl.press("Escape")
        return False
    if ctl.input_value():
        return True  # already filled (e.g. autofilled from resume parse)
    ctl.fill(str(value))
    return True


YES_NO_JS = """() => {
  const btns = [...document.querySelectorAll('button, [role=button]')].filter(b => /^(yes|no)$/i.test((b.innerText || '').trim()));
  const groups = new Map();
  for (const b of btns) {
    let n = b.parentElement;
    for (let d = 0; d < 5 && n; d++, n = n.parentElement) {
      const t = [...n.querySelectorAll('button, [role=button]')].map(x => (x.innerText || '').trim().toLowerCase());
      if (t.includes('yes') && t.includes('no')) break;
    }
    if (!n || groups.has(n)) continue;
    let q = n.parentElement || n;
    for (let d = 0; d < 3 && q && (q.innerText || '').replace(/\\b(yes|no)\\b/gi, '').trim().length < 6; d++) q = q.parentElement;
    const label = (q.querySelector('label, legend') || q).innerText.replace(/\\b(yes|no)\\b\\s*$/gi, '').trim().split('\\n')[0];
    const id = 'aa' + groups.size;
    n.querySelectorAll('button, [role=button]').forEach(x => { const v = (x.innerText || '').trim().toLowerCase();
      if (v === 'yes' || v === 'no') x.setAttribute('data-aa-yn', id + '-' + v); });
    groups.set(n, {id, q: label});
  }
  return [...groups.values()];
}"""


def fill_yes_no(page, answers):
    filled = 0
    for g in page.evaluate(YES_NO_JS):
        q = g["q"].replace("*", "").strip().lower()
        key = next((k for rx, k in FIELD_MAP if re.search(rx, q)), None)
        val = str(answers.get(key, "")).strip().lower() if key else ""
        if val in ("yes", "no"):
            btn = page.locator(f'[data-aa-yn="{g["id"]}-{val}"]').first
            if btn.count():
                btn.click()
                filled += 1
    return filled


RADIO_GROUPS_JS = """() => [...document.querySelectorAll('fieldset, [role=radiogroup], [role=group]')]
  .filter(g => g.querySelector('input[type=radio], input[type=checkbox]'))
  .map(g => {
    const q = (g.querySelector('legend') || g).innerText.split('\\n')[0];
    const radios = [...g.querySelectorAll('input[type=radio], input[type=checkbox]')];
    const labels = radios.map(r => (r.labels && r.labels[0] ? r.labels[0].innerText : r.value) || '');
    return {q, labels, checked: radios.some(r => r.checked)};
  })"""


def fill_radio_groups(page, answers):
    filled = 0
    groups = page.locator("fieldset:has(input[type=radio], input[type=checkbox]), [role=radiogroup]:has(input[type=radio]), "
                          "[role=group]:has(input[type=radio], input[type=checkbox])")
    info = page.evaluate(RADIO_GROUPS_JS)
    for gi, g in enumerate(info):
        if g["checked"] or gi >= groups.count():
            continue
        q = g["q"].replace("*", "").strip().lower()
        key = next((k for rx, k in FIELD_MAP if re.search(rx, q)), None)
        if not key:   # e.g. "Please check one of the boxes below" with disability options
            joined = " ".join(g["labels"]).lower()
            key = next((k for rx, k in FIELD_MAP if k.startswith("eeo_") and re.search(rx, joined)), None)
        if not key or key not in answers:
            continue
        i = pick_option(g["labels"], preferences(key, answers[key]))
        if i is not None:
            groups.nth(gi).locator("input[type=radio], input[type=checkbox]").nth(i).check(force=True)
            filled += 1
    return filled


def fill_form(page, answers, resume_path, log=print):
    # 0) wait for the form to actually render (Ashby/Lever build it with JavaScript)
    try:
        page.wait_for_function("document.querySelectorAll('input:not([type=hidden]), textarea').length >= 2", timeout=15000)
    except Exception:  # noqa: BLE001
        pass

    # 1) resume upload. Some sites have an "Autofill from resume" box AND a Resume field:
    #    upload to the autofill box first (it pre-fills the form), then to the Resume field.
    files = page.locator("input[type=file]")
    autofill, resume_field, first = None, None, None
    for i in range(files.count()):
        f = files.nth(i)
        info = f.evaluate("""e => { let n = e, near = '';
            for (let d = 0; d < 6 && n; d++, n = n.parentElement) { near = (n.innerText || '').trim(); if (near.length > 12) break; }
            const lab = [...(e.labels || [])].map(l => l.innerText).join(' ') || e.getAttribute('aria-label') || '';
            return {lab, ids: (e.id || '') + ' ' + (e.name || ''), near: near.slice(0, 250)}; }""") or {}
        lab, ids, near = info.get("lab", ""), info.get("ids", ""), info.get("near", "")
        first = first or f
        own = lab + " " + ids          # the field's own label/name beats nearby text
        if re.search(r"cover", own, re.I):
            continue
        if re.search(r"resume|\bcv\b|curriculum", own, re.I) and not re.search(r"autofill|auto-fill|parse", own, re.I):
            resume_field = resume_field or f
        elif re.search(r"autofill|auto-fill|parse", own + " " + near, re.I):
            autofill = autofill or f
        elif re.search(r"cover", near, re.I) and not re.search(r"resume|\bcv\b", near, re.I):
            continue
        elif re.search(r"resume|\bcv\b|curriculum", near, re.I):
            resume_field = resume_field or f
    for target in [t for t in (autofill, resume_field or (None if autofill else first)) if t]:
        try:
            target.set_input_files(resume_path)
            log("    uploaded resume")
            page.wait_for_timeout(3000)  # let the site read it
        except Exception:  # noqa: BLE001
            pass

    # 2) text/select fields by label
    filled, unfilled_required = 0, []
    labels = page.locator("label")
    for i in range(labels.count()):
        lab = labels.nth(i)
        text = re.sub(r"\s+", " ", (lab.inner_text() or "")).strip()
        if not text:
            continue
        required = "*" in text or "required" in text.lower()
        clean = text.replace("*", "").strip().lower()
        key = next((k for rx, k in FIELD_MAP if re.search(rx, clean)), None)
        for_id = lab.get_attribute("for")
        ctl = page.locator(f"[id='{for_id}']") if for_id else lab.locator("input,select,textarea")
        if not ctl.count():
            continue
        ctl = ctl.first
        done = False
        if key and key in answers:
            try:
                done = _fill_control(page, ctl, answers[key], key)
            except Exception:  # noqa: BLE001
                done = False
        if done:
            filled += 1
        elif required:
            try:
                if not ctl.input_value():
                    unfilled_required.append(text[:70])
            except Exception:  # noqa: BLE001
                unfilled_required.append(text[:70])
    try:
        filled += fill_radio_groups(page, answers)
    except Exception:  # noqa: BLE001
        pass
    try:
        filled += fill_yes_no(page, answers)
    except Exception:  # noqa: BLE001
        pass
    log(f"    filled {filled} fields; {len(unfilled_required)} required fields need you")
    for t in unfilled_required:
        log(f"      - {t}")
    return unfilled_required


def show_problem(page, msg, url=""):
    """Never leave a blank window: explain what happened right in it."""
    import html as _h
    try:
        link = f'<p><a href="{_h.escape(url)}">Open the job page</a> to apply by hand.</p>' if url else ""
        page.set_content(f"""<html><body style="font:16px -apple-system,system-ui,sans-serif;background:#121311;color:#ecebe6;padding:40px;max-width:640px">
          <h2>AutoApply couldn't fill out this application</h2><p>{_h.escape(msg)}</p>{link}
          <p style="color:#9c9b95">You can close this window. If this keeps happening, tell Claude what this page says.</p></body></html>""")
    except Exception:  # noqa: BLE001
        pass


def _launch(pw, headless):
    try:
        return pw.chromium.launch(headless=headless)
    except Exception:  # noqa: BLE001  - bundled Chromium missing: fall back to installed Chrome/Edge
        for ch in ("chrome", "msedge"):
            try:
                return pw.chromium.launch(headless=headless, channel=ch)
            except Exception:  # noqa: BLE001
                pass
        raise RuntimeError("No browser found. Install Google Chrome, or run: playwright install chromium")


def apply_one(job, pdf, cfg, profile, on_status=print, accounts=None, save_account=None):
    """For the app: open one application, fill it, then either auto-submit or
    keep the window open until the user closes it. Reports progress via on_status."""
    answers = _answers(cfg, profile)
    with sync_playwright() as pw:
        browser = _launch(pw, headless=False)
        page = browser.new_page()
        if job.source == "workday":
            import workday_apply
            a = dict(answers, city=answers.get("city_only", answers.get("city", "")))
            try:
                workday_apply.run(page, job, job.url, a, pdf, accounts or {}, save_account or (lambda *_: None),
                                  cfg["apply"].get("auto_submit", False), on_status)
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                msg = f"The Workday assistant got stuck: {str(e)[:200]}"
                on_status("error", msg + ". The window is still open, so you can finish by hand.")
                if page.url in ("about:blank", "") or not page.url.startswith("http"):
                    import workday_apply as _w
                    show_problem(page, msg, _w.posting_url(job, job.url))
                try:
                    page.wait_for_event("close", timeout=0)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                browser.close()
            return
        try:
            on_status("opening", "Opening the application page...")
            page.goto(apply_url(job), wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            missing = fill_form(page, answers, pdf, log=lambda *_: None)
            if cfg["apply"]["auto_submit"] and not missing:
                page.get_by_role("button", name=re.compile(r"submit", re.I)).first.click()
                page.wait_for_timeout(4000)
                ok = re.search(r"thank|received|submitted", page.content(), re.I)
                on_status("submitted" if ok else "check",
                          "Submitted." if ok else "Clicked Submit. Check the browser window to confirm it went through.")
                page.wait_for_timeout(3000)
            else:
                msg = "Filled in. Check the browser window, then click Submit there."
                if missing:
                    msg += " Still needs you: " + "; ".join(m.replace("*", "").strip() for m in missing[:5])
                on_status("opened", msg)
                page.wait_for_event("close", timeout=0)  # wait until the user closes the tab
                on_status("closed", "Window closed. Did you submit it?")
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            if page.url in ("about:blank", "") or not page.url.startswith("http"):
                show_problem(page, f"Couldn't open the application page: {str(e)[:200]}", apply_url(job))
            on_status("error", f"Couldn't fill the form automatically: {str(e)[:160]}. "
                               f"The page is still open, so you can finish it by hand.")
            try:
                page.wait_for_event("close", timeout=0)
            except Exception:  # noqa: BLE001
                pass
        finally:
            browser.close()


def apply_to_jobs(items, cfg, profile, log=print):
    """items: list of (job, resume_pdf_path). Returns {job.key: status}."""
    answers = _answers(cfg, profile)
    results = {}
    with sync_playwright() as pw:
        browser = _launch(pw, cfg["apply"]["headless"])
        ctx = browser.new_context()
        for job, pdf in items:
            log(f"\n>> {job.company}: {job.title}\n   {apply_url(job)}")
            page = ctx.new_page()
            try:
                page.goto(apply_url(job), wait_until="networkidle", timeout=60000)
                missing = fill_form(page, answers, pdf, log)
                if cfg["apply"]["auto_submit"] and not missing:
                    page.get_by_role("button", name=re.compile(r"submit", re.I)).first.click()
                    page.wait_for_timeout(4000)
                    ok = re.search(r"thank|received|submitted", page.content(), re.I)
                    results[job.key] = "submitted" if ok else "submit-clicked-unconfirmed"
                else:
                    resp = input("   Review the browser tab, submit it yourself, then type "
                                 "[s]ubmitted / [k]skipped and press Enter: ").strip().lower()
                    results[job.key] = "submitted" if resp.startswith("s") else "skipped"
            except Exception as e:  # noqa: BLE001
                log(f"   ! error: {e}")
                results[job.key] = f"error: {e}"[:200]
            finally:
                page.close()
        browser.close()
    return results
