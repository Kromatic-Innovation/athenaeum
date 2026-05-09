# SPDX-License-Identifier: Apache-2.0
"""Apollo.io connector — people enrichment for type:person wikis.

Two surfaces:

- :class:`ApolloClient` — generic REST wrapper around the Apollo API.
  Supports ``people_match`` (single-call, recommended) and
  ``people_bulk_match`` (opt-in, **not recommended** — see warning below).
- :func:`enrich_person` — given a person wiki frontmatter dict, build the
  Apollo request, call the API, and return an :class:`EnrichResult` with
  the fields that should be written and the matching ``field_sources``
  entries (``api:apollo:<YYYY-MM-DD>``). Caller composes the write.

Known bug — bulk_match silently drops valid matches
---------------------------------------------------
Apollo's ``/people/bulk_match`` endpoint silently returns ~70% fewer
matches than the identical request shape sent to ``/people/match`` one
record at a time. Verified 2026-05-08 by replaying ten "no_match" rows:
bulk returned 3/10, single returned 10/10. The default for any caller
in this module (including :func:`enrich_person`) is **single-call**.
:meth:`ApolloClient.people_bulk_match` is preserved as opt-in only and
emits a :class:`UserWarning` so opt-in callers are reminded of the cost.
Do not change the default.

Provenance (#90)
----------------
Every field returned in :class:`EnrichResult` carries a corresponding
entry in ``field_sources`` of the canonical form
``api:apollo:<YYYY-MM-DD>`` (UTC date of the enrichment call). Callers
merge ``fields`` into wiki frontmatter and ``field_sources`` into the
wiki's ``field_sources`` map.

YAML round-trip
---------------
This connector returns plain Python dicts/lists. Persistence uses
:func:`athenaeum.models.render_frontmatter` (PyYAML ``safe_dump`` with
``sort_keys=False``), which produces YAML that round-trips cleanly
through ``yaml.safe_load``. The connector does NOT splice raw lines
into existing frontmatter text — that legacy path (the cwc-side
``apollo_enrich_warm_tier.py``) is the source of the
indent-corruption bug tracked in
``reference_apollo_enricher_yaml_corruption``. Lane E uses dict
composition + ``render_frontmatter`` instead.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

APOLLO_BASE = "https://api.apollo.io/api/v1"
DEFAULT_USER_AGENT = "athenaeum/0.x (apollo-enrichment)"
# Apollo's edge (Cloudflare) blocks the default Python urllib UA with a
# 1010 challenge. A normal-looking UA bypasses it.

# 1Password lookup defaults — used only if APOLLO_API_KEY env-var is
# unset. Keeps parity with the cwc-side wrapper so existing operators
# don't need to re-stash the key.
OP_VAULT_DEFAULT = "Agent Tools"
OP_ITEM_DEFAULT = "Apollo API Key"
OP_FIELD_DEFAULT = "credential"


class ApolloError(RuntimeError):
    """Raised when the Apollo API returns an unrecoverable error."""


def _resolve_api_key(
    explicit: str | None = None,
    *,
    op_vault: str | None = None,
    op_item: str | None = None,
    op_field: str | None = None,
) -> str:
    """Resolve the Apollo API key.

    Order: explicit arg → ``APOLLO_API_KEY`` env var → 1Password (``op``).
    Raises :class:`ApolloError` if no key can be obtained.
    """
    if explicit:
        return explicit.strip()
    env = os.environ.get("APOLLO_API_KEY")
    if env:
        return env.strip()
    vault = op_vault or os.environ.get("APOLLO_1PASSWORD_VAULT", OP_VAULT_DEFAULT)
    item = op_item or os.environ.get("APOLLO_1PASSWORD_ITEM", OP_ITEM_DEFAULT)
    field_name = op_field or os.environ.get("APOLLO_1PASSWORD_FIELD", OP_FIELD_DEFAULT)
    try:
        out = subprocess.check_output(
            ["op", "read", f"op://{vault}/{item}/{field_name}"],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()
    except FileNotFoundError as exc:
        raise ApolloError(
            "Apollo API key not found: pass api_key=, set APOLLO_API_KEY, "
            "or install the `op` CLI for 1Password lookup."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ApolloError(
            f"Apollo API key lookup via 1Password failed: {exc.stderr.strip()}"
        ) from exc
    if not out:
        raise ApolloError("Apollo API key resolved but is empty.")
    return out


def _apollo_source(date_str: str | None = None) -> str:
    """Return the canonical provenance source string for an Apollo claim.

    Form: ``api:apollo:<YYYY-MM-DD>``. ``date_str`` may be supplied for
    determinism in tests; otherwise UTC ``today`` is used.
    """
    if date_str is None:
        date_str = datetime.now(tz=timezone.utc).date().isoformat()
    return f"api:apollo:{date_str}"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ApolloClient:
    """Minimal Apollo.io REST client.

    Parameters
    ----------
    api_key:
        Explicit key. If omitted, falls back to ``APOLLO_API_KEY`` env
        then 1Password ``op``.
    base_url:
        Apollo API base. Override only for tests / mock servers.
    user_agent:
        Sent as ``User-Agent``. Default identifies this client; override
        only when the caller wants its own attribution.
    transport:
        Optional callable ``(method, url, headers, body) -> dict`` for
        injecting a fake transport in tests. When set, no network I/O is
        attempted.
    retries:
        Number of additional attempts after the first failed call.
        Retried on HTTP 429 + 5xx + transient network errors with
        exponential backoff (``2 ** attempt`` seconds).
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = APOLLO_BASE,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: Any = None,
        retries: int = 2,
    ) -> None:
        self._api_key = (
            _resolve_api_key(api_key) if transport is None else (api_key or "test")
        )
        self._base_url = base_url.rstrip("/")
        self._user_agent = user_agent
        self._transport = transport
        self._retries = max(0, int(retries))

    # -- low-level ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        url = f"{self._base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Api-Key": self._api_key,
            "User-Agent": self._user_agent,
        }
        body = json.dumps(payload).encode() if payload is not None else None

        if self._transport is not None:
            return self._transport(method, url, headers, body)

        last_err: str | None = None
        for attempt in range(self._retries + 1):
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode(errors="replace")
                last_err = f"{exc.code} {err_body[:200]}"
                retryable = exc.code == 429 or exc.code >= 500
                if retryable and attempt < self._retries:
                    time.sleep(2**attempt)
                    continue
                raise ApolloError(f"Apollo API error: {last_err}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_err = str(exc)
                if attempt < self._retries:
                    time.sleep(1)
                    continue
                raise ApolloError(f"Apollo network error: {last_err}") from exc
        raise ApolloError(f"Apollo retries exhausted: {last_err}")

    # -- people/match (single, recommended) --------------------------------

    def people_match(
        self,
        *,
        email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        name: str | None = None,
        organization_name: str | None = None,
        domain: str | None = None,
        linkedin_url: str | None = None,
        reveal_personal_emails: bool = False,
    ) -> dict | None:
        """Single-call match. Returns the matched person dict or None."""
        payload: dict = {}
        if email:
            payload["email"] = email
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if name:
            payload["name"] = name
        if organization_name:
            payload["organization_name"] = organization_name
        if domain:
            payload["domain"] = domain
        if linkedin_url:
            payload["linkedin_url"] = linkedin_url
        if reveal_personal_emails:
            payload["reveal_personal_emails"] = True

        if not payload:
            return None

        resp = self._request("POST", "/people/match", payload=payload)
        person = resp.get("person")
        return person or None

    # -- people/bulk_match (opt-in, NOT recommended) -----------------------

    def people_bulk_match(self, records: list[dict]) -> list[dict | None]:
        """Bulk match — **not recommended**.

        Apollo's ``/people/bulk_match`` silently drops ~70% of valid
        matches that ``/people/match`` accepts with the identical
        per-record payload. Verified 2026-05-08. Use
        :meth:`people_match` in a loop instead. Preserved here for
        callers that have explicitly accepted the trade-off (e.g.
        replays where partial coverage is acceptable).
        """
        if not records:
            return []
        if len(records) > 10:
            raise ValueError("Apollo bulk_match accepts at most 10 records per call")
        warnings.warn(
            "Apollo /people/bulk_match silently drops ~70% of valid matches "
            "vs single-call /people/match. Use ApolloClient.people_match in "
            "a loop instead. See "
            "athenaeum.connectors.apollo for details.",
            UserWarning,
            stacklevel=2,
        )
        payload = {"details": records}
        resp = self._request("POST", "/people/bulk_match", payload=payload)
        matches = resp.get("matches") or []
        out: list[dict | None] = []
        for m in matches:
            if m and m.get("id"):
                out.append(m)
            else:
                out.append(None)
        while len(out) < len(records):
            out.append(None)
        return out[: len(records)]


# ---------------------------------------------------------------------------
# Enrichment surface
# ---------------------------------------------------------------------------


@dataclass
class EnrichResult:
    """Output of :func:`enrich_person`.

    ``fields`` maps frontmatter keys to values to write. ``field_sources``
    is the matching per-claim provenance map keyed by the same field
    names; merging it into the wiki's existing ``field_sources`` is the
    caller's responsibility.

    ``matched`` is True iff Apollo returned a person record. When False,
    ``fields`` and ``field_sources`` are empty.
    """

    matched: bool
    fields: dict[str, Any] = field(default_factory=dict)
    field_sources: dict[str, str] = field(default_factory=dict)
    raw: dict | None = None


def _split_name(full: str) -> tuple[str, str]:
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _derive_current_org(person: dict) -> str:
    history = person.get("employment_history") or []
    for h in history:
        if h.get("current") and (h.get("organization_name") or "").strip():
            return h["organization_name"].strip()
    org = person.get("organization") or {}
    if org.get("name"):
        return str(org["name"]).strip()
    return ""


def _derive_location(person: dict) -> str:
    parts = []
    for k in ("city", "state", "country"):
        v = (person.get(k) or "").strip()
        if v:
            parts.append(v)
    return ", ".join(parts)


def _short_employment_history(person: dict, n: int = 5) -> list[dict]:
    history = person.get("employment_history") or []
    out: list[dict] = []
    for h in history[:n]:
        entry = {
            "title": (h.get("title") or "").strip(),
            "organization_name": (h.get("organization_name") or "").strip(),
            "current": bool(h.get("current")),
        }
        if h.get("start_date"):
            entry["start_date"] = str(h["start_date"])
        if h.get("end_date"):
            entry["end_date"] = str(h["end_date"])
        out.append(entry)
    return out


def build_match_request(meta: dict) -> dict | None:
    """Build an Apollo ``people_match`` kwargs dict from a person wiki dict.

    Returns ``None`` if there is not enough identifying data (no email,
    no LinkedIn URL, no last name).
    """
    name = str(meta.get("name") or "").strip()
    if not name or name in ("(unknown)", "(no name)"):
        return None
    first, last = _split_name(name)
    req: dict = {}
    emails = meta.get("emails") or []
    if isinstance(emails, list) and emails:
        req["email"] = str(emails[0]).strip().lower()
    elif isinstance(meta.get("email"), str) and meta["email"]:
        req["email"] = str(meta["email"]).strip().lower()
    if first:
        req["first_name"] = first
    if last:
        req["last_name"] = last
    linkedin = str(meta.get("linkedin_url") or "").strip()
    if linkedin:
        req["linkedin_url"] = linkedin
    if not (req.get("email") or req.get("linkedin_url") or req.get("last_name")):
        return None
    return req


def enrich_person(
    meta: dict,
    client: ApolloClient,
    *,
    today: str | None = None,
) -> EnrichResult:
    """Enrich a person wiki frontmatter dict via Apollo.

    Returns an :class:`EnrichResult` with the fields to write plus a
    per-field provenance map. Caller composes the write — this function
    is pure aside from the Apollo API call.

    ``today`` overrides the date stamp embedded in ``field_sources``
    (useful for deterministic tests). Defaults to UTC today.
    """
    request = build_match_request(meta)
    if request is None:
        return EnrichResult(matched=False)

    person = client.people_match(**request)
    if not person:
        return EnrichResult(matched=False)

    src = _apollo_source(today)
    fields: dict[str, Any] = {}
    field_sources: dict[str, str] = {}

    def set_field(key: str, value: Any) -> None:
        if value in (None, "", [], {}):
            return
        fields[key] = value
        field_sources[key] = src

    apollo_id = (person.get("id") or "").strip()
    set_field("apollo_id", apollo_id)

    title = (person.get("title") or "").strip()
    set_field("current_title", title)

    company = _derive_current_org(person)
    set_field("current_company", company)

    headline = (person.get("headline") or "").strip()
    set_field("apollo_headline", headline)

    location = _derive_location(person)
    set_field("apollo_location", location)

    # Fill linkedin_url ONLY if missing on input — do not clobber
    # operator-curated values. Same pattern for twitter / github.
    if not str(meta.get("linkedin_url") or "").strip():
        set_field("linkedin_url", (person.get("linkedin_url") or "").strip())
    if not str(meta.get("twitter_url") or "").strip():
        set_field("twitter_url", (person.get("twitter_url") or "").strip())
    if not str(meta.get("github_url") or "").strip():
        set_field("github_url", (person.get("github_url") or "").strip())

    history = _short_employment_history(person, n=5)
    if history:
        set_field("apollo_employment_history", history)

    # Stamp the enrichment date on its own field so downstream "skip
    # recently enriched" logic works without parsing field_sources.
    if today is None:
        today = datetime.now(tz=timezone.utc).date().isoformat()
    set_field("apollo_enriched_on", today)

    return EnrichResult(
        matched=True,
        fields=fields,
        field_sources=field_sources,
        raw=person,
    )


__all__ = [
    "ApolloClient",
    "ApolloError",
    "EnrichResult",
    "_apollo_source",
    "build_match_request",
    "enrich_person",
]
