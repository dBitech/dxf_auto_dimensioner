#!/usr/bin/env python3
"""
DXF Auto Dimensioner / 1:1 PDF Exporter
Y14.5-oriented batch utility with recursive folder support and auto-scaling title block.

This utility is intentionally conservative: it adds overall width/height dimensions,
radius notes for circles/arcs, angle notes for non-orthogonal line segments, and a
clean engineering title block. It also exports a vector 1:1 PDF using ReportLab.

Dependencies:
    pip install ezdxf reportlab
"""
from __future__ import annotations

import argparse
import datetime as _dt
import math
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import ezdxf
from ezdxf.document import Drawing
from ezdxf.entities import DXFEntity
from ezdxf.math import Vec2, Vec3
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.pagesizes import landscape, portrait

MM_PER_INCH = 25.4
PT_PER_INCH = 72.0
PT_PER_MM = PT_PER_INCH / MM_PER_INCH

ANNOT_LAYER = "AUTO_DIMENSIONS"
TITLE_LAYER = "AUTO_TITLE_BLOCK"
PART_LAYER_FALLBACK = "0"

ANSI_SHEETS_IN = {
    "A": (11.0, 8.5),
    "B": (17.0, 11.0),
    "C": (22.0, 17.0),
    "D": (34.0, 22.0),
    "E": (44.0, 34.0),
}

INSUNITS_TO_MM = {
    0: None,       # unitless
    1: 25.4,       # inches
    2: 304.8,      # feet
    3: 1609344.0,  # miles
    4: 1.0,        # millimeters
    5: 10.0,       # centimeters
    6: 1000.0,     # meters
    7: 1_000_000.0,
    8: 0.0000254,  # microinches
    9: 0.0254,     # mils
    10: 914.4,     # yards
}

@dataclass
class BBox:
    minx: float
    miny: float
    maxx: float
    maxy: float

    @property
    def width(self) -> float:
        return self.maxx - self.minx

    @property
    def height(self) -> float:
        return self.maxy - self.miny

    @property
    def cx(self) -> float:
        return (self.minx + self.maxx) / 2

    @property
    def cy(self) -> float:
        return (self.miny + self.maxy) / 2

    def expand(self, amount: float) -> "BBox":
        return BBox(self.minx - amount, self.miny - amount, self.maxx + amount, self.maxy + amount)

@dataclass
class Sheet:
    name: str
    width_mm: float
    height_mm: float
    landscape: bool
    is_custom: bool = False

@dataclass
class RenderTransform:
    scale_pt_per_mm: float
    origin_x_pt: float
    origin_y_pt: float
    source_minx_mm: float
    source_miny_mm: float

    def p(self, x_mm: float, y_mm: float) -> Tuple[float, float]:
        return (
            self.origin_x_pt + (x_mm - self.source_minx_mm) * self.scale_pt_per_mm,
            self.origin_y_pt + (y_mm - self.source_miny_mm) * self.scale_pt_per_mm,
        )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Batch annotate DXF files and export 1:1 Y14.5-style PDFs.")
    p.add_argument("-i", "--input", required=True, help="Input DXF file or folder.")
    p.add_argument("-o", "--output", required=True, help="Output folder.")
    p.add_argument("--recursive", action="store_true", help="Process nested subdirectories.")
    p.add_argument("--source-units", choices=["metric", "imperial", "auto"], default="metric",
                   help="Units used by source DXF geometry. metric assumes mm; imperial assumes inches; auto uses $INSUNITS when available.")
    p.add_argument("--output-units", choices=["source", "metric", "imperial"], default="source",
                   help="Units displayed in dimension text. Default follows source units.")
    p.add_argument("--fractional-inches", action="store_true", help="Format imperial lengths as fractions to nearest 1/32 inch.")
    p.add_argument("--sheet", choices=list(ANSI_SHEETS_IN.keys()) + ["auto"], default="auto",
                   help="ANSI sheet size. Auto chooses the smallest sheet that fits at 1:1.")
    p.add_argument("--orientation", choices=["auto", "portrait", "landscape"], default="auto")
    p.add_argument("--title-block-height-ratio", type=float, default=0.22,
                   help="Title block height as fraction of sheet height. Default 0.22.")
    p.add_argument("--standard-sheets-only", action="store_true",
                   help="Only use ANSI A-E sheets. By default, oversized custom PDF pages are allowed to preserve 1:1 output without clipping.")
    p.add_argument("--title-font-pt", type=float, default=12.0,
                   help="Preferred title-block font size in points. Default 12.")
    p.add_argument("--min-title-font-pt", type=float, default=8.0,
                   help="Minimum fitted title-block font size. Default 8.")
    p.add_argument("--drawn-by", default="AUTO", help="Title block drawn-by field.")
    p.add_argument("--revision", default="A", help="Title block revision.")
    p.add_argument("--description", default="AUTO-DIMENSIONED PART", help="Title block description.")
    p.add_argument("--material", default="N/A")
    p.add_argument("--finish", default="N/A")
    p.add_argument("--angle-notes", action="store_true",
                   help="Add angle notes for original non-orthogonal straight edges. Disabled by default to avoid clutter.")
    p.add_argument("--no-pdf", action="store_true", help="Do not generate PDFs.")
    p.add_argument("--no-annotated-dxf", action="store_true", help="Do not write annotated DXFs.")
    return p.parse_args()


def source_units_and_scale(doc: Drawing, requested: str) -> Tuple[str, float]:
    if requested == "metric":
        return "metric", 1.0
    if requested == "imperial":
        return "imperial", MM_PER_INCH
    ins = int(doc.header.get("$INSUNITS", 0) or 0)
    mm = INSUNITS_TO_MM.get(ins)
    if mm is None:
        return "metric", 1.0
    return ("imperial" if ins in {1, 2, 8, 9, 10} else "metric"), mm


def get_entities(msp) -> List[DXFEntity]:
    return [e for e in msp if e.dxftype() not in {"DIMENSION", "TEXT", "MTEXT"}]


def entity_points(e: DXFEntity) -> List[Tuple[float, float]]:
    t = e.dxftype()
    pts: List[Tuple[float, float]] = []
    try:
        if t == "LINE":
            pts = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
        elif t in {"LWPOLYLINE", "POLYLINE"}:
            if t == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in e.get_points()]
            else:
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
        elif t == "CIRCLE":
            c, r = e.dxf.center, e.dxf.radius
            pts = [(c.x-r, c.y-r), (c.x+r, c.y+r)]
        elif t == "ARC":
            c, r = e.dxf.center, e.dxf.radius
            # conservative: full circle bbox for arcs
            pts = [(c.x-r, c.y-r), (c.x+r, c.y+r)]
        elif t == "ELLIPSE":
            c = e.dxf.center
            mx = e.dxf.major_axis.magnitude
            my = mx * float(e.dxf.ratio)
            pts = [(c.x-mx, c.y-my), (c.x+mx, c.y+my)]
        elif t == "SPLINE":
            pts = [(p.x, p.y) for p in e.flattening(0.5)]
    except Exception:
        return []
    return pts


def compute_bbox(entities: Iterable[DXFEntity], scale_to_mm: float) -> Optional[BBox]:
    xs: List[float] = []
    ys: List[float] = []
    for e in entities:
        for x, y in entity_points(e):
            xs.append(x * scale_to_mm)
            ys.append(y * scale_to_mm)
    if not xs:
        return None
    return BBox(min(xs), min(ys), max(xs), max(ys))


def fmt_inches(value_in: float, fractional: bool) -> str:
    if not fractional:
        return f'{value_in:.3f}"'.rstrip("0").rstrip(".") + '"' if "." in f"{value_in:.3f}" else f'{value_in:.0f}"'
    whole = int(math.floor(abs(value_in)))
    frac = Fraction(abs(value_in) - whole).limit_denominator(32)
    if frac.numerator == frac.denominator:
        whole += 1
        frac = Fraction(0, 1)
    sign = "-" if value_in < 0 else ""
    if frac.numerator == 0:
        return f'{sign}{whole}"'
    if whole == 0:
        return f'{sign}{frac.numerator}/{frac.denominator}"'
    return f'{sign}{whole}-{frac.numerator}/{frac.denominator}"'


def fmt_len(mm: float, units: str, fractional: bool) -> str:
    if units == "imperial":
        return fmt_inches(mm / MM_PER_INCH, fractional)
    if abs(mm) >= 1000:
        return f"{mm:.1f} mm"
    return f"{mm:.2f} mm".rstrip("0").rstrip(".") + " mm" if "." in f"{mm:.2f}" else f"{mm:.0f} mm"


def fmt_radius(mm: float, units: str, fractional: bool) -> str:
    return "R" + fmt_len(mm, units, fractional).replace(" ", "")


def ensure_layers(doc: Drawing) -> None:
    for name, color in [(ANNOT_LAYER, 1), (TITLE_LAYER, 7)]:
        if name not in doc.layers:
            doc.layers.add(name, color=color)


def add_line(msp, p1, p2, layer=ANNOT_LAYER):
    msp.add_line(p1, p2, dxfattribs={"layer": layer})


def add_text(msp, text: str, pos, height: float, layer=ANNOT_LAYER, rotation=0, align="MIDDLE_CENTER"):
    ent = msp.add_text(text, dxfattribs={"layer": layer, "height": height, "rotation": rotation})
    try:
        ent.set_placement(pos, align=getattr(ezdxf.enums.TextEntityAlignment, align))
    except Exception:
        ent.dxf.insert = pos
    return ent


def add_overall_dimensions(doc: Drawing, bbox_src: BBox, scale_to_mm: float, out_units: str, fractional: bool) -> None:
    msp = doc.modelspace()
    # Coordinates remain in source units for annotated DXF; text is converted from mm.
    margin_mm = max(bbox_src.width * scale_to_mm, bbox_src.height * scale_to_mm) * 0.06 + 8
    off = margin_mm / scale_to_mm
    th = max(2.5 / scale_to_mm, min(bbox_src.width, bbox_src.height) * 0.02)
    arrow = max(1.5 / scale_to_mm, th * 0.8)
    minx, miny, maxx, maxy = bbox_src.minx, bbox_src.miny, bbox_src.maxx, bbox_src.maxy
    # bottom width dimension
    y = miny - off
    add_line(msp, (minx, miny), (minx, y))
    add_line(msp, (maxx, miny), (maxx, y))
    add_line(msp, (minx, y), (maxx, y))
    add_line(msp, (minx, y), (minx + arrow, y + arrow * 0.45))
    add_line(msp, (minx, y), (minx + arrow, y - arrow * 0.45))
    add_line(msp, (maxx, y), (maxx - arrow, y + arrow * 0.45))
    add_line(msp, (maxx, y), (maxx - arrow, y - arrow * 0.45))
    add_text(msp, fmt_len((maxx-minx)*scale_to_mm, out_units, fractional), ((minx+maxx)/2, y + th*0.65), th)
    # right height dimension
    x = maxx + off
    add_line(msp, (maxx, miny), (x, miny))
    add_line(msp, (maxx, maxy), (x, maxy))
    add_line(msp, (x, miny), (x, maxy))
    add_line(msp, (x, miny), (x + arrow*0.45, miny + arrow))
    add_line(msp, (x, miny), (x - arrow*0.45, miny + arrow))
    add_line(msp, (x, maxy), (x + arrow*0.45, maxy - arrow))
    add_line(msp, (x, maxy), (x - arrow*0.45, maxy - arrow))
    add_text(msp, fmt_len((maxy-miny)*scale_to_mm, out_units, fractional), (x + th*0.75, (miny+maxy)/2), th, rotation=90)


def _entity_layer(e: DXFEntity) -> str:
    try:
        return str(e.dxf.layer)
    except Exception:
        return ""


def _poly_points(e: DXFEntity) -> List[Tuple[float, float]]:
    if e.dxftype() == "LWPOLYLINE":
        return [(p[0], p[1]) for p in e.get_points()]
    if e.dxftype() == "POLYLINE":
        return [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
    return []


def _is_closed_poly(e: DXFEntity) -> bool:
    try:
        if e.dxftype() == "LWPOLYLINE":
            return bool(e.closed)
        if e.dxftype() == "POLYLINE":
            return bool(getattr(e, "is_closed", False))
    except Exception:
        return False
    return False


def approximate_circle_from_polyline(e: DXFEntity) -> Optional[Tuple[float, float, float]]:
    """Detect faceted circular holes imported as many straight polyline segments."""
    pts = _poly_points(e)
    if not _is_closed_poly(e) or len(pts) < 10:
        return None
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    rs = [math.hypot(x - cx, y - cy) for x, y in pts]
    r = sum(rs) / len(rs)
    if r <= 0:
        return None
    max_dev = max(abs(v - r) for v in rs)
    if max_dev / r <= 0.06:
        return cx, cy, r
    return None


def add_feature_notes(doc: Drawing, entities: Iterable[DXFEntity], scale_to_mm: float,
                      out_units: str, fractional: bool, angle_notes: bool = False) -> None:
    """Add clean feature-level notes from original geometry only.

    Default behaviour:
      * circles -> one diameter note
      * arcs -> one radius note
      * faceted circular polylines -> one diameter note
      * line-angle notes are opt-in only (--angle-notes)
    """
    msp = doc.modelspace()
    noted_centres: List[Tuple[float, float, float]] = []

    def already_noted(cx: float, cy: float, r: float) -> bool:
        for x, y, rr in noted_centres:
            if math.hypot(cx - x, cy - y) <= max(r, rr) * 0.08 and abs(r - rr) <= max(r, rr) * 0.08:
                return True
        return False

    for e in list(entities):
        try:
            if _entity_layer(e) in {ANNOT_LAYER, TITLE_LAYER}:
                continue
            typ = e.dxftype()
            if typ == "CIRCLE":
                c = e.dxf.center
                r = float(e.dxf.radius)
                if already_noted(c.x, c.y, r):
                    continue
                add_text(msp, f"Ø{fmt_len(2*r*scale_to_mm, out_units, fractional)}",
                         (c.x, c.y), max(r*0.25, 1.8/scale_to_mm))
                noted_centres.append((c.x, c.y, r))
            elif typ == "ARC":
                c = e.dxf.center
                r = float(e.dxf.radius)
                start_ang = float(e.dxf.start_angle)
                end_ang = float(e.dxf.end_angle)
                if end_ang < start_ang:
                    end_ang += 360.0
                a = math.radians((start_ang + end_ang) / 2.0)
                pos = (c.x + math.cos(a)*r*1.25, c.y + math.sin(a)*r*1.25)
                add_text(msp, fmt_radius(r*scale_to_mm, out_units, fractional),
                         pos, max(r*0.18, 1.8/scale_to_mm))
            elif typ in {"LWPOLYLINE", "POLYLINE"}:
                circ = approximate_circle_from_polyline(e)
                if circ is not None:
                    cx, cy, r = circ
                    if already_noted(cx, cy, r):
                        continue
                    add_text(msp, f"Ø{fmt_len(2*r*scale_to_mm, out_units, fractional)}",
                             (cx, cy), max(r*0.25, 1.8/scale_to_mm))
                    noted_centres.append((cx, cy, r))
            elif angle_notes and typ == "LINE":
                dx = e.dxf.end.x - e.dxf.start.x
                dy = e.dxf.end.y - e.dxf.start.y
                length = math.hypot(dx, dy)
                if length < 4.0 / scale_to_mm:
                    continue
                ang = math.degrees(math.atan2(dy, dx)) % 180
                if 2 < ang < 88 or 92 < ang < 178:
                    mid = ((e.dxf.start.x + e.dxf.end.x)/2, (e.dxf.start.y + e.dxf.end.y)/2)
                    add_text(msp, f"{ang:.1f}°", mid, 2.0/scale_to_mm)
        except Exception:
            continue

def choose_sheet(content_mm: BBox, args: argparse.Namespace) -> Sheet:
    """Choose a sheet that preserves 1:1 output.

    Standard ANSI A-E sheets are tried first. If the drawing plus its automatic
    dimensions will not fit at 1:1, the default behaviour is to create a custom
    oversized PDF page large enough for the content and title block. This avoids
    clipping large parts such as bumper plates. Use --standard-sheets-only to
    force ANSI A-E output, accepting that very large drawings may be clipped.
    """
    margin = 12.0
    gap = 10.0

    def title_block_h(sh: float) -> float:
        return max(40.0, min(sh * args.title_block_height_ratio, sh * 0.34))

    for name in ([args.sheet] if args.sheet != "auto" else ["A", "B", "C", "D", "E"]):
        w_in, h_in = ANSI_SHEETS_IN[name]
        candidates = []
        if args.orientation in {"auto", "landscape"}:
            candidates.append((w_in*MM_PER_INCH, h_in*MM_PER_INCH, True))
        if args.orientation in {"auto", "portrait"}:
            candidates.append((h_in*MM_PER_INCH, w_in*MM_PER_INCH, False))
        for sw, sh, land in candidates:
            tb_h = title_block_h(sh)
            avail_w = sw - 2*margin
            avail_h = sh - tb_h - gap - 2*margin
            if content_mm.width <= avail_w and content_mm.height <= avail_h:
                return Sheet(name, sw, sh, land)

    # Drawing is too large for ANSI E at 1:1. Preserve the requested 1:1 scale by
    # creating an oversized PDF page unless the user explicitly disallows it.
    if not args.standard_sheets_only:
        custom_w = content_mm.width + 2*margin
        needed_without_tb = content_mm.height + gap + 2*margin
        ratio = max(0.05, min(float(args.title_block_height_ratio), 0.34))
        custom_h = max(needed_without_tb + 40.0, needed_without_tb / (1.0 - ratio))
        # Keep a usable minimum page for the title block.
        custom_w = max(custom_w, 279.4)  # ANSI A width
        custom_h = max(custom_h, 215.9)  # ANSI A height
        if args.orientation == "landscape" and custom_h > custom_w:
            custom_w, custom_h = custom_h, custom_w
        elif args.orientation == "portrait" and custom_w > custom_h:
            custom_w, custom_h = custom_h, custom_w
        return Sheet("CUSTOM", custom_w, custom_h, custom_w >= custom_h, True)

    # Forced ANSI fallback. This can clip if the drawing is physically larger
    # than the selected sheet at 1:1.
    w_in, h_in = ANSI_SHEETS_IN["E"]
    if args.orientation == "portrait":
        return Sheet("E", h_in*MM_PER_INCH, w_in*MM_PER_INCH, False)
    return Sheet("E", w_in*MM_PER_INCH, h_in*MM_PER_INCH, True)

def text_width_pt(txt: str, size: float, font_name: str = "Helvetica") -> float:
    """Return text width in PDF points using ReportLab public API."""
    return float(pdfmetrics.stringWidth(str(txt), font_name, size))


def fit_font(text: str, max_width_pt: float, preferred: float, minimum: float) -> float:
    if not text:
        return preferred
    size = preferred
    while size > minimum and text_width_pt(text, size) > max_width_pt:
        size -= 0.5
    return max(size, minimum)


def draw_fitted_text(c: canvas.Canvas, text: str, x: float, y: float, w: float, h: float,
                     preferred: float, minimum: float = 8.0, bold=False, align="center") -> None:
    pad = 3.0
    size = fit_font(text, max(1, w - 2*pad), preferred, minimum)
    c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    # If even min font is too wide, truncate with ellipsis.
    display = text
    while text_width_pt(display, size) > max(1, w - 2*pad) and len(display) > 4:
        display = display[:-4] + "..."
    ty = y + (h - size) / 2 + size * 0.22
    if align == "left":
        c.drawString(x + pad, ty, display)
    elif align == "right":
        c.drawRightString(x + w - pad, ty, display)
    else:
        c.drawCentredString(x + w/2, ty, display)


def draw_cell(c: canvas.Canvas, x, y, w, h, label: str, value: str, preferred=12.0, minimum=8.0, bold_value=False):
    c.rect(x, y, w, h, stroke=1, fill=0)
    c.setFont("Helvetica", 6.5)
    c.drawString(x+3, y+h-8, label.upper())
    draw_fitted_text(c, value, x+2, y+2, w-4, h-12, preferred, minimum, bold=bold_value)


def draw_title_block(c: canvas.Canvas, sheet: Sheet, args: argparse.Namespace, filename: str, units_label: str) -> Tuple[float, float, float, float]:
    sw, sh = sheet.width_mm * PT_PER_MM, sheet.height_mm * PT_PER_MM
    margin = 12 * PT_PER_MM
    tb_h = max(40, min(sheet.height_mm * args.title_block_height_ratio, sheet.height_mm * 0.34)) * PT_PER_MM
    x0, y0, w, h = margin, margin, sw - 2*margin, tb_h
    c.setLineWidth(0.5)
    c.rect(x0, y0, w, h, stroke=1, fill=0)
    # Row heights proportional; no giant text. All values fitted.
    r1, r2, r3 = h*0.32, h*0.22, h*0.24
    r4 = h - r1 - r2 - r3
    # Top row
    y = y0 + h - r1
    title_w = w*0.42
    draw_cell(c, x0, y, title_w, r1, "Drawing", Path(filename).stem, args.title_font_pt, args.min_title_font_pt, True)
    draw_cell(c, x0+title_w, y, w*0.48, r1, "Description", args.description, args.title_font_pt, args.min_title_font_pt)
    draw_cell(c, x0+title_w+w*0.48, y, w*0.10, r1, "Rev.", args.revision, args.title_font_pt, args.min_title_font_pt, True)
    # Second row
    y = y0 + h - r1 - r2
    draw_cell(c, x0, y, w*0.22, r2, "Material", args.material, args.title_font_pt, args.min_title_font_pt)
    draw_cell(c, x0+w*0.22, y, w*0.22, r2, "Finish", args.finish, args.title_font_pt, args.min_title_font_pt)
    draw_cell(c, x0+w*0.44, y, w*0.18, r2, "Scale", "1:1", args.title_font_pt, args.min_title_font_pt, True)
    draw_cell(c, x0+w*0.62, y, w*0.18, r2, "Units", units_label.upper(), args.title_font_pt, args.min_title_font_pt)
    draw_cell(c, x0+w*0.80, y, w*0.20, r2, "Sheet", "1 OF 1", args.title_font_pt, args.min_title_font_pt)
    # Third row
    y = y0 + r4
    tol = ".X ±0.1   .XX ±0.03   .XXX ±0.010   ANG ±1°" if units_label == "inch" else ".X ±0.5   .XX ±0.25   ANG ±1°"
    draw_cell(c, x0, y, w*0.50, r3, "Tolerances unless otherwise specified", tol, 9.0, 6.5)
    draw_cell(c, x0+w*0.50, y, w*0.20, r3, "Drawn By", args.drawn_by, args.title_font_pt, args.min_title_font_pt)
    draw_cell(c, x0+w*0.70, y, w*0.15, r3, "Date", _dt.date.today().isoformat(), args.title_font_pt, args.min_title_font_pt)
    draw_cell(c, x0+w*0.85, y, w*0.15, r3, "Standard", "Y14.5", args.title_font_pt, args.min_title_font_pt)
    # Bottom row
    y = y0
    draw_cell(c, x0, y, w*0.28, r4, "Projection", "THIRD ANGLE", 9.5, 7.0)
    draw_cell(c, x0+w*0.28, y, w*0.52, r4, "Notice", "AUTO-DIMENSIONED. VERIFY BEFORE FABRICATION.", 8.5, 6.5)
    draw_cell(c, x0+w*0.80, y, w*0.20, r4, "File", filename, 8.5, 6.0)
    return x0, y0, w, h


def scaled_bbox_for_doc_bbox(doc_bbox_src: BBox, scale_to_mm: float) -> BBox:
    return BBox(doc_bbox_src.minx*scale_to_mm, doc_bbox_src.miny*scale_to_mm, doc_bbox_src.maxx*scale_to_mm, doc_bbox_src.maxy*scale_to_mm)


def render_entity_pdf(c: canvas.Canvas, e: DXFEntity, tx: RenderTransform, scale_to_mm: float) -> None:
    t = e.dxftype()
    try:
        if t == "LINE":
            c.line(*tx.p(e.dxf.start.x*scale_to_mm, e.dxf.start.y*scale_to_mm), *tx.p(e.dxf.end.x*scale_to_mm, e.dxf.end.y*scale_to_mm))
        elif t == "LWPOLYLINE":
            pts = [(p[0]*scale_to_mm, p[1]*scale_to_mm) for p in e.get_points()]
            for a, b in zip(pts, pts[1:]):
                c.line(*tx.p(*a), *tx.p(*b))
            if e.closed and len(pts) > 2:
                c.line(*tx.p(*pts[-1]), *tx.p(*pts[0]))
        elif t == "POLYLINE":
            pts = [(v.dxf.location.x*scale_to_mm, v.dxf.location.y*scale_to_mm) for v in e.vertices]
            for a, b in zip(pts, pts[1:]):
                c.line(*tx.p(*a), *tx.p(*b))
            if getattr(e, "is_closed", False) and len(pts) > 2:
                c.line(*tx.p(*pts[-1]), *tx.p(*pts[0]))
        elif t == "CIRCLE":
            cx, cy = tx.p(e.dxf.center.x*scale_to_mm, e.dxf.center.y*scale_to_mm)
            r = e.dxf.radius*scale_to_mm*tx.scale_pt_per_mm
            c.circle(cx, cy, r, stroke=1, fill=0)
        elif t == "ARC":
            cx, cy = tx.p(e.dxf.center.x*scale_to_mm, e.dxf.center.y*scale_to_mm)
            r = e.dxf.radius*scale_to_mm*tx.scale_pt_per_mm
            c.arc(cx-r, cy-r, cx+r, cy+r, e.dxf.start_angle, e.dxf.end_angle)
        elif t == "TEXT":
            ins = e.dxf.insert
            x, y = tx.p(ins.x*scale_to_mm, ins.y*scale_to_mm)
            size = max(5, e.dxf.height*scale_to_mm*tx.scale_pt_per_mm)
            c.setFont("Helvetica", size)
            c.saveState(); c.translate(x, y); c.rotate(float(e.dxf.rotation or 0)); c.drawCentredString(0, 0, e.dxf.text); c.restoreState()
    except Exception:
        return


def export_pdf(doc: Drawing, pdf_path: Path, source_bbox_src: BBox, scale_to_mm: float, out_units: str, args: argparse.Namespace, source_file: Path) -> None:
    # Use the annotated extents, plus a small physical margin, for sheet selection.
    # The margin is calculated in source units first so dimensions/text added in
    # modelspace are fully included when the PDF page is sized.
    pad_src = max(max(source_bbox_src.width, source_bbox_src.height) * 0.04, 8.0 / scale_to_mm)
    content_mm = scaled_bbox_for_doc_bbox(source_bbox_src.expand(pad_src), scale_to_mm)
    sheet = choose_sheet(content_mm, args)
    page_size = (sheet.width_mm*PT_PER_MM, sheet.height_mm*PT_PER_MM)
    c = canvas.Canvas(str(pdf_path), pagesize=page_size)
    sw_pt, sh_pt = page_size
    c.setLineWidth(0.5)
    # Border and zone marks, lightweight
    margin_pt = 12*PT_PER_MM
    c.rect(margin_pt, margin_pt, sw_pt-2*margin_pt, sh_pt-2*margin_pt, stroke=1, fill=0)
    units_label = "inch" if out_units == "imperial" else "mm"
    _, _, _, tb_h_pt = draw_title_block(c, sheet, args, source_file.name, units_label)
    available_x = margin_pt
    available_y = margin_pt + tb_h_pt + 10*PT_PER_MM
    available_w = sw_pt - 2*margin_pt
    available_h = sh_pt - available_y - margin_pt
    # 1:1 scale: one mm in source = one mm on PDF.
    scale_pt_per_mm = PT_PER_MM
    draw_w_pt = content_mm.width * scale_pt_per_mm
    draw_h_pt = content_mm.height * scale_pt_per_mm
    origin_x = available_x + (available_w - draw_w_pt)/2
    origin_y = available_y + (available_h - draw_h_pt)/2
    tx = RenderTransform(scale_pt_per_mm, origin_x, origin_y, content_mm.minx, content_mm.miny)
    c.setStrokeColorRGB(0, 0, 0)
    c.setFillColorRGB(0, 0, 0)
    for e in doc.modelspace():
        render_entity_pdf(c, e, tx, scale_to_mm)
    c.showPage()
    c.save()


def output_path_for(src: Path, root: Path, out_root: Path, suffix: str) -> Path:
    rel = src.relative_to(root) if src.is_relative_to(root) else Path(src.name)
    return out_root / rel.with_suffix(suffix)


def process_file(src: Path, root: Path, out_root: Path, args: argparse.Namespace) -> Tuple[bool, str]:
    try:
        doc = ezdxf.readfile(src)
        ensure_layers(doc)
        source_units, scale_to_mm = source_units_and_scale(doc, args.source_units)
        out_units = source_units if args.output_units == "source" else args.output_units
        msp = doc.modelspace()
        base_entities = get_entities(msp)
        bbox_src = compute_bbox(base_entities, 1.0)
        if bbox_src is None:
            return False, f"No drawable geometry: {src}"
        # Feature notes are generated from original geometry only so we do not annotate dimension lines.
        add_feature_notes(doc, base_entities, scale_to_mm, out_units, args.fractional_inches, args.angle_notes)
        add_overall_dimensions(doc, bbox_src, scale_to_mm, out_units, args.fractional_inches)
        if not args.no_annotated_dxf:
            dxf_out = output_path_for(src, root, out_root, ".annotated.dxf")
            dxf_out.parent.mkdir(parents=True, exist_ok=True)
            doc.saveas(dxf_out)
        if not args.no_pdf:
            pdf_out = output_path_for(src, root, out_root, ".pdf")
            pdf_out.parent.mkdir(parents=True, exist_ok=True)
            # Use expanded bbox after annotations so dimensions fit, but not title block.
            bbox_annot_src = compute_bbox(list(doc.modelspace()), 1.0) or bbox_src
            export_pdf(doc, pdf_out, bbox_annot_src, scale_to_mm, out_units, args, src)
        return True, f"Processed: {src}"
    except Exception as ex:
        return False, f"ERROR {src}: {ex}"


def iter_dxfs(input_path: Path, recursive: bool) -> List[Path]:
    if input_path.is_file() and input_path.suffix.lower() == ".dxf":
        return [input_path]
    pattern = "**/*.dxf" if recursive else "*.dxf"
    return sorted(input_path.glob(pattern))


def main() -> int:
    args = parse_args()
    inp = Path(args.input).resolve()
    out = Path(args.output).resolve()
    root = inp.parent if inp.is_file() else inp
    files = iter_dxfs(inp, args.recursive)
    if not files:
        print(f"No DXF files found in {inp}")
        return 2
    ok = 0
    for f in files:
        success, msg = process_file(f.resolve(), root.resolve(), out, args)
        print(msg)
        ok += int(success)
    print(f"Complete: {ok}/{len(files)} files processed. Output: {out}")
    return 0 if ok == len(files) else 1

if __name__ == "__main__":
    raise SystemExit(main())
