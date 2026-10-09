"""Attach commons `source` (and comment) onto zone rrsets.

Prefers hybrid-backend meta API (post-merge, sqlite overrides excluded).
Falls back to parsing local zones/tree.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path


def _pdns_root() -> Path:
    env = os.environ.get("PDNS_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "zones" / "tree" / "inc").is_dir() and (p / "conf" / "catalog.tsv").is_file():
            return p
    return Path("/home/instance/pdns.1")


def _meta_url() -> str:
    return os.environ.get("PDNS_COMMONS_META_URL", "http://127.0.0.1:9199").rstrip("/")


@lru_cache(maxsize=1)
def _zonefile_mod():
    root = _pdns_root()
    scripts = str(root / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import zonefile  # noqa: WPS433

    return zonefile, root


def _normalize_entry(val) -> tuple[str, str]:
    """Return (source, comment) from meta value (str or dict)."""
    if isinstance(val, dict):
        src = (val.get("source") or "").strip()
        comment = (val.get("comment") or "").strip()
        if not comment and src:
            comment = f"commons:{src}"
        return src, comment
    if isinstance(val, str):
        s = val.strip()
        for prefix in ("mixin:", "commons:"):
            if s.startswith(prefix):
                return s[len(prefix) :], s if s.startswith("mixin:") else f"mixin:{s[len(prefix):]}"
        return s, (f"mixin:{s}" if s else "")
    return "", ""


def _from_meta(zone: str) -> dict[tuple[str, str], tuple[str, str]] | None:
    url = f"{_meta_url()}/sources/{zone.rstrip('.')}"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for k, val in (data or {}).items():
        if "|" not in k:
            continue
        name, typ = k.rsplit("|", 1)
        src, comment = _normalize_entry(val)
        if src:
            out[(name.rstrip(".").lower(), typ.upper())] = (src, comment)
    return out


@lru_cache(maxsize=64)
def _from_files(zone: str) -> dict[tuple[str, str], tuple[str, str]]:
    zonefile, root = _zonefile_mod()
    catalog = root / "conf" / "catalog.tsv"
    tree = root / "zones" / "tree"
    zkey = zone.rstrip(".").lower()
    tmpl = kind = None
    for z, t, k in zonefile.iter_catalog(catalog):
        if z.rstrip(".").lower() == zkey:
            tmpl, kind = t, k
            break
    if not tmpl:
        return {}
    path = zonefile.template_path(tree, tmpl, kind)
    if not path.is_file():
        return {}
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for r in zonefile.shared_records_for_zone(zkey, path):
        src = (r.get("source") or "").strip()
        if not src:
            continue
        out[(r["name"].rstrip(".").lower(), r["type"].upper())] = (
            src,
            r.get("comment") or f"mixin:{src}",
        )
    return out


def source_map_for_zone(zone: str) -> dict[tuple[str, str], tuple[str, str]]:
    """{(name, TYPE): (source, comment)} for commons RRs only."""
    meta = _from_meta(zone)
    if meta is not None:
        return meta
    return _from_files(zone)


# backwards-compatible alias
def comment_map_for_zone(zone: str) -> dict[tuple[str, str], str]:
    return {k: v[1] for k, v in source_map_for_zone(zone).items()}


def annotate_rrsets(zone: str, rrsets: list) -> list:
    """Attach `source` on each record (and rrset) from commons; fill empty comments."""
    smap = source_map_for_zone(zone)
    for r in rrsets:
        key = (r["name"].rstrip(".").lower(), r["type"].upper())
        entry = smap.get(key)
        source = entry[0] if entry else None
        comment = entry[1] if entry else None

        # rrset-level source (null if unique / not from commons)
        r["source"] = source

        records = r.get("records") or []
        comments = r.setdefault("comments", [])
        while len(comments) < len(records):
            comments.append({"content": "", "account": ""})

        for i, rec in enumerate(records):
            rec["source"] = source
            if source and not (comments[i].get("content") or "").strip():
                comments[i] = {"content": comment or f"commons:{source}", "account": "commons"}
    return rrsets


def clear_cache() -> None:
    _from_files.cache_clear()
    _zonefile_mod.cache_clear()
