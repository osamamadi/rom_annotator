"""
Build one deduplicated training CSV from every per-date labels_*.csv.

The per-date files stay exactly as they are - they are the record of what was
annotated when. This writes a single derived file:

    allignments/training_labels.csv

Dedup rule, per (Side, Image_Name):
    1. prefer a row with every keypoint present,
    2. among those, the one from the newest date folder.
A row that is missing a landmark is only used if nothing better exists, and is
reported so it can be redone.

The angle columns are recomputed from the stored points rather than trusted, so
an older row written before the angle existed still comes out complete. The
image height cancels out of the angle formula, so no image needs to be opened.

Rows written under the previous 4-point protocol are read through
app.LEGACY_KEY_ALIASES (LateralMalleolus -> bottom_heel, and so on), and their
PlatformEdge columns are dropped. The output is always the current protocol.

Run:  python build_training_csv.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import app as backend

OUT_NAME = "training_labels.csv"
POINT_KEYS = [k for k, _l, _c in backend.LANDMARKS]

# Where the row came from, kept so a suspect value can be traced back.
PROVENANCE = ["Source_File"]
OUT_COLUMNS = backend.CSV_COLUMNS + PROVENANCE


LEGACY_PREFIXES = {}
for _old, _new in backend.LEGACY_KEY_ALIASES.items():
    LEGACY_PREFIXES.setdefault(_new, []).append(_old)


def coord(row, key, axis):
    """A coordinate under its current column name, else under a legacy one."""
    value = row.get(key + axis)
    if value not in (None, ""):
        return value
    for old in LEGACY_PREFIXES.get(key, ()):
        value = row.get(old + axis)
        if value not in (None, ""):
            return value
    return None


def read_points(row):
    """One (x, y) per keypoint, or None if any coordinate is missing/unparsable."""
    pts = []
    for key in POINT_KEYS:
        sx, sy = coord(row, key, "_X"), coord(row, key, "_Y")
        if sx in (None, "") or sy in (None, ""):
            return None
        try:
            pts.append({"x": float(sx), "y": float(sy)})
        except ValueError:
            return None
    return pts


def collect():
    """Every row in every per-date CSV, tagged with its date folder."""
    found = []
    for csv_path in sorted(backend.BASE_OUTPUT_DIR.glob("*/*/labels_*.csv")):
        date_dir = csv_path.parent.name          # YYYY-MM-DD, sorts lexically
        rel = csv_path.relative_to(backend.BASE_OUTPUT_DIR).as_posix()
        try:
            with csv_path.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    name = (row.get("Image_Name") or "").strip()
                    if name:
                        found.append((date_dir, rel, row))
        except OSError as exc:
            print("  ! could not read %s: %s" % (rel, exc), file=sys.stderr)
    return found


def main() -> int:
    rows = collect()
    if not rows:
        print("No labels_*.csv found under %s" % backend.BASE_OUTPUT_DIR)
        return 1

    best = {}
    superseded = 0
    for date_dir, rel, row in rows:
        side = (row.get("Side") or "").strip().lower()
        key = (side, (row.get("Image_Name") or "").strip())
        pts = read_points(row)
        # (complete?, date) - a complete row always beats an incomplete one.
        rank = (pts is not None, date_dir)
        if key in best:
            superseded += 1
            if rank <= best[key][0]:
                continue
        best[key] = (rank, rel, row, pts)

    out_rows = []
    incomplete = []
    for (side, name), (_rank, rel, row, pts) in sorted(best.items()):
        out = {c: (row.get(c) or "") for c in backend.CSV_COLUMNS}
        # Keypoint columns resolve through the legacy aliases, so an old
        # 4-point row lands in the current columns instead of coming out blank.
        for key in POINT_KEYS:
            for axis in ("_X", "_Y"):
                out[key + axis] = coord(row, key, axis) or ""
        out["Image_Name"] = name
        out["Side"] = side
        out["Source_File"] = rel
        if pts is None:
            out["Angle_Degrees"] = ""
            out["Dorsiflexion_Degrees"] = ""
            incomplete.append((side, name, rel))
        else:
            # Height cancels out of the formula, so any value works here.
            ang = backend.ankle_angle(pts, 1000)
            out["Angle_Degrees"] = round(ang["theta"], backend.ANGLE_DECIMALS)
            out["Dorsiflexion_Degrees"] = round(
                ang["dorsiflexion"], backend.ANGLE_DECIMALS)
        out_rows.append(out)

    dst = backend.BASE_OUTPUT_DIR / OUT_NAME
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(out_rows)

    per_side = {}
    for r in out_rows:
        per_side[r["Side"]] = per_side.get(r["Side"], 0) + 1

    print("read      : %d row(s) across %d per-date file(s)"
          % (len(rows), len({r[1] for r in rows})))
    print("superseded: %d duplicate row(s) dropped" % superseded)
    print("written   : %s" % dst)
    print("            %d unique image(s)  %s"
          % (len(out_rows),
             ", ".join("%s=%d" % kv for kv in sorted(per_side.items()))))
    magnet = sum(1 for r in out_rows if str(r.get(backend.RADIUS_COLUMN) or "").strip())
    print("            shin by magnet: %d   shin by eye (no %s): %d"
          % (magnet, backend.RADIUS_COLUMN, len(out_rows) - magnet))
    if incomplete:
        print("\nINCOMPLETE - missing a keypoint, no angle written (redo these):")
        for side, name, rel in incomplete:
            print("   %-6s %-20s (%s)" % (side, name, rel))
    else:
        print("            every row has all %d keypoints and an angle"
              % backend.POINT_COUNT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
