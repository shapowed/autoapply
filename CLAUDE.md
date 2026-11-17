# AutoApply

Local job-application helper: tailors a resume to each job (truthfully, from `profile.yaml` only),
searches jobs (Greenhouse/Lever/Ashby boards, Google Jobs via SerpApi, Adzuna), and fills
application forms with Playwright. Flask server (`server.py`) + single-page UI (`static/index.html`).

## Shipping changes: the app updates itself from this repo

Installed copies check `updates.json` on `main` at launch and every 6 hours, download
the code zip, verify it, and replace only code files (see `updater.py`). To release:

1. Make the change. Keep Python 3.9-compatible.
2. Bump `VERSION` **and** `updates.json` → `version` to the same new value, and write a
   one-line, plain-English `notes` for the user.
3. Commit and push to `main`. Users get it next time AutoApply opens (or via "Update now").

`VERSION` in the downloaded zip must equal `updates.json`'s version or the update is refused.

Rebuild installers (only needed for new installs or launcher changes): `python build.py` → `dist/`.

## Never commit personal data

`profile.yaml`, `config.yaml`, `data/*` (keys live in `data/secrets.json`) and generated resumes are
git-ignored. The repo ships blank templates: `profile.example.yaml`, `config.example.yaml`.
New config keys must be read with `.get(...)` defaults, because existing users' `config.yaml`
won't have them.

## Truthfulness rules (keep these)

Tailoring may reorder/reword only what's in the master profile. `tailor.validate_rewrite`
rejects rewrites that add tools, numbers or low-confidence skills. Missing JD keywords are
reported to the user, never inserted.
