"""Render one resume PDF PER PROFILE, from the profile itself - resume and profile are ONE identity
(directive: 'in reality no one should have two different things'). Workday's autofill then pre-fills
the SAME values L1 would fill, so autofill becomes free labor instead of a rival truth source.
The overwrite-and-fix machinery stays for real-world divergence. Run: python gen_resumes.py"""

import sys
from pathlib import Path

from fpdf import FPDF

sys.path.insert(0, str(Path(__file__).parent))
from wd_one import PROFILES

OUT = Path(__file__).parent / "fixtures" / "resumes"
OUT.mkdir(parents=True, exist_ok=True)


def render(p: dict) -> Path:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(True, margin=18)

    def line(txt, size=11, style="", h=6):
        pdf.set_font("Helvetica", style, size)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, h, txt, new_x="LMARGIN", new_y="NEXT")

    name = f"{p['first_name']} {p['last_name']}"
    line(name, 20, "B", 10)
    line(f"{p['address']}, {p['city']}, {p['state']} {p['postal_code']}", 10)
    line(f"{p['email']}  |  {p['phone']}  |  {p['linkedin']}", 10, h=8)

    line("Work Experience", 14, "B", 9)
    for e in p.get("experience", []):
        end = "Present" if e.get("current") else e.get("end", "")
        line(f"{e['title']} - {e['company']} ({e.get('location', p['city'] + ', ' + p['state'])})", 11, "B")
        line(f"{e.get('start', '')} to {end}", 10, "I")
        line(e.get("summary", ""), 10, h=7)

    line("Education", 14, "B", 9)
    for ed in p.get("education", []):
        line(f"{ed['degree']}, {ed['major']} - {ed['university']}", 11, "B")
        line(f"GPA: {ed.get('gpa', '')}", 10, h=7)

    line("Skills", 14, "B", 9)
    line(", ".join(p.get("skills", [])), 10)

    out = OUT / f"{p['_name']}.pdf"
    pdf.output(str(out))
    return out


if __name__ == "__main__":
    for p in PROFILES:
        path = render(p)
        print("wrote", path.name, path.stat().st_size, "bytes")
