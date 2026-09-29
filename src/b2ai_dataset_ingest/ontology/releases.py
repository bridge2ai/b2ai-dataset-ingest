"""Pinned ontology releases, read straight from the OBO obographs JSON a release publishes.

The SSSOM validator has to check a mapping's ``object_id``/``object_label`` against the release
the file *declares* in ``object_source_version``. oaklib's ``sqlite:obo:hp`` cannot do that: it
downloads whatever build is current, so every relabel between the pinned release and today
reads as a "hallucinated" label -- and the same file can be green one week and red the next
with no change to it. ``main`` went red exactly that way (``HP:0011950`` was relabelled).

So a file that declares a release is checked against that release. The obographs JSON for a
release lives at a stable PURL (``.../obo/hp/releases/2026-02-16/hp.json``), is fetched once
into a cache directory, and is read here with the standard library -- oaklib's obograph
adapter was tried first and its ``obsoletes()`` raises on the handful of nodes a release
ships without ``meta``. :class:`PinnedRelease` exposes the three calls the validator makes
(``label``, ``obsoletes``, ``entity_metadata_map``) with the same shapes oaklib returns, so
the checking code is backend-agnostic.

Files that declare no release fall back to oaklib's current build, as before.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where a release's obographs JSON is published. ``prefix`` is the OBO id space in lower case.
PURL = "http://purl.obolibrary.org/obo/{prefix}/releases/{date}/{prefix}.json"
#: Overrides the cache directory (default: ``<repo>/.ontology-cache``, gitignored).
CACHE_ENV = "B2AI_ONTOLOGY_CACHE"

_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
#: ``http://purl.obolibrary.org/obo/HP_0000001`` -> (``HP``, ``0000001``)
_OBO_IRI = re.compile(r"/obo/([A-Za-z][A-Za-z0-9]*)_([A-Za-z0-9]+)$")


class ReleaseUnavailable(RuntimeError):
    """The pinned release could not be fetched or read."""


def release_date(declared: str | None) -> str | None:
    """The ``YYYY-MM-DD`` in an ``object_source_version`` such as ``hp/releases/2026-02-16``."""
    match = _DATE.search(str(declared or ""))
    return match.group(1) if match else None


def cache_dir(repo_root: Path | None = None) -> Path:
    """The release cache: ``$B2AI_ONTOLOGY_CACHE`` if set, else ``<repo>/.ontology-cache``."""
    override = os.environ.get(CACHE_ENV)
    if override:
        return Path(override)
    root = repo_root or Path(__file__).resolve().parents[3]
    return root / ".ontology-cache"


def fetch_release(prefix: str, date: str, cache: Path) -> Path:
    """Return the cached obographs file for ``prefix``/``date``, downloading it if absent.

    The download lands in a sibling temp file and is renamed into place, so an interrupted
    fetch never leaves a truncated JSON that the next run would try to parse.
    """
    path = cache / f"{prefix}-{date}.json"
    if path.is_file():
        return path
    cache.mkdir(parents=True, exist_ok=True)
    url = PURL.format(prefix=prefix, date=date)
    tmp = path.with_suffix(".json.part")
    logger.info("fetching pinned %s release %s from %s", prefix, date, url)
    try:
        with urllib.request.urlopen(url, timeout=120) as response, open(tmp, "wb") as fh:
            while chunk := response.read(1 << 20):
                fh.write(chunk)
        os.replace(tmp, path)
    except (urllib.error.URLError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise ReleaseUnavailable(f"could not fetch {url}: {exc}") from exc
    return path


def _curie(iri: str) -> str | None:
    match = _OBO_IRI.search(iri)
    return f"{match.group(1)}:{match.group(2)}" if match else None


class PinnedRelease:
    """One ontology release, loaded from its obographs JSON.

    Duck-types the slice of an oaklib adapter that ``ontology/sssom_validate.py`` uses.
    """

    def __init__(self, path: Path) -> None:
        try:
            graph = json.loads(Path(path).read_text())["graphs"][0]
        except (OSError, ValueError, KeyError, IndexError) as exc:
            raise ReleaseUnavailable(f"could not read obographs JSON at {path}: {exc}") from exc
        self.path = Path(path)
        #: The release date, taken from the graph's own ``meta.version`` IRI.
        self.version: str | None = release_date((graph.get("meta") or {}).get("version"))
        self._labels: dict[str, str] = {}
        self._deprecated: set[str] = set()
        self._exact_synonyms: dict[str, list[str]] = {}
        for node in graph.get("nodes", []):
            if node.get("type") != "CLASS":
                continue
            curie = _curie(node.get("id", ""))
            if curie is None:
                continue
            meta = node.get("meta") or {}
            self._labels[curie] = node.get("lbl", "")
            if meta.get("deprecated"):
                self._deprecated.add(curie)
            exact = [
                s["val"] for s in meta.get("synonyms", []) if s.get("pred") == "hasExactSynonym"
            ]
            if exact:
                self._exact_synonyms[curie] = exact

    @classmethod
    def load(cls, prefix: str, date: str, cache: Path) -> PinnedRelease:
        """Fetch (if needed) and read the release for ``prefix``/``date``."""
        return cls(fetch_release(prefix, date, cache))

    # -- the oaklib-shaped surface the validator calls ---------------------------------
    def label(self, curie: str) -> str | None:
        return self._labels.get(curie)

    def obsoletes(self) -> Iterator[str]:
        yield from sorted(self._deprecated)

    def entity_metadata_map(self, curie: str) -> dict[str, list[str]]:
        return {"oio:hasExactSynonym": list(self._exact_synonyms.get(curie, []))}

    def __len__(self) -> int:
        return len(self._labels)
