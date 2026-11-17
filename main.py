#!/usr/bin/env python3
"""Auto Apply & Adapt — find jobs, tailor your resume to each, fill the applications.

  python main.py base                       # render your base resume (sanity check)
  python main.py run                        # find -> score -> tailor -> apply
  python main.py run --dry-run              # everything except opening application forms
  python main.py tailor jd.txt --company Acme --title "IT Support Analyst"   # any job (LinkedIn, Indeed...)
  python main.py url <link> [<link> ...]    # job links from LinkedIn/Indeed -> company apply page
  python main.py url --file urls.txt        # a list of links, one per line
  python main.py status                     # what you've applied to
"""
import argparse
import csv
import datetime as dt
import json
import os
import re

import yaml

from jobs import discover, manual_job
from render import render_docx, render_pdf_one_page
from tailor import load_profile, score, tailor

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")
TRACK = os.path.join(HERE, "data", "applied.json")
CSV = os.path.join(OUT, "tracker.csv")


def load_cfg():
    with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_track():
    return json.load(open(TRACK)) if os.path.exists(TRACK) else {}


def save_track(t):
    os.makedirs(os.path.dirname(TRACK), exist_ok=True)
    json.dump(t, open(TRACK, "w"), indent=2)


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:50]


def build_resume(profile, job, cfg):
    tailored, method = tailor(profile, job, cfg)
    base = f"{slug(profile['name'])}_Resume_{slug(job.company)}_{slug(job.title)}"
    pdf, size = render_pdf_one_page(tailored, os.path.join(OUT, base + ".pdf"))
    render_docx(tailored, os.path.join(OUT, base + ".docx"), size)
    return pdf, method


def write_csv_row(row):
    new = not os.path.exists(CSV)
    with open(CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def cmd_base(_):
    p = load_profile(os.path.join(HERE, "profile.yaml"))
    os.makedirs(OUT, exist_ok=True)
    pdf, size = render_pdf_one_page(p, os.path.join(OUT, f"{slug(p['name'] or 'My')}_Resume_BASE.pdf"))
    render_docx(p, os.path.join(OUT, f"{slug(p['name'] or 'My')}_Resume_BASE.docx"), size)
    print(f"Wrote {pdf} (body {size}pt)")


def cmd_tailor(a):
    cfg, p = load_cfg(), load_profile(os.path.join(HERE, "profile.yaml"))
    os.makedirs(OUT, exist_ok=True)
    job = manual_job(a.jd, a.company, a.title, a.url or "")
    s, matched, missing = score(job, p, cfg["search"])
    pdf, method = build_resume(p, job, cfg)
    print(f"Fit score: {s}/100   (tailored with {method})")
    print(f"You match:      {', '.join(matched[:20])}")
    print(f"JD wants, not on your resume: {', '.join(missing[:20]) or '—'}")
    print("  -> if you genuinely have any of those, add them to profile.yaml and re-run.")
    print(f"Resume: {pdf}")


def cmd_run(a):
    cfg, p = load_cfg(), load_profile(os.path.join(HERE, "profile.yaml"))
    os.makedirs(OUT, exist_ok=True)
    track = load_track()
    print("Finding jobs...")
    jobs = [j for j in discover(cfg) if j.key not in track]
    scored = []
    for j in jobs:
        s, matched, missing = score(j, p, cfg["search"])
        if s >= cfg["search"]["min_score"]:
            scored.append((s, j, matched, missing))
    scored.sort(key=lambda x: -x[0])
    scored = scored[: cfg["search"]["max_applications_per_run"]]
    print(f"\n{len(scored)} new jobs scored >= {cfg['search']['min_score']}:")
    for s, j, *_ in scored:
        print(f"  {s:3d}  {j.company:14s} {j.title}  [{j.location}]")
    if not scored:
        return

    items = []
    for s, j, matched, missing in scored:
        print(f"\nTailoring for {j.company} — {j.title}")
        pdf, method = build_resume(p, j, cfg)
        print(f"    {method}; missing keywords: {', '.join(missing[:8]) or '—'}")
        items.append((j, pdf, s, matched, missing))

    results = {}
    if not a.dry_run:
        from apply import apply_to_jobs
        results = apply_to_jobs([(j, pdf) for j, pdf, *_ in items], cfg, p)

    today = dt.date.today().isoformat()
    for j, pdf, s, matched, missing in items:
        status = results.get(j.key, "tailored-only")
        if status in ("submitted", "submit-clicked-unconfirmed", "skipped"):
            track[j.key] = {"date": today, "status": status, "title": j.title, "company": j.company}
        write_csv_row({"date": today, "company": j.company, "title": j.title, "location": j.location,
                       "score": s, "status": status, "url": j.url, "resume": os.path.basename(pdf),
                       "missing_keywords": "; ".join(missing[:10])})
    save_track(track)
    print(f"\nDone. Tracker: {CSV}")


def cmd_url(a):
    from jobs import fetch_job
    cfg, p = load_cfg(), load_profile(os.path.join(HERE, "profile.yaml"))
    os.makedirs(OUT, exist_ok=True)
    track = load_track()
    urls = list(a.urls)
    if a.file:
        urls += [l.strip() for l in open(a.file, encoding="utf-8")
                 if l.strip() and not l.strip().startswith("#")]
    if not urls:
        print("Give one or more job URLs, or --file urls.txt")
        return

    items = []
    for u in urls:
        print(f"\n{u}")
        try:
            job = fetch_job(u, a.slug)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {e}")
            continue
        if job.key in track and not a.force:
            print(f"  already {track[job.key]['status']} on {track[job.key]['date']} (use --force to redo)")
            continue
        s, matched, missing = score(job, p, cfg["search"])
        pdf, method = build_resume(p, job, cfg)
        print(f"  {job.company} — {job.title} [{job.location}]")
        print(f"  Fit {s}/100 ({method}).  Missing: {', '.join(missing[:8]) or '—'}")
        print(f"  Resume: {os.path.basename(pdf)}")
        items.append((job, pdf, s, missing))

    results = {}
    if items and not a.no_apply:
        from apply import apply_to_jobs
        results = apply_to_jobs([(j, pdf) for j, pdf, *_ in items], cfg, p)

    today = dt.date.today().isoformat()
    for j, pdf, s, missing in items:
        status = results.get(j.key, "tailored-only")
        if status in ("submitted", "submit-clicked-unconfirmed", "skipped"):
            track[j.key] = {"date": today, "status": status, "title": j.title, "company": j.company}
        write_csv_row({"date": today, "company": j.company, "title": j.title, "location": j.location,
                       "score": s, "status": status, "url": j.url, "resume": os.path.basename(pdf),
                       "missing_keywords": "; ".join(missing[:10])})
    save_track(track)
    if items:
        print(f"\nDone. Tracker: {CSV}")


def cmd_status(_):
    t = load_track()
    for k, v in sorted(t.items(), key=lambda kv: kv[1]["date"]):
        print(f"{v['date']}  {v['status']:12s} {v['company']:14s} {v['title']}")
    print(f"{len(t)} total")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("base").set_defaults(fn=cmd_base)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="tailor resumes but don't open applications")
    r.set_defaults(fn=cmd_run)
    t = sub.add_parser("tailor")
    t.add_argument("jd", help="text file containing the job description")
    t.add_argument("--company", default="manual")
    t.add_argument("--title", default="")
    t.add_argument("--url")
    t.set_defaults(fn=cmd_tailor)
    u = sub.add_parser("url", help="tailor + apply from job links (Greenhouse/Lever/Ashby)")
    u.add_argument("urls", nargs="*")
    u.add_argument("--file", help="text file with one URL per line")
    u.add_argument("--slug", help="Greenhouse company name, for career-site links with ?gh_jid=")
    u.add_argument("--no-apply", action="store_true", help="only make the tailored resume")
    u.add_argument("--force", action="store_true", help="redo jobs already in the tracker")
    u.set_defaults(fn=cmd_url)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = ap.parse_args()
    args.fn(args)
