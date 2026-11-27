"""What teams-probe records of the client's calls: their shape, never their content. Pure."""

import re

# Values kept as they are in a skeleton: enumerations that say what a thing
# is, never what anyone wrote.
SAFE_KEYS = frozenset({"messagetype", "contenttype", "threadType", "type", "productThreadType", "version",
                       "spaceType", "kind", "role", "isDeleted", "isOneOnOne", "membershipType"})


def skeleton(v, key: str = "", depth: int = 0):
    """The structure of a JSON value with its text replaced: letters become a, digits 9.

    A list shows its length and the skeleton of its first element; deeper
    than 8 levels, only the type is kept. `key` is the key `v` is under.
    """
    if depth > 8:
        return type(v).__name__
    if isinstance(v, dict):
        return {k: skeleton(x, k, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return {"<list>": len(v), "first": skeleton(v[0], key, depth + 1)} if v else []
    if isinstance(v, str):
        if key in SAFE_KEYS and len(v) <= 60:
            return v
        return _shape(v[:80]) + ("…" if len(v) > 80 else "")
    return v if isinstance(v, (bool, type(None))) else type(v).__name__


def _shape(s: str) -> str:
    return re.sub(r"[^\W\d_]", "a", re.sub(r"\d", "9", s))


def url_pattern(url: str) -> str:
    """The URL with ids and query values replaced: what kind of call it is, not about whom."""
    path, _, query = url.partition("?")
    segs = ["{id}" if (":" in s or "%3A" in s or re.search(r"\d{5,}|[0-9a-f]{8}-", s)) and i > 2 else s
            for i, s in enumerate(path.split("/"))]
    keys = [q.split("=", 1)[0] for q in query.split("&") if q]
    return "/".join(segs) + (f"?{'&'.join(f'{k}=…' for k in keys)}" if keys else "")
