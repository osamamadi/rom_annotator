"""
ROM Annotator - scheme "v2_malleolus": the 5-point rig-referenced protocol.

WHY THIS EXISTS
    In the original 3-point scheme the leg axis ran to a "shin" point placed by
    eye on the lower leg, with no anatomical landmark under it. That judgement
    was the dominant error source: foot-axis error ran 0.8-1.6 deg while
    leg-axis error ran 2.8-3.0 deg.

    Here the leg axis IS the rig line. The wooden back plate of the rig is
    physically perpendicular to the ground, so two points on its front edge
    define the physical vertical. bottom_heel is then CONSTRUCTED by walking
    from the malleolus along that direction to the sole, which makes the vector
    bottom_heel -> malleolus parallel to the rig line by construction. Camera
    tilt therefore cancels out of angle_deg exactly; it is logged, not
    corrected for.

ISOLATION
    This module is additive. It shares nothing mutable with the 3-point scheme
    and imports nothing from app.py - app.py hands it the shared image helpers
    through configure(). It writes only under BASE_OUTPUT/photos/ and
    BASE_OUTPUT/csv/, and never reads or writes any labels_*.csv, any dated
    3-point folder, or anything under the deeplabcut export.

ERROR BUDGET (the reason for the thresholds below)
    Because the leg axis is the rig line, click error on the two rig points
    enters angle_deg 1:1, as roughly eps*sqrt(2)/separation radians:

        separation     +-2px    +-3px    +-5px
            300 px     0.540    0.810    1.350  deg
           1000 px     0.162    0.243    0.405
           2000 px     0.081    0.122    0.203

    The wooden edge is NOT the whole frame: measured on IMG_0528 it runs about
    885 px of a 3088 px frame, or 0.287 of the height, and the same fraction at
    the other capture resolution. So the best achievable rig term is about
    +-0.28 deg at 3 px, which makes it co-dominant with the sole level rather
    than negligible. Separations well under the available edge are refused.

    The other two sensitive quantities, at realistic geometry (foot 771 px,
    malleolus 688 px above the heel):

        sole level, 10 px along the construction line  ->  0.736 deg
        5th_metatarsal, 10 px vertically               ->  0.739 deg
        malleolus, 10 px sideways                      ->  0.090 deg
        5th_metatarsal, 10 px along the foot           ->  0.093 deg

    So the sole level and the VERTICAL position of 5th_metatarsal matter
    equally and dominate; the malleolus and the along-foot position of
    5th_metatarsal are an order of magnitude less sensitive.
"""

from __future__ import annotations

import csv
import math
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw
from pydantic import BaseModel

SCHEME_VERSION = "v2_malleolus"

# --------------------------------------------------------------------------
# Keypoints.
#
# Two orders exist and they are deliberately different:
#
#   KEYPOINT_ORDER  the stored / model index order, fixed by the protocol:
#                   0 bottom_heel, 1 5th_metatarsal, 2 malleolus,
#                   3 rig_top, 4 rig_bottom
#   CLICK_ORDER     the order the annotator clicks in: the rig line first,
#                   because bottom_heel cannot be constructed without it.
#
# Everything is keyed BY NAME everywhere - the CSV, the payload, the drawing.
# Neither order is ever used to look a value up positionally.
# --------------------------------------------------------------------------
KEYPOINT_ORDER = ["bottom_heel", "5th_metatarsal", "malleolus", "rig_top", "rig_bottom"]
CLICK_ORDER = ["rig_top", "rig_bottom", "malleolus", "bottom_heel", "5th_metatarsal"]

RIG_COLOR = (255, 170, 0)            # both rig points and the rig line
MALLEOLUS_COLOR = (220, 30, 30)
HEEL_COLOR = (30, 190, 60)
METATARSAL_COLOR = (40, 110, 255)

FOOT_AXIS_COLOR = (0, 255, 255)      # cyan   - bottom_heel -> 5th_metatarsal
LEG_AXIS_COLOR = (255, 0, 255)       # magenta - bottom_heel -> malleolus
CONSTRUCTION_COLOR = (255, 255, 255)  # the ray the heel is constrained to
GUIDE_CASING = (0, 0, 0)
ARC_COLOR = (255, 240, 0)

# (key, colour, precision, hint). "precision" drives the UI: whether the zoom
# panel is forced open while this point is being placed or moved.
LANDMARKS = [
    ("rig_top", RIG_COLOR, "critical",
     "Front edge of the wooden back plate, as HIGH in the frame as the edge goes. "
     "The rig line is the leg axis, so error here enters the angle 1:1."),
    ("rig_bottom", RIG_COLOR, "critical",
     "Same edge, as LOW in the frame as it goes. Separation is the whole point: "
     "further apart is strictly better."),
    ("malleolus", MALLEOLUS_COLOR, "relaxed",
     "Centre of the lateral malleolus, the bony bump on the outside of the ankle. "
     "Low sensitivity (~0.009 deg/px) - it only decides where the construction "
     "line sits. Place it honestly, but do not agonise."),
    ("bottom_heel", HEEL_COLOR, "critical",
     "Constrained to the construction line. Slide it down to where the sole meets "
     "the glass. 5 px of sole-level error = 0.37 deg."),
    ("5th_metatarsal", METATARSAL_COLOR, "critical",
     "Base of the fifth metatarsal. VERTICAL position carries the error "
     "(5 px = 0.37 deg); position along the foot barely matters (~0.009 deg/px)."),
]
LANDMARK_COLOR = {key: colour for key, colour, _p, _h in LANDMARKS}
assert sorted(LANDMARK_COLOR) == sorted(KEYPOINT_ORDER) == sorted(CLICK_ORDER)

# --------------------------------------------------------------------------
# Thresholds. Refusals block the save outright; warnings need an explicit
# acknowledgement from the UI and are recorded in the notes column.
# --------------------------------------------------------------------------
# Rig separation is judged as a FRACTION OF FRAME HEIGHT, not in pixels.
#
# The first version of this used a flat 1200 px, on my assumption that the
# wooden edge spanned most of the frame. It does not: measured on IMG_0528 the
# plate's front edge runs y=1845..2730, about 885 px of a 3088 px frame, so
# every honest placement was refused. The same rig fills the same FRACTION at
# both capture resolutions (885/3088 = 1637/5712 = 0.287), which is why the
# fraction is the right invariant and pixels are not.
#
#   0.287  the whole visible edge on an uncropped frame
#
# The WARNING IS DISABLED at the annotator's request. Many frames are cropped
# tightly enough that a longer rig line is physically impossible, so the popup
# fired on most images and blocked the workflow for information the annotator
# already has. The accuracy cost is accepted knowingly: at 0.22 of a 3088 px
# frame the rig term is about +-0.36 deg instead of +-0.27 deg.
#
# Nothing is lost by silencing it. rig_sep_px is stored on every row, so the
# per-image uncertainty is recoverable at analysis time with
# degrees(3*sqrt(2)/rig_sep_px), and rows can be filtered or weighted then.
#
# Set RIG_SEP_WARN_FRAC back above 0 to bring the warning back.
RIG_SEP_WARN_FRAC = 0.0

# The refusal stays, but only low enough to catch a genuine mis-click - two rig
# points landing almost on top of each other would produce a wildly wrong angle
# with nothing on screen to suggest it. 0.05 of a 3088 px frame is 154 px, far
# below any deliberate placement.
RIG_SEP_REFUSE_FRAC = 0.05
PARALLEL_DEV_MAX_DEG = 0.5
RIG_TILT_WARN_DEG = 2.0
ANGLE_WARN_MIN_DEG = 50.0
ANGLE_WARN_MAX_DEG = 130.0

# Used only for the live error-budget readout, never for validation.
CLICK_ERROR_PX = 3.0      # a normal click error on a rig point
BUDGET_ERROR_PX = 5.0     # the sole-level / 5th-MT term the readout quotes

COORD_DECIMALS = 2        # sub-pixel: 5 px is already 0.37 deg, so do not round to int
DERIVED_DECIMALS = 4
LENGTH_DECIMALS = 2

# --------------------------------------------------------------------------
# Output layout. Nothing outside these two folders is ever written: every
# annotated export goes flat into PHOTOS_DIRNAME (no per-date subfolder - all
# sessions write into the same folder, keyed by filename) and the one running
# CSV lives separately in CSV_DIRNAME.
# --------------------------------------------------------------------------
PHOTOS_DIRNAME = "photos"
CSV_DIRNAME = "csv"
MASTER_NAME = "master_v2_malleolus.csv"

MASTER_COLUMNS = [
    "image", "capture_date", "annotation_date", "subject_id", "side", "pair_id",
    "phase", "screen_angle", "scheme_version",
    "bottom_heel_x", "bottom_heel_y",
    "5th_metatarsal_x", "5th_metatarsal_y",
    "malleolus_x", "malleolus_y",
    "rig_top_x", "rig_top_y",
    "rig_bottom_x", "rig_bottom_y",
    "angle_deg", "rig_tilt_deg", "parallel_dev_deg",
    "foot_len_px", "leg_len_px", "rig_sep_px",
    "image_w", "image_h", "notes",
]

EXIF_DATETIME_ORIGINAL = 0x9003
EXIF_DATETIME = 0x0132
EXIF_SUB_IFD = 0x8769


# --------------------------------------------------------------------------
# Wiring. app.py calls configure() once, passing the helpers that are shared
# with the 3-point scheme. Importing them instead would be circular.
# --------------------------------------------------------------------------
_deps: Dict[str, object] = {}


def configure(**kwargs) -> None:
    required = {"open_upright", "resolve_input_image", "validate_side",
                "list_input_images", "natural_key", "load_font", "base_output"}
    missing = required - set(kwargs)
    if missing:
        raise RuntimeError("scheme_v2.configure is missing: %s" % ", ".join(sorted(missing)))
    _deps.update(kwargs)


def _dep(name: str):
    try:
        return _deps[name]
    except KeyError:
        raise RuntimeError("scheme_v2 was never configured by app.py") from None


def photos_dir() -> Path:
    return Path(_dep("base_output")) / PHOTOS_DIRNAME


def csv_dir() -> Path:
    return Path(_dep("base_output")) / CSV_DIRNAME


def master_path() -> Path:
    return csv_dir() / MASTER_NAME


def export_dir() -> Path:
    return photos_dir()


# --------------------------------------------------------------------------
# Geometry
#
# Image coordinates: x right, y DOWN. signed_angle is positive when b is
# rotated CLOCKWISE from a as seen on screen.
# --------------------------------------------------------------------------

def signed_angle(ax: float, ay: float, bx: float, by: float) -> float:
    return math.degrees(math.atan2(ax * by - ay * bx, ax * bx + ay * by))


def included_angle(vx: float, vy: float, wx: float, wy: float) -> float:
    """Unsigned angle between two vectors. Sign-convention independent."""
    return math.degrees(math.atan2(abs(vx * wy - vy * wx), vx * wx + vy * wy))


def rig_uncertainty_deg(separation: float, click_px: float = CLICK_ERROR_PX) -> float:
    """Angle uncertainty contributed by click error on the two rig points."""
    if separation <= 0:
        return float("inf")
    return math.degrees(click_px * math.sqrt(2.0) / separation)


def point_uncertainty_deg(foot_len: float, shift_px: float = BUDGET_ERROR_PX) -> float:
    """
    Angle error from displacing bottom_heel or 5th_metatarsal perpendicular to
    the foot axis. The lever arm is the foot length, so both terms are equal.
    """
    if foot_len <= 0:
        return float("inf")
    return math.degrees(shift_px / foot_len)


def derive(pts: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    """
    Every derived quantity, from the five points. Pure - no validation, no I/O.

    pts maps each key in KEYPOINT_ORDER to {"x": float, "y": float}.
    """
    heel, met = pts["bottom_heel"], pts["5th_metatarsal"]
    mal, top, bot = pts["malleolus"], pts["rig_top"], pts["rig_bottom"]

    rx, ry = bot["x"] - top["x"], bot["y"] - top["y"]        # rig "down"
    fx, fy = met["x"] - heel["x"], met["y"] - heel["y"]      # foot axis
    lx, ly = mal["x"] - heel["x"], mal["y"] - heel["y"]      # leg axis
    mx, my = heel["x"] - mal["x"], heel["y"] - mal["y"]      # malleolus -> heel

    rig_sep = math.hypot(rx, ry)
    rhx, rhy = (rx / rig_sep, ry / rig_sep) if rig_sep else (0.0, 0.0)

    return {
        "angle_deg": included_angle(fx, fy, lx, ly),
        "rig_tilt_deg": signed_angle(0.0, 1.0, rx, ry),
        "parallel_dev_deg": signed_angle(rx, ry, mx, my),
        "foot_len_px": math.hypot(fx, fy),
        "leg_len_px": math.hypot(lx, ly),
        "rig_sep_px": rig_sep,
        # How far the heel sits DOWN the rig direction from the malleolus.
        # Must be positive: the sole is below the ankle.
        "heel_travel_px": mx * rhx + my * rhy,
    }


def error_budget(rig_sep: float, foot_len: float) -> Dict[str, float]:
    """The live readout: each contribution and their root-sum-square."""
    rig = rig_uncertainty_deg(rig_sep)
    sole = point_uncertainty_deg(foot_len)
    met = point_uncertainty_deg(foot_len)
    return {
        "rig_sep_px": rig_sep,
        "rig_deg": rig,
        "sole_deg": sole,
        "metatarsal_deg": met,
        "total_deg": math.sqrt(rig * rig + sole * sole + met * met),
        "click_px": CLICK_ERROR_PX,
        "budget_px": BUDGET_ERROR_PX,
    }


def project_onto_construction(mal: Dict[str, float], top: Dict[str, float],
                              bot: Dict[str, float],
                              px: float, py: float) -> Dict[str, float]:
    """
    Foot of the perpendicular from (px, py) onto the construction ray: the line
    through the malleolus in the rig direction. This is the server-side twin of
    the constraint the canvas applies, so the two can be checked against each
    other.
    """
    rx, ry = bot["x"] - top["x"], bot["y"] - top["y"]
    norm = math.hypot(rx, ry)
    if norm < 1e-9:
        return {"x": mal["x"], "y": mal["y"]}
    rhx, rhy = rx / norm, ry / norm
    t = (px - mal["x"]) * rhx + (py - mal["y"]) * rhy
    return {"x": mal["x"] + t * rhx, "y": mal["y"] + t * rhy}


# --------------------------------------------------------------------------
# Capture date
# --------------------------------------------------------------------------

def capture_date(path: Path) -> str:
    """
    Full capture timestamp, "YYYY-MM-DD HH:MM:SS".

    EXIF DateTimeOriginal, else EXIF DateTime, else the file's mtime. The time
    is kept, not just the date, so several shots from one session sort into the
    order they were actually taken.
    """
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            raw = None
            if exif:
                try:
                    raw = exif.get_ifd(EXIF_SUB_IFD).get(EXIF_DATETIME_ORIGINAL)
                except Exception:       # noqa: BLE001 - malformed EXIF is not fatal
                    raw = None
                if not raw:
                    raw = exif.get(EXIF_DATETIME)
            if raw:
                # EXIF writes "YYYY:MM:DD HH:MM:SS"; only the date half uses colons
                text = str(raw).strip()
                m = re.match(r"^(\d{4})[:-](\d{2})[:-](\d{2})[ T]"
                             r"(\d{2}):(\d{2}):(\d{2})", text)
                if m:
                    return "%s-%s-%s %s:%s:%s" % m.groups()
                m = re.match(r"^(\d{4})[:-](\d{2})[:-](\d{2})$", text)
                if m:
                    return "%s-%s-%s 00:00:00" % m.groups()
    except Exception:                    # noqa: BLE001 - fall through to mtime
        pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return ""


# --------------------------------------------------------------------------
# Master CSV
#
# One file for every new-mode annotation across every session, appended to.
# A row is replaced in place when the same image is annotated again, so the
# master never grows duplicate rows for one image.
# --------------------------------------------------------------------------

def read_master() -> List[Dict[str, str]]:
    path = master_path()
    if not path.is_file():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        return [r for r in csv.DictReader(fh) if (r.get("image") or "").strip()]


def check_master_schema() -> None:
    """
    Refuse to append to a master whose header is not this scheme's.

    Rows are written by field name, but a header mismatch still means the file
    was produced by different code, and mixing the two silently is exactly what
    scheme_version exists to prevent.
    """
    path = master_path()
    if not path.is_file():
        return
    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        header = next(csv.reader(fh), [])
    if header != MASTER_COLUMNS:
        raise HTTPException(status_code=409, detail=(
            "%s has a different header than this scheme expects, so appending to "
            "it could corrupt it. Nothing was saved. Move or rename that file, "
            "then save again." % path.name))


def upsert_master(row: Dict[str, str]) -> str:
    check_master_schema()
    path = master_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.is_file():
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=MASTER_COLUMNS)
            writer.writeheader()
            writer.writerow(row)
        return "appended"

    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        existing = list(csv.DictReader(fh))

    for i, old in enumerate(existing):
        if ((old.get("image") or "").strip() == row["image"]
                and (old.get("side") or "").strip() == row["side"]):
            existing[i] = row
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=MASTER_COLUMNS)
                writer.writeheader()
                writer.writerows(existing)
            return "replaced"

    with path.open("a", newline="", encoding="utf-8") as fh:
        csv.DictWriter(fh, fieldnames=MASTER_COLUMNS).writerow(row)
    return "appended"


def done_names(side: str) -> set:
    """
    Images DONE under this scheme: a master row with scheme_version v2_malleolus.

    An image annotated under the old 3-point scheme is explicitly NOT done here,
    and nothing in this function looks at the 3-point files at all.
    """
    return {r["image"].strip() for r in read_master()
            if (r.get("scheme_version") or "").strip() == SCHEME_VERSION
            and (r.get("side") or "").strip() == side}


def known_rows(side: str) -> Dict[str, Dict[str, str]]:
    """Existing master rows for this side, so re-annotating prefills the metadata."""
    return {r["image"].strip(): r for r in read_master()
            if (r.get("side") or "").strip() == side
            and (r.get("scheme_version") or "").strip() == SCHEME_VERSION}


def vocabulary() -> Dict[str, List[str]]:
    """Values already used, to offer as dropdowns instead of free-typing twice."""
    rows = read_master()
    out = {}
    for field in ("subject_id", "pair_id", "phase"):
        seen = {(r.get(field) or "").strip() for r in rows}
        out[field] = sorted(v for v in seen if v)
    return out


# --------------------------------------------------------------------------
# Annotated export
# --------------------------------------------------------------------------

def _line(draw, xy, colour, core_w, casing_w):
    draw.line(xy, fill=GUIDE_CASING, width=casing_w)
    draw.line(xy, fill=colour, width=core_w)


def draw_annotations(src: Path, dst: Path, pts: Dict[str, Dict[str, float]],
                     derived: Dict[str, float]) -> None:
    """
    Draw the five markers, the rig line and the two axes onto an upright copy.

    Marker text is deliberately not drawn beside the points - it obscured the
    anatomy for the reviewing clinicians in the 3-point tool. A corner legend
    carries the colour key instead.
    """
    open_upright = _dep("open_upright")
    load_font = _dep("load_font")

    with open_upright(src) as raw:
        im = raw.convert("RGB")
    draw = ImageDraw.Draw(im)

    scale = max(im.width, im.height) / 1000.0
    radius = max(4, int(round(7 * scale)))
    ring = max(1, int(round(2 * scale)))
    arm = radius * 2
    font_size = max(12, int(round(22 * scale)))
    font = load_font(font_size)
    core_w = max(2, int(round(2.5 * scale)))
    casing_w = core_w + 2 * max(1, int(round(1.5 * scale)))

    xy = lambda k: (pts[k]["x"], pts[k]["y"])                       # noqa: E731

    _line(draw, [*xy("rig_top"), *xy("rig_bottom")], RIG_COLOR, core_w, casing_w)
    _line(draw, [*xy("bottom_heel"), *xy("5th_metatarsal")], FOOT_AXIS_COLOR, core_w, casing_w)
    _line(draw, [*xy("bottom_heel"), *xy("malleolus")], LEG_AXIS_COLOR, core_w, casing_w)

    _draw_angle(draw, pts, derived, scale, font, arm)

    for key in KEYPOINT_ORDER:
        x, y = int(round(pts[key]["x"])), int(round(pts[key]["y"]))
        colour = LANDMARK_COLOR[key]
        draw.line([x - arm, y, x + arm, y], fill=colour, width=ring)
        draw.line([x, y - arm, x, y + arm], fill=colour, width=ring)
        draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                     fill=colour, outline=(255, 255, 255), width=ring)

    im = _draw_legend(im, font, font_size, scale, derived)

    dst.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"quality": 95} if dst.suffix.lower() in {".jpg", ".jpeg"} else {}
    im.save(dst, **kwargs)


def _draw_angle(draw, pts, derived, scale, font, arm):
    """Arc at bottom_heel between the two axes, plus the angle in degrees."""
    heel = pts["bottom_heel"]
    r = max(24.0, 50.0 * scale)

    a_foot = math.degrees(math.atan2(pts["5th_metatarsal"]["y"] - heel["y"],
                                     pts["5th_metatarsal"]["x"] - heel["x"]))
    a_leg = math.degrees(math.atan2(pts["malleolus"]["y"] - heel["y"],
                                    pts["malleolus"]["x"] - heel["x"]))
    sweep = (a_leg - a_foot + 540.0) % 360.0 - 180.0
    start, end = (a_foot, a_foot + sweep) if sweep >= 0 else (a_foot + sweep, a_foot)

    box = [heel["x"] - r, heel["y"] - r, heel["x"] + r, heel["y"] + r]
    draw.arc(box, start, end, fill=GUIDE_CASING, width=max(3, int(round(4 * scale))))
    draw.arc(box, start, end, fill=ARC_COLOR, width=max(2, int(round(2.5 * scale))))

    mid = math.radians(start + (end - start) / 2.0)
    tx = heel["x"] + math.cos(mid) * r * 1.55
    ty = heel["y"] + math.sin(mid) * r * 1.55
    text = "%.1f°" % derived["angle_deg"]
    draw.text((tx, ty), text, font=font, fill=(255, 255, 255), anchor="mm",
              stroke_width=max(1, int(round(2 * scale))), stroke_fill=GUIDE_CASING)


def _draw_legend(im, font, font_size, scale, derived):
    """Colour key plus the numbers, on a semi-transparent panel in the top-left."""
    entries = [(key, LANDMARK_COLOR[key], "dot") for key in KEYPOINT_ORDER]
    entries += [("Rig line (physical vertical)", RIG_COLOR, "line"),
                ("Foot axis", FOOT_AXIS_COLOR, "line"),
                ("Leg axis", LEG_AXIS_COLOR, "line"),
                ("angle %.2f°" % derived["angle_deg"], ARC_COLOR, "line"),
                ("rig tilt %+.2f°  dev %+.3f°"
                 % (derived["rig_tilt_deg"], derived["parallel_dev_deg"]),
                 (200, 200, 200), "text"),
                ("rig sep %.0f px  ±%.3f°"
                 % (derived["rig_sep_px"], rig_uncertainty_deg(derived["rig_sep_px"])),
                 (200, 200, 200), "text"),
                (SCHEME_VERSION, (160, 160, 160), "text")]

    margin = max(6, int(round(14 * scale)))
    swatch = max(6, int(round(font_size * 0.62)))
    gap = max(4, int(round(font_size * 0.45)))
    line_gap = max(2, int(round(font_size * 0.34)))
    inner = max(4, int(round(font_size * 0.5)))
    stroke = max(1, int(round(scale)))

    measure = ImageDraw.Draw(im)
    rows, text_w, row_h = [], 0, swatch
    for label, colour, kind in entries:
        bbox = measure.textbbox((0, 0), label, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        rows.append((label, colour, bbox, h, kind))
        text_w = max(text_w, w)
        row_h = max(row_h, h)

    panel_w = inner * 2 + swatch + gap + text_w
    panel_h = inner * 2 + row_h * len(rows) + line_gap * (len(rows) - 1)

    overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([margin, margin, margin + panel_w, margin + panel_h],
                 fill=(0, 0, 0, 145), outline=(255, 255, 255, 90), width=stroke)

    y = margin + inner
    for label, colour, bbox, h, kind in rows:
        cy = y + row_h / 2.0
        if kind == "line":
            od.line([margin + inner, cy, margin + inner + swatch, cy],
                    fill=colour + (255,), width=max(2, stroke * 2))
        elif kind == "dot":
            r = swatch / 2.0
            cx = margin + inner + r
            od.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour + (255,),
                       outline=(255, 255, 255, 220), width=stroke)
        od.text((margin + inner + swatch + gap, cy - h / 2.0 - bbox[1]), label,
                font=font, fill=colour + (255,) if kind == "text" else (255, 255, 255, 255))
        y += row_h + line_gap

    return Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")


# --------------------------------------------------------------------------
# Validation
#
# Every check runs BEFORE the annotated image is drawn and before the CSV row
# is written, so a refused save leaves nothing behind at all.
# --------------------------------------------------------------------------

def validate(pts: Dict[str, Dict[str, float]], width: int, height: int):
    """
    Raise on anything fatal; return (derived, warnings).

    The presence and bounds checks run BEFORE derive() so a missing point gives
    a clear refusal rather than a KeyError out of the geometry.
    """
    for key in KEYPOINT_ORDER:
        p = pts.get(key)
        if p is None:
            raise HTTPException(status_code=422, detail=(
                "%s is missing. All five points are required. Nothing was saved." % key))
        if not (0 <= p["x"] <= width and 0 <= p["y"] <= height):
            raise HTTPException(status_code=422, detail=(
                "%s at (%.1f, %.1f) is outside the %dx%d image. Nothing was saved."
                % (key, p["x"], p["y"], width, height)))

    unknown = [k for k in pts if k not in LANDMARK_COLOR]
    if unknown:
        raise HTTPException(status_code=422, detail=(
            "Unknown point(s): %s. Nothing was saved." % ", ".join(sorted(unknown))))

    derived = derive(pts)

    sep = derived["rig_sep_px"]
    refuse_at = RIG_SEP_REFUSE_FRAC * height
    warn_at = RIG_SEP_WARN_FRAC * height
    if sep < refuse_at:
        raise HTTPException(status_code=422, detail=(
            "The rig points are only %.0f px apart (%.0f%% of the frame height), "
            "which is too close together to define a direction - the two points "
            "are almost on top of each other. That would give ±%.2f° from a "
            "normal %.0f px click error. The floor is %.0f px. Nothing was saved."
            % (sep, 100.0 * sep / height, rig_uncertainty_deg(sep),
               CLICK_ERROR_PX, refuse_at)))

    if derived["foot_len_px"] < 1.0:
        raise HTTPException(status_code=422, detail=(
            "bottom_heel and 5th_metatarsal are on the same pixel, so there is no "
            "foot axis. Nothing was saved."))
    if derived["leg_len_px"] < 1.0:
        raise HTTPException(status_code=422, detail=(
            "bottom_heel and malleolus are on the same pixel, so there is no leg "
            "axis. Nothing was saved."))

    if derived["heel_travel_px"] <= 0:
        raise HTTPException(status_code=422, detail=(
            "bottom_heel is %.0f px ABOVE the malleolus along the rig direction. "
            "The heel is constructed by travelling DOWN from the malleolus to the "
            "sole. Nothing was saved." % abs(derived["heel_travel_px"])))

    dev = derived["parallel_dev_deg"]
    if abs(dev) > PARALLEL_DEV_MAX_DEG:
        raise HTTPException(status_code=422, detail=(
            "parallel_dev_deg is %+.3f°, beyond the ±%.1f° limit, so "
            "bottom_heel is not on the construction line and the constraint was "
            "bypassed. Re-place bottom_heel. Nothing was saved."
            % (dev, PARALLEL_DEV_MAX_DEG)))

    warnings = []
    if warn_at > 0 and sep < warn_at:
        warnings.append(
            "rig separation %.0f px, %.0f%% of the frame (±%.2f°): place the "
            "rig points further apart, this is the dominant error term"
            % (sep, 100.0 * sep / height, rig_uncertainty_deg(sep)))
    if abs(derived["rig_tilt_deg"]) > RIG_TILT_WARN_DEG:
        warnings.append("rig tilt %+.2f° exceeds ±%.0f°"
                        % (derived["rig_tilt_deg"], RIG_TILT_WARN_DEG))
    ang = derived["angle_deg"]
    if not (ANGLE_WARN_MIN_DEG <= ang <= ANGLE_WARN_MAX_DEG):
        warnings.append("angle %.2f° is outside the plausible %.0f-%.0f°"
                        % (ang, ANGLE_WARN_MIN_DEG, ANGLE_WARN_MAX_DEG))
    return derived, warnings


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

class V2Point(BaseModel):
    x: float
    y: float


class V2SavePayload(BaseModel):
    filename: str
    side: str
    # Keyed BY NAME, never by position.
    points: Dict[str, V2Point]
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    subject_id: str = ""
    pair_id: str = ""
    phase: str = ""
    screen_angle: str = ""
    notes: str = ""
    # Set by the UI once the annotator has confirmed the listed warnings.
    acknowledged: bool = False


router = APIRouter(prefix="/api/v2", tags=["v2_malleolus"])


@router.get("/config")
def v2_config():
    return {
        "scheme_version": SCHEME_VERSION,
        "keypoint_order": KEYPOINT_ORDER,
        "click_order": CLICK_ORDER,
        "landmarks": [
            {"key": k, "color": "rgb(%d,%d,%d)" % c, "precision": p, "hint": h}
            for k, c, p, h in LANDMARKS
        ],
        "colors": {
            "rig": "rgb(%d,%d,%d)" % RIG_COLOR,
            "foot_axis": "rgb(%d,%d,%d)" % FOOT_AXIS_COLOR,
            "leg_axis": "rgb(%d,%d,%d)" % LEG_AXIS_COLOR,
            "construction": "rgb(%d,%d,%d)" % CONSTRUCTION_COLOR,
            "arc": "rgb(%d,%d,%d)" % ARC_COLOR,
        },
        "limits": {
            "rig_sep_refuse_frac": RIG_SEP_REFUSE_FRAC,
            "rig_sep_warn_frac": RIG_SEP_WARN_FRAC,
            "parallel_dev_max_deg": PARALLEL_DEV_MAX_DEG,
            "rig_tilt_warn_deg": RIG_TILT_WARN_DEG,
            "angle_warn_min_deg": ANGLE_WARN_MIN_DEG,
            "angle_warn_max_deg": ANGLE_WARN_MAX_DEG,
            "click_error_px": CLICK_ERROR_PX,
            "budget_error_px": BUDGET_ERROR_PX,
        },
        "master": str(master_path()),
        "export_dir": str(photos_dir()),
        "today": date.today().isoformat(),
    }


@router.get("/images")
def v2_images(side: str = Query(...), mode: str = Query("resume")):
    side = _dep("validate_side")(side)
    mode = (mode or "resume").strip().lower()
    if mode not in {"resume", "fresh"}:
        raise HTTPException(status_code=400, detail="mode must be 'resume' or 'fresh'.")

    all_images = _dep("list_input_images")(side)
    done = done_names(side)
    queue = [n for n in all_images if n not in done] if mode == "resume" else list(all_images)
    rows = known_rows(side)

    return {
        "side": side,
        "mode": mode,
        "scheme_version": SCHEME_VERSION,
        "total_in_folder": len(all_images),
        "already_annotated": len(done & set(all_images)),
        "images": queue,
        "known": {n: rows[n] for n in queue if n in rows},
        "vocabulary": vocabulary(),
        "master": str(master_path()),
    }


@router.post("/save")
def v2_save(payload: V2SavePayload):
    side = _dep("validate_side")(payload.side)
    src = _dep("resolve_input_image")(side, payload.filename)

    with _dep("open_upright")(src) as im:
        width, height = im.size

    # The canvas must be measuring against the same upright full-resolution
    # frame the server uses. A stale cached v2.js would otherwise write
    # landmarks that look plausible and are quietly wrong.
    cw, ch = payload.image_width, payload.image_height
    if cw and ch and (cw, ch) != (width, height):
        raise HTTPException(status_code=400, detail=(
            "Coordinate space mismatch for %s: the browser measured %dx%d but the "
            "upright image is %dx%d. Hard-refresh the page (Ctrl+F5) and redo this "
            "image. Nothing was saved." % (src.name, cw, ch, width, height)))

    pts = {k: {"x": float(p.x), "y": float(p.y)} for k, p in payload.points.items()}
    try:
        derived, warnings = validate(pts, width, height)
    except HTTPException as exc:
        # "422 Unprocessable Entity" on its own tells nobody anything.
        print("[v2] REFUSED %s: %s" % (src.name, exc.detail), file=sys.stderr)
        raise

    if warnings and not payload.acknowledged:
        raise HTTPException(status_code=422, detail={
            "message": "This frame has warnings. Confirm them to save, or fix them.",
            "warnings": warnings,
        })

    check_master_schema()       # refuse before drawing anything

    dst = export_dir() / src.name
    try:
        draw_annotations(src, dst, pts, derived)
    except Exception as exc:    # noqa: BLE001 - surface the real reason to the UI
        raise HTTPException(status_code=500,
                            detail="Failed to write annotated image: " + str(exc))

    note_parts = [payload.notes.strip()] if payload.notes.strip() else []
    note_parts += ["[warn] " + w for w in warnings]

    row = {
        "image": src.name,
        "capture_date": capture_date(src),
        "annotation_date": date.today().isoformat(),
        "subject_id": payload.subject_id.strip(),
        "side": side,
        "pair_id": payload.pair_id.strip(),
        "phase": payload.phase.strip(),
        "screen_angle": payload.screen_angle.strip(),
        "scheme_version": SCHEME_VERSION,
        "angle_deg": "%.*f" % (DERIVED_DECIMALS, derived["angle_deg"]),
        "rig_tilt_deg": "%.*f" % (DERIVED_DECIMALS, derived["rig_tilt_deg"]),
        "parallel_dev_deg": "%.*f" % (DERIVED_DECIMALS, derived["parallel_dev_deg"]),
        "foot_len_px": "%.*f" % (LENGTH_DECIMALS, derived["foot_len_px"]),
        "leg_len_px": "%.*f" % (LENGTH_DECIMALS, derived["leg_len_px"]),
        "rig_sep_px": "%.*f" % (LENGTH_DECIMALS, derived["rig_sep_px"]),
        "image_w": str(width),
        "image_h": str(height),
        "notes": " | ".join(note_parts),
    }
    for key in KEYPOINT_ORDER:
        row[key + "_x"] = "%.*f" % (COORD_DECIMALS, pts[key]["x"])
        row[key + "_y"] = "%.*f" % (COORD_DECIMALS, pts[key]["y"])

    action = upsert_master(row)

    return JSONResponse({
        "status": "ok",
        "csv_action": action,
        "warnings": warnings,
        "derived": {k: round(v, 4) for k, v in derived.items()},
        "budget": {k: (round(v, 4) if isinstance(v, float) else v)
                   for k, v in error_budget(derived["rig_sep_px"],
                                            derived["foot_len_px"]).items()},
        "image_size": {"width": width, "height": height},
        "annotated_image": str(dst),
        "master": str(master_path()),
        "row": row,
    })
