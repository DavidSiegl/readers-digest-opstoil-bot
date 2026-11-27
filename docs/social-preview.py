"""Render docs/social-preview.svg to docs/social-preview.png, the image GitHub shows for the repository.

Run it from the repository root after a change to the SVG or to the app's
icon, which the SVG draws the bot from:

    python3 docs/social-preview.py

Needs PyGObject with librsvg and pycairo, as the window does. The PNG is
uploaded by hand: GitHub, Settings → General → Social preview.
"""

import base64
from pathlib import Path

import cairo
import gi

gi.require_version("Rsvg", "2.0")
from gi.repository import Rsvg  # noqa: E402

# GitHub's recommended size for a social preview.
WIDTH, HEIGHT = 1280, 640

here = Path(__file__).parent
ICON = "../src/readers_digest/gui/icons/readers-digest.svg"

# librsvg loads no image from outside the SVG's own directory, so the icon
# the SVG names goes in as a data: URI.
source = (here / "social-preview.svg").read_text()
if ICON not in source:
    raise SystemExit(f"social-preview.svg no longer draws {ICON}")
icon = base64.b64encode((here / ICON).read_bytes()).decode()
svg = Rsvg.Handle.new_from_data(source.replace(ICON, f"data:image/svg+xml;base64,{icon}").encode())
surface = cairo.ImageSurface(cairo.FORMAT_RGB24, WIDTH, HEIGHT)
viewport = Rsvg.Rectangle()
viewport.x, viewport.y, viewport.width, viewport.height = 0, 0, WIDTH, HEIGHT
svg.render_document(cairo.Context(surface), viewport)
surface.write_to_png(str(here / "social-preview.png"))
