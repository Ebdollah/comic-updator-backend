"""Small helpers shared by the HTML scrapers."""

from typing import Optional

from selectolax.parser import Node


def attr(node: Node, name: str, default: str = "") -> str:
    """An attribute's value as a string, treating a valueless one as absent.

    HTML allows an attribute with no value at all, and selectolax represents
    that as the key being present but mapped to None — so the obvious
    `node.attributes.get("alt", "")` hands back None, because a dict default
    only applies when the key is missing entirely. A single `<img alt>` on one
    series page was enough to take the whole scrape down with
    "'NoneType' object has no attribute 'startswith'".

    Every caller wants a string to match, split or regex against, so the
    coercion lives here rather than being remembered at each use.
    """
    value: Optional[str] = node.attributes.get(name)
    return default if value is None else value
