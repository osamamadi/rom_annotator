"""
Export the v2_malleolus annotations to COCO keypoint JSON for MMPose.

WHY THREE TASKS AND NOT ONE
    A top-down model crops to one bbox per instance. The rig points sit
    2500-5000 px apart while the foot is about 780 px, so a single box holding
    all five keypoints is essentially the whole 2316x3088 frame. HRNet at
    256x192 emits a 64x48 heatmap, which puts roughly 48 original pixels in one
    heatmap cell - around 12 px of localisation error after sub-pixel decoding,
    or about 0.9 deg of angle error. That is larger than the entire budget the
    5-point scheme exists to protect.

    A tight crop around the three anatomical points gives about 15 px per cell,
    so roughly 0.3 deg. Hence:

        foot   3 anatomical keypoints, TIGHT bbox    <- the accurate one
        rig    2 rig keypoints, full-frame bbox      <- ML fallback for the rig
        all5   all five, full-frame bbox             <- for comparison

    Each is a self-contained COCO dataset over the same images.

WHAT IT READS
    Only master_v2_malleolus.csv, and only rows whose scheme_version is
    "v2_malleolus". It never reads a labels_*.csv, a dated 3-point folder, or
    the deeplabcut export, and it never writes outside OUT_ROOT.

WHY THE IMAGES ARE RE-ENCODED
    cv2.imread ignores the EXIF orientation flag. Reading these photos raw
    would give a 3088x2316 landscape frame and every keypoint would land in the
    wrong place. Each image is therefore written out already rotated upright
    with the orientation tag cleared, which is the frame the CSV coordinates
    are in.

Run:  python export_coco.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter, OrderedDict
from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageOps

# --------------------------------------------------------------------------
# Configuration
#
# See app.py: ROM_ASSETS_DIR overrides where assets/ lives, default is ../assets
# next to this repo.
# --------------------------------------------------------------------------

ASSETS_ROOT = Path(os.environ.get("ROM_ASSETS_DIR", Path(__file__).resolve().parent.parent / "assets"))

BASE = ASSETS_ROOT / "annotations"
MASTER = BASE / "csv" / "master_v2_malleolus.csv"
OUT_ROOT = BASE / "coco"

# Only the left ankle is shot, so there is one flat raw-image folder, not a
# per-side split (see app.py).
INPUT_DIRS = {
    "left": ASSETS_ROOT / "raw",
}

# Subjects held out for validation. Subject-wise, because the same subject in
# both halves leaks anatomy between them and inflates the numbers.
VAL_SUBJECTS = ["adel"]

# Tight-bbox margin, as a fraction of that image's foot length on every side.
BBOX_MARGIN_FACTOR = 0.25

# OKS sigma per keypoint. Affects reported AP only, never training.
SIGMA = 0.035

JPEG_QUALITY = 95
SCHEME_VERSION = "v2_malleolus"
EXIF_ORIENTATION = 274

# Keypoint colours, matching the annotator so a rendered prediction reads the
# same way the annotation tool does.
COLOR = {
    "bottom_heel": [30, 190, 60],
    "5th_metatarsal": [40, 110, 255],
    "malleolus": [220, 30, 30],
    "rig_top": [255, 170, 0],
    "rig_bottom": [255, 170, 0],
}

FOOT_AXIS_COLOR = [0, 255, 255]
LEG_AXIS_COLOR = [255, 0, 255]
RIG_COLOR = [255, 170, 0]

TASKS = OrderedDict([
    ("foot", {
        "keypoints": ["bottom_heel", "5th_metatarsal", "malleolus"],
        "bbox": "tight",
        "skeleton": [("bottom_heel", "5th_metatarsal", FOOT_AXIS_COLOR),
                     ("bottom_heel", "malleolus", LEG_AXIS_COLOR)],
        "note": "tight crop around the three anatomical points",
    }),
    ("rig", {
        "keypoints": ["rig_top", "rig_bottom"],
        "bbox": "full",
        "skeleton": [("rig_top", "rig_bottom", RIG_COLOR)],
        "note": "full frame; the rig edge spans most of the image",
    }),
    ("all5", {
        "keypoints": ["bottom_heel", "5th_metatarsal", "malleolus",
                      "rig_top", "rig_bottom"],
        "bbox": "full",
        "skeleton": [("bottom_heel", "5th_metatarsal", FOOT_AXIS_COLOR),
                     ("bottom_heel", "malleolus", LEG_AXIS_COLOR),
                     ("rig_top", "rig_bottom", RIG_COLOR)],
        "note": "full frame; foot resolution suffers - included for comparison",
    }),
])

ALL_KEYS = ["bottom_heel", "5th_metatarsal", "malleolus", "rig_top", "rig_bottom"]

# Metadata carried through onto each annotation. MMPose ignores unknown keys,
# and these are what the evaluation needs afterwards.
EXTRA_FIELDS = ["subject_id", "side", "pair_id", "phase", "screen_angle",
                "capture_date", "annotation_date", "angle_deg", "rig_tilt_deg",
                "parallel_dev_deg", "foot_len_px", "leg_len_px", "rig_sep_px",
                "notes"]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def read_master():
    if not MASTER.is_file():
        sys.exit("No master file at %s - annotate something in the 5-point mode first."
                 % MASTER)
    with MASTER.open("r", newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("image") or "").strip()]
    keep = [r for r in rows if (r.get("scheme_version") or "").strip() == SCHEME_VERSION]
    skipped = len(rows) - len(keep)
    if skipped:
        print("Ignored %d row(s) that are not scheme_version %s" % (skipped, SCHEME_VERSION))
    return keep


def source_image(row):
    side = (row.get("side") or "left").strip().lower()
    folder = INPUT_DIRS.get(side)
    return (folder / row["image"].strip()) if folder else None


def write_upright(src: Path, dst: Path) -> tuple:
    """Write the image already rotated upright, with the orientation tag cleared."""
    with Image.open(src) as raw:
        im = ImageOps.exif_transpose(raw).convert("RGB")
        size = im.size
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, format="JPEG", quality=JPEG_QUALITY, subsampling=0)
    return size


def points_of(row):
    return {k: (float(row[k + "_x"]), float(row[k + "_y"])) for k in ALL_KEYS}


def bbox_for(spec, pts, row, width, height):
    if spec["bbox"] == "full":
        return [0.0, 0.0, float(width), float(height)]
    xs = [pts[k][0] for k in spec["keypoints"]]
    ys = [pts[k][1] for k in spec["keypoints"]]
    margin = BBOX_MARGIN_FACTOR * float(row["foot_len_px"])
    x0 = max(0.0, min(xs) - margin)
    y0 = max(0.0, min(ys) - margin)
    x1 = min(float(width), max(xs) + margin)
    y1 = min(float(height), max(ys) + margin)
    return [round(x0, 2), round(y0, 2), round(x1 - x0, 2), round(y1 - y0, 2)]


def dataset_info_text(name, spec):
    """The MMPose dataset_info block, generated so it cannot drift from the JSON."""
    keys = spec["keypoints"]
    kp = ",\n".join(
        "        %d:\n        dict(name='%s', id=%d, color=%s, type='lower', swap='')"
        % (i, k, i, COLOR[k]) for i, k in enumerate(keys))
    sk = ",\n".join(
        "        %d:\n        dict(link=('%s', '%s'), id=%d, color=%s)"
        % (i, a, b, i, c) for i, (a, b, c) in enumerate(spec["skeleton"])
        if a in keys and b in keys)
    return (
        '"""\n'
        "MMPose dataset_info for the ROM ankle '%s' task.\n\n"
        "%s\n\n"
        "GENERATED by export_coco.py on %s - do not hand-edit. The keypoint order\n"
        "here must match the COCO JSON exactly; editing one without the other\n"
        "fails silently rather than loudly.\n"
        '"""\n\n'
        "dataset_info = dict(\n"
        "    dataset_name='rom_ankle_%s',\n"
        "    paper_info=dict(\n"
        "        author='Osama Madi',\n"
        "        title='Markerless ankle ROM, rig-referenced 5-point scheme',\n"
        "        container='',\n"
        "        year='2026',\n"
        "        homepage='',\n"
        "    ),\n"
        "    keypoint_info={\n%s\n    },\n"
        "    skeleton_info={\n%s\n    },\n"
        "    joint_weights=[1.0] * %d,\n"
        "    sigmas=[%s] * %d,\n"
        ")\n"
    ) % (name, spec["note"], date.today().isoformat(), name, kp, sk,
         len(keys), SIGMA, len(keys))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    rows = read_master()
    if not rows:
        sys.exit("No %s rows in %s - nothing to export." % (SCHEME_VERSION, MASTER))

    rows.sort(key=lambda r: (r["image"].strip()))
    seen = Counter(r["image"].strip() for r in rows)
    dupes = sorted(n for n, c in seen.items() if c > 1)

    # ---- validate everything up front -----------------------------------
    problems, missing, plan = [], [], []
    if dupes:
        problems.append("duplicate rows for: %s" % ", ".join(dupes))

    for row in rows:
        name = row["image"].strip()
        src = source_image(row)
        if src is None:
            problems.append("%s: unknown side %r" % (name, row.get("side")))
            continue
        if not src.is_file():
            missing.append(name)          # deleted photos are expected; warn and skip
            continue
        try:
            pts = points_of(row)
            w, h = int(row["image_w"]), int(row["image_h"])
        except (KeyError, ValueError) as exc:
            problems.append("%s: unreadable row (%s)" % (name, exc))
            continue
        for k, (x, y) in pts.items():
            if not (0 <= x <= w and 0 <= y <= h):
                problems.append("%s: %s (%.1f, %.1f) outside 0..%d x 0..%d"
                                % (name, k, x, y, w, h))
        plan.append((row, src, pts, w, h))

    for name in missing:
        print("Skipped missing image: %s" % name)

    if problems:
        print("\nSTOP - nothing written. %d problem(s):" % len(problems))
        for p in problems[:25]:
            print("  -", p)
        return 1
    if not plan:
        print("No exportable rows left after skipping missing images - nothing written.")
        return 1

    known_subjects = {(r["subject_id"] or "").strip() for r, _s, _p, _w, _h in plan}
    for s in VAL_SUBJECTS:
        if s not in known_subjects:
            print("Warning: VAL_SUBJECTS lists %r, which no row uses. Known: %s"
                  % (s, sorted(x for x in known_subjects if x)))
    if "" in known_subjects:
        print("Warning: %d row(s) have a blank subject_id and will go to train."
              % sum(1 for r, _s, _p, _w, _h in plan if not (r["subject_id"] or "").strip()))

    out_dir = OUT_ROOT / date.today().isoformat()
    img_dir = out_dir / "images"
    ann_dir = out_dir / "annotations"

    # ---- images, written once and shared by all three tasks --------------
    image_meta = {}
    for i, (row, src, _pts, w, h) in enumerate(plan, start=1):
        name = row["image"].strip()
        got = write_upright(src, img_dir / name)
        if got != (w, h):
            print("\nSTOP: %s is %dx%d upright but the CSV row says %dx%d. The labels "
                  "would not match the pixels. Nothing further written." % (name, got[0], got[1], w, h))
            return 1
        image_meta[name] = {
            "id": i,
            "file_name": name,
            "width": w,
            "height": h,
            "date_captured": (row.get("capture_date") or "").strip(),
        }

    # ---- one COCO dataset per task per split ------------------------------
    ann_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    for task, spec in TASKS.items():
        keys = spec["keypoints"]
        splits = {"train": [], "val": []}
        for row, _src, pts, w, h in plan:
            subject = (row["subject_id"] or "").strip()
            splits["val" if subject in VAL_SUBJECTS else "train"].append((row, pts, w, h))

        for split, members in splits.items():
            images, annotations = [], []
            for row, pts, w, h in members:
                name = row["image"].strip()
                images.append(dict(image_meta[name]))
                flat = []
                for k in keys:
                    x, y = pts[k]
                    flat += [round(x, 2), round(y, 2), 2]      # v=2: labelled and visible
                box = bbox_for(spec, pts, row, w, h)
                ann = {
                    "id": len(annotations) + 1,
                    "image_id": image_meta[name]["id"],
                    "category_id": 1,
                    "iscrowd": 0,
                    "num_keypoints": len(keys),
                    "keypoints": flat,
                    "bbox": box,
                    "area": round(box[2] * box[3], 2),
                    "segmentation": [],
                }
                for f in EXTRA_FIELDS:
                    ann[f] = (row.get(f) or "").strip()
                annotations.append(ann)

            doc = {
                "info": {
                    "description": "ROM ankle, %s task (%s)" % (task, spec["note"]),
                    "scheme_version": SCHEME_VERSION,
                    "split": split,
                    "val_subjects": VAL_SUBJECTS,
                    "bbox_mode": spec["bbox"],
                    "bbox_margin_factor": BBOX_MARGIN_FACTOR if spec["bbox"] == "tight" else None,
                    "date_created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source": str(MASTER),
                },
                "licenses": [],
                "images": images,
                "annotations": annotations,
                "categories": [{
                    "id": 1,
                    "name": "rom_ankle_%s" % task,
                    "supercategory": "person",
                    "keypoints": keys,
                    "skeleton": [[keys.index(a) + 1, keys.index(b) + 1]
                                 for a, b, _c in spec["skeleton"]
                                 if a in keys and b in keys],
                }],
            }
            path = ann_dir / ("%s_%s.json" % (task, split))
            with path.open("w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=1)
            summary.append((task, split, len(images), len(annotations), path))

        (out_dir / ("dataset_info_%s.py" % task)).write_text(
            dataset_info_text(task, spec), encoding="utf-8")

    # ---- report -----------------------------------------------------------
    subjects = Counter((r["subject_id"] or "(blank)").strip() for r, _s, _p, _w, _h in plan)
    print("\n=== COCO export ===")
    print("source          : %s" % MASTER)
    print("written to      : %s" % out_dir)
    print("rows exported   : %d   (%d missing image(s) skipped)" % (len(plan), len(missing)))
    print("subjects        : %s" % dict(subjects))
    print("val subjects    : %s" % VAL_SUBJECTS)
    print("images written  : %d upright JPEG q%d, EXIF orientation cleared"
          % (len(image_meta), JPEG_QUALITY))
    print("\n  %-6s %-6s %7s %7s  %s" % ("task", "split", "images", "anns", "file"))
    for task, split, ni, na, path in summary:
        print("  %-6s %-6s %7d %7d  %s" % (task, split, ni, na, path.name))
    print("\ndataset_info    : %s" % ", ".join("dataset_info_%s.py" % t for t in TASKS))
    print("sigmas          : %s uniform" % SIGMA)
    return 0


if __name__ == "__main__":
    sys.exit(main())
