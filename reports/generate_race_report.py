#!/usr/bin/env python3.12
"""
generate-race-report.py
=======================
Generates a professional PDF race-analysis report from the VSS postgres DB.

Usage:
  python3.12 generate-race-report.py                            # report for all videos
  python3.12 generate-race-report.py womenswomens.mp4           # single video
  python3.12 generate-race-report.py womenswomens.mp4 --race 1  # single race/heat
  python3.12 generate-race-report.py --list                     # list available videos

Output: <video_stem>_race_report.pdf  (or  all_races_report.pdf)
"""

import subprocess, sys, re, json, textwrap
from datetime import datetime
from pathlib import Path

# ── ReportLab ─────────────────────────────────────────────────────────────────
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether, PageBreak
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.pdfgen import canvas as rl_canvas

# ── Config ────────────────────────────────────────────────────────────────────
PGPOD     = None   # auto-detected
PGPASS    = "1AJBaufOr6zvMV23WfFhg9xo"
PGUSER    = "mgOXx61u_user"
PGDB      = "vsYR_db"
REPORT_DATE = datetime.now().strftime("%d %B %Y")

# Colour palette (Oracle-inspired)
C_RED      = colors.HexColor("#C74634")
C_DARK     = colors.HexColor("#1B1B1B")
C_MID      = colors.HexColor("#3A3A3A")
C_ACCENT   = colors.HexColor("#E8F0F8")
C_GOLD     = colors.HexColor("#F5A623")
C_SILVER   = colors.HexColor("#A8A8A8")
C_BRONZE   = colors.HexColor("#CD7F32")
C_HEADER   = colors.HexColor("#1C3557")
C_ROW_ALT  = colors.HexColor("#F4F6F9")
C_WHITE    = colors.white

# ── Postgres helpers ──────────────────────────────────────────────────────────
def find_pg_pod():
    out = subprocess.check_output(
        ["kubectl", "get", "pods", "-l", "app=vss-postgres",
         "--no-headers", "-o", "custom-columns=NAME:.metadata.name"],
        stderr=subprocess.PIPE
    ).decode().strip().split("\n")
    pods = [p for p in out if p and "postgres" in p and "bp-" not in p]
    if not pods:
        raise RuntimeError("vss-postgres pod not found")
    return pods[0]


def pg_query(sql):
    global PGPOD
    if PGPOD is None:
        PGPOD = find_pg_pod()
    result = subprocess.run(
        ["kubectl", "exec", PGPOD, "-c", "vss-postgres", "--",
         "sh", "-c",
         f'PGPASSWORD={PGPASS} psql -U {PGUSER} -d {PGDB} -t -A -F"|" -c "{sql}"'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,   # capture but ignore OCI auth-plugin warnings
        text=True
    )
    output = result.stdout.strip()
    # Treat as error only if no usable output AND psql explicitly failed
    # (kubectl may exit non-zero solely due to OCI auth-plugin deprecation warnings)
    if not output:
        real_err = "\n".join(
            l for l in result.stderr.split("\n")
            if l.strip() and "CryptographyDeprecationWarning" not in l
               and "httpsig_cffi" not in l and "deprecated in cryptography" not in l
               and "Python 3.6" not in l and "from cryptography" not in l
        )
        if real_err:
            print(f"DB error: {real_err[:300]}", file=sys.stderr)
        return []
    lines = [l for l in output.split("\n") if l.strip()]
    return [line.split("|") for line in lines]


def list_videos():
    rows = pg_query(
        'SELECT v."objectKey", v."createdAt", s."resultLength" '
        'FROM "Video" v LEFT JOIN "Summary" s ON s."videoId"=v.id '
        'ORDER BY v."createdAt" DESC;'
    )
    return rows


def get_summary(object_key):
    rows = pg_query(
        f'SELECT s."resultText", s."createdAt", s."resultLength" '
        f'FROM "Summary" s JOIN "Video" v ON v.id=s."videoId" '
        f'WHERE v."objectKey"=\'{object_key}\';'
    )
    if not rows:
        return None, None, None
    result_text = rows[0][0]
    created_at  = rows[0][1] if len(rows[0]) > 1 else ""
    result_len  = rows[0][2] if len(rows[0]) > 2 else ""
    return result_text, created_at, result_len


def get_job_params(object_key):
    rows = pg_query(
        f'SELECT params FROM "SummarizationJob" '
        f'WHERE "objectKey"=\'{object_key}\' AND status=\'COMPLETED\' '
        f'ORDER BY "createdAt" DESC LIMIT 1;'
    )
    if rows and rows[0][0]:
        try:
            return json.loads(rows[0][0])
        except Exception:
            pass
    return {}

# ── Markdown parser ───────────────────────────────────────────────────────────
def parse_md_table(block):
    """Parse a markdown table block into list-of-dicts."""
    lines = [l.strip() for l in block.strip().split("\n") if l.strip()]
    # Find header row (contains |)
    table_lines = [l for l in lines if "|" in l]
    if len(table_lines) < 2:
        return [], []
    header = [c.strip() for c in table_lines[0].split("|") if c.strip()]
    rows = []
    for line in table_lines[2:]:   # skip separator line
        cols = [c.strip() for c in line.split("|")]
        # remove empty first/last from leading/trailing |
        if cols and not cols[0]:
            cols = cols[1:]
        if cols and not cols[-1]:
            cols = cols[:-1]
        if cols:
            rows.append(cols)
    return header, rows


def extract_races(text):
    """
    Extracts a list of race dicts from the LLM summary text.
    Each race has: t0, headers, rows, timeline
    """
    races = []

    # Split on race section markers
    race_blocks = re.split(
        r"(?:^|\n)(?:#{1,4}\s*)?(?:Race\s+(?:at\s+)?(?:T0\s*[=:]\s*)?(\d+\.?\d*)\s*s|\*\*Race\s+at\s+T0\s*[=:]\s*(\d+\.?\d*)s\*\*)",
        text, flags=re.IGNORECASE | re.MULTILINE
    )

    # Alternative: find all "RACE RESULTS" table blocks
    result_blocks = re.findall(
        r"(?:RACE RESULTS[:\s]*\n|#{1,4}\s*(?:RACE RESULTS|Final Output)[^\n]*\n)(.*?)(?=\n#{1,4}|\Z)",
        text, flags=re.DOTALL | re.IGNORECASE
    )

    # Try to find numbered races like "1. **Race at T0 = 1.15s**"
    numbered = re.findall(
        r"\d+\.\s*\*\*Race\s+at\s+T0\s*[=:]\s*(\d+\.?\d*)s\*\*(.*?)(?=\n\d+\.\s*\*\*Race|\Z)",
        text, flags=re.DOTALL
    )

    for t0_str, block in numbered:
        t0 = float(t0_str)
        # find table in block
        table_match = re.search(r"(\|[^\n]+\|\n(?:\|[-| :]+\|\n)(?:\|[^\n]+\|\n?)*)", block)
        headers, rows = [], []
        if table_match:
            headers, rows = parse_md_table(table_match.group(1))
        # find timeline
        timeline = []
        for m in re.finditer(r"-\s*([\d.]+s):?([\d.]+s)?\s*\|\s*([A-Z_]+)\s*\|\s*(.+)", block):
            timeline.append({
                "time": m.group(1),
                "event": m.group(3),
                "detail": m.group(4).strip()
            })
        races.append({"t0": t0, "headers": headers, "rows": rows, "timeline": timeline})

    # Fallback: if no numbered races found, try to get first RACE RESULTS table
    if not races:
        table_match = re.search(
            r"RACE RESULTS[^\n]*\n(\|[^\n]+\|\n(?:\|[-| :]+\|\n)(?:\|[^\n]+\|\n?)*)",
            text, flags=re.IGNORECASE
        )
        if table_match:
            headers, rows = parse_md_table(table_match.group(1))
            if rows:
                races.append({"t0": None, "headers": headers, "rows": rows, "timeline": []})

    return races


def extract_heat_races(text):
    """
    For 600m-style heat videos, extract heats with their race numbers.
    Falls back to extract_races if no HEAT pattern found.
    """
    # Look for "Race X" style in the heat analysis
    heat_pattern = re.findall(
        r"(?:Race|Heat)\s+(\d+)[^\n]*\n(.*?)(?=(?:Race|Heat)\s+\d+|\Z)",
        text, flags=re.DOTALL | re.IGNORECASE
    )
    if heat_pattern:
        results = []
        for race_num, block in heat_pattern:
            table_match = re.search(r"(\|[^\n]+\|\n(?:\|[-| :]+\|\n)(?:\|[^\n]+\|\n?)*)", block)
            headers, rows = [], []
            if table_match:
                headers, rows = parse_md_table(table_match.group(1))
            if rows:
                results.append({"race_num": int(race_num), "headers": headers, "rows": rows, "t0": None, "timeline": []})
        if results:
            return results
    return extract_races(text)

# ── PDF Styles ────────────────────────────────────────────────────────────────
def make_styles():
    base = getSampleStyleSheet()
    styles = {}

    styles["cover_title"] = ParagraphStyle(
        "cover_title", fontSize=32, fontName="Helvetica-Bold",
        textColor=C_WHITE, alignment=TA_CENTER, spaceAfter=8, leading=38
    )
    styles["cover_sub"] = ParagraphStyle(
        "cover_sub", fontSize=14, fontName="Helvetica",
        textColor=colors.HexColor("#CCDDEE"), alignment=TA_CENTER, leading=20
    )
    styles["cover_meta"] = ParagraphStyle(
        "cover_meta", fontSize=11, fontName="Helvetica",
        textColor=colors.HexColor("#AABBCC"), alignment=TA_CENTER, leading=16
    )
    styles["section"] = ParagraphStyle(
        "section", fontSize=14, fontName="Helvetica-Bold",
        textColor=C_HEADER, spaceAfter=6, spaceBefore=14, leading=18
    )
    styles["subsection"] = ParagraphStyle(
        "subsection", fontSize=11, fontName="Helvetica-Bold",
        textColor=C_MID, spaceAfter=4, spaceBefore=8, leading=14
    )
    styles["body"] = ParagraphStyle(
        "body", fontSize=9, fontName="Helvetica",
        textColor=C_DARK, leading=13, spaceAfter=4
    )
    styles["caption"] = ParagraphStyle(
        "caption", fontSize=8, fontName="Helvetica-Oblique",
        textColor=C_SILVER, alignment=TA_CENTER, leading=11
    )
    styles["medal"] = ParagraphStyle(
        "medal", fontSize=9, fontName="Helvetica-Bold",
        textColor=C_DARK, alignment=TA_CENTER, leading=12
    )
    return styles

# ── Medal colour helper ────────────────────────────────────────────────────────
def pos_bg(pos_str):
    try:
        pos = int(re.sub(r"\D", "", pos_str))
    except Exception:
        return C_WHITE
    if pos == 1:  return colors.HexColor("#FFF8E1")   # gold tint
    if pos == 2:  return colors.HexColor("#F5F5F5")   # silver tint
    if pos == 3:  return colors.HexColor("#FBF0E6")   # bronze tint
    return C_WHITE if pos % 2 == 0 else C_ROW_ALT

def medal_str(pos_str):
    try:
        pos = int(re.sub(r"\D", "", pos_str))
    except Exception:
        return pos_str
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    return medals.get(pos, str(pos))

# ── Cover page canvas callback ────────────────────────────────────────────────
def make_cover_canvas(title_lines, subtitle, meta_lines):
    def draw(canvas, doc):
        w, h = A4
        # Dark gradient background
        canvas.setFillColor(C_HEADER)
        canvas.rect(0, 0, w, h, fill=1, stroke=0)
        # Accent bar top
        canvas.setFillColor(C_RED)
        canvas.rect(0, h - 8*mm, w, 8*mm, fill=1, stroke=0)
        # Accent bar bottom
        canvas.rect(0, 0, w, 6*mm, fill=1, stroke=0)
        # White stripe accent
        canvas.setFillColor(colors.HexColor("#FFFFFF22"))
        canvas.rect(0, h*0.38, w, 2*mm, fill=1, stroke=0)
        canvas.rect(0, h*0.35, w, 0.5*mm, fill=1, stroke=0)
        # Title lines
        canvas.setFillColor(C_WHITE)
        canvas.setFont("Helvetica-Bold", 36)
        y = h * 0.62
        for line in title_lines:
            canvas.drawCentredString(w/2, y, line)
            y -= 44
        # Subtitle
        canvas.setFont("Helvetica", 16)
        canvas.setFillColor(colors.HexColor("#AACCEE"))
        canvas.drawCentredString(w/2, h*0.44, subtitle)
        # Meta lines
        canvas.setFont("Helvetica", 11)
        canvas.setFillColor(colors.HexColor("#88AACC"))
        y = h*0.30
        for line in meta_lines:
            canvas.drawCentredString(w/2, y, line)
            y -= 16
        # Oracle branding watermark
        canvas.setFont("Helvetica-Bold", 9)
        canvas.setFillColor(colors.HexColor("#FFFFFF44"))
        canvas.drawString(2*cm, 1.2*cm, "Generated by Oracle VSS · AI-powered Video Analysis")
    return draw


# ── Page header/footer callback ────────────────────────────────────────────────
class PageDecorator:
    def __init__(self, title, video_name):
        self.title = title
        self.video_name = video_name

    def __call__(self, canvas, doc):
        w, h = A4
        # Header bar
        canvas.setFillColor(C_HEADER)
        canvas.rect(0, h - 14*mm, w, 14*mm, fill=1, stroke=0)
        canvas.setFillColor(C_RED)
        canvas.rect(0, h - 16*mm, w, 2*mm, fill=1, stroke=0)
        canvas.setFillColor(C_WHITE)
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(1.5*cm, h - 9*mm, self.title)
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(w - 1.5*cm, h - 9*mm, f"Video: {self.video_name}")
        # Footer
        canvas.setFillColor(C_HEADER)
        canvas.rect(0, 0, w, 10*mm, fill=1, stroke=0)
        canvas.setFillColor(C_RED)
        canvas.rect(0, 10*mm, w, 1.5*mm, fill=1, stroke=0)
        canvas.setFillColor(C_WHITE)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(1.5*cm, 3.5*mm, f"Oracle VSS Race Analysis  ·  {REPORT_DATE}")
        canvas.drawRightString(w - 1.5*cm, 3.5*mm, f"Page {doc.page}")


# ── Build results table flowable ──────────────────────────────────────────────
def build_results_table(headers, rows, styles):
    if not headers or not rows:
        return Paragraph("No results data available.", styles["body"])

    col_widths = []
    page_w = A4[0] - 3*cm  # usable width
    # Heuristic column widths
    for h in headers:
        hl = h.lower()
        if "pos" in hl:             col_widths.append(1.2*cm)
        elif "lane" in hl:          col_widths.append(1.2*cm)
        elif "athlete" in hl:       col_widths.append(5.5*cm)
        elif "time" in hl:          col_widths.append(2.2*cm)
        elif "video" in hl:         col_widths.append(2.0*cm)
        elif "screen" in hl:        col_widths.append(2.2*cm)
        elif "country" in hl:       col_widths.append(2.5*cm)
        else:                       col_widths.append(2.5*cm)

    # Normalise to page width
    total = sum(col_widths)
    if total > page_w:
        factor = page_w / total
        col_widths = [w * factor for w in col_widths]

    # Header row
    hdr_cells = [Paragraph(f"<b>{h}</b>", ParagraphStyle(
        "th", fontSize=8, fontName="Helvetica-Bold",
        textColor=C_WHITE, alignment=TA_CENTER, leading=11
    )) for h in headers]

    table_data = [hdr_cells]
    for row in rows:
        # Pad or trim row to header length
        padded = (row + [""]*(len(headers)-len(row)))[:len(headers)]
        pos_cell = padded[0] if padded else ""
        bg = pos_bg(pos_cell)
        cells = []
        for i, cell in enumerate(padded):
            ha = TA_CENTER if i == 0 or "lane" in headers[i].lower() else TA_LEFT
            st = ParagraphStyle(
                f"td{i}", fontSize=8, fontName="Helvetica",
                textColor=C_DARK, alignment=ha, leading=11
            )
            # Inject medal for position column
            if i == 0 and pos_cell:
                cell = medal_str(pos_cell)
            cells.append(Paragraph(str(cell), st))
        table_data.append(cells)

    # TableStyle
    n_rows = len(table_data)
    ts = TableStyle([
        # Header
        ("BACKGROUND",   (0,0), (-1,0),  C_HEADER),
        ("ROWBACKGROUNDS",(0,1),(-1,-1), [C_WHITE, C_ROW_ALT]),
        ("TOPPADDING",   (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0), (-1,-1), 4),
        ("LEFTPADDING",  (0,0), (-1,-1), 5),
        ("RIGHTPADDING", (0,0), (-1,-1), 5),
        ("GRID",         (0,0), (-1,-1), 0.3, colors.HexColor("#DDDDDD")),
        ("LINEBELOW",    (0,0), (-1,0),  1.5, C_RED),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
    ])
    # Medal row highlights
    for r_idx, row in enumerate(rows, start=1):
        bg = pos_bg(row[0] if row else "")
        if bg != C_WHITE:
            ts.add("BACKGROUND", (0, r_idx), (-1, r_idx), bg)

    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(ts)
    return t


# ── Build timeline table flowable ─────────────────────────────────────────────
def build_timeline_table(timeline, styles):
    if not timeline:
        return None
    data = [[
        Paragraph("<b>Time</b>", ParagraphStyle("th2", fontSize=7, fontName="Helvetica-Bold", textColor=C_WHITE, alignment=TA_CENTER)),
        Paragraph("<b>Event</b>", ParagraphStyle("th2", fontSize=7, fontName="Helvetica-Bold", textColor=C_WHITE, alignment=TA_CENTER)),
        Paragraph("<b>Detail</b>", ParagraphStyle("th2", fontSize=7, fontName="Helvetica-Bold", textColor=C_WHITE)),
    ]]
    for i, ev in enumerate(timeline):
        bg = C_WHITE if i % 2 == 0 else C_ROW_ALT
        row_style = ParagraphStyle(f"tl{i}", fontSize=7, fontName="Helvetica", textColor=C_DARK)
        data.append([
            Paragraph(ev["time"], row_style),
            Paragraph(ev["event"].replace("_", " "), row_style),
            Paragraph(ev["detail"][:120], row_style),
        ])
    t = Table(data, colWidths=[1.8*cm, 3.5*cm, 11.5*cm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0),  C_HEADER),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),  [C_WHITE, C_ROW_ALT]),
        ("GRID",          (0,0), (-1,-1), 0.3, colors.HexColor("#DDDDDD")),
        ("LINEBELOW",     (0,0), (-1,0),  1.0, C_RED),
        ("TOPPADDING",    (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ("LEFTPADDING",   (0,0), (-1,-1), 4),
        ("RIGHTPADDING",  (0,0), (-1,-1), 4),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ]))
    return t


# ── Main report builder ───────────────────────────────────────────────────────
def build_report(video_keys, race_filter=None, out_path=None):
    styles = make_styles()

    if out_path is None:
        stem = Path(video_keys[0]).stem if len(video_keys) == 1 else "all_races"
        if race_filter is not None:
            stem += f"_race{race_filter}"
        out_path = f"/home/opc/code/dp/vss/{stem}_race_report.pdf"

    # Two pass: cover knows totals after we fetch
    all_data = []
    for vk in video_keys:
        print(f"  Fetching: {vk} ...", end=" ", flush=True)
        text, created_at, result_len = get_summary(vk)
        if not text:
            print("⚠ no summary found")
            continue
        params = get_job_params(vk)
        races = extract_heat_races(text)
        print(f"{len(races)} race(s) found")
        all_data.append({
            "video": vk,
            "text": text,
            "created_at": created_at,
            "params": params,
            "races": races,
        })

    if not all_data:
        print("ERROR: no data to report on.")
        return

    # ── Title info ────────────────────────────────────────────────────────────
    if len(video_keys) == 1:
        stem = Path(video_keys[0]).stem
        event_name = stem.replace("-", " ").replace("_", " ").title()
        if race_filter is not None:
            event_name += f" — Race {race_filter}"
        doc_title = event_name
    else:
        doc_title = "Multi-Event Race Report"
        event_name = "Multi-Event Race Report"

    total_races = sum(len(d["races"]) for d in all_data)

    # ── Document setup ────────────────────────────────────────────────────────
    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=1.5*cm, rightMargin=1.5*cm,
        topMargin=2.0*cm, bottomMargin=1.8*cm,
        title=doc_title,
        author="Oracle VSS · AI Video Analysis",
    )

    story = []

    # ── Cover page ─────────────────────────────────────────────────────────────
    cover_fn = make_cover_canvas(
        title_lines=[event_name] if len(event_name) < 32
            else [event_name[:event_name.rfind(" ", 0, 32)], event_name[event_name.rfind(" ", 0, 32):].strip()],
        subtitle="AI-Powered Race Analysis",
        meta_lines=[
            f"Report Date: {REPORT_DATE}",
            f"Videos analysed: {len(all_data)}  ·  Races detected: {total_races}",
            f"Powered by Oracle VSS  ·  Meta Llama-4 Maverick",
        ]
    )
    story.append(PageBreak())   # triggers cover draw on page 1 via onFirstPage

    # Use a special first-page template via onFirstPage / onLaterPages
    # We'll inject the cover differently — build it manually via a Spacer + canvas hack
    # Instead: write cover as a "canvas-only" page via a custom Flowable
    class CoverPage(rl_canvas.Canvas.__class__):
        pass

    # Simple approach: draw cover via onFirstPage, content on later pages
    decorator = PageDecorator(doc_title, ", ".join(video_keys))

    story_content = []

    for data in all_data:
        vk    = data["video"]
        races = data["races"]
        params= data["params"]
        text  = data["text"]

        video_label = Path(vk).stem.replace("-", " ").replace("_", " ").title()

        # Video section header
        story_content.append(Spacer(1, 0.3*cm))
        story_content.append(HRFlowable(width="100%", thickness=2, color=C_RED, spaceAfter=4))
        story_content.append(Paragraph(f"📽  {video_label}", styles["section"]))

        if params:
            model = params.get("model", "unknown")
            chunk = params.get("chunk_duration", "?")
            frames= params.get("num_frames_per_chunk", "?")
            story_content.append(Paragraph(
                f"<i>Model: {model}  ·  Chunk: {chunk}s  ·  Frames/chunk: {frames}</i>",
                styles["caption"]
            ))

        story_content.append(Spacer(1, 0.2*cm))

        display_races = races
        if race_filter is not None:
            display_races = [r for r in races if r.get("race_num") == race_filter
                             or r.get("t0") == race_filter]

        if not display_races:
            story_content.append(Paragraph(
                f"No races matching filter in this video.", styles["body"]
            ))
            continue

        for idx, race in enumerate(display_races):
            race_num  = race.get("race_num", idx + 1)
            t0        = race.get("t0")
            headers   = race.get("headers", [])
            rows      = race.get("rows", [])
            timeline  = race.get("timeline", [])

            if t0 is not None:
                race_label = f"Race {race_num}  (T₀ = {t0}s)"
            else:
                race_label = f"Race {race_num}"

            winner = ""
            if rows:
                first_row = rows[0]
                if len(first_row) >= 3:
                    winner = first_row[2]

            block = []
            block.append(Paragraph(f"◈  {race_label}", styles["subsection"]))
            if winner:
                block.append(Paragraph(f"<b>🏆 Winner:</b> {winner}", styles["body"]))

            results_tbl = build_results_table(headers, rows, styles)
            block.append(results_tbl)
            block.append(Spacer(1, 0.15*cm))

            if timeline:
                block.append(Paragraph("Race Timeline", ParagraphStyle(
                    "tl_hdr", fontSize=9, fontName="Helvetica-Bold",
                    textColor=C_HEADER, spaceBefore=6, spaceAfter=3
                )))
                tl_tbl = build_timeline_table(timeline, styles)
                if tl_tbl:
                    block.append(tl_tbl)
                block.append(Spacer(1, 0.15*cm))

            story_content.append(KeepTogether(block))

        # --- add full text appendix (collapsed) ---
        story_content.append(Spacer(1, 0.4*cm))
        story_content.append(HRFlowable(width="100%", thickness=0.5, color=C_SILVER))
        story_content.append(Paragraph("Full AI Analysis Transcript", ParagraphStyle(
            "app_hdr", fontSize=9, fontName="Helvetica-Bold",
            textColor=C_MID, spaceBefore=6, spaceAfter=2
        )))
        # Wrap long text into Paragraphs
        for line in text.strip().split("\n")[:80]:   # cap at 80 lines
            clean = line.strip()
            if not clean:
                story_content.append(Spacer(1, 2))
                continue
            if clean.startswith("###"):
                story_content.append(Paragraph(
                    clean.lstrip("#").strip(),
                    ParagraphStyle("app_h3", fontSize=8, fontName="Helvetica-Bold",
                                   textColor=C_HEADER, spaceBefore=5, spaceAfter=2)
                ))
            elif "|" in clean and "---" not in clean:
                # table line — skip (already shown above)
                pass
            else:
                story_content.append(Paragraph(
                    clean.replace("**", ""),
                    ParagraphStyle("app_body", fontSize=7, fontName="Helvetica",
                                   textColor=C_MID, leading=10)
                ))

        story_content.append(PageBreak())

    # Remove trailing PageBreak
    if story_content and isinstance(story_content[-1], PageBreak):
        story_content.pop()

    # ── Build PDF ─────────────────────────────────────────────────────────────
    # Cover: blank page with canvas-only art
    # We use onFirstPage for cover, onLaterPages for content

    def cover_page(canvas, doc):
        cover_fn(canvas, doc)

    def content_page(canvas, doc):
        decorator(canvas, doc)

    # Insert blank story element to force page 1 = cover
    doc.build(
        [Spacer(1, A4[1])] + story_content,
        onFirstPage=cover_page,
        onLaterPages=content_page,
    )

    print(f"\n✓ Report written: {out_path}")
    return out_path


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]

    if "--list" in args:
        print("Available videos in VSS DB:")
        rows = list_videos()
        for r in rows:
            key = r[0] if r else "?"
            ts  = r[1] if len(r) > 1 else ""
            length = r[2] if len(r) > 2 else ""
            has_summary = "✔" if (length and length.strip() not in ("", "0")) else "✗"
            print(f"  [{has_summary}] {key:<40}  created: {ts}")
        return

    race_filter = None
    if "--race" in args:
        idx = args.index("--race")
        race_filter = int(args[idx + 1])
        args = [a for a in args if a != "--race" and a != str(race_filter)]

    out_path = None
    if "--out" in args:
        idx = args.index("--out")
        out_path = args[idx + 1]
        args = [a for a in args if a != "--out" and a != out_path]

    # Remaining args are video names
    video_keys = [a for a in args if not a.startswith("-")]

    if not video_keys:
        # Default: all videos with summaries
        rows = list_videos()
        video_keys = [r[0] for r in rows if len(r) > 2 and r[2] and r[2].strip() not in ("", "0")]
        if not video_keys:
            print("No completed summaries found. Run a job first.")
            sys.exit(1)
        print(f"No video specified — reporting on all {len(video_keys)} video(s):")
        for v in video_keys:
            print(f"  {v}")

    print(f"\nGenerating report...")
    build_report(video_keys, race_filter=race_filter, out_path=out_path)


if __name__ == "__main__":
    main()
