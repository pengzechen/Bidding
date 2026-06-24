"""Anji-plus slider captcha solver using OpenCV template matching."""
from __future__ import annotations

import base64
from io import BytesIO

import cv2
import numpy as np
from PIL import Image


def solve_slider_gap(background_b64: str, jigsaw_b64: str) -> int | None:
    """Find gap x position (left edge) in image coordinates using template matching.

    Returns the x pixel offset in the original image coordinate space (310px wide),
    or None if matching confidence is too low.
    """
    bg_bytes = base64.b64decode(background_b64)
    jig_bytes = base64.b64decode(jigsaw_b64)

    bg_img = Image.open(BytesIO(bg_bytes))
    jig_img = Image.open(BytesIO(jig_bytes))

    bg_arr = np.array(bg_img.convert("RGB"))
    jig_arr = np.array(jig_img.convert("RGBA"))

    alpha = jig_arr[:, :, 3]
    mask = (alpha > 128).astype(np.uint8) * 255

    bg_gray = cv2.cvtColor(bg_arr, cv2.COLOR_RGB2GRAY)
    jig_gray = cv2.cvtColor(jig_arr[:, :, :3], cv2.COLOR_RGB2GRAY)

    result = cv2.matchTemplate(bg_gray, jig_gray, cv2.TM_CCOEFF_NORMED, mask=mask)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    if max_val < 0.4:
        return None

    return max_loc[0]
