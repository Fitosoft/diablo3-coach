"""Calibrated geometry for the tested 2560 x 1440 Diablo III UI.

Pixel boxes refer to this reference resolution. Normalized boxes use BOX_SCALE
independently of image size. Changing constants alone does not establish support
for another resolution or UI layout; validate with screenshots first.
"""
X_RESOLUTION = 2560
Y_RESOLUTION = 1440
REFERENCE_SIZE = (X_RESOLUTION, Y_RESOLUTION)
BOX_SCALE = 1000
HALF_SCREEN_FACTOR = 0.5
HALF_OVERLAP_FACTOR = 0.016
CROP_PADDING = 10
CROP_EDGE_MARGIN = 5

# ponytail: calibrated crops; full-screen detection remains first for centered views.
FALLBACK_BOX = [156, 0, 820, BOX_SCALE]
HALF_BOXES = [
    [0, 0, round((HALF_SCREEN_FACTOR + HALF_OVERLAP_FACTOR) * BOX_SCALE), BOX_SCALE],
    [round((HALF_SCREEN_FACTOR - HALF_OVERLAP_FACTOR) * BOX_SCALE), 0, BOX_SCALE, BOX_SCALE],
]
# ponytail: fixed panels; add another tested profile when contributors supply evidence.
GUIDED_PANELS = {
    "player_stats": ("player_stats", "player", "unknown", [515, 0, 730, 835]),
    "materials": ("materials", "unknown", "unknown", [515, 0, 735, 845]),
    "follower_stats": ("follower_stats", "follower", "unknown", [270, 0, 480, 835]),
    "follower_item": ("item", "follower", "equipped", [195, 0, 425, 650]),
}
HEADER_BANDS = [
    (0, 155, 700, 215), (650, 50, 1250, 150),
    (1250, 50, 1850, 150), (1850, 140, X_RESOLUTION, 215),
]
HEADER_FRAMES = [
    (180, 15, 520, 125), (780, 15, 1180, 55),
    (1450, 15, 1780, 55), (2050, 15, 2380, 125),
]
FRAME_RED_MINIMUM = 40
FRAME_RED_DOMINANCE = 1.5
FRAME_RED_FRACTION = 0.25
