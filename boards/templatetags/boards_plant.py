"""
The plant board's one template tag: the page as data.

``{% plant_view board meta as page %}`` hands the fragment the header and the
four bands, each tile already carrying its pill, subtitle, figure and meter,
from ``boards.services.plant_view.present``. The arithmetic the factory's
React page did on top of the payload lives there, in Python, where it can be
tested; the templates only lay it out.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from django import template

from ..services.plant_view import present

register = template.Library()


@register.simple_tag
def plant_view(board: Optional[Dict[str, Any]], meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return present(board or {}, meta)
