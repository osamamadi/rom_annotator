"""
Auto-suggest the malleolus with RTMW-x (COCO-WholeBody) via rtmlib.

WHAT THIS IS
    A human-in-the-loop aid, not a measurement. The model proposes where the
    lateral malleolus is; the annotator looks at it and either accepts it or
    nudges it with the arrow keys. Nothing here ever reaches the CSV without
    passing through the same validation as a hand-placed point.

WHY THE MALLEOLUS AND NOT THE OTHERS
    Of the five points, the malleolus is the one that tolerates being wrong:
    about 0.009 deg of angle error per pixel sideways, because it only decides
    where the construction line sits. bottom_heel and 5th_metatarsal are
    roughly 80x more sensitive (0.074 deg/px), so those stay hand-placed.

    Measured on this dataset, the model's own left and right ankle estimates
    differ by 5-53 px, which is well inside what the malleolus can absorb and
    in any case gets corrected by eye.

MODEL
    rtmlib's Wholebody(mode="performance") is RTMW-x:
    rtmw-dw-x-l_simcc-cocktail14_270e-384x288, plus YOLOX-m for detection.
    About 330 MB of ONNX, downloaded once by rtmlib into ~/.cache/rtmlib and
    reused. Roughly 0.5 s per image on CPU once warm.

FAILURE IS ALWAYS SOFT
    rtmlib missing, weights not downloadable, no person found, inference
    raising - every one of those returns a status the UI can ignore. The
    annotator is never blocked, and the tool works exactly as before.

NOTE ON SCORES
    RTMPose returns SimCC peak values, not probabilities. They run about 2-5
    on this data. Treat them as relative confidence only.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Optional

import numpy as np
from fastapi import APIRouter, HTTPException, Query

# rtmlib's "performance" preset IS RTMW-x at 384x288.
MODEL_MODE = "performance"
BACKEND = "onnxruntime"
DEVICE = "cpu"
MODEL_LABEL = "RTMW-x (rtmw-dw-x-l_simcc-cocktail14_270e-384x288) + YOLOX-m"

# COCO-WholeBody indices
L_ANKLE, R_ANKLE = 15, 16
FOOT_PARTS = {
    17: "left_big_toe", 18: "left_small_toe", 19: "left_heel",
    20: "right_big_toe", 21: "right_small_toe", 22: "right_heel",
}

# SimCC peaks, not probabilities. Below FLOOR the suggestion is withheld;
# below LOW it is offered but flagged so the UI can say "check this one".
SCORE_FLOOR = 1.5
SCORE_LOW = 3.0

CACHE_MAX = 64

_deps: Dict[str, object] = {}
_model = None
_model_lock = threading.Lock()          # one inference at a time
_status = {"state": "cold", "detail": "not started", "elapsed_s": None}
_cache: "OrderedDict[tuple, dict]" = OrderedDict()

router = APIRouter(prefix="/api/v2", tags=["suggest"])


def configure(**kwargs) -> None:
    required = {"open_upright", "resolve_input_image", "validate_side"}
    missing = required - set(kwargs)
    if missing:
        raise RuntimeError("pose_suggest.configure is missing: %s" % ", ".join(sorted(missing)))
    _deps.update(kwargs)


def _dep(name):
    try:
        return _deps[name]
    except KeyError:
        raise RuntimeError("pose_suggest was never configured by app.py") from None


# --------------------------------------------------------------------------
# Model loading, off the request path entirely
# --------------------------------------------------------------------------

def _load():
    """Import rtmlib and build the model. Runs on a background thread."""
    global _model
    started = time.time()
    _status.update(state="loading",
                   detail="importing rtmlib and fetching weights if needed "
                          "(~330 MB, first run only)")
    try:
        from rtmlib import Wholebody
    except Exception as exc:   # noqa: BLE001 - a missing dep must not kill the app
        _status.update(state="unavailable",
                       detail="rtmlib is not installed (%s). Run: "
                              "pip install rtmlib onnxruntime" % exc)
        return
    try:
        model = Wholebody(mode=MODEL_MODE, backend=BACKEND, device=DEVICE)
        # One tiny forward pass so the first real image is not the slow one.
        model(np.zeros((640, 480, 3), dtype=np.uint8))
        _model = model
        _status.update(state="ready", detail=MODEL_LABEL,
                       elapsed_s=round(time.time() - started, 1))
    except Exception as exc:   # noqa: BLE001 - offline, corrupt cache, whatever
        _status.update(state="unavailable",
                       detail="could not load RTMW-x: %s" % exc)
        traceback.print_exc()


def warmup() -> None:
    """Kick the load off in the background so the server starts instantly."""
    if _status["state"] != "cold":
        return
    _status.update(state="loading", detail="starting")
    threading.Thread(target=_load, name="rtmw-warmup", daemon=True).start()


def status() -> dict:
    return {"model": MODEL_LABEL, "backend": BACKEND, "device": DEVICE, **_status}


# --------------------------------------------------------------------------
# Inference
# --------------------------------------------------------------------------

def _cache_key(path: Path):
    st = path.stat()
    return (str(path), st.st_mtime_ns, st.st_size)


def suggest_for(path: Path) -> dict:
    """
    Suggested malleolus for one image, in UPRIGHT FULL-RESOLUTION pixels -
    the same frame the CSV and the canvas use.
    """
    if _status["state"] != "ready" or _model is None:
        return {"status": _status["state"], "detail": _status["detail"]}

    key = _cache_key(path)
    hit = _cache.get(key)
    if hit is not None:
        _cache.move_to_end(key)
        return dict(hit, cached=True)

    started = time.time()
    with _dep("open_upright")(path) as im:
        rgb = np.asarray(im.convert("RGB"))
    width, height = rgb.shape[1], rgb.shape[0]
    bgr = rgb[:, :, ::-1].copy()          # rtmlib/cv2 convention

    try:
        with _model_lock:                 # onnxruntime session, one call at a time
            keypoints, scores = _model(bgr)
    except Exception as exc:              # noqa: BLE001 - never break annotating
        traceback.print_exc()
        return {"status": "error", "detail": str(exc)}

    if keypoints is None or len(keypoints) == 0:
        result = {"status": "no_person",
                  "detail": "no person detected in this frame",
                  "image_size": {"width": width, "height": height}}
        _remember(key, result)
        return result

    # The instance the model is most sure about AT THE ANKLE, which is not
    # necessarily the largest or first detection.
    best = max(range(len(keypoints)),
               key=lambda i: max(scores[i][L_ANKLE], scores[i][R_ANKLE]))
    kp, sc = keypoints[best], scores[best]

    left = {"x": float(kp[L_ANKLE][0]), "y": float(kp[L_ANKLE][1]),
            "score": float(sc[L_ANKLE])}
    right = {"x": float(kp[R_ANKLE][0]), "y": float(kp[R_ANKLE][1]),
             "score": float(sc[R_ANKLE])}
    picked, label = (left, "left") if left["score"] >= right["score"] else (right, "right")

    if picked["score"] < SCORE_FLOOR:
        result = {"status": "low_confidence",
                  "detail": "best ankle score %.2f is below the %.1f floor"
                            % (picked["score"], SCORE_FLOOR),
                  "image_size": {"width": width, "height": height}}
        _remember(key, result)
        return result

    result = {
        "status": "ok",
        "malleolus": {"x": round(picked["x"], 2), "y": round(picked["y"], 2)},
        "score": round(picked["score"], 3),
        "confident": picked["score"] >= SCORE_LOW,
        "ankle_used": label,
        "people": int(len(keypoints)),
        "alternatives": {"left": left, "right": right},
        # Offered for information only - nothing in the UI consumes these yet.
        # left_small_toe sits near the 5th metatarsal and left_heel near the
        # sole, but both are far too sensitive to auto-place (0.074 deg/px).
        "foot_parts": {name: {"x": round(float(kp[i][0]), 2),
                              "y": round(float(kp[i][1]), 2),
                              "score": round(float(sc[i]), 3)}
                       for i, name in FOOT_PARTS.items()},
        "image_size": {"width": width, "height": height},
        "elapsed_ms": int(round((time.time() - started) * 1000)),
        "model": MODEL_LABEL,
    }
    _remember(key, result)
    return result


def _remember(key, result):
    _cache[key] = result
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@router.get("/suggest_status")
def suggest_status():
    return status()


@router.get("/suggest_malleolus")
def suggest_malleolus(side: str = Query(...), filename: str = Query(...)):
    """
    Suggested malleolus for one image.

    Always answers 200 with a status field - a suggestion failing is a normal
    outcome, not an error the annotator should have to deal with. Only a bad
    side or filename is a real 4xx, because that is a bug in the caller.
    """
    side = _dep("validate_side")(side)
    path = _dep("resolve_input_image")(side, filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No such image: %s" % filename)
    return suggest_for(path)
