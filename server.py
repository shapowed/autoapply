"""AutoApply desktop app: a local web server + UI. Only listens on this computer."""
import datetime as dt
import hashlib
import json
import os
import re
import threading
import time
import traceback
import webbrowser
from dataclasses import asdict

import yaml
from flask import Flask, jsonify, request, send_from_directory

import updater
from importer import finalize, import_resume
from jobs import Job, discover, fetch_job, full_description, manual_job, parse_job_url, parse_locations
from render import render_docx, render_pdf_one_page
from tailor import score, tailor

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser(os.environ.get("AUTOAPPLY_HOME") or HERE)   # where your data lives
DATA, OUT = os.path.join(HOME, "data"), os.path.join(HOME, "Resumes" if HOME != HERE else "output")
PROFILE, CONFIG = os.path.join(HOME, "profile.yaml"), os.path.join(HOME, "config.yaml")
os.makedirs(HOME, exist_ok=True)
for _f in ("profile", "config"):   # first run: start from the bundled copy, else the blank template
    _dst = os.path.join(HOME, _f + ".yaml")
    if not os.path.exists(_dst):
        import shutil
        for _src in (os.path.join(HERE, _f + ".yaml"), os.path.join(HERE, _f + ".example.yaml")):
            if os.path.exists(_src) and os.path.abspath(_src) != os.path.abspath(_dst):
                shutil.copy(_src, _dst)
                break
JOBS_DB, SECRETS, SETUP = os.path.join(DATA, "jobs.json"), os.path.join(DATA, "secrets.json"), os.path.join(DATA, "setup_done")
os.makedirs(DATA, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")
app.json.sort_keys = False  # keep resume sections in the order the user set
lock = threading.RLock()


# ------------------------------------------------------------------ storage
def read_yaml(p, default=None):
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or default


def write_yaml(p, data):
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=1000)


def load_profile():
    return read_yaml(PROFILE)


def load_cfg():
    return read_yaml(CONFIG, {})


def load_db():
    with lock:
        return json.load(open(JOBS_DB)) if os.path.exists(JOBS_DB) else {}


def save_db(db):
    with lock:
        tmp = JOBS_DB + ".tmp"
        json.dump(db, open(tmp, "w"), indent=1)
        os.replace(tmp, JOBS_DB)


def update_job(jid, **fields):
    with lock:
        db = load_db()
        if jid in db:
            db[jid].update(fields)
            save_db(db)


def load_key():
    if os.path.exists(SECRETS):
        k = load_secrets().get("anthropic_api_key", "")
        if k:
            os.environ["ANTHROPIC_API_KEY"] = k
    return os.environ.get("ANTHROPIC_API_KEY", "")


def jid_for(job):
    return hashlib.md5(job.key.encode()).hexdigest()[:12]


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:40]


def job_from_record(r):
    return Job(**{k: r[k] for k in ("source", "company", "job_id", "title", "location", "url", "description")},
               extra=r.get("extra") or {})


SOURCE_KEYS = ("serpapi_key", "adzuna_app_id", "adzuna_app_key")


def err(msg, code=400):
    return jsonify({"error": msg}), code


# ------------------------------------------------------------------ core action
def resume_dir(cfg=None):
    """Where tailored resumes go: the folder chosen in Settings, else the default."""
    d = ((cfg or load_cfg()).get("output") or {}).get("dir") or ""
    d = os.path.expanduser(d) if d else OUT
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return OUT


def resume_filename(profile, ext):
    """'Brandon Lagman' -> 'Brandon-Lagman-Resume.pdf'"""
    words = [re.sub(r"[^A-Za-z0-9'-]", "", w) for w in (profile.get("name") or "").split()]
    words = [w for w in words if w]
    name = "-".join([words[0], words[-1]] if len(words) > 1 else words)
    return f"{name}-Resume.{ext}" if name else f"Resume.{ext}"


def job_folder_name(job):
    company = job.extra.get("company_name") or (job.company.split(".")[0].title() if job.source == "workday" else job.company)
    raw = f"{company} - {job.title}".strip(" -")
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", raw)).strip()[:90] or "Job"


def make_resume(job):
    profile, cfg = load_profile(), load_cfg()
    load_key()
    s, matched, missing = score(job, profile, cfg["search"])
    tailored, method = tailor(profile, job, cfg, log=lambda *_: None)
    folder = os.path.join(resume_dir(cfg), job_folder_name(job))
    os.makedirs(folder, exist_ok=True)
    pdf_path = os.path.join(folder, resume_filename(profile, "pdf"))
    docx_path = os.path.join(folder, resume_filename(profile, "docx"))
    pdf, size = render_pdf_one_page(tailored, pdf_path)
    render_docx(tailored, docx_path, size)
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    jid = jid_for(job)
    db = load_db()
    prev = db.get(jid, {})
    rec = {**asdict(job), "id": jid, "score": s, "matched": matched[:25], "missing": missing[:25],
           "method": method, "pdf": base + ".pdf", "docx": base + ".docx",
           "pdf_path": pdf_path, "docx_path": docx_path, "folder": folder,
           "can_apply": job.source in ("greenhouse", "lever", "ashby", "workday"),
           "status": prev.get("status", "ready"), "apply_msg": prev.get("apply_msg", ""),
           "date": prev.get("date", dt.date.today().isoformat()),
           "updated": dt.datetime.now().isoformat(timespec="seconds")}
    with lock:
        db = load_db()
        db[jid] = rec
        save_db(db)
    return rec


# ------------------------------------------------------------------ phone access
import secrets as _secrets
import socket

MAIN_PORT = int(os.environ.get("AUTOAPPLY_PORT", "8765"))
PHONE_PORT = MAIN_PORT + 1
phone = {"server": None, "fails": {}}


def load_secrets():
    return json.load(open(SECRETS)) if os.path.exists(SECRETS) else {}


def save_secrets(d):
    json.dump(d, open(SECRETS, "w"))
    try:
        os.chmod(SECRETS, 0o600)
    except OSError:
        pass


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # no packets sent; just picks the Wi-Fi interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def start_phone_server():
    if phone["server"]:
        return
    from werkzeug.serving import make_server
    srv = make_server("0.0.0.0", PHONE_PORT, app, threaded=True)
    phone["server"] = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def stop_phone_server():
    if phone["server"]:
        phone["server"].shutdown()
        phone["server"] = None


def is_local():
    return request.remote_addr in ("127.0.0.1", "::1") and request.host.endswith(f":{MAIN_PORT}")


OPEN_PATHS = ("/login", "/static/", "/manifest.webmanifest")
LOCAL_ONLY = ("/api/phone", "/api/quit", "/api/open-folder", "/api/test-key", "/api/workday", "/api/resume-folder")


@app.before_request
def guard():
    if is_local():
        return None
    sec = load_secrets()
    if not sec.get("phone_enabled"):
        return "Phone access is turned off in AutoApply's Settings on your computer.", 403
    if request.path.startswith(OPEN_PATHS):
        return None
    if request.path.startswith(LOCAL_ONLY):
        return err("Only available on the computer.", 403)
    tok = request.cookies.get("aa_token", "")
    if not (tok and sec.get("phone_token") and _secrets.compare_digest(tok, sec["phone_token"])):
        if request.path.startswith("/api/") or request.path.startswith("/files/"):
            return err("Please enter your PIN.", 401)
        from flask import redirect
        return redirect("/login")
    return None


@app.get("/login")
def login_page():
    return send_from_directory(app.static_folder, "login.html")


@app.post("/login")
def login():
    ip = request.remote_addr
    fails = phone["fails"].get(ip, 0)
    if fails >= 8:
        return err("Too many wrong PINs. Make a new PIN in Settings on your computer.", 429)
    sec = load_secrets()
    pin = str((request.get_json() or {}).get("pin", "")).strip()
    if not sec.get("phone_pin") or not _secrets.compare_digest(pin, sec["phone_pin"]):
        phone["fails"][ip] = fails + 1
        return err("Wrong PIN.", 401)
    phone["fails"].pop(ip, None)
    resp = jsonify(ok=True)
    resp.set_cookie("aa_token", sec["phone_token"], max_age=31536000, httponly=True, samesite="Lax")
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    return app.response_class(json.dumps({
        "name": "AutoApply", "short_name": "AutoApply", "start_url": "/", "display": "standalone",
        "background_color": "#121311", "theme_color": "#121311",
        "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}]}),
        mimetype="application/manifest+json")


def phone_info():
    sec = load_secrets()
    url = f"http://{lan_ip()}:{PHONE_PORT}"
    qr = ""
    if sec.get("phone_enabled"):
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
        qr = img.to_string(encoding="unicode")
    return {"enabled": bool(sec.get("phone_enabled")), "url": url, "pin": sec.get("phone_pin", ""), "qr": qr}


@app.get("/api/phone")
def get_phone():
    return jsonify(phone_info())


@app.post("/api/phone")
def set_phone():
    body = request.get_json() or {}
    sec = load_secrets()
    if body.get("new_pin") or not sec.get("phone_pin"):
        sec["phone_pin"] = f"{_secrets.randbelow(10**6):06d}"
        sec["phone_token"] = _secrets.token_urlsafe(32)   # signs out phones that used the old PIN
        phone["fails"].clear()
    if "enabled" in body:
        sec["phone_enabled"] = bool(body["enabled"])
    save_secrets(sec)
    try:
        start_phone_server() if sec.get("phone_enabled") else stop_phone_server()
    except OSError as e:
        return err(f"Couldn't open phone access: {e}")
    return jsonify(phone_info())


def save_workday_account(tenant, acct):
    with lock:
        sec = load_secrets()
        accts = sec.setdefault("workday_accounts", {})
        if acct:
            accts[tenant] = acct
        else:
            accts.pop(tenant, None)
        save_secrets(sec)


@app.get("/api/workday/accounts")
def get_workday_accounts():
    accts = load_secrets().get("workday_accounts", {})
    return jsonify([{"company": k, **v} for k, v in sorted(accts.items())])


@app.put("/api/workday/accounts")
def put_workday_account():
    b = request.get_json() or {}
    company = re.sub(r"[^a-z0-9-]", "", (b.get("company") or "").lower())
    if not company or not b.get("password"):
        return err("Company and password are required.")
    save_workday_account(company, {"email": b.get("email") or (load_cfg().get("applicant") or {}).get("email", ""),
                                   "password": b["password"], "created": dt.date.today().isoformat()})
    return get_workday_accounts()


@app.delete("/api/workday/accounts/<company>")
def delete_workday_account(company):
    save_workday_account(company, None)
    return get_workday_accounts()


@app.post("/api/quit")
def quit_app():
    threading.Timer(0.4, lambda: os._exit(0)).start()
    return jsonify(ok=True)


@app.post("/api/open-folder")
def open_folder():
    _open_in_file_manager(resume_dir())
    return jsonify(ok=True)


def _open_in_file_manager(path, select=None):
    try:
        _open_in_file_manager_(path, select)
    except Exception:  # noqa: BLE001
        pass


def _open_in_file_manager_(path, select=None):
    import subprocess
    import sys
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", select] if select else ["open", path])
    elif os.name == "nt":
        subprocess.Popen(["explorer", "/select,", select]) if select else os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", path])


def _choose_folder_dialog(start):
    """Show the computer's own 'choose a folder' window. Returns the path, or '' if cancelled."""
    import subprocess
    import sys
    prompt = "Choose where AutoApply saves your tailored resumes"
    try:
        if sys.platform == "darwin":
            script = (f'set f to choose folder with prompt "{prompt}" default location (POSIX file "{start}")\n'
                      'POSIX path of f')
            r = subprocess.run(["osascript", "-e", 'tell application "System Events" to activate', "-e", script],
                               capture_output=True, text=True, timeout=600)
            return r.stdout.strip()
        if os.name == "nt":
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"
                  "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
                  f"$f.Description='{prompt}';$f.SelectedPath='{start}';"
                  "if($f.ShowDialog($o) -eq 'OK'){$f.SelectedPath}")
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                               capture_output=True, text=True, timeout=600, creationflags=0x08000000)
            return r.stdout.strip()
        r = subprocess.run(["zenity", "--file-selection", "--directory", f"--title={prompt}", f"--filename={start}/"],
                           capture_output=True, text=True, timeout=600)
        return r.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


@app.get("/api/resume-folder")
def get_resume_folder():
    d = resume_dir()
    return jsonify({"dir": d, "default": os.path.abspath(d) == os.path.abspath(OUT),
                    "example": os.path.join(d, "Acme - IT Support Analyst", resume_filename(load_profile() or {}, "pdf"))})


@app.post("/api/resume-folder")
def set_resume_folder():
    body = request.get_json() or {}
    if body.get("reset"):
        path = ""
    elif body.get("choose"):
        path = _choose_folder_dialog(resume_dir())
        if not path:
            return get_resume_folder()   # cancelled
    else:
        path = (body.get("dir") or "").strip()
    if path:
        path = os.path.expanduser(path.rstrip("/\\") or path)
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".autoapply-write-test")
            open(probe, "w").close()
            os.remove(probe)
        except OSError as e:
            return err(f"AutoApply can't save to that folder: {e.strerror or e}. "
                       "On a Mac, allow access if asked (System Settings → Privacy & Security → Files and Folders).")
    cfg = load_cfg()
    cfg.setdefault("output", {})["dir"] = path
    write_yaml(CONFIG, cfg)
    return get_resume_folder()


# ------------------------------------------------------------------ routes: app state
@app.get("/")
def index():
    if not os.path.exists(os.path.join(app.static_folder, "index.html")):
        return (f"AutoApply's files are incomplete (missing {app.static_folder}/index.html). "
                "Please download and install AutoApply again."), 500
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
def state():
    p = load_profile()
    db = load_db()
    counts = {}
    for r in db.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return jsonify({"app": "autoapply", "version": updater.local_version(), "setup_done": os.path.exists(SETUP), "has_key": bool(load_key()),
                    "profile_name": (p or {}).get("name", ""), "has_profile": bool(p and p.get("experience")),
                    "counts": counts, "total": len(db), "is_remote": not is_local(),
                    "phone_enabled": bool(load_secrets().get("phone_enabled"))})


@app.post("/api/setup-done")
def setup_done():
    open(SETUP, "w").write(dt.datetime.now().isoformat())
    return jsonify(ok=True)


# ------------------------------------------------------------------ routes: profile
@app.get("/api/profile")
def get_profile():
    return jsonify(load_profile() or {})


@app.put("/api/profile")
def put_profile():
    p = request.get_json()
    if not p or not p.get("name"):
        return err("Your name is required.")
    write_yaml(PROFILE, finalize(p))
    return jsonify(load_profile())


@app.post("/api/profile/import")
def import_profile():
    f = request.files.get("file")
    if not f:
        return err("No file uploaded.")
    if not load_key():
        return err("Importing needs an Anthropic API key. Add one in Settings, or fill in My Resume by hand.")
    try:
        p = import_resume(f.filename, f.read(), load_cfg()["tailoring"]["model"])
    except Exception as e:  # noqa: BLE001
        return err(f"Couldn't import that resume: {e}")
    if os.path.exists(PROFILE):
        os.replace(PROFILE, PROFILE + ".backup")
    write_yaml(PROFILE, p)
    # pre-fill application answers from the resume
    cfg = load_cfg()
    a = cfg.setdefault("applicant", {})
    first, _, last = p["name"].partition(" ")
    a.update({"first_name": first, "last_name": last, "email": p["contact"].get("email", ""),
              "phone": p["contact"].get("phone", ""), "city": p["contact"].get("location", ""),
              "linkedin": p["contact"].get("linkedin_url", "")})
    write_yaml(CONFIG, cfg)
    return jsonify(p)


@app.get("/api/profile/preview")
def preview():
    p = load_profile()
    if not p:
        return err("No resume yet.")
    pdf, _ = render_pdf_one_page(p, os.path.join(OUT, "_preview.pdf"))
    return send_from_directory(OUT, "_preview.pdf", max_age=0)


# ------------------------------------------------------------------ routes: settings
@app.get("/api/settings")
def get_settings():
    k = load_key()
    sec = load_secrets()
    return jsonify({"config": load_cfg(), "api_key_set": bool(k), "api_key_hint": ("…" + k[-4:]) if k else "",
                    "sources": {n: ("…" + sec[n][-4:]) if sec.get(n) else "" for n in SOURCE_KEYS},
                    "places": [p for p in parse_locations((load_cfg().get("search") or {}).get("location", "")) if p]})


@app.put("/api/settings")
def put_settings():
    body = request.get_json()
    if "config" in body:
        write_yaml(CONFIG, body["config"])
    if body.get("sources"):
        if not is_local():
            return err("Change keys on your computer.", 403)
        sec = load_secrets()
        for n in SOURCE_KEYS:
            v = (body["sources"].get(n) or "").strip()
            if v:
                sec[n] = v
            elif body["sources"].get(n) == "" and body.get("clear_sources"):
                sec.pop(n, None)
        save_secrets(sec)
    if body.get("api_key") is not None:
        if not is_local():
            return err("Change the API key on your computer.", 403)
        key = body["api_key"].strip()
        sec = load_secrets()
        sec["anthropic_api_key"] = key
        save_secrets(sec)
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
        else:
            os.environ.pop("ANTHROPIC_API_KEY", None)
    return get_settings()


# ------------------------------------------------------------------ updates
upd = {"checked": 0, "current": updater.local_version(), "latest": None, "available": False,
       "notes": "", "state": "idle", "message": "", "error": ""}


def check_updates(force=False):
    if not force and time.time() - upd["checked"] < 600:
        return upd
    try:
        upd.update(updater.check(), checked=time.time(), error="")
    except Exception as e:  # noqa: BLE001
        upd.update(checked=time.time(), error=f"Couldn't check for updates: {str(e)[:120]}")
    return upd


def run_update(open_browser=False):
    def progress(m):
        upd.update(state="installing", message=m)
    try:
        upd.update(state="installing", message="Starting…", error="")
        updater.install(upd["latest"], progress)
        upd.update(state="restarting", message="Restarting AutoApply…")
        time.sleep(0.8)   # let the page see "restarting"
        updater.restart(open_browser=open_browser)
    except Exception as e:  # noqa: BLE001
        upd.update(state="idle", message="", error=f"Update failed, nothing was changed: {str(e)[:200]}")


@app.get("/api/update")
def get_update():
    check_updates(force=request.args.get("force") == "1")
    return jsonify({k: upd[k] for k in ("current", "latest", "available", "notes", "state", "message", "error")} |
                   {"auto": (load_cfg().get("updates") or {}).get("auto_install", True)})


@app.post("/api/update")
def post_update():
    if upd["state"] != "idle":
        return err("An update is already running.")
    check_updates(force=True)
    if not upd["available"]:
        return err("You already have the latest version.")
    threading.Thread(target=run_update, daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/app-settings")
def get_app_settings():
    a = load_cfg().get("app") or {}
    return jsonify({"quit_on_close": a.get("quit_on_close", True)})


@app.put("/api/app-settings")
def put_app_settings():
    cfg = load_cfg()
    cfg.setdefault("app", {})["quit_on_close"] = bool((request.get_json() or {}).get("quit_on_close", True))
    write_yaml(CONFIG, cfg)
    return get_app_settings()


@app.put("/api/update/auto")
def set_auto_update():
    cfg = load_cfg()
    cfg.setdefault("updates", {})["auto_install"] = bool((request.get_json() or {}).get("auto"))
    write_yaml(CONFIG, cfg)
    return get_update()


@app.get("/api/sources/usage")
def sources_usage():
    key = load_secrets().get("serpapi_key")
    if not key:
        return jsonify({})
    try:
        import requests
        a = requests.get("https://serpapi.com/account.json", params={"api_key": key}, timeout=10).json()
        return jsonify({"google_left": a.get("plan_searches_left", a.get("total_searches_left")),
                        "google_per_month": a.get("searches_per_month")})
    except Exception:  # noqa: BLE001
        return jsonify({})


@app.post("/api/test-key")
def test_key():
    if not load_key():
        return err("No key saved.")
    try:
        import anthropic
        anthropic.Anthropic().messages.create(model=load_cfg()["tailoring"]["model"], max_tokens=5,
                                              messages=[{"role": "user", "content": "hi"}])
        return jsonify(ok=True)
    except Exception as e:  # noqa: BLE001
        return err(f"Key didn't work: {str(e)[:200]}")


# ------------------------------------------------------------------ routes: tailoring
@app.post("/api/tailor")
def tailor_route():
    b = request.get_json()
    text = (b.get("input") or "").strip()
    if not text:
        return err("Paste a job link or the job description.")
    try:
        if re.match(r"^https?://\S+$", text):
            try:
                parse_job_url(text, b.get("slug") or None)
            except ValueError as e:
                msg = str(e).split("\n")[0]
                if "gh_jid" in text:
                    return err(msg, 422)
                return jsonify({"need_description": True,
                                "message": "This site doesn't let apps read or fill its applications "
                                           "(LinkedIn, Indeed, Workday and others). Paste the job description "
                                           "below and you'll still get a tailored resume."}), 200
            job = fetch_job(text, b.get("slug") or None)
        else:
            if len(text) < 150:
                return err("That looks too short for a job description. Paste the whole posting.")
            job = manual_job(text=text, company=b.get("company") or "Unknown company",
                             title=b.get("title") or "", url=b.get("url") or "")
        return jsonify(make_resume(job))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return err(str(e))


@app.get("/api/jobs")
def list_jobs():
    return jsonify(sorted(load_db().values(), key=lambda r: r.get("updated", ""), reverse=True))


@app.post("/api/jobs/<jid>/status")
def set_status(jid):
    update_job(jid, status=request.get_json()["status"], updated=dt.datetime.now().isoformat(timespec="seconds"))
    return jsonify(load_db().get(jid))


@app.delete("/api/jobs/<jid>")
def delete_job(jid):
    with lock:
        db = load_db()
        db.pop(jid, None)
        save_db(db)
    return jsonify(ok=True)


@app.post("/api/jobs/<jid>/retailor")
def retailor(jid):
    r = load_db().get(jid)
    if not r:
        return err("Not found", 404)
    return jsonify(make_resume(job_from_record(r)))


applying = {"busy": False}


@app.post("/api/jobs/<jid>/apply")
def apply_route(jid):
    r = load_db().get(jid)
    if not r:
        return err("Not found", 404)
    if not r.get("can_apply"):
        return err("This job's site can't be filled automatically. Open the posting and apply by hand.")
    if applying["busy"]:
        return err("Another application window is still open. Finish or close that one first.")
    from apply import apply_one

    def on_status(state, msg):
        st = {"opening": "applying", "opened": "applying", "applying": "applying", "closed": "check",
              "check": "check", "submitted": "submitted", "error": "applying"}.get(state, "applying")
        update_job(jid, status=st, apply_msg=msg, updated=dt.datetime.now().isoformat(timespec="seconds"))

    def run():
        applying["busy"] = True
        try:
            apply_one(job_from_record(r), record_file(r, "pdf") or os.path.join(OUT, r["pdf"]), load_cfg(), load_profile(), on_status,
                      accounts=load_secrets().get("workday_accounts", {}), save_account=save_workday_account)
        except Exception as e:  # noqa: BLE001
            update_job(jid, status="ready", apply_msg=f"Couldn't open the browser: {e}")
        finally:
            applying["busy"] = False

    threading.Thread(target=run, daemon=True).start()
    update_job(jid, status="applying", apply_msg="Opening the application page...")
    return jsonify(load_db()[jid])


@app.get("/files/<path:name>")
def files(name):
    return send_from_directory(OUT, name, as_attachment=request.args.get("dl") == "1")


def record_file(r, ext):
    p = r.get(f"{ext}_path") or os.path.join(OUT, r.get(ext, ""))
    return p if os.path.exists(p) else None


@app.get("/resume/<jid>.<ext>")
def resume_file(jid, ext):
    r = load_db().get(jid)
    if not r or ext not in ("pdf", "docx"):
        return err("Not found", 404)
    p = record_file(r, ext)
    if not p:
        return err("That resume file was moved or deleted. Click 'Re-tailor' to make it again.", 404)
    from flask import send_file
    return send_file(p, as_attachment=request.args.get("dl") == "1", download_name=os.path.basename(p), max_age=0)


@app.post("/api/jobs/<jid>/reveal")
def reveal_job_file(jid):
    r = load_db().get(jid)
    p = r and record_file(r, "pdf")
    if not p or not is_local():
        return err("Not available.", 404)
    _open_in_file_manager(os.path.dirname(p), select=p)
    return jsonify(ok=True)


# ------------------------------------------------------------------ routes: job search
search = {"running": False, "log": [], "results": [], "jobs": {}}


@app.post("/api/search")
def start_search():
    if search["running"]:
        return err("Already searching.")
    cfg, profile = load_cfg(), load_profile()
    db = load_db()

    def run():
        search.update(running=True, log=[], results=[], jobs={})
        try:
            found = discover(cfg, log=lambda m: search["log"].append(m.strip()), keys=load_secrets())
            bad = cfg.pop("_bad_boards", [])
            if bad:   # company names that don't exist on that job site: drop them for next time
                saved = load_cfg()
                for source, slug in bad:
                    if slug in (saved.get("boards", {}).get(source) or []):
                        saved["boards"][source].remove(slug)
                write_yaml(CONFIG, saved)
            res = []
            for j in found:
                s, matched, missing = score(j, profile, cfg["search"])
                if j.extra.get("snippet_only"):   # only a 2-line preview: don't let it outrank real scores
                    s = min(round(s * 0.85), 80)
                jid = jid_for(j)
                search["jobs"][jid] = j
                res.append({"id": jid, "company": j.extra.get("company_name") or j.company, "title": j.title,
                            "location": j.location, "url": j.url, "score": s, "missing": missing[:8],
                            "via": j.extra.get("via") or j.source.title(), "posted": j.extra.get("posted", ""),
                            "can_apply": j.source in ("greenhouse", "lever", "ashby", "workday"),
                            "snippet": bool(j.extra.get("snippet_only")),
                            "already": db.get(jid, {}).get("status")})
            res.sort(key=lambda x: -x["score"])
            search["results"] = res
            search["log"].append(f"Done: {len(res)} matching jobs.")
        except Exception as e:  # noqa: BLE001
            search["log"].append(f"Error: {e}")
        finally:
            search["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/search")
def get_search():
    return jsonify({k: search[k] for k in ("running", "log", "results")})


@app.post("/api/search/<jid>/tailor")
def search_tailor(jid):
    j = search["jobs"].get(jid)
    if not j:
        return err("Search again, that result expired.")
    try:
        j = full_description(j, log=lambda *_: None)
        return jsonify(make_resume(j))
    except Exception as e:  # noqa: BLE001
        return err(str(e))


PORT_FILE = os.path.join(DATA, "port")


def _is_autoapply(port):
    """True only if AutoApply itself answers on this port (not some other program)."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1.5) as r:
            return json.loads(r.read().decode()).get("app") == "autoapply"
    except Exception:  # noqa: BLE001
        return False


def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def choose_port():
    """Use the last port that worked; if another program has it, move to a free pair (app + phone)."""
    global MAIN_PORT, PHONE_PORT
    if os.environ.pop("AUTOAPPLY_RESTART", None):   # restarting after an update: wait for the old copy to exit
        try:
            p = int(open(PORT_FILE).read().strip())
            for _ in range(60):
                if _port_free(p) and _port_free(p + 1):
                    MAIN_PORT, PHONE_PORT = p, p + 1
                    return p, False
                time.sleep(0.25)
        except (OSError, ValueError):
            pass
    preferred = []
    if os.environ.get("AUTOAPPLY_PORT"):
        preferred.append(int(os.environ["AUTOAPPLY_PORT"]))
    try:
        preferred.append(int(open(PORT_FILE).read().strip()))
    except (OSError, ValueError):
        pass
    preferred.append(8765)
    for p in preferred:
        if _is_autoapply(p):
            return p, True
    for p in preferred + list(range(8771, 8871, 2)):
        if _port_free(p) and _port_free(p + 1):
            MAIN_PORT, PHONE_PORT = p, p + 1
            open(PORT_FILE, "w").write(str(p))
            return p, False
    raise SystemExit("No free port found for AutoApply.")


# ------------------------------------------------------------------ quit when the window closes
life = {"last_beat": 0.0, "bye_at": 0.0, "seen": False}


@app.post("/api/heartbeat")
def heartbeat():
    if is_local():
        life.update(last_beat=time.time(), bye_at=0.0, seen=True)
    return jsonify(ok=True)


@app.post("/api/bye")
def bye():
    if is_local():
        life["bye_at"] = time.time()
    return ("", 204)


def _busy():
    """Things that should keep AutoApply running even with no window open."""
    return (applying["busy"] or search["running"] or upd["state"] != "idle"
            or bool(load_secrets().get("phone_enabled")))


def quit_watchdog():
    """Fully quit (like the Quit button) once its window is closed: after the tab says goodbye and
    doesn't come back within 20 s (a reload comes back), or if no open tab has checked in for ~100 s
    (browsers still let background tabs check in about once a minute)."""
    while True:
        time.sleep(5)
        if not life["seen"] or not (load_cfg().get("app") or {}).get("quit_on_close", True) or _busy():
            continue
        now = time.time()
        closed = life["bye_at"] and now - life["bye_at"] > 20 and life["last_beat"] < life["bye_at"]
        silent = now - life["last_beat"] > float(os.environ.get("AUTOAPPLY_QUIT_AFTER", "100"))
        if closed or silent:
            print("Window closed: quitting AutoApply.")
            os._exit(0)


def main():
    import sys
    port, running = choose_port()
    url = f"http://127.0.0.1:{port}"
    if running:   # already open (e.g. double-clicked twice): just show it
        webbrowser.open(url)
        return
    # Started without a terminal (Mac app / Windows shortcut): log to a file instead
    if sys.stdout is None or os.environ.get("AUTOAPPLY_LOGFILE"):
        log = open(os.path.join(DATA, "app.log"), "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = log
    load_key()
    # Install updates on launch (before any work is open), if turned on
    if (load_cfg().get("updates") or {}).get("auto_install", True) and not os.environ.get("AUTOAPPLY_NO_UPDATE"):
        check_updates(force=True)
        if upd["available"]:
            print(f"Updating {upd['current']} -> {upd['latest']}")
            run_update(open_browser=os.environ.get("AUTOAPPLY_NO_BROWSER") != "1")   # restarts on success
    threading.Thread(target=quit_watchdog, daemon=True).start()

    def periodic_check():
        while True:
            time.sleep(15 * 60)
            check_updates(force=True)
    threading.Thread(target=periodic_check, daemon=True).start()
    if load_secrets().get("phone_enabled"):
        try:
            start_phone_server()
        except OSError:
            pass
    print(f"\n  AutoApply is running at {url}\n  Keep this window open while you use it. Close it to quit.\n")
    if os.environ.get("AUTOAPPLY_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
