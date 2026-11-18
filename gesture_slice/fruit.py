"""
fruit.py - the things that fly across the screen: Fruit, Bomb and FruitHalf.

These classes only hold *state and physics* (position, velocity, rotation).
They know nothing about Tkinter, which keeps them easy to unit-test.  Drawing
is done by GameRenderer in game_engine.py.

Collision detection
-------------------
Every camera frame gives us a short line segment A->B (where the fingertip
was, where it is now).  A fruit is a circle (centre C, radius r).  The blade
cuts the fruit when the shortest distance from C to the segment AB is <= r.
See ``segment_intersects_circle`` below.
"""

import itertools
import math
import random
from dataclasses import dataclass

from . import config


# --------------------------------------------------------------------------
# Fruit catalogue
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FruitType:
    """Static description of one kind of fruit (colours, size, points)."""
    name: str
    radius: int
    skin: str           # main skin colour
    skin_dark: str      # shadow side of the skin
    flesh: str          # inside colour shown after slicing
    rim: str            # thin ring between skin and flesh
    seed: str           # seed / juice-dot colour ('' = no seeds)
    juice: str          # colour of the splash on the background
    points: int = config.NORMAL_POINTS
    weight: float = 1.0          # relative spawn probability
    special: bool = False
    striped: bool = False
    leaf: bool = True
    stem: bool = True


FRUIT_TYPES = (
    FruitType("apple", 38, "#ef2b45", "#7d0a1d", "#fff2d1", "#ff5a6e", "#3a2210", "#ffe7b0"),
    FruitType("orange", 38, "#ff9a1f", "#b4500a", "#ffb84d", "#fff1d0", "", "#ffad33", stem=False),
    FruitType("lime", 32, "#8fe03a", "#3a7a0c", "#d4f76b", "#f1fdd3", "", "#c4f25a", leaf=False, stem=False),
    FruitType("watermelon", 50, "#32b356", "#0f4a26", "#ff3d5a", "#e3fbe6", "#1c1c1c", "#ff4d6a",
              striped=True, leaf=False, stem=False),
    FruitType("plum", 31, "#9b4fd0", "#3f1560", "#ffcf63", "#efc3ff", "#8a5a12", "#d58cff", leaf=False),
    FruitType("peach", 36, "#ffab7a", "#d2483f", "#ffd585", "#ffe9cf", "#9b4a1d", "#ffc27a"),
    FruitType("golden star", 34, "#ffd447", "#c08a00", "#fff6b0", "#fffbe0", "", "#ffe680",
              points=config.SPECIAL_POINTS, weight=0.12, special=True, leaf=False, stem=False),
)

FRUIT_BY_NAME = {kind.name: kind for kind in FRUIT_TYPES}

BOMB_RADIUS = 34


def choose_fruit_type(rng=random, special_chance=None):
    """Pick a fruit type using the weights (optionally forcing the special odds)."""
    if special_chance is not None:
        specials = [k for k in FRUIT_TYPES if k.special]
        normals = [k for k in FRUIT_TYPES if not k.special]
        if specials and rng.random() < special_chance:
            return rng.choice(specials)
        return rng.choices(normals, weights=[k.weight for k in normals])[0]
    return rng.choices(FRUIT_TYPES, weights=[k.weight for k in FRUIT_TYPES])[0]


# --------------------------------------------------------------------------
# Mathematical Geometry & Vector Collision Detection
# --------------------------------------------------------------------------
def distance_point_to_segment(px, py, ax, ay, bx, by):
    """
    Computes the shortest Euclidean distance from a point P(px, py) to a finite
    line segment AB with endpoints A(ax, ay) and B(bx, by).

    Mathematical Derivation (Vector Projection):
    1. Define vector AB = B - A, and vector AP = P - A.
    2. The projection parameter 't' along line AB is obtained via dot product:
           t = (AP . AB) / |AB|^2
             = ((px - ax)*abx + (py - ay)*aby) / (abx^2 + aby^2)
    3. To constrain the closest point onto the finite segment (rather than an
       infinite line), clamp t between [0.0, 1.0]:
           - If t <= 0.0, closest point is endpoint A.
           - If t >= 1.0, closest point is endpoint B.
           - If 0.0 < t < 1.0, closest point lies strictly within segment AB.
    4. Compute closest point coordinates:
           C = A + t * AB = (ax + t*abx, ay + t*aby)
    5. Return Euclidean distance ||P - C|| = hypot(px - cx, py - cy).
    """
    abx, aby = bx - ax, by - ay
    length_sq = abx * abx + aby * aby
    if length_sq == 0:                       # Degenerate segment: A and B coincide
        return math.hypot(px - ax, py - ay)

    # Calculate scalar projection t and clamp to segment bounds [0, 1]
    t = ((px - ax) * abx + (py - ay) * aby) / length_sq
    t = max(0.0, min(1.0, t))

    # Determine coordinates of the closest point on segment AB
    cx, cy = ax + t * abx, ay + t * aby
    return math.hypot(px - cx, py - cy)


def segment_intersects_circle(ax, ay, bx, by, cx, cy, radius):
    """
    Checks for intersection between a blade cut segment AB and a circular fruit:
    Returns True if shortest distance from circle centre C(cx, cy) to segment AB <= radius.
    """
    return distance_point_to_segment(cx, cy, ax, ay, bx, by) <= radius


# --------------------------------------------------------------------------
# Physics & Flying Entities (Semi-Implicit Euler Integration)
# --------------------------------------------------------------------------
class GameObject:
    """
    GameObject: Base physics model for all flying projectiles (Fruit, Bomb, FruitHalf).
    - Position (x, y) and Velocity (vx, vy).
    - Angular rotation (angle) and Angular velocity (spin).
    - Uses Semi-Implicit Euler integration for numerical stability in real-time physics.
    """

    _ids = itertools.count(1)

    def __init__(self, x, y, vx, vy, radius, gravity=config.GRAVITY, spin=0.0, angle=0.0):
        self.uid = next(GameObject._ids)
        self.x, self.y = float(x), float(y)
        self.vx, self.vy = float(vx), float(vy)
        self.radius = radius
        self.gravity = gravity
        self.angle = angle % 360          # Clockwise rotation degrees on screen
        self.spin = spin                  # Degrees per second
        self.alive = True

    def update(self, dt):
        """
        Semi-Implicit (Symplectic) Euler Numerical Integration:
        1. Velocity update: v_y(t + dt) = v_y(t) + g * dt
        2. Position update: y(t + dt)   = y(t) + v_y(t + dt) * dt
                            x(t + dt)   = x(t) + v_x * dt
        3. Rotation update: theta(t + dt) = (theta(t) + spin * dt) % 360
        Symplectic Euler preserves orbital energy better than standard forward Euler.
        """
        self.vy += self.gravity * dt
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.angle = (self.angle + self.spin * dt) % 360

    def is_offscreen(self, width, height):
        """Checks if the projectile has fallen off the bottom screen boundary or drifted sideways."""
        below = self.y - self.radius > height + 20 and self.vy > 0
        sideways = self.x < -self.radius * 3 or self.x > width + self.radius * 3
        return below or sideways

    def hit_by(self, ax, ay, bx, by):
        """Tests if this object was struck by the blade movement segment A -> B."""
        return segment_intersects_circle(ax, ay, bx, by, self.x, self.y, self.radius)


class Fruit(GameObject):
    """A whole fruit that can be sliced into two FruitHalf objects."""

    HALF_SEPARATION_SPEED = 170.0

    def __init__(self, kind, x, y, vx, vy, gravity=config.GRAVITY, rng=random):
        super().__init__(x, y, vx, vy, kind.radius, gravity,
                         spin=rng.uniform(-220, 220), angle=rng.uniform(0, 360))
        self.kind = kind
        self.sliced = False

    @property
    def points(self):
        return self.kind.points

    def slice(self, blade_angle_deg, rng=random):
        """Cut the fruit along the blade direction and return its two halves.

        The halves are pushed apart along the normal of the cut line so they
        visibly split open in the direction the player swiped.
        """
        self.sliced = True
        self.alive = False
        a = math.radians(blade_angle_deg)
        # Normal of the cut line (screen coordinates, y grows downwards).
        nx, ny = math.sin(a), -math.cos(a)
        offset = self.radius * 0.12
        sep = self.HALF_SEPARATION_SPEED
        halves = []
        for side, sign in ((0, 1), (1, -1)):
            halves.append(FruitHalf(
                self.kind,
                x=self.x + nx * offset * sign,
                y=self.y + ny * offset * sign,
                vx=self.vx * 0.6 + nx * sep * sign,
                vy=min(self.vy, 0) * 0.4 + ny * sep * sign - 60,
                gravity=self.gravity,
                # half 0 keeps the dome on the normal side, half 1 is flipped
                angle=blade_angle_deg + (0 if side == 0 else 180),
                spin=rng.uniform(60, 160) * sign,
            ))
        return halves


class FruitHalf(GameObject):
    """One half of a sliced fruit - purely decorative, it just falls away."""

    def __init__(self, kind, x, y, vx, vy, gravity, angle, spin):
        super().__init__(x, y, vx, vy, kind.radius, gravity, spin=spin, angle=angle)
        self.kind = kind


class Bomb(GameObject):
    """Touching a bomb with a fast blade costs one life."""

    def __init__(self, x, y, vx, vy, gravity=config.GRAVITY, rng=random):
        super().__init__(x, y, vx, vy, BOMB_RADIUS, gravity,
                         spin=rng.uniform(-90, 90), angle=rng.uniform(-25, 25))
        self.exploded = False

    def fuse_tip(self):
        """Screen position of the burning fuse tip (rotates with the bomb)."""
        # Fuse tip in the bomb's own frame, before rotation.
        lx, ly = self.radius * 0.62, -self.radius * 1.28
        a = math.radians(self.angle)
        ca, sa = math.cos(a), math.sin(a)
        return self.x + lx * ca - ly * sa, self.y + lx * sa + ly * ca

    def explode(self):
        self.exploded = True
        self.alive = False
