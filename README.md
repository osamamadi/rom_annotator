# rom-annotation

A lightweight local web app for manually marking three keypoints on each image, clicked
in this order:

1. **`bottom_heel`** (green)
2. **`5th_metatarsal`** (blue)
3. **`shin`** (red) - snapped onto a circle around `bottom_heel` by the
   [shin magnet](#shin-magnet)

These protocol names are used verbatim everywhere: the sidebar, the legend on the
exported image, and the CSV columns.

FastAPI backend + vanilla HTML/JS/CSS frontend with an HTML5 canvas. Everything runs
locally against the file system — no upload, no database.

This is one of two repos in the ROM project — see the [project-level README](../README.md)
for how this fits together with `rom-calc` and the shared `assets/`/`research/` folders.

## Project structure

```
rom-annotation/
├── app.py              FastAPI backend: file listing, resume logic, image + CSV export
├── build_training_csv.py   merges every per-date CSV into one deduplicated file
├── export_coco.py          writes COCO keypoint JSON for MMPose (v2_malleolus scheme)
├── export_deeplabcut.py    writes a DeepLabCut labeled-data folder (legacy 3-point tooling)
├── pose_suggest.py         RTMW-x auto-suggest for the malleolus point (optional)
├── scheme_v2.py            the 3-point v2_malleolus annotation scheme
├── requirements.txt
├── README.md
└── static/
    ├── index.html      UI shell
    ├── style.css / v2.css   Styling
    └── app.js / v2.js       Canvas rendering, click sequencing, API calls
```

## Paths

`app.py` resolves an **assets root** and reads/writes everything relative to it:

| Purpose      | Path (relative to assets root)  |
| ------------ | -------------------------------- |
| Input left   | `raw/left`                       |
| Input right  | `raw/right`                      |
| Base output  | `annotations/`                   |

By default the assets root is `../assets` — i.e. `rom-annotation/` and `assets/` are
expected to be sibling folders (as they are in this project's layout). If your copy of
`assets/` lives somewhere else (e.g. a different Drive-synced path), set the
`ROM_ASSETS_DIR` environment variable to that folder before running `app.py`, and the
other scripts (`export_coco.py`, etc., which import `app.py`'s config) will follow it too.

## Install

From this repo's root:

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

```bash
pip install -r requirements.txt
```

(Installing globally with `pip install -r requirements.txt` works too — the venv is
just tidier.)

## Run

```bash
python app.py
```

Then open <http://127.0.0.1:8000>. Stop with `Ctrl+C`.

Alternatively, with auto-reload during development:

```bash
uvicorn app:app --reload --port 8000
```

Two UIs are served:

| Route | Scheme | Status |
| ----- | ------ | ------ |
| `/v2` | 5-point, rig-referenced (`v2_malleolus`) | **current — use this one** |
| `/` | 3-point (`shin` placed by eye) | retired, kept for reference only |

## v2_malleolus scheme (current)

The active annotation workflow, served at `/v2` and implemented in `scheme_v2.py`
(wired into `app.py` via `scheme_v2.configure(...)`, sharing only the generic image
helpers — it never reads or writes anything the 3-point scheme touches).

**Why it replaced the 3-point scheme:** in the old scheme the leg axis ran to a `shin`
point placed by eye on the lower leg, with no anatomical landmark under it — that
judgement call was the dominant source of error. Here the leg axis is instead
constructed from two points on the physically-vertical edge of the measurement rig, so
camera tilt cancels out of the angle by construction instead of by eye. See the module
docstring at the top of `scheme_v2.py` for the full error-budget derivation.

**Click order:** `rig_top`, `rig_bottom` (the two rig-edge points — click these first,
since `bottom_heel` is constructed from them), `malleolus`, `bottom_heel`,
`5th_metatarsal`.

**Output** — everything lands flat in two folders under `assets/annotations/` (no
per-date subfolders — every session writes into the same place, keyed by filename):

```
assets/annotations/
├── photos/     annotated export of every image, flat (<filename>.jpeg)
└── csv/
    └── master_v2_malleolus.csv   one row per image, replaced in place on re-annotation
```

A save can be **refused** (rig points too close together, `bottom_heel` off the
construction line, etc.) or can come back with **warnings** that the UI must show you
and you must explicitly acknowledge before it's allowed to save (rig separation on the
low side, rig tilt, an implausible angle) — see `validate()` in `scheme_v2.py` for the
exact thresholds and the reasoning behind each one.

API: `GET /api/v2/config`, `GET /api/v2/images?side=left&mode=resume`,
`POST /api/v2/save`. Interactive docs at <http://127.0.0.1:8000/docs>.

## Retired: 3-point scheme (`/`)

The sections below (How to use, Output, Building the training set, DeepLabCut export,
Shin magnet, Protocol history) describe the **original 3-point tool**, superseded by
`v2_malleolus` above. Its route (`/`), API, and export scripts (`build_training_csv.py`,
`export_deeplabcut.py`) are left in the codebase working and documented, not deleted —
but there is no data left under this protocol to resume, and new annotation should go
through `/v2`.

### How to use (retired 3-point scheme)

1. Pick **Side** (only `left` is available — see [Paths](#paths)).
2. Pick a mode:
   - **Resume Existing Work** — skips every image that is **done**: it has a complete
     row *with a magnet radius* in some `labels_*.csv`, in any date folder. Rows from the
     older by-eye protocol do not count, so those images come back to be redone; stray
     image files in a date folder do not count either.
   - **Start Fresh** — loads every image in the folder; re-saving an image overwrites
     its annotated copy and replaces its row in today's CSV.
3. Click **Load Images**. The queue count appears next to the button.
4. Click the image **exactly three times**, in order: `bottom_heel`, `5th_metatarsal`,
   then `shin`. The numbered sidebar list shows which keypoint is next; a 4th click is
   rejected and Save stays disabled until all three are placed. After the second click
   a dashed circle appears - click near the **back edge of the leg** and the shin snaps
   onto it (see [Shin magnet](#shin-magnet)).
5. **Adjust any point by dragging it.** Hover a placed dot — the cursor turns into a
   grab hand — then drag it; the sidebar coordinates update live. Dragging the **shin**
   slides it along the circle, never off it. Dragging `bottom_heel` or `5th_metatarsal`
   resizes and moves the circle, and the shin follows, keeping its direction.
6. **Save & Next** writes the output and advances. **Reset Clicks** clears the current
   image, **Undo Last Point** removes just the last one, **Skip Image** moves on without
   saving.

On the canvas each dot is labelled so you can tell them apart while working, and those
labels never print on top of each other: each is placed in the first of eight positions
around its dot that collides with nothing, falling back to a vertical search when the
area is crowded.

**The exported image carries no text beside the markers.** Clinicians read the anatomy,
not the annotation, so the export shows three bare markers, the alignment guides, and a
small colour key on a semi-transparent panel in the top-left corner. The panel is
translucent, so the image underneath still shows through.

### Alignment guides

Two clinical checks are drawn as connecting lines, on the canvas and burned into the
export:

| Guide | Colour | Segments | What it checks |
| ----- | ------ | -------- | -------------- |
| **Foot axis** | cyan `#00FFFF` | `bottom_heel` &rarr; `5th_metatarsal` | must run **parallel** to the resting platform |
| **Leg axis** | magenta `#FF00FF` | `shin` &rarr; `bottom_heel` | the shin line |

The two guides are exactly the two arms of the ankle angle.

The segments are defined once in `GUIDE_LINES` in `app.py` and served to the browser
from `/api/config`, so the canvas and the exporter cannot disagree about them.

Each is a thin bright core over a dark casing, which keeps them legible on pale skin,
dark skin, bare wood and the yellow platform tape alike. Guides are drawn **underneath**
the markers, so a dot is never hidden by a line, and on the canvas each segment appears
the moment both of its endpoints are placed — the foot axis after the 2nd click, the leg
axis after the 3rd. Dragging a point redraws them live.

### Ankle angle

Once all three points are placed, the angle at the ankle is measured, drawn as an arc,
and shown in the sidebar as `Ankle Angle: 117.1°  |  DF: -27.1°`.

The vertex is **always** `bottom_heel`. Image `y` grows downward while the
clinical convention is `y` up, so every `y` is flipped to `y_math = image_height - y`
before the vectors are formed:

```
u   = (shin_x - heel_x,            shin_y_math - heel_y_math)            # leg vector
v   = (5th_metatarsal_x - heel_x,  5th_metatarsal_y_math - heel_y_math)  # foot vector
dot   = u_x*v_x + u_y*v_y
cross = u_x*v_y - u_y*v_x
theta = abs(atan2(abs(cross), dot) * 180/pi)
DF    = 90 - theta
```

`abs(cross)` makes the result unsigned, so a foot pointing left and one pointing right
give the same angle. `ankle_angle()` in `app.py` and `ankleAngle()` in `app.js` run this
identical arithmetic, and the save response returns the server's value so the two can be
compared directly — they agree to full float precision.

The arc is neon yellow over a dark casing, at a 50px radius (scaled proportionally on the
export so it looks the same as on screen). It sweeps the short way round, which is the
angle actually being measured. The reading is drawn in white with a black outline, placed
along the angle bisector and pushed outwards until it clears every marker.

The angle is written to the CSV as `Angle_Degrees` and `Dorsiflexion_Degrees`, and
returned by `POST /api/save` as `angle`. It is a derived value: the points remain the
source of truth, and `build_training_csv.py` recomputes rather than trusts it.

A magnifier follows the cursor (toggle in the controls bar) so landmarks can be placed
precisely even when the image is scaled down to fit — necessary here, since a 24MP photo
displays at roughly 13%, where one screen pixel covers about eight image pixels. It
samples the full-resolution preview rather than the shrunken canvas, so magnifying
reveals real detail, and it stays active while dragging.

### Keyboard shortcuts

| Key | Action |
| --- | --- |
| `Enter` | Save & Next |
| `R` | Reset clicks |
| `S` | Skip image |
| `Backspace` | Undo last point |
| drag a dot | Move that point (the shin stays on its circle) |
| scroll over the image | Magnet radius &plusmn;0.01 foot lengths (`Shift` &plusmn;0.05) |

## Output (retired 3-point scheme)

Saving creates, for example, `assets\annotations\left\2026-09-08\` (directories are
created automatically — this is the legacy per-side/per-date layout; the current
`v2_malleolus` scheme writes flat, see above) containing:

- **`<original filename>`** — a copy of the source image with the three dots,
  crosshairs, alignment guides and the angle arc + reading burned in, plus the
  colour-key legend in the top-left. No per-point text.
- **`labels_2026-09-08.csv`** — one row per annotated image:

```
Image_Name,Side,shin_X,shin_Y,bottom_heel_X,bottom_heel_Y,5th_metatarsal_X,5th_metatarsal_Y,Angle_Degrees,Dorsiflexion_Degrees,shin_radius_px
IMG_0410.jpeg,left,3462,2613,3359,4172,2348,3951,81.4493,8.5507,1562.66,manual
```

`gt_source` records where the image's ground-truth angle came from: `screen` (photographed
on the device display) or `manual` (recorded by hand). Pick it per image in the sidebar's
**Ground-truth angle source** panel. It defaults to `manual` for a new image, and to the
value already on record when an image is re-annotated. Saves without a valid value are
refused. Like `shin_radius_px` it is metadata: it is kept in `training_labels.csv` and
never exported to DeepLabCut.

`shin_radius_px` is metadata: the magnet radius, in full-image pixels, that the shin was
placed on. Divided by the foot length (`bottom_heel` &rarr; `5th_metatarsal`) it gives
the factor used. It is **blank** for shins placed by eye before the magnet existed.

Column order is a fixed data contract and is independent of the click order, so changing
how points are clicked never reshuffles columns in existing files.

`Angle_Degrees` and `Dorsiflexion_Degrees` are **derived** - stored for convenience, but
recomputable from the three points at any time (the image height cancels out of the
formula, so no image needs to be opened to recover them).

**Coordinates are in original image pixels**, not canvas pixels. The frontend divides
the click position by the display scale factor, and the backend clamps the result to the
real image dimensions before drawing and writing.

### Photo rotation (EXIF)

Phone photos are usually stored sideways with an EXIF flag telling viewers how to rotate
them. Browsers apply that flag; PIL does not. Left unhandled, that mismatch puts every
exported point in the wrong place.

The server therefore normalises each image to its **upright** orientation
(`ImageOps.exif_transpose`) in one place, `open_upright()`, and everything derives from
it:

- `/api/image/...` serves the image with the rotation already baked in and no EXIF flag
  left, so no browser can rotate it a second time. It also sends the full upright size in
  the `X-Full-Width` / `X-Full-Height` headers, and downscales the transferred bytes
  (longest side 2400px) so 24MP photos load quickly.
- The canvas maps clicks against those full upright dimensions, so a click maps to full
  resolution regardless of the preview size.
- The exported copy and the CSV are in that same upright frame.

The result: what you see on the canvas, what lands in the CSV, and what is drawn on the
exported image are all the same coordinate space.

### Coordinate-space guard

Wrong landmarks are worse than an obvious crash, because they look plausible in the CSV.
Two safeguards make a mismatch impossible to write silently:

- **The frontend is served with caching disabled** (`no-store`, and conditional requests
  never get a `304`). A browser holding a stale `app.js` would measure clicks against the
  wrong frame; now it cannot hold one.
- **Every save states the frame it measured against** (`image_width` / `image_height`).
  If that does not match the upright image, the server rescales the coordinates and
  returns a warning shown in the sidebar; if the aspect ratio does not even match, the
  save is rejected with a `400` and nothing is written.

If you ever see a warning about a rescale, hard-refresh with `Ctrl+F5` and redo that
image.

If you re-annotate an image on the same day, its existing CSV row is replaced rather
than duplicated; on a different day a new dated folder and CSV are created.

## Building the training set (retired 3-point scheme)

> As of this handover, the historical 3-point per-date folders/CSVs have been removed
> (superseded by `v2_malleolus`, see above) — running this now starts from a clean
> slate and produces nothing until new 3-point annotations exist.

The per-date files are the record of what was annotated when, so an image redone on a
later day appears in more than one of them. To get a single clean table:

```bash
python build_training_csv.py
```

That writes `assets/annotations/training_labels.csv`: one row per image, keeping the newest
**complete** row, with the angle recomputed from the points and a `Source_File` column
so any value can be traced back. It reports anything still missing a landmark. Re-run it
whenever you have annotated more; it only reads the per-date files and never edits them.

It also reports how many shins were placed with the magnet and how many by eye.

> **Train on the originals in `assets/raw/`, not on the images in `assets/annotations/`.** The
> exported copies have the landmarks, guides and angle painted onto them - the answer is
> drawn on the input. Pair the original photos with the coordinates from the CSV.

## DeepLabCut export

`training_labels.csv` is a flat, one-row-per-image table. It is **not** DeepLabCut
format. To produce a DeepLabCut `labeled-data` folder:

```bash
python export_deeplabcut.py --scorer YOUR_DLC_EXPERIMENTER
```

`--scorer` must equal `scorer:` in your DeepLabCut `config.yaml` (the experimenter name
given to `create_new_project`). It writes `assets/annotations/deeplabcut/labeled-data/rom_left/`
containing `CollectedData_<scorer>.csv` with DeepLabCut's scorer / bodyparts / coords
header, and the images. Body parts are written in click order - put the same list in
`config.yaml`:

```yaml
bodyparts:
- bottom_heel
- 5th_metatarsal
- shin
```

Details that matter:

- **Only the three body parts are exported.** `shin_radius_px`, the angle and `Side` stay
  in `training_labels.csv`; any extra column in a `CollectedData` file would be read by
  DeepLabCut as another body part.
- **Images are written upright with no EXIF rotation flag.** The photos are stored
  sideways with a rotation flag and the labels are in the upright frame, but some image
  readers DeepLabCut uses ignore that flag. Every exported image is re-opened **without**
  EXIF handling and checked against its label frame; the export halts on any mismatch.
- **The `.h5` needs PyTables.** If `tables` is not installed only the CSV is written; build
  the `.h5` inside your DeepLabCut environment with
  `deeplabcut.convertcsv2h5(config_path, userfeedback=False)`, which also guarantees it
  matches that environment's pandas.
- `--magnet-only` exports only images whose shin was placed with the magnet, for a
  strictly consistent training set.

## Shin magnet

Placing the shin by eye gave inconsistent heights. The magnet constrains it to a circle
centred on `bottom_heel`:

```
radius = factor x foot length          foot length = |bottom_heel -> 5th_metatarsal|
```

**Why foot lengths, not pixels.** The dataset mixes 4284&times;5712 and 2316&times;3088
photos. A fixed pixel radius would be 9.6% of image height on one and 17.8% on the other -
a different anatomical height. Measured in foot lengths it is the same on both. The
default factor **1.00** is the median of the shins previously placed by eye (1.04 and 1.00
on the two photo sizes). Change it with the sidebar slider or by scrolling over the image;
the value is remembered between sessions.

**Placing the shin.** After `bottom_heel` and `5th_metatarsal`, hover near the back edge of
the leg. A hollow red ring previews exactly where a click will put the shin, and the
magnifier centres on that spot rather than on the cursor:

- **solid ring** - it has locked onto the leg's edge;
- **dashed ring** - no clear edge nearby, so the shin goes where your click's direction
  meets the circle. The status line says which happened after every click.

**How the edge is found.** Samples are taken along the circle within &plusmn;25 screen px of
the click. Each position is scored by the difference in *average* brightness between the
12 samples before it and the 12 after - a sustained skin-to-background change - rather
than a local gradient, which hair and skin texture also produce. A snap is taken only
when the winning edge is at least **twice** as strong as any other edge in the window.

Checked against all 37 labelled photos, the rule was confident on 32 and, on every one of
those, landed on the posterior skin edge - hairy and pale legs alike. It declined on the
rest rather than guess: the four photos whose by-eye shin sits near the knee (where the
circle crosses thigh or clothing), and IMG_0454, a very low-contrast pale leg. On pale
skin it can also decline when you click from the skin side; clicking just outside the
leg usually lets it lock on. **Always glance at the ring and the magnifier.** Untick
*Snap to leg edge* to place purely along the click direction.

**Enforced on the server, not just the page.** Every save sends `shin_radius_px`, and the
server refuses (`422`, nothing written) a save that has no radius, whose shin is more
than 1.5 px off that circle, whose factor is outside 0.50-2.50, or whose heel and 5th
metatarsal coincide. A stale or broken page cannot slip a by-eye shin into the dataset.
Coordinates are whole pixels, so the stored shin sits within about 0.7 px of the exact
radius.

**UI only.** The circle, the ring and the snap are never drawn into the exported image.

## Protocol history and older labels

Labels written before 2026-09-17 used a 4-point protocol: `Shin`, `Lateral Malleolus`,
`5th Metatarsal`, `Platform Edge`. It changed to the current 3-point protocol:

- **`Lateral Malleolus` was renamed `bottom_heel`.** This is a terminology correction,
  not a new location: in every earlier annotation that point was already placed at the
  bottom of the heel, so those coordinates are valid under the new name as they stand.
- **`Platform Edge` was removed.** It never fed the angle. With it gone, the leg axis
  runs `shin` &rarr; `bottom_heel` directly.

On 2026-09-17 every existing per-date CSV was migrated in place to the current columns
(`Shin` &rarr; `shin`, `LateralMalleolus` &rarr; `bottom_heel`, `5thMetatarsal` &rarr;
`5th_metatarsal`, `PlatformEdge` removed, angle recomputed from the three points). All 77
rows were checked against a pre-migration backup
(`allignments_BACKUP_before_csv_migration_2026-09-17_140825`): no image name, row order,
coordinate or angle changed. Six older rows that had a blank angle only because their
Platform Edge was empty now carry an angle.

Also on 2026-09-17, the shin magnet added a `shin_radius_px` column. It was appended
**blank** to all 77 existing rows (backup:
`allignments_BACKUP_before_radius_column_2026-09-17_142235`), because those shins were
placed by eye - writing their measured heel-to-shin distance there would falsely claim
they were magnet-placed. Nothing else in those rows changed. Four of them - `IMG_0480`,
`IMG_0481`, `IMG_0483` and `IMG_0485` - have the shin up near the knee, about 2.9 foot
lengths from the heel against about 1.0 for the rest, and are worth redoing.

`build_training_csv.py` still understands the old column names through
`LEGACY_KEY_ALIASES` in `app.py`, so a CSV restored from an older backup is read
correctly rather than coming out blank.

On 2026-09-19 `gt_source` was added. `labels_2026-09-19.csv` was created from
`labels_2026-09-18.csv` with every cell kept and `gt_source = screen` appended to its 54
rows (the 2026-09-18 folder itself was not modified). Older per-date files have no
`gt_source`; that column is blank for their rows in `training_labels.csv`.

Old and new rows are never mixed in one file. Saving into a CSV whose header is not the
current protocol is **refused with a `409`** before anything is written - neither the
row nor the annotated image - because appending under a different header would put
values in the wrong columns without any error.

## API (retired 3-point scheme)

For the current scheme's API, see [v2_malleolus scheme (current)](#v2_malleolus-scheme-current) above.

| Method | Endpoint | Purpose |
| ------ | -------- | ------- |
| `GET` | `/api/config` | Landmark definitions, alignment guides, magnet settings, configured paths, today's date |
| `GET` | `/api/images?side=left&mode=resume` | Image queue (`mode` = `resume` or `fresh`) |
| `GET` | `/api/image/{side}/{filename}` | Serves the upright, downscaled preview (+ full size in headers) |
| `POST` | `/api/save` | `{ filename, side, points: [{x,y} x3], shin_radius_px, gt_source, image_width, image_height }` — points in click order; writes image + CSV |

Interactive docs at <http://127.0.0.1:8000/docs> (lists both schemes' endpoints).

## Notes

- Supported input extensions: `.jpg .jpeg .png .bmp .tif .tiff .webp`.
- An image is "done" only if some CSV has a current-protocol row for it (all three
  keypoints plus `shin_radius_px`). An interrupted save that left an image file but no
  row therefore re-queues the image rather than silently skipping it.
- The server binds to `127.0.0.1` only — it is not reachable from other machines.
- Filenames are validated against path traversal before any file is opened.
