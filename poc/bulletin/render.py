"""Render BulletinVerified → HTML template.

TODO: Implement full HTML rendering using Jinja2 template
      (bulletin_standalone.html) once design is finalised.
"""

from __future__ import annotations

from .schema import BulletinVerified


def render_to_html(bulletin: BulletinVerified) -> str:
    """Render bulletin JSON to standalone HTML string.

    TODO: Replace placeholder with full Jinja2 template rendering.
    """
    raise NotImplementedError(
        "render_to_html is not yet implemented. "
        "Replace this with Jinja2 rendering using bulletin_standalone.html."
    )
