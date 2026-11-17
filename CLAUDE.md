# AutoApply

Local job-application helper: tailors a resume to each job (truthfully, from `profile.yaml` only),
searches jobs (Greenhouse/Lever/Ashby boards, Google Jobs via SerpApi, Adzuna), and fills
application forms with Playwright. Flask server (`server.py`) + single-page UI (`static/index.html`).
Workday: postings are read via the public `/wday/cxs/{tenant}/{site}` JSON endpoints (`jobs.py`);
applications go through `workday_apply.py` (beta: per-company accounts saved in `data/secrets.json`
under `workday_accounts`; it stops and waits for the person whenever a page needs them, and at Review).

## Shipping changes: the app updates itself from this repo

Installed copies check for updates at launch and every 15 minutes (newest commit SHA via the API,
then `updates.json` + code zip by SHA so nothing is stale), verify, and replace only code files
(see `updater.py`). New entries in `requirements.txt` are pip-installed during the update. To release:

1. Make the change. Keep Python 3.9-compatible.
2. Bump `VERSION` **and** `updates.json` → `version` to the same new value, and write a
   one-line, plain-English `notes` for the user.
3. Commit and push to `main`. Users get it next time AutoApply opens (or via "Update now").

`VERSION` in the downloaded zip must equal `updates.json`'s version or the update is refused.

Rebuild installers (only needed for new installs or launcher changes): `python build.py` → `dist/`.

Radius search: `geo.py` (offline US ZIP/city coordinates from the `zipcodes` package) measures each
job's distance; `jobs.search_places` / `_within` apply it.

## Never commit personal data

`profile.yaml`, `config.yaml`, `data/*` (keys live in `data/secrets.json`) and generated resumes are
git-ignored. The repo ships blank templates: `profile.example.yaml`, `config.example.yaml`.
New config keys must be read with `.get(...)` defaults, because existing users' `config.yaml`
won't have them.

## Truthfulness rules (keep these)

Tailoring may reorder/reword only what's in the master profile. `tailor.validate_rewrite`
rejects rewrites that add tools, numbers or low-confidence skills. Missing JD keywords are
reported to the user and only added (to the skills section, never bullets; never certifications)
after the user ticks them and confirms.
