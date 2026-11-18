"""
mouse_input.py - Trackpad and Mouse Gesture Recognition Engine.

Architecture Overview:
- Maps standard Tkinter `<Motion>` and `<B1-Motion>` events to directional cut vectors.
- Replaces heavy external vision libraries (OpenCV/MediaPipe) with native Tkinter event
  coordinates, ensuring 100% pure Python/Tkinter portability and zero frame drops.
- Implements Euclidean velocity calculation:
      Speed = sqrt((x_2 - x_1)^2 + (y_2 - y_1)^2) / delta_t  [pixels/second]
- Employs a velocity threshold (default 650 px/s):
  * Slow movement (< threshold) = Aiming (no slice registered).
  * Fast movement (>= threshold) = Slicing gesture (generates cutting line segment).
- Uses temporal gap rejection (dt > 0.25s) to avoid "teleport slicing" when the mouse
  leaves and re-enters the window or when the player lifts their hand.
"""

import math
from dataclasses import dataclass

from . import config


@dataclass
class PointerFrame:
    """
    PointerFrame: Snapshot of player input at an instantaneous point in time.
    - present: True if pointer is within active window boundaries.
    - (x, y): Instantaneous coordinate in canvas space.
    - speed: Velocity magnitude in pixels per second.
    - segment: 5-tuple (ax, ay, bx, by, speed) representing the motion vector.
    - timestamp: High-precision monotonic timestamp (time.perf_counter()).
    """
    present: bool                       # Has the pointer been registered on canvas
    x: float = 0.0
    y: float = 0.0
    speed: float = 0.0                  # Speed in px/s of the latest movement
    segment: tuple = None               # (ax, ay, bx, by, speed) or None
    timestamp: float = 0.0


class MotionTracker:
    """
    MotionTracker: State machine computing cutting vectors and smoothed speeds.
    - Maintains the previous coordinate point P_prev = (x, y, t).
    - Filters jitter and applies temporal smoothing (Exponential Moving Average).
    """

    def __init__(self, max_gap=config.MAX_SEGMENT_GAP):
        # Maximum allowed idle time (0.25s) before resetting movement history
        self.max_gap = max_gap
        self.reset()

    def reset(self):
        """Clears previous coordinate history and resets velocity."""
        self.prev = None          # (x, y, t)
        self.speed = 0.0

    def update(self, x, y, t):
        """
        Receives new coordinate (x, y) at timestamp t.
        Returns a cutting segment (px, py, x, y, seg_speed) if valid, else None.
        """
        segment = None
        if self.prev is not None:
            px, py, pt = self.prev
            dt = t - pt

            # Validate delta-time to ensure continuity without teleport cuts
            if 0 < dt <= self.max_gap:
                # Euclidean distance divided by time difference -> speed in px/s
                seg_speed = math.hypot(x - px, y - py) / dt

                # Exponential Moving Average (EMA) smoothing for display stability:
                # speed_t = 0.5 * speed_{t-1} + 0.5 * raw_speed
                self.speed = 0.5 * self.speed + 0.5 * seg_speed
                segment = (px, py, x, y, seg_speed)
            elif dt > self.max_gap:
                # If player paused or lifted finger, reset speed to prevent artificial spikes
                self.speed = 0.0
        self.prev = (x, y, t)
        return segment


class MouseDetector:
    """
    MouseDetector: High-level input adapter providing PointerFrame objects to App.
    """

    def __init__(self):
        self.motion = MotionTracker()
        self.last = PointerFrame(present=False)

    def reset(self):
        """Resets underlying tracker state (e.g., when switching screens)."""
        self.motion.reset()

    def update(self, x, y, t):
        """Processes raw coordinate event and updates current PointerFrame state."""
        segment = self.motion.update(x, y, t)
        self.last = PointerFrame(True, x, y, self.motion.speed, segment, timestamp=t)
        return self.last
