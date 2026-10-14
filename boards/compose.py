"""
Composing one board: open the factory database, run the service, stamp the
payload, and keep it briefly in the cache so several screens showing the
same board don't each query SAP on every refresh.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from django.conf import settings
from django.core.cache import cache
from django.template.loader import render_to_string

from . import formatting
from .registry import Slide
from .sources import FactoryUnavailable, Unavailable, connect, hana_for

logger = logging.getLogger(__name__)


def build_board(slide: Slide, *, company_code: Optional[str] = None, slug: Optional[str] = None) -> Dict[str, Any]:
    """The board as a JSON-ready dict. Never raises for a source failure:
    those become degraded sections with reasons."""
    company_code = company_code or settings.FACTORY_COMPANY_CODE
    service_class = slide.service_class()
    kwargs: Dict[str, Any] = {"hana_for": hana_for}
    if slug is not None:
        kwargs["slug"] = slug

    try:
        with connect() as postgres:
            payload = service_class(company_code, postgres=postgres, **kwargs).build()
    except FactoryUnavailable as exc:
        # Every section that reads the factory database degrades with this
        # reason; the ones that only read SAP still show.
        payload = service_class(company_code, postgres=Unavailable(exc), **kwargs).build()

    meta = payload.setdefault("meta", {})
    meta.setdefault("degraded", [])
    meta.setdefault("reasons", {})
    meta.setdefault("warnings", [])
    meta.setdefault("refresh_seconds", slide.refresh_seconds)
    meta.update(
        slide=slide.key,
        label=slide.label,
        company=company_code,
        generated_at=formatting.now().isoformat(),
    )
    if slug is not None:
        meta["slug"] = slug

    return formatting.jsonable(payload)


def cached_board(slide: Slide, *, slug: Optional[str] = None) -> Dict[str, Any]:
    """``build_board``, reused for ``BOARDS_CACHE_SECONDS``."""
    seconds = settings.BOARDS_CACHE_SECONDS
    if seconds <= 0:
        return build_board(slide, slug=slug)

    key = f"boards:{settings.FACTORY_COMPANY_CODE}:{slide.key}:{slug or ''}"
    payload = cache.get(key)
    if payload is None:
        payload = build_board(slide, slug=slug)
        cache.set(key, payload, seconds)
    return payload


def render_fragment(slide: Slide, payload: Dict[str, Any], request=None) -> str:
    """The board's HTML, from its template."""
    return render_to_string(
        slide.template,
        {"board": payload, "slide": slide, "meta": payload.get("meta", {})},
        request=request,
    )
