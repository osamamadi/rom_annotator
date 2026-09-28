"""
Export the labels as a DeepLabCut labeled-data folder.

    python export_deeplabcut.py --scorer YOUR_DLC_EXPERIMENTER [--magnet-only]

Writes, per side:

    allignments/deeplabcut/labeled-data/rom_<side>/
        CollectedData_<scorer>.csv     DeepLabCut's 3-row header
        CollectedData_<scorer>.h5      only if PyTables is installed
        <image files>                  upright, with no EXIF rotation left

Why this is not just a copy of training_labels.csv:

* DeepLabCut needs a scorer / bodyparts / coords column MultiIndex with ONLY
  body parts in it. shin_radius_px, the angle and Side are metadata; any extra
  column would be read as another body part, so none of them are exported.
  They stay in training_labels.csv.
* The coordinates are in the upright (EXIF-applied) frame, but some libraries
  DeepLabCut uses to read images ignore the EXIF rotation flag. Each image is
  therefore written already upright with no rotation flag, and re-opened
  WITHOUT EXIF handling to prove its size matches the coordinate frame.

--scorer must equal `scorer:` in your DeepLabCut config.yaml (the experimenter
name given to create_new_project). Body parts are written in click order,
which must match `bodyparts:` in config.yaml.

If the .h5 is missing (no PyTables here) or unreadable in your DeepLabCut
environment, rebuild it there from the CSV:

    deeplabcut.convertcsv2h5(config_path, userfeedback=False)

Labelled rows whose photo has been deleted from the data folder are skipped with
a warning ("Skipped missing image: ...") rather than stopping the export.

--magnet-only exports just the images whose shin was placed with the magnet
(shin_radius_px filled in), for a strictly consistent training set.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

import app as backend
import build_training_csv

EXIF_ORIENTATION = 274
BODYPARTS = [key for key, _label, _colour in backend.LANDMARKS]   # click order


def folder_for(side: str) -> str:
    return "rom_" + side


def orientation(path: Path) -> int:
    from PIL import Image
    with Image.open(path) as im:
        return int((im.getexif() or {}).get(EXIF_ORIENTATION, 1) or 1)


def write_upright(src: Path, dst: Path) -> str:
    """Copy bytes if already upright; otherwise rotate and re-encode with no flag."""
    from PIL import Image, ImageOps
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return "kept"
    if orientation(src) == 1:
        shutil.copy2(src, dst)
        return "copied"
    with Image.open(src) as im:
        up = ImageOps.exif_transpose(im).convert("RGB")
    up.save(dst, format="JPEG", quality=95, subsampling=0)   # no EXIF written
    return "rotated"


def check_frame(dst: Path, w: int, h: int) -> None:
    """Open WITHOUT EXIF handling: the raw pixels must already be the label frame."""
    from PIL import Image
    with Image.open(dst) as im:
        raw = im.size
        flag = int((im.getexif() or {}).get(EXIF_ORIENTATION, 1) or 1)
    if raw != (w, h) or flag != 1:
        raise SystemExit("HALT: %s is %dx%d with orientation %d, but its labels are "
                         "for %dx%d upright. Export aborted." % (dst, raw[0], raw[1],
                                                                 flag, w, h))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scorer", required=True,
                    help="must equal `scorer:` in your DeepLabCut config.yaml")
    ap.add_argument("--magnet-only", action="store_true",
                    help="only images whose shin was placed with the magnet")
    ap.add_argument("--out", default=str(backend.BASE_OUTPUT_DIR / "deeplabcut"),
                    help="output folder (default: allignments/deeplabcut)")
    args = ap.parse_args()

    import pandas as pd
    try:
        import tables  # noqa: F401  - needed by pandas for .h5
        have_h5 = True
    except ImportError:
        have_h5 = False

    # Always export from a freshly merged, deduplicated table.
    print("refreshing training_labels.csv ...")
    if build_training_csv.main() != 0:
        return 1
    print()

    merged = backend.BASE_OUTPUT_DIR / build_training_csv.OUT_NAME
    with merged.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    by_side, skipped_incomplete, skipped_by_eye = {}, 0, 0
    for r in rows:
        if any(not r.get(b + axis) for b in BODYPARTS for axis in ("_X", "_Y")):
            skipped_incomplete += 1
            continue
        if args.magnet_only and not (r.get(backend.RADIUS_COLUMN) or "").strip():
            skipped_by_eye += 1
            continue
        by_side.setdefault(r["Side"], []).append(r)

    if not by_side:
        print("Nothing to export.")
        return 1

    out_root = Path(args.out) / "labeled-data"
    columns = pd.MultiIndex.from_arrays(
        [pd.Index([args.scorer] * (2 * len(BODYPARTS)), dtype=object),
         pd.Index([b for b in BODYPARTS for _ in (0, 1)], dtype=object),
         pd.Index(["x", "y"] * len(BODYPARTS), dtype=object)],
        names=["scorer", "bodyparts", "coords"])

    missing = []
    exported_any = False
    for side, side_rows in sorted(by_side.items()):
        folder = folder_for(side)
        dest = out_root / folder

        counts = {"copied": 0, "rotated": 0, "kept": 0}
        index, values = [], []
        for r in sorted(side_rows, key=lambda r: backend.natural_key(r["Image_Name"])):
            src = backend.INPUT_DIRS[side] / r["Image_Name"]
            if not src.is_file():
                # Photos are deleted on purpose (bad shots), so a labelled row
                # whose photo is gone is skipped, not fatal. Only rows whose
                # image is actually exported go into CollectedData.
                print("Skipped missing image: %s" % src.name)
                missing.append((side, src.name))
                continue
            dest.mkdir(parents=True, exist_ok=True)
            with backend.open_upright(src) as im:
                w, h = im.size
            dst = dest / src.name
            counts[write_upright(src, dst)] += 1
            check_frame(dst, w, h)

            index.append(("labeled-data", folder, src.name))
            values.append([float(r[b + axis]) for b in BODYPARTS for axis in ("_X", "_Y")])

        if not index:
            print("%s: no labelled photos left to export, nothing written" % side)
            continue
        exported_any = True

        idx = pd.MultiIndex.from_arrays(
            [pd.Index([t[i] for t in index], dtype=object) for i in range(3)])
        df = pd.DataFrame(values, index=idx, columns=columns)

        csv_path = dest / ("CollectedData_%s.csv" % args.scorer)
        df.to_csv(csv_path)

        # Round-trip exactly the way DeepLabCut reads it back.
        back = pd.read_csv(csv_path, header=[0, 1, 2], index_col=[0, 1, 2])
        if back.shape != df.shape or not (back.to_numpy() == df.to_numpy()).all() \
                or list(back.columns) != list(df.columns):
            raise SystemExit("HALT: %s did not read back identically." % csv_path)

        h5_note = "not written (PyTables not installed - see docstring)"
        if have_h5:
            h5_path = dest / ("CollectedData_%s.h5" % args.scorer)
            df.to_hdf(h5_path, key="df_with_missing", mode="w")
            if not pd.read_hdf(h5_path, "df_with_missing").equals(df):
                raise SystemExit("HALT: %s did not read back identically." % h5_path)
            h5_note = h5_path.name

        extra = sorted(p.name for p in dest.iterdir()
                       if p.suffix.lower() in backend.IMAGE_EXTENSIONS
                       and p.name not in {t[2] for t in index})

        print("%s -> %s" % (side, dest))
        print("  images    : %d  (%d copied, %d rotated upright, %d unchanged)"
              % (len(index), counts["copied"], counts["rotated"], counts["kept"]))
        print("  csv       : %s  (read back identically)" % csv_path.name)
        print("  h5        : %s" % h5_note)
        print("  frame     : every image re-opened without EXIF matches its labels")
        if extra:
            print("  NOTE      : %d image(s) in this folder are not in this export "
                  "(left over from an earlier run): %s" % (len(extra), ", ".join(extra[:5])))

    if missing:
        print("\nskipped %d labelled row(s) whose photo is no longer in the data folder"
              % len(missing))
    if not exported_any:
        print("Nothing exported: none of the labelled photos exist any more.")
        return 1
    if skipped_incomplete:
        print("\nskipped %d row(s) missing a keypoint" % skipped_incomplete)
    if args.magnet_only:
        print("skipped %d row(s) whose shin was placed by eye (--magnet-only)"
              % skipped_by_eye)

    print("\nIn config.yaml:")
    print("  scorer: %s" % args.scorer)
    print("  bodyparts:")
    for b in BODYPARTS:
        print("  - %s" % b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
