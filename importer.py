"""Turn an uploaded resume (PDF / DOCX / TXT) into a profile the app can tailor."""
import io
import json
import re

IMPORT_PROMPT = """Convert this resume into JSON. Copy text EXACTLY as written: do not improve,
shorten, merge or invent anything. Keep every bullet and every skill.

Return ONLY JSON in this shape:
{
 "name": "...",
 "contact": {"email": "", "phone": "", "location": "", "linkedin_label": "LinkedIn", "linkedin_url": ""},
 "summary": "...",
 "experience": [{"title": "", "company": "", "location": "", "dates": "",
                 "bullets": [{"text": "...", "tags": ["3-6 lowercase keywords describing the bullet"]}]}],
 "education": [{"school": "School — City, ST", "detail": "Degree — Date", "extra": "coursework/honors line or empty"}],
 "certifications": [["Cert name", "date or status"]],
 "skills": {"Category name": [{"name": "skill exactly as written",
                               "aliases": ["other common names recruiters use for this exact skill"]}]}
}
If the resume has no skill categories, group skills sensibly. Use "" for anything missing.

RESUME:
"""


def extract_text(filename, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    if name.endswith(".docx"):
        from docx import Document
        d = Document(io.BytesIO(data))
        return "\n".join(p.text for p in d.paragraphs)
    return data.decode("utf-8", errors="ignore")


def finalize(profile: dict) -> dict:
    """Fill in ids and defaults so the rest of the app can rely on them."""
    profile.setdefault("contact", {})
    for k in ("email", "phone", "location", "linkedin_url"):
        profile["contact"].setdefault(k, "")
    profile["contact"].setdefault("linkedin_label", "LinkedIn")
    profile.setdefault("summary", "")
    for i, e in enumerate(profile.get("experience", [])):
        e.setdefault("id", re.sub(r"\W+", "_", (e.get("company") or f"job{i}").lower())[:20] + f"_{i}")
        for k, b in enumerate(e.get("bullets", [])):
            if isinstance(b, str):
                b = e["bullets"][k] = {"text": b}
            b.setdefault("id", f"{e['id']}_b{k}")
            b.setdefault("tags", [])
            b["text"] = b["text"].lstrip("•-– ").rstrip()
    profile.setdefault("education", [])
    profile["certifications"] = [list(c) if isinstance(c, (list, tuple)) else [str(c), ""]
                                 for c in profile.get("certifications", [])]
    skills = profile.get("skills") or {}
    for cat, items in skills.items():
        skills[cat] = [({"name": s} if isinstance(s, str) else s) for s in items]
        for s in skills[cat]:
            s.setdefault("aliases", [])
    profile["skills"] = skills
    return profile


def import_resume(filename, data, model):
    text = extract_text(filename, data)
    if len(text.strip()) < 200:
        raise ValueError("Couldn't read text from that file. If it's a scanned image, upload the Word version.")
    import anthropic
    msg = anthropic.Anthropic().messages.create(
        model=model, max_tokens=8000,
        messages=[{"role": "user", "content": IMPORT_PROMPT + text[:30000]}])
    raw = msg.content[0].text
    return finalize(json.loads(raw[raw.find("{"): raw.rfind("}") + 1]))
