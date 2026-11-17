"""Self-updater: pulls new versions of AutoApply's code from GitHub.

Only code is replaced (the *.py files, static/, fonts/, requirements.txt, VERSION
and the blank templates). Your resume, settings, keys and saved resumes are never
touched. Updates download over HTTPS from the public repository below and are
checked (version match, every Python file compiles) before anything is replaced;
if anything fails, the old files are put back.
"""
import io
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

import requests

REPO = os.environ.get("AUTOAPPLY_REPO", "shapowed/autoapply")
BRANCH = os.environ.get("AUTOAPPLY_BRANCH", "main")
HERE = os.path.dirname(os.path.abspath(__file__))

CODE_FILES = {"requirements.txt", "VERSION", "profile.example.yaml", "config.example.yaml", "README.md"}
CODE_DIRS = ("static/", "fonts/")
NEVER = {"profile.yaml", "config.yaml", "build.py"}


def local_version():
    try:
        return open(os.path.join(HERE, "VERSION")).read().strip()
    except OSError:
        return "0"


def vtuple(v):
    out = []
    for part in str(v).strip().split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits or 0))
    return tuple(out)


def latest_commit(timeout=8):
    """ID of the newest published snapshot. Files fetched by this ID are never stale copies."""
    try:
        r = requests.get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}", timeout=timeout,
                         headers={"Accept": "application/vnd.github.sha"})
        sha = r.text.strip() if r.ok else ""
        return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else ""
    except Exception:  # noqa: BLE001
        return ""


def check(timeout=6):
    """Return {current, latest, available, notes, sha}. Raises on network trouble."""
    sha = latest_commit()
    if sha:
        r = requests.get(f"https://raw.githubusercontent.com/{REPO}/{sha}/updates.json", timeout=timeout)
    else:
        r = requests.get(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/updates.json",
                         params={"t": int(time.time())}, timeout=timeout, headers={"Cache-Control": "no-cache"})
    r.raise_for_status()
    data = r.json()
    cur = local_version()
    return {"current": cur, "latest": data.get("version", cur), "notes": data.get("notes", ""), "sha": sha,
            "available": vtuple(data.get("version", "0")) > vtuple(cur)}


def _download(expected_version, sha, progress):
    """Get the code zip for this version, waiting for GitHub to catch up if it's still publishing."""
    for attempt in range(4):
        url = (f"https://codeload.github.com/{REPO}/zip/{sha}" if sha
               else f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}")
        r = requests.get(url, params=None if sha else {"t": int(time.time())}, timeout=180)
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        root = z.namelist()[0].split("/")[0] + "/"
        try:
            got = z.read(root + "VERSION").decode().strip()
        except KeyError:
            got = ""
        if got == expected_version:
            return z, root
        if attempt < 3:
            progress("GitHub is still publishing this update. Trying again in a few seconds…")
            time.sleep(15)
            sha = latest_commit() or sha
    raise RuntimeError(f"GitHub is still publishing version {expected_version}. Try again in a couple of minutes.")


def _wanted(rel):
    if rel in NEVER or rel.endswith("/"):
        return False
    if "/" not in rel:
        return rel.endswith(".py") or rel in CODE_FILES
    return rel.startswith(CODE_DIRS)


def install(expected_version, progress=lambda msg: None, sha=""):
    """Download, verify and install. Returns True if requirements changed."""
    progress("Downloading the update…")
    z, root = _download(expected_version, sha, progress)

    stage = tempfile.mkdtemp(prefix="autoapply-update-")
    backup = tempfile.mkdtemp(prefix="autoapply-backup-")
    try:
        files = []
        for name in z.namelist():
            rel = name[len(root):] if name.startswith(root) else name
            if not rel or not _wanted(rel) or ".." in rel.split("/") or rel.startswith("/"):
                continue
            dest = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(name) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            files.append(rel)

        progress("Checking the update…")
        got = open(os.path.join(stage, "VERSION")).read().strip() if "VERSION" in files else ""
        if got != expected_version:
            raise RuntimeError(f"Downloaded version {got or '?'} doesn't match {expected_version}. Try again in a minute.")
        for rel in files:
            if rel.endswith(".py"):
                py_compile.compile(os.path.join(stage, rel), doraise=True,
                                   cfile=os.path.join(tempfile.gettempdir(), "aa_check.pyc"))
        for required in ("server.py", "static/index.html", "requirements.txt"):
            if required not in files:
                raise RuntimeError(f"The update is missing {required}.")

        old_req = _read(os.path.join(HERE, "requirements.txt"))
        new_req = _read(os.path.join(stage, "requirements.txt"))
        req_changed = old_req != new_req
        if req_changed:
            progress("Installing new components (this can take a minute)…")
            subprocess.run([_python(), "-m", "pip", "install", "-q", "-r", os.path.join(stage, "requirements.txt")],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        progress("Installing…")
        replaced = []
        try:
            for rel in files:
                cur = os.path.join(HERE, rel)
                if os.path.exists(cur):
                    b = os.path.join(backup, rel)
                    os.makedirs(os.path.dirname(b), exist_ok=True)
                    shutil.copy2(cur, b)
                os.makedirs(os.path.dirname(cur), exist_ok=True)
                shutil.copy2(os.path.join(stage, rel), cur)
                replaced.append(rel)
        except Exception:
            for rel in replaced:   # put the old version back
                b = os.path.join(backup, rel)
                if os.path.exists(b):
                    shutil.copy2(b, os.path.join(HERE, rel))
            raise
        return req_changed
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)


def _read(p):
    try:
        return open(p, encoding="utf-8").read()
    except OSError:
        return ""


def _python():
    # pythonw.exe can't run pip reliably; use python.exe from the same folder on Windows
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("pythonw.exe"):
        alt = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(alt):
            return alt
    return exe


def restart(open_browser=False):
    """Start a fresh copy of AutoApply, then exit this one."""
    env = dict(os.environ, AUTOAPPLY_RESTART="1")
    if not open_browser:
        env["AUTOAPPLY_NO_BROWSER"] = "1"
    kwargs = {"cwd": HERE, "env": env, "stdin": subprocess.DEVNULL,
              "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], **kwargs)
    time.sleep(0.3)
    os._exit(0)


if __name__ == "__main__":
    print(json.dumps(check(), indent=2))
