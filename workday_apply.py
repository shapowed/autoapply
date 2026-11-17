"""Workday application assistant (beta).

Workday applications are multi-page wizards behind a per-company account. This:
  1. opens the job and clicks Apply → "Autofill with Resume"
  2. signs in with the saved account for that company, or creates one
     (the password is generated and saved in data/secrets.json so you can reuse it)
  3. uploads your tailored resume and lets Workday pre-fill from it
  4. on each page, fills what it knows (name, contact, address, work authorization,
     demographic answers…) and clicks Next
  5. stops whenever something needs you: it tells you what, waits while you fill it in
     and click Next yourself, then carries on from the next page
  6. stops at Review: you submit (unless automatic submit is on)

Workday changes its markup across companies and versions, so every step falls back
to "stop and let the person do it" instead of guessing.
"""
import re
import secrets as _secrets
import string
import time

from apply import FIELD_MAP, _fill_control, fill_radio_groups

AID = lambda a: f'[data-automation-id="{a}"]'  # noqa: E731

NEXT_SELECTORS = [AID("bottom-navigation-next-button"), AID("pageFooterNextButton"),
                  'button:has-text("Save and Continue")', 'button:has-text("Next")', 'button:has-text("Continue")']


def _visible(page, sel, timeout=0):
    try:
        loc = page.locator(sel).first
        if timeout:
            loc.wait_for(state="visible", timeout=timeout)
        return loc if loc.is_visible() else None
    except Exception:  # noqa: BLE001
        return None


def _click(page, *names_or_selectors, timeout=4000):
    """Click a Workday button. Workday often covers buttons with a 'click_filter' overlay."""
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for n in names_or_selectors:
            for sel in ([n] if n.startswith(("[", "button", "a[", "text=")) else
                        [f'{AID("click_filter")}[aria-label="{n}"]', f'button:has-text("{n}")', f'[role=button]:has-text("{n}")',
                         f'a:has-text("{n}")']):
                loc = _visible(page, sel)
                if loc:
                    try:
                        loc.click()
                        return True
                    except Exception:  # noqa: BLE001
                        pass
        page.wait_for_timeout(300)
    return False


def _wait_closed(page):
    try:
        if not page.is_closed():
            page.wait_for_event("close", timeout=0)
    except Exception:  # noqa: BLE001
        pass


def _header(page):
    for sel in (AID("progressBarActiveStep"), AID("pageHeader"), "main h2", "h2", "h1"):
        loc = _visible(page, sel)
        if loc:
            t = re.sub(r"\s+", " ", loc.inner_text() or "").strip()
            if t:
                return t[:80]
    return page.url


def _page_text(page):
    try:
        return page.locator("body").inner_text().lower()
    except Exception:  # noqa: BLE001
        return ""


def _new_password():
    core = "".join(_secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
    return f"Aa1!{core}"   # upper, lower, digit, special: meets Workday's usual rules


# ------------------------------------------------------------------ account
def sign_in(page, tenant, email, accounts, save_account, on_status):
    """Returns True when past the sign-in screen, False if the person needs to step in."""
    if not _visible(page, AID("email"), timeout=4000) and not _visible(page, AID("password")):
        return True   # no sign-in needed (already signed in, or this company doesn't require it)
    acct = accounts.get(tenant)

    def fill(aid, value):
        loc = _visible(page, AID(aid))
        if loc:
            loc.fill(value)
            return True
        return False

    if acct:
        if not _visible(page, AID("signInSubmitButton")):
            _click(page, AID("signInLink"), "Sign In", timeout=2500)
        fill("email", acct["email"])
        fill("password", acct["password"])
        _click(page, AID("signInSubmitButton"), "Sign In")
        page.wait_for_timeout(3500)
        if _visible(page, AID("password")) and re.search(r"incorrect|invalid|wrong|locked", _page_text(page)):
            on_status("opened", f"Your saved {tenant} Workday password didn't work. Sign in in the browser window "
                                f"(use 'Forgot password' if needed), then save the password under Settings → Workday accounts.")
            return False
        return True

    # create a new account for this company
    if not _visible(page, AID("verifyPassword")):
        _click(page, AID("createAccountLink"), "Create Account", timeout=2500)
        page.wait_for_timeout(800)
    password = _new_password()
    save_account(tenant, {"email": email, "password": password, "created": time.strftime("%Y-%m-%d")})
    fill("email", email)
    fill("password", password)
    fill("verifyPassword", password)
    box = _visible(page, AID("createAccountCheckbox"))
    if box:
        try:
            box.check()
        except Exception:  # noqa: BLE001
            box.click()
    _click(page, AID("createAccountSubmitButton"), "Create Account")
    page.wait_for_timeout(4000)
    text = _page_text(page)
    if re.search(r"already (exists|in use|registered)|account with this email", text):
        save_account(tenant, None)
        on_status("opened", f"You already have a {tenant} Workday account. Sign in in the browser window "
                            f"(or use 'Forgot password'), then save the password under Settings → Workday accounts.")
        return False
    if re.search(r"verify your (account|email)|verification (email|link)|check your email", text):
        on_status("opened", f"{tenant.title()} sent a verification email to {email}. Click the link in it, sign in "
                            f"(your password is under Settings → Workday accounts), and AutoApply will continue.")
        return False
    if _visible(page, AID("verifyPassword")):
        on_status("opened", "Couldn't create the Workday account automatically. Finish signing up in the browser window.")
        return False
    return True


# ------------------------------------------------------------------ pages
def _required_empty(page):
    """Labels of required fields that are still empty on this page."""
    return page.evaluate("""() => {
      const out = [];
      const lab = el => { const id = el.id; let l = id && document.querySelector(`label[for="${CSS.escape(id)}"]`);
        if (!l) l = el.closest('[data-automation-id^="formField"], .css-1wc6kgh, div')?.querySelector('label');
        return (l ? l.innerText : el.getAttribute('aria-label') || el.name || '').replace(/\\*/g, '').trim(); };
      for (const el of document.querySelectorAll('input[aria-required=true], textarea[aria-required=true], input[required], textarea[required]')) {
        if (el.type === 'hidden' || el.type === 'file' || el.offsetParent === null) continue;
        if ((el.type === 'checkbox' || el.type === 'radio')) continue;
        if (!el.value) out.push(lab(el));
      }
      for (const b of document.querySelectorAll('button[aria-haspopup=listbox][aria-required=true]')) {
        if (b.offsetParent === null) continue;
        if (/^select one$|^select$/i.test(b.innerText.trim())) out.push(lab(b));
      }
      return [...new Set(out.filter(Boolean))].slice(0, 8);
    }""")


def _errors(page):
    try:
        return [t.strip() for t in page.locator(f'{AID("errorMessage")}, [role=alert]').all_inner_texts() if t.strip()][:5]
    except Exception:  # noqa: BLE001
        return []


def fill_workday_page(page, answers):
    """Fill one page by field labels; returns how many fields were set."""
    filled = 0
    labels = page.locator("label")
    for i in range(labels.count()):
        lab = labels.nth(i)
        try:
            text = re.sub(r"\s+", " ", lab.inner_text() or "").replace("*", "").strip().lower()
        except Exception:  # noqa: BLE001
            continue
        key = next((k for rx, k in FIELD_MAP if re.search(rx, text)), None)
        if not key or key not in answers:
            continue
        for_id = lab.get_attribute("for")
        ctl = page.locator(f"[id='{for_id}']") if for_id else lab.locator("xpath=..").locator("input,select,textarea,button[aria-haspopup=listbox]")
        if not ctl.count():
            continue
        try:
            if _fill_control(page, ctl.first, answers[key], key):
                filled += 1
        except Exception:  # noqa: BLE001
            pass
    try:
        filled += fill_radio_groups(page, answers)
    except Exception:  # noqa: BLE001
        pass
    return filled


def _wait_for_person(page, header, on_status, msg=None):
    """Let the person act; resume when the page changes. Returns False if they closed the window."""
    if msg:
        on_status("opened", msg)
    while True:
        if page.is_closed():
            return False
        try:
            page.wait_for_timeout(2000)
            if _header(page) != header:
                on_status("applying", "Continuing…")
                return True
        except Exception:  # noqa: BLE001
            return False


def run(page, job, apply_url, answers, resume_path, accounts, save_account, auto_submit, on_status):
    tenant = job.company.split(".")[0]
    on_status("applying", "Opening the Workday posting…")
    page.goto(apply_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)

    if not _click(page, AID("adventureButton"), "Apply", timeout=8000):
        return _wait_for_person(page, _header(page), on_status,
                                "Couldn't find the Apply button. Click Apply in the browser window and AutoApply will continue.")
    page.wait_for_timeout(1500)
    _click(page, AID("autofillWithResume"), "Autofill with Resume", AID("applyManually"), "Apply Manually", timeout=5000)
    page.wait_for_timeout(2000)

    on_status("applying", "Signing in…")
    while not sign_in(page, tenant, answers.get("email", ""), accounts, save_account, on_status):
        if not _wait_for_person(page, _header(page), on_status) or page.is_closed():
            return
        if not _visible(page, AID("email")):
            break

    up = _visible(page, AID("file-upload-input-ref"), timeout=6000) or _visible(page, "input[type=file]")
    if up:
        on_status("applying", "Uploading your tailored resume…")
        page.locator(f'{AID("file-upload-input-ref")}, input[type=file]').first.set_input_files(resume_path)
        page.wait_for_timeout(5000)   # Workday parses the resume

    last = None
    for _ in range(15):
        if page.is_closed():
            return
        page.wait_for_timeout(1500)
        header = _header(page)
        text = _page_text(page)
        if re.search(r"\breview\b", header.lower()) or _visible(page, 'button:has-text("Submit")'):
            if auto_submit:
                _click(page, 'button:has-text("Submit")', AID("bottom-navigation-next-button"))
                page.wait_for_timeout(5000)
                ok = re.search(r"thank|submitted|received", _page_text(page))
                on_status("submitted" if ok else "check",
                          "Submitted." if ok else "Clicked Submit. Check the browser window to confirm it went through.")
                return
            on_status("opened", "All set: you're on the Review page. Check everything, then click Submit in the browser window.")
            _wait_closed(page)
            on_status("closed", "Window closed. Did you submit it?")
            return
        if "thank you" in text and "application" in text:
            on_status("submitted", "Submitted.")
            return

        on_status("applying", f"Filling in: {header}")
        fill_workday_page(page, answers)
        missing = _required_empty(page)
        if missing or header == last:
            reason = ("Needs you: " + "; ".join(missing)) if missing else ("Workday didn't accept this page: " +
                                                                           ("; ".join(_errors(page)) or "check the highlighted fields"))
            if not _wait_for_person(page, header, on_status,
                                    f"On '{header}'. {reason}. Fill these in and click Next; AutoApply takes over on the next page."):
                return
            last = None
            continue
        last = header
        if not _click(page, *NEXT_SELECTORS, timeout=4000):
            if not _wait_for_person(page, header, on_status, f"On '{header}'. Click Next in the browser window to continue."):
                return
            last = None
        page.wait_for_timeout(2500)

    on_status("opened", "AutoApply has done what it can. Finish the rest in the browser window.")
    _wait_closed(page)
    on_status("closed", "Window closed. Did you submit it?")
