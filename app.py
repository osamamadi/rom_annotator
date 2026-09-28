"""
ROM Annotator - local FastAPI app for manual 3-keypoint image annotation.

Keypoints (strict click order):
    1. shin
    2. bottom_heel
    3. 5th_metatarsal

The exported image carries the three markers plus a colour-key legend in the
top-left. Per-point text labels are deliberately NOT drawn on the export -
they obscured the anatomy for the reviewing clinicians.

Run:  python app.py       (then open http://127.0.0.1:8000)
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Configuration
#
# Paths are relative to an "assets root" so this runs on any machine without
# edits: default is ../assets next to this repo (see project-level README),
# override with the ROM_ASSETS_DIR env var if your assets/ lives elsewhere
# (e.g. a Drive-synced folder in a different location).
# --------------------------------------------------------------------------

ASSETS_ROOT = Path(os.environ.get("ROM_ASSETS_DIR", Path(__file__).resolve().parent.parent / "assets"))

# Only the left ankle is ever shot, so there is one flat raw-image folder, not
# a per-side split. The "left" key is kept (rather than dropped) because it is
# still recorded as the `side` value on every CSV row and passed through the
# API/UI - it now just always resolves to the same folder.
INPUT_DIRS: Dict[str, Path] = {
    "left": ASSETS_ROOT / "raw",
}

BASE_OUTPUT_DIR = ASSETS_ROOT / "annotations"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

# Phone photos carry an EXIF rotation flag. Browsers apply it, PIL does not, so
# every image is normalised to its upright ("displayed") orientation on both
# sides - the canvas, the coordinates, and the exported copy all agree.
PREVIEW_MAX = 2400          # longest side sent to the browser
PREVIEW_CACHE_MAX = 8

# --------------------------------------------------------------------------
# The 3-point protocol.
#
# (key, label, RGB colour). Order is the enforced click order. The key is the
# protocol name and is used verbatim everywhere: the UI, the legend on the
# exported image, and the CSV columns. Everything below refers to keypoints
# BY KEY, never by position.
#
# shin is clicked LAST: its magnet circle is centred on bottom_heel and sized
# from the foot length, so both of those must already exist.
# --------------------------------------------------------------------------
LANDMARKS = [
    ("bottom_heel", "bottom_heel", (30, 190, 60)),
    ("5th_metatarsal", "5th_metatarsal", (40, 110, 255)),
    ("shin", "shin", (220, 30, 30)),
]

KEY_INDEX = {key: i for i, (key, _label, _colour) in enumerate(LANDMARKS)}

POINT_COUNT = len(LANDMARKS)

# --------------------------------------------------------------------------
# Shin magnet.
#
# The shin is constrained to a circle centred on bottom_heel whose radius is
# a multiple of the foot length (bottom_heel -> 5th_metatarsal). Measuring in
# foot lengths rather than pixels keeps the shin at the same anatomical height
# on both photo resolutions in the dataset. The circle is a UI aid only - it is
# never drawn on the exported image. The pixel radius actually used is logged
# per image in the CSV as shin_radius_px.
# --------------------------------------------------------------------------
MAGNET_TARGET = "shin"
MAGNET_CENTER = "bottom_heel"
MAGNET_REFERENCE = "5th_metatarsal"     # radius = factor x |center - reference|

MAGNET_FACTOR_DEFAULT = 1.00            # the median of the by-eye labels so far
MAGNET_FACTOR_MIN = 0.50
MAGNET_FACTOR_MAX = 2.50
MAGNET_FACTOR_STEP = 0.01

# Coordinates are whole pixels, so a point on the circle can sit up to ~0.7 px
# off the exact radius. The server rejects anything further than this.
MAGNET_TOLERANCE_PX = 1.5

if not (KEY_INDEX[MAGNET_CENTER] < KEY_INDEX[MAGNET_TARGET]
        and KEY_INDEX[MAGNET_REFERENCE] < KEY_INDEX[MAGNET_TARGET]):
    raise RuntimeError("the magnet target must be clicked after its centre and reference")

# Old column-name prefix -> current key, for reading CSVs written under the
# previous 4-point protocol. "LateralMalleolus" was always clicked at the
# bottom of the heel, so it maps straight onto bottom_heel. "PlatformEdge" has
# no entry: it is dropped when old rows are read.
LEGACY_KEY_ALIASES = {
    "Shin": "shin",
    "LateralMalleolus": "bottom_heel",
    "5thMetatarsal": "5th_metatarsal",
}

# CSV column order is a data contract and is deliberately independent of the
# click order, so changing how points are clicked never reshuffles columns in
# files that already exist. The angle columns are DERIVED values, recomputable
# from the points at any time. shin_radius_px is metadata: the magnet radius
# used for that image (blank for shins placed by eye before the magnet).
#   Image_Name, Side, shin_X, shin_Y, bottom_heel_X, bottom_heel_Y,
#   5th_metatarsal_X, 5th_metatarsal_Y, Angle_Degrees, Dorsiflexion_Degrees,
#   shin_radius_px
CSV_KEY_ORDER = ["shin", "bottom_heel", "5th_metatarsal"]
if sorted(CSV_KEY_ORDER) != sorted(KEY_INDEX):
    raise RuntimeError("CSV_KEY_ORDER must list exactly the LANDMARKS keys")

ANGLE_COLUMNS = ["Angle_Degrees", "Dorsiflexion_Degrees"]
RADIUS_COLUMN = "shin_radius_px"

# Provenance of the ground-truth angle for each image:
#   screen = photographed on the device display
#   manual = recorded by hand, not photographed
# Metadata only - never exported to DeepLabCut.
GT_SOURCE_COLUMN = "gt_source"
GT_SOURCE_VALUES = ("screen", "manual")
GT_SOURCE_DEFAULT = "manual"

CSV_COLUMNS = ["Image_Name", "Side"] + [
    key + axis for key in CSV_KEY_ORDER for axis in ("_X", "_Y")
] + ANGLE_COLUMNS + [RADIUS_COLUMN, GT_SOURCE_COLUMN]

ANGLE_DECIMALS = 4      # the points are integers, so this is exact enough
RADIUS_DECIMALS = 2

# --------------------------------------------------------------------------
# Alignment guides, drawn as connecting lines.
#
#   Foot axis  bottom_heel -> 5th_metatarsal, which must run PARALLEL to the
#              resting platform.
#   Leg axis   shin -> bottom_heel.
#
# These are exactly the two arms of the ankle angle. Served to the frontend
# from /api/config so the canvas and the export can never disagree.
# --------------------------------------------------------------------------
FOOT_AXIS_COLOR = (0, 255, 255)      # cyan
LEG_AXIS_COLOR = (255, 0, 255)       # magenta

GUIDE_LINES = (
    ("Foot axis", KEY_INDEX["bottom_heel"], KEY_INDEX["5th_metatarsal"], FOOT_AXIS_COLOR),
    ("Leg axis", KEY_INDEX["shin"], KEY_INDEX["bottom_heel"], LEG_AXIS_COLOR),
)

GUIDE_CASING = (0, 0, 0)        # dark casing under every line, for light backgrounds

# --------------------------------------------------------------------------
# Ankle angle.
#
# Vertex is ALWAYS bottom_heel. The leg vector points from there to shin, the
# foot vector to 5th_metatarsal. Clinical dorsiflexion is 90 - theta.
# --------------------------------------------------------------------------
ANGLE_VERTEX = KEY_INDEX["bottom_heel"]
ANGLE_ARM_LEG = KEY_INDEX["shin"]
ANGLE_ARM_FOOT = KEY_INDEX["5th_metatarsal"]

ARC_RADIUS = 50.0       # CSS px on the canvas; scaled to match on the export
ARC_COLOR = (255, 240, 0)            # neon yellow, distinct from both axes
ANGLE_TEXT_COLOR = (255, 255, 255)   # white with a black outline
ANGLE_TEXT_OUTLINE = (0, 0, 0)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="ROM Annotator", version="1.0.0")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def validate_side(side: str) -> str:
    side = (side or "").strip().lower()
    if side not in INPUT_DIRS:
        raise HTTPException(status_code=400, detail="Invalid side. Only 'left' is available.")
    return side


def safe_filename(filename: str) -> str:
    """Reject anything that could escape the input directory."""
    name = (filename or "").strip()
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if "/" in name or "\\" in name or ":" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    return name


def resolve_input_image(side: str, filename: str) -> Path:
    root = INPUT_DIRS[side].resolve()
    path = (root / safe_filename(filename)).resolve()
    if root not in path.parents:
        raise HTTPException(status_code=400, detail="Path traversal blocked.")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Image not found: " + filename)
    return path


def natural_key(name: str):
    """Sort so img2 comes before img10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def list_input_images(side: str) -> List[str]:
    root = INPUT_DIRS[side]
    if not root.is_dir():
        return []
    names = [
        p.name for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    ]
    return sorted(names, key=natural_key)


def side_output_root(side: str) -> Path:
    return BASE_OUTPUT_DIR / side


def today_output_dir(side: str) -> Path:
    """allignments/<side>/<YYYY-MM-DD>/ - created if missing."""
    out = side_output_root(side) / date.today().isoformat()
    out.mkdir(parents=True, exist_ok=True)
    return out


def today_csv_path(side: str) -> Path:
    return today_output_dir(side) / ("labels_" + date.today().isoformat() + ".csv")


def iter_label_rows(side: str):
    """Every row of every labels_*.csv for this side, oldest date folder first."""
    root = side_output_root(side)
    if not root.is_dir():
        return
    for csv_path in sorted(root.glob("*/labels_*.csv")):
        try:
            with csv_path.open("r", newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    if (row.get("Image_Name") or "").strip():
                        yield row
        except OSError:
            continue


def meets_protocol(row: Dict[str, str]) -> bool:
    """A row labelled under the CURRENT protocol: all keypoints plus a magnet radius."""
    for key in KEY_INDEX:
        if not (row.get(key + "_X") or "").strip() or not (row.get(key + "_Y") or "").strip():
            return False
    return bool((row.get(RADIUS_COLUMN) or "").strip())


def collect_annotated_names(side: str) -> set:
    """
    Images that are DONE: labelled under the current protocol in some CSV.

    A row only counts if it has every keypoint AND a shin_radius_px - i.e. the
    shin was placed with the magnet. Rows from the older by-eye protocol do not
    count, so those images come back into the queue to be redone; their old
    rows stay in their files untouched, and the newer row wins in
    build_training_csv.py.

    Marked-up image files in the date folders are deliberately NOT a done
    signal: an image with no protocol row must be annotated, whatever else is
    lying in a folder.
    """
    return {row["Image_Name"].strip() for row in iter_label_rows(side)
            if meets_protocol(row)}


def known_gt_sources(side: str) -> Dict[str, str]:
    """Newest recorded gt_source per image, so a re-annotation keeps its value."""
    out: Dict[str, str] = {}
    for row in iter_label_rows(side):
        value = (row.get(GT_SOURCE_COLUMN) or "").strip().lower()
        if value in GT_SOURCE_VALUES:
            out[row["Image_Name"].strip()] = value
    return out


def open_upright(path: Path) -> Image.Image:
    """
    Open an image already rotated per its EXIF orientation tag.

    This is the single source of truth for "the image as a human sees it".
    Everything downstream - the preview sent to the canvas, the coordinates in
    the CSV, and the annotated export - lives in this orientation.
    """
    with Image.open(path) as raw:
        return ImageOps.exif_transpose(raw)


_preview_cache: Dict[tuple, tuple] = {}


def build_preview(path: Path) -> tuple:
    """
    Return (jpeg_bytes, full_width, full_height) for the upright image.

    The bytes are downscaled for fast transfer; the reported size is the full
    upright resolution that the coordinates refer to.
    """
    key = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
    cached = _preview_cache.get(key)
    if cached is not None:
        return cached

    im = open_upright(path).convert("RGB")
    full_w, full_h = im.size

    longest = max(full_w, full_h)
    if longest > PREVIEW_MAX:
        ratio = PREVIEW_MAX / float(longest)
        im = im.resize(
            (max(1, int(round(full_w * ratio))), max(1, int(round(full_h * ratio)))),
            Image.LANCZOS,
        )

    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88)
    im.close()

    result = (buf.getvalue(), full_w, full_h)
    if len(_preview_cache) >= PREVIEW_CACHE_MAX:
        _preview_cache.pop(next(iter(_preview_cache)))
    _preview_cache[key] = result
    return result


def load_font(size: int):
    for candidate in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def ankle_angle(points: List[Dict[str, int]], image_height: int):
    """
    Angle at the ankle between the leg vector and the foot vector.

    Image y grows downward; the clinical convention is y up, so every y is
    flipped to y_math = image_height - y before the vectors are formed. The
    frontend runs this identical arithmetic so the live readout and the burned
    -in number can never disagree.

    Returns {"theta": deg, "dorsiflexion": 90 - deg} or None if a point is
    still missing.
    """
    needed = (ANGLE_VERTEX, ANGLE_ARM_LEG, ANGLE_ARM_FOOT)
    if any(i >= len(points) for i in needed):
        return None

    def mx(i):
        return float(points[i]["x"])

    def my(i):
        return float(image_height - points[i]["y"])

    # Vertex is always the ankle.
    ox, oy = mx(ANGLE_VERTEX), my(ANGLE_VERTEX)
    ux, uy = mx(ANGLE_ARM_LEG) - ox, my(ANGLE_ARM_LEG) - oy      # leg vector
    vx, vy = mx(ANGLE_ARM_FOOT) - ox, my(ANGLE_ARM_FOOT) - oy    # foot vector

    dot = (ux * vx) + (uy * vy)
    cross = (ux * vy) - (uy * vx)
    theta = abs(math.atan2(abs(cross), dot) * (180.0 / math.pi))

    return {"theta": theta, "dorsiflexion": 90.0 - theta}


def rects_overlap(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def draw_angle(im: Image.Image, draw: ImageDraw.ImageDraw,
               points: List[Dict[str, int]], scale: float, font,
               marker_arm: int) -> None:
    """Arc at the ankle plus the angle in degrees, placed clear of the dots."""
    ang = ankle_angle(points, im.height)
    if ang is None or len(points) < POINT_COUNT:
        return

    cx = float(points[ANGLE_VERTEX]["x"])
    cy = float(points[ANGLE_VERTEX]["y"])

    # Screen-space directions (y down) for drawing, not the flipped maths ones.
    def screen_angle(i):
        return math.atan2(points[i]["y"] - cy, points[i]["x"] - cx)

    a_leg = screen_angle(ANGLE_ARM_LEG)
    a_foot = screen_angle(ANGLE_ARM_FOOT)
    # Sweep the SHORT way round, which is the angle actually being measured.
    delta = (a_foot - a_leg + math.pi) % (2 * math.pi) - math.pi
    start, end = (a_leg, a_foot) if delta >= 0 else (a_foot, a_leg)

    r = ARC_RADIUS * scale
    core_w = max(2, int(round(2.5 * scale)))
    pad = max(1, int(round(1.5 * scale)))

    draw.arc(
        [cx - r - pad, cy - r - pad, cx + r + pad, cy + r + pad],
        math.degrees(start), math.degrees(end),
        fill=GUIDE_CASING, width=core_w + 2 * pad,
    )
    draw.arc(
        [cx - r, cy - r, cx + r, cy + r],
        math.degrees(start), math.degrees(end),
        fill=ARC_COLOR, width=core_w,
    )

    # Text along the bisector, pushed outwards until it clears every marker.
    def unit(i):
        dx, dy = points[i]["x"] - cx, points[i]["y"] - cy
        n = math.hypot(dx, dy) or 1.0
        return dx / n, dy / n

    lx, ly = unit(ANGLE_ARM_LEG)
    fx, fy = unit(ANGLE_ARM_FOOT)
    bx, by = lx + fx, ly + fy
    n = math.hypot(bx, by)
    if n < 1e-6:                      # arms opposite: bisector undefined
        bx, by, n = -ly, lx, 1.0
    bx, by = bx / n, by / n

    text = "%.1f°" % ang["theta"]
    tb = draw.textbbox((0, 0), text, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    stroke = max(2, int(round(2 * scale)))

    obstacles = [
        (p["x"] - marker_arm, p["y"] - marker_arm,
         p["x"] + marker_arm, p["y"] + marker_arm)
        for p in points
    ]

    step = max(4.0, 8.0 * scale)
    tx = ty = 0.0
    for i in range(40):
        px = cx + bx * (r * 1.45 + i * step)
        py = cy + by * (r * 1.45 + i * step)
        tx, ty = px - tw / 2.0, py - th / 2.0
        box = (tx - stroke, ty - stroke, tx + tw + stroke, ty + th + stroke)
        inside = (box[0] >= 0 and box[1] >= 0
                  and box[2] <= im.width and box[3] <= im.height)
        if inside and not any(rects_overlap(box, o) for o in obstacles):
            break

    tx = min(max(tx, stroke), im.width - tw - stroke)
    ty = min(max(ty, stroke), im.height - th - stroke)
    draw.text(
        (tx - tb[0], ty - tb[1]), text, font=font,
        fill=ANGLE_TEXT_COLOR, stroke_width=stroke, stroke_fill=ANGLE_TEXT_OUTLINE,
    )


def draw_legend(im: Image.Image, font, font_size: int, scale: float) -> Image.Image:
    """
    Colour key in the top-left, on a semi-transparent panel.

    The clinicians read the anatomy, not the annotation, so nothing is written
    beside the markers themselves. One small key in a corner says what each
    colour means and leaves the rest of the frame clear.
    """
    margin = max(6, int(round(14 * scale)))
    swatch = max(6, int(round(font_size * 0.62)))
    gap = max(4, int(round(font_size * 0.45)))        # swatch -> text
    line_gap = max(2, int(round(font_size * 0.34)))   # between rows
    inner = max(4, int(round(font_size * 0.5)))       # panel padding
    stroke = max(1, int(round(scale)))

    measure = ImageDraw.Draw(im)
    rows = []
    text_w = 0
    row_h = swatch
    # "dot" rows for the landmarks, then one "line" row so the guides are
    # explained by the same key rather than left unlabelled on the image.
    entries = [(label, colour, "dot") for _key, label, colour in LANDMARKS]
    entries.append(("Foot axis", FOOT_AXIS_COLOR, "line"))
    entries.append(("Leg axis", LEG_AXIS_COLOR, "line"))
    entries.append(("Ankle angle", ARC_COLOR, "line"))
    for label, colour, kind in entries:
        bbox = measure.textbbox((0, 0), label, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        rows.append((label, colour, bbox, w, h, kind))
        text_w = max(text_w, w)
        row_h = max(row_h, h)

    panel_w = inner * 2 + swatch + gap + text_w
    panel_h = inner * 2 + row_h * len(rows) + line_gap * (len(rows) - 1)

    overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle(
        [margin, margin, margin + panel_w, margin + panel_h],
        fill=(0, 0, 0, 145),                   # semi-transparent, not opaque
        outline=(255, 255, 255, 90),
        width=stroke,
    )

    y = margin + inner
    for label, colour, bbox, _w, h, kind in rows:
        cy = y + row_h / 2.0
        r = swatch / 2.0
        cx = margin + inner + r
        if kind == "line":
            od.line(
                [margin + inner, cy, margin + inner + swatch, cy],
                fill=colour + (255,),
                width=max(2, stroke * 2),
            )
        else:
            od.ellipse(
                [cx - r, cy - r, cx + r, cy + r],
                fill=colour + (255,),
                outline=(255, 255, 255, 220),
                width=stroke,
            )
        od.text(
            (margin + inner + swatch + gap, cy - h / 2.0 - bbox[1]),
            label,
            font=font,
            fill=(255, 255, 255, 255),
        )
        y += row_h + line_gap

    return Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")


def draw_annotations(src: Path, dst: Path, points: List[Dict[str, int]]) -> None:
    """Draw the landmarks + a corner legend onto an upright copy of the original."""
    with open_upright(src) as raw:
        im = raw.convert("RGB")

    draw = ImageDraw.Draw(im)

    scale = max(im.width, im.height) / 1000.0
    radius = max(4, int(round(7 * scale)))
    ring = max(1, int(round(2 * scale)))
    font_size = max(12, int(round(22 * scale)))
    font = load_font(font_size)
    arm = radius * 2

    # Guides BEFORE the markers, so a dot is never hidden under a line.
    core_w = max(2, int(round(2.5 * scale)))
    casing_w = core_w + 2 * max(1, int(round(1.5 * scale)))
    segments = [
        ([points[a]["x"], points[a]["y"], points[b]["x"], points[b]["y"]], colour)
        for _name, a, b, colour in GUIDE_LINES
        if a < len(points) and b < len(points)
    ]
    # Two passes: every casing first, so a casing never cuts across the bright
    # core of the segment next to it where they meet.
    for xy, _colour in segments:
        draw.line(xy, fill=GUIDE_CASING, width=casing_w)
    for xy, colour in segments:
        draw.line(xy, fill=colour, width=core_w)

    draw_angle(im, draw, points, scale, font, arm)

    # Markers only - no text beside them, by clinical request.
    for (_key, _label, colour), pt in zip(LANDMARKS, points):
        x, y = int(pt["x"]), int(pt["y"])
        draw.line([x - arm, y, x + arm, y], fill=colour, width=ring)
        draw.line([x, y - arm, x, y + arm], fill=colour, width=ring)
        draw.ellipse(
            [x - radius, y - radius, x + radius, y + radius],
            fill=colour,
            outline=(255, 255, 255),
            width=ring,
        )

    im = draw_legend(im, font, font_size, scale)

    dst.parent.mkdir(parents=True, exist_ok=True)
    save_kwargs = {}
    if dst.suffix.lower() in {".jpg", ".jpeg"}:
        save_kwargs["quality"] = 95
    im.save(dst, **save_kwargs)


def check_magnet(points: List[Dict[str, int]], radius: Optional[float],
                 frame_scale: float) -> float:
    """
    Enforce the shin magnet on the server. Returns the radius in full-image px.

    The browser snaps the shin onto the circle, but the rule is enforced here
    too: a stale or broken client must not be able to save a shin placed by
    eye into a dataset that is meant to be consistent.
    """
    if radius is None:
        raise HTTPException(
            status_code=422,
            detail="shin_radius_px is missing, so the shin was not placed with the "
                   "magnet. Nothing was saved. Hard-refresh the page (Ctrl+F5).",
        )
    radius = float(radius) * frame_scale          # same frame as the points
    center = points[KEY_INDEX[MAGNET_CENTER]]
    target = points[KEY_INDEX[MAGNET_TARGET]]
    reference = points[KEY_INDEX[MAGNET_REFERENCE]]

    foot = math.hypot(reference["x"] - center["x"], reference["y"] - center["y"])
    if foot < 1.0:
        raise HTTPException(status_code=422, detail=(
            "%s and %s are on the same pixel, so there is no foot length to size "
            "the magnet from. Nothing was saved." % (MAGNET_CENTER, MAGNET_REFERENCE)))

    factor = radius / foot
    if not (MAGNET_FACTOR_MIN - 0.005 <= factor <= MAGNET_FACTOR_MAX + 0.005):
        raise HTTPException(status_code=422, detail=(
            "Magnet radius is %.2f foot lengths, outside the allowed %.2f-%.2f. "
            "Nothing was saved." % (factor, MAGNET_FACTOR_MIN, MAGNET_FACTOR_MAX)))

    dist = math.hypot(target["x"] - center["x"], target["y"] - center["y"])
    if abs(dist - radius) > MAGNET_TOLERANCE_PX:
        raise HTTPException(status_code=422, detail=(
            "%s is %.1f px from %s but the magnet radius is %.1f px, so it is not "
            "on the circle. Nothing was saved." % (
                MAGNET_TARGET, dist, MAGNET_CENTER, radius)))
    return radius


def csv_header(csv_path: Path) -> Optional[List[str]]:
    """The header row of an existing CSV, or None if there is no file yet."""
    if not csv_path.exists():
        return None
    with csv_path.open("r", newline="", encoding="utf-8-sig") as fh:
        return next(csv.reader(fh), [])


def check_csv_schema(csv_path: Path) -> None:
    """
    Refuse to write into a CSV whose header is not the current protocol.

    Rows are appended positionally, so a 3-point row appended under an old
    4-point header would land in the wrong columns without any error. That is
    silent label corruption, so it is refused instead - and BEFORE the
    annotated image is written, or Resume would count the image as done.
    """
    header = csv_header(csv_path)
    if header is not None and header != CSV_COLUMNS:
        raise HTTPException(
            status_code=409,
            detail=(
                "%s was written under a different keypoint protocol and cannot "
                "be appended to safely. Nothing was saved. Move or rename that "
                "file, then save again." % csv_path.name
            ),
        )


def upsert_csv_row(csv_path: Path, row: Dict[str, str]) -> str:
    """
    Append the row, or replace it in place if this Image_Name already has a row
    in today's CSV (re-annotating in "Start Fresh" mode).
    Returns 'appended' or 'replaced'.
    """
    check_csv_schema(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    if not csv_path.exists():
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerow(row)
        return "appended"

    with csv_path.open("r", newline="", encoding="utf-8-sig") as fh:
        existing = list(csv.DictReader(fh))

    for idx, existing_row in enumerate(existing):
        if (existing_row.get("Image_Name") or "").strip() == row["Image_Name"]:
            existing[idx] = row
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(existing)
            return "replaced"

    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        csv.DictWriter(fh, fieldnames=CSV_COLUMNS).writerow(row)
    return "appended"


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class Point(BaseModel):
    x: float
    y: float


class SavePayload(BaseModel):
    filename: str
    side: str
    points: List[Point] = Field(..., min_length=POINT_COUNT, max_length=POINT_COUNT)
    # The upright dimensions the browser measured its clicks against. Lets the
    # server detect a client working in the wrong coordinate space (a stale
    # cached app.js, say) instead of silently writing bad landmarks.
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    # Magnet radius the shin was placed on, in the same frame as the points.
    shin_radius_px: Optional[float] = None
    # "screen" or "manual" - where this image's ground-truth angle came from.
    gt_source: Optional[str] = None


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.get("/api/config")
def get_config():
    return {
        "landmarks": [
            {"key": k, "label": lbl, "color": "rgb(%d,%d,%d)" % c}
            for k, lbl, c in LANDMARKS
        ],
        "guides": [
            {"name": name, "from": a, "to": b, "color": "rgb(%d,%d,%d)" % colour}
            for name, a, b, colour in GUIDE_LINES
        ],
        "angle": {
            "vertex": ANGLE_VERTEX,
            "arm_leg": ANGLE_ARM_LEG,
            "arm_foot": ANGLE_ARM_FOOT,
            "arc_radius": ARC_RADIUS,
            "arc_color": "rgb(%d,%d,%d)" % ARC_COLOR,
            "text_color": "rgb(%d,%d,%d)" % ANGLE_TEXT_COLOR,
        },
        "magnet": {
            "target": KEY_INDEX[MAGNET_TARGET],
            "center": KEY_INDEX[MAGNET_CENTER],
            "reference": KEY_INDEX[MAGNET_REFERENCE],
            "factor_default": MAGNET_FACTOR_DEFAULT,
            "factor_min": MAGNET_FACTOR_MIN,
            "factor_max": MAGNET_FACTOR_MAX,
            "factor_step": MAGNET_FACTOR_STEP,
            "tolerance_px": MAGNET_TOLERANCE_PX,
        },
        "gt_source": {
            "values": list(GT_SOURCE_VALUES),
            "default": GT_SOURCE_DEFAULT,
        },
        "input_dirs": {k: str(v) for k, v in INPUT_DIRS.items()},
        "output_dir": str(BASE_OUTPUT_DIR),
        "today": date.today().isoformat(),
    }


@app.get("/api/images")
def get_images(
    side: str = Query(..., description="left or right"),
    mode: str = Query("resume", description="resume or fresh"),
):
    side = validate_side(side)
    mode = (mode or "resume").strip().lower()
    if mode not in {"resume", "fresh"}:
        raise HTTPException(status_code=400, detail="mode must be 'resume' or 'fresh'.")

    input_dir = INPUT_DIRS[side]
    if not input_dir.is_dir():
        raise HTTPException(status_code=404, detail="Input folder does not exist: " + str(input_dir))

    all_images = list_input_images(side)
    done = collect_annotated_names(side)

    if mode == "resume":
        queue = [n for n in all_images if n not in done]
    else:
        queue = list(all_images)

    known = known_gt_sources(side)
    return {
        "side": side,
        "mode": mode,
        "input_dir": str(input_dir),
        "total_in_folder": len(all_images),
        "already_annotated": len(done & set(all_images)),
        "images": queue,
        # gt_source already on record for queued images (re-annotation keeps it)
        "gt_source": {n: known[n] for n in queue if n in known},
    }


@app.get("/api/image/{side}/{filename}")
def get_image(side: str, filename: str):
    """
    Serve the image already rotated upright, with its full upright size in the
    headers. Baking the rotation in means the browser has no EXIF flag left to
    interpret, so what the canvas shows always matches what the exporter draws.
    """
    side = validate_side(side)
    path = resolve_input_image(side, filename)
    data, full_w, full_h = build_preview(path)
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "X-Full-Width": str(full_w),
            "X-Full-Height": str(full_h),
            "Access-Control-Expose-Headers": "X-Full-Width, X-Full-Height",
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/save")
def save_annotation(payload: SavePayload):
    side = validate_side(payload.side)
    src = resolve_input_image(side, payload.filename)

    with open_upright(src) as im:
        width, height = im.size

    # Coordinates must arrive in UPRIGHT FULL-resolution image space. If the
    # client measured against a different frame (a stale cached app.js sending
    # preview-sized coordinates, for instance), rescale rather than write
    # landmarks that are quietly wrong - and say so in the response.
    note = None
    sx = sy = 1.0
    claimed_w, claimed_h = payload.image_width, payload.image_height
    if claimed_w and claimed_h and (claimed_w, claimed_h) != (width, height):
        claimed_aspect = claimed_w / float(claimed_h)
        actual_aspect = width / float(height)
        if abs(claimed_aspect - actual_aspect) > 0.02 * actual_aspect:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Coordinate space mismatch for %s: the browser measured "
                    "%dx%d but the upright image is %dx%d, and the shapes do "
                    "not match. Hard-refresh the page (Ctrl+F5) and redo this "
                    "image." % (src.name, claimed_w, claimed_h, width, height)
                ),
            )
        sx = width / float(claimed_w)
        sy = height / float(claimed_h)
        note = (
            "Client sent %dx%d coordinates for a %dx%d image; rescaled by "
            "%.3f. Hard-refresh the page (Ctrl+F5) to load the current app.js."
            % (claimed_w, claimed_h, width, height, sx)
        )

    points: List[Dict[str, int]] = []
    for p in payload.points:
        points.append({
            "x": int(round(min(max(p.x * sx, 0), width - 1))),
            "y": int(round(min(max(p.y * sy, 0), height - 1))),
        })

    radius = check_magnet(points, payload.shin_radius_px, (sx + sy) / 2.0)

    # Refused BEFORE anything is written, like the magnet: a blank or unknown
    # value would silently lose which images have a photographed angle.
    gt_source = (payload.gt_source or "").strip().lower()
    if gt_source not in GT_SOURCE_VALUES:
        raise HTTPException(status_code=422, detail=(
            "gt_source must be one of %s (got %r). Nothing was saved. If the page "
            "shows no Screen/Manual choice, hard-refresh it (Ctrl+F5)."
            % (", ".join(GT_SOURCE_VALUES), payload.gt_source)))

    # Check the CSV can take this row BEFORE drawing: an annotated image with
    # no matching CSV row would be skipped by Resume as if it were labelled.
    check_csv_schema(today_csv_path(side))

    dst = today_output_dir(side) / src.name

    try:
        draw_annotations(src, dst, points)
    except Exception as exc:  # noqa: BLE001 - surface the real reason to the UI
        raise HTTPException(status_code=500, detail="Failed to write annotated image: " + str(exc))

    row = {"Image_Name": src.name, "Side": side}
    for (key, _label, _colour), pt in zip(LANDMARKS, points):
        row[key + "_X"] = pt["x"]
        row[key + "_Y"] = pt["y"]

    ang = ankle_angle(points, height)
    row["Angle_Degrees"] = round(ang["theta"], ANGLE_DECIMALS) if ang else ""
    row["Dorsiflexion_Degrees"] = (
        round(ang["dorsiflexion"], ANGLE_DECIMALS) if ang else ""
    )
    row[RADIUS_COLUMN] = round(radius, RADIUS_DECIMALS)
    row[GT_SOURCE_COLUMN] = gt_source
    csv_path = today_csv_path(side)
    action = upsert_csv_row(csv_path, {k: str(v) for k, v in row.items()})

    return JSONResponse({
        "status": "ok",
        "csv_action": action,
        "note": note,
        "angle": ankle_angle(points, height),
        "shin_radius_px": round(radius, RADIUS_DECIMALS),
        "image_size": {"width": width, "height": height},
        "annotated_image": str(dst),
        "csv": str(csv_path),
        "row": row,
    })


NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


class NoCacheStaticFiles(StaticFiles):
    """
    Serve the frontend with caching disabled.

    A browser holding on to an old app.js measures clicks against the wrong
    frame and writes landmarks that look plausible but are wrong. For a
    single-user local tool the bandwidth is free and the correctness is not.
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:
        return False   # never answer 304

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.update(NO_CACHE)
        return response


@app.get("/v1")
def index_v1():
    """
    The retired 3-point (shin) page.

    Unlinked and no longer the landing page - the project moved to the 5-point
    scheme. Kept reachable only so the legacy tooling that reads its CSVs has a
    matching UI if it is ever needed.
    """
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)


# --------------------------------------------------------------------------
# The 5-point rig-referenced scheme ("v2_malleolus"), added alongside.
#
# Purely additive: nothing above this line changes behaviour. scheme_v2 owns
# its own keypoints, validation, output folder and master CSV, and never reads
# or writes any labels_*.csv, any dated 3-point folder, or the deeplabcut
# export. It imports nothing from this module - the shared image helpers are
# handed to it here, which is also what keeps the import acyclic.
# --------------------------------------------------------------------------
import scheme_v2      # noqa: E402 - imported here so the helpers above exist

scheme_v2.configure(
    open_upright=open_upright,
    resolve_input_image=resolve_input_image,
    validate_side=validate_side,
    list_input_images=list_input_images,
    natural_key=natural_key,
    load_font=load_font,
    base_output=BASE_OUTPUT_DIR,
)
app.include_router(scheme_v2.router)


# --------------------------------------------------------------------------
# RTMW-x malleolus auto-suggest (human-in-the-loop).
#
# Also additive. The model is loaded on a BACKGROUND thread at startup, not
# inline: the weights are ~330 MB on first run, and the server must come up
# immediately and keep working if rtmlib is absent or the download fails.
# Every failure path returns a status the UI quietly ignores.
# --------------------------------------------------------------------------
import pose_suggest      # noqa: E402

pose_suggest.configure(
    open_upright=open_upright,
    resolve_input_image=resolve_input_image,
    validate_side=validate_side,
)
app.include_router(pose_suggest.router)


@app.on_event("startup")
def _warm_pose_model():
    pose_suggest.warmup()


@app.get("/")
@app.get("/v2")
def index_v2():
    """The 5-point rig-referenced annotator - the only page the project uses."""
    return FileResponse(STATIC_DIR / "v2.html", headers=NO_CACHE)


app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    for _side, _path in INPUT_DIRS.items():
        if not _path.is_dir():
            print("[warn] input folder missing (%s): %s" % (_side, _path), file=sys.stderr)
    BASE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("ROM Annotator (5-point) -> http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
