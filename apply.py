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
    (r"phone|mobile", "phone"),
    (r"linkedin", "linkedin"),
    (r"website|portfolio|github", "website"),
    (r"^(current\s*)?(city|location)\b", "city"),
    (r"current (company|employer)|^company$|^org", "current_company"),
    (r"salary|compensation expectation", "salary_expectation"),
    (r"start date|earliest.*start|notice", "earliest_start"),
    (r"authori[sz]ed to work|legally (authorized|eligible)|right to work", "work_authorized_us"),
    (r"sponsorship|visa", "needs_sponsorship"),
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
    if job.source == "greenhouse":   # bypass company iframes, go to the hosted form
        return f"https://job-boards.greenhouse.io/{job.company}/jobs/{job.job_id}"
    if job.source == "ashby" and "/application" not in job.url:
        return job.url.rstrip("/") + "/application"
    return job.url


def _answers(cfg, profile):
    a = dict(cfg["applicant"])
    a["full_name"] = f"{a['first_name']} {a['last_name']}"
    a["current_company"] = profile["experience"][0]["company"]
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


RADIO_GROUPS_JS = """() => [...document.querySelectorAll('fieldset, [role=radiogroup]')]
  .filter(g => g.querySelector('input[type=radio]'))
  .map(g => {
    const q = (g.querySelector('legend') || g).innerText.split('\\n')[0];
    const radios = [...g.querySelectorAll('input[type=radio]')];
    const labels = radios.map(r => (r.labels && r.labels[0] ? r.labels[0].innerText : r.value) || '');
    return {q, labels, checked: radios.some(r => r.checked)};
  })"""


def fill_radio_groups(page, answers):
    filled = 0
    groups = page.locator("fieldset:has(input[type=radio]), [role=radiogroup]:has(input[type=radio])")
    info = page.evaluate(RADIO_GROUPS_JS)
    for gi, g in enumerate(info):
        if g["checked"] or gi >= groups.count():
            continue
        q = g["q"].replace("*", "").strip().lower()
        key = next((k for rx, k in FIELD_MAP if re.search(rx, q)), None)
        if not key or key not in answers:
            continue
        i = pick_option(g["labels"], preferences(key, answers[key]))
        if i is not None:
            groups.nth(gi).locator("input[type=radio]").nth(i).check(force=True)
            filled += 1
    return filled


def fill_form(page, answers, resume_path, log=print):
    # 1) resume upload: first file input whose context mentions resume/CV, else first file input
    files = page.locator("input[type=file]")
    target = None
    for i in range(files.count()):
        f = files.nth(i)
        ctx = f.evaluate("e => (e.closest('div,fieldset,section')||e).innerText.slice(0,200)")
        if re.search(r"resume|cv", ctx or "", re.I):
            target = f
            break
    target = target or (files.first if files.count() else None)
    if target:
        target.set_input_files(resume_path)
        log("    uploaded resume")
        page.wait_for_timeout(2500)  # let the ATS parse it

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
    log(f"    filled {filled} fields; {len(unfilled_required)} required fields need you")
    for t in unfilled_required:
        log(f"      - {t}")
    return unfilled_required


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


def apply_one(job, pdf, cfg, profile, on_status=print):
    """For the app: open one application, fill it, then either auto-submit or
    keep the window open until the user closes it. Reports progress via on_status."""
    answers = _answers(cfg, profile)
    with sync_playwright() as pw:
        browser = _launch(pw, headless=False)
        page = browser.new_page()
        try:
            on_status("opening", "Opening the application page...")
            page.goto(apply_url(job), wait_until="networkidle", timeout=60000)
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
