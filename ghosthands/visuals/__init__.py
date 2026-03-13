"""Visual feedback system for GhostHands agent actions.

Injects a cursor pointer + click ripple into the browser page so the user
can see what the agent is doing. Inspired by Magnitude's cursor visualization.

Global mode (recommended):
    from ghosthands.visuals.patch import enable_visual_cursor
    enable_visual_cursor()  # patches Mouse + Element globally

Per-action mode (legacy):
    from ghosthands.visuals.cursor import CursorVisual
    cursor = CursorVisual(page)
    await cursor.move(x, y)
"""

from ghosthands.visuals.cursor import CursorVisual
from ghosthands.visuals.patch import disable_visual_cursor, enable_visual_cursor

__all__ = ["CursorVisual", "enable_visual_cursor", "disable_visual_cursor"]
