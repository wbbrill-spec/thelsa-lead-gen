"""ZoomInfo GTM API client (client-credentials).

Why a rewrite (2026-09-03)
--------------------------
The previous MOD-05 code called ``/contacts/enrich`` with only a company name
and a job title. ZoomInfo's contact enrich needs a *person* identifier —
``personId``, an email/phone, or first+last name plus company — so that call
could never match and every lead silently fell back to web search.

The documented flow is two steps:

  1. **Search** ``POST /gtm/data/v1/contacts/search`` by company (website or
     name) + target job titles → returns candidate people with ``personId``
     and ``contactAccuracyScore`` (no email/phone; free).
  2. **Enrich** ``POST /gtm/data/v1/contacts/enrich`` with the chosen
     ``personId`` → returns verified business email / phone (costs a credit).

Envelope: the GTM API is JSON:API-style — ``{"data": {"type": ..., "attributes": {...}}}``
(``type`` = ``ContactSearch`` per the API conventions doc). The exact enrich
``type`` string is not shown in the public docs, so ``enrich_person`` tries a
short list of candidate envelopes on HTTP 400 and remembers the one that works.
Everything is logged with the ``[ZI]`` prefix and counted in ``STATS`` so the
Runs page can show what ZoomInfo actually did.
"""
from __future__ import annotations

import time
import threading

import requests

import config

STATS = {"search_calls": 0, "search_hits": 0, "enrich_calls": 0, "enrich_hits": 0,
         "http_errors": 0, "last_error": ""}

OUTPUT_FIELDS = [
    "firstName", "lastName", "jobTitle", "email", "phone", "mobilePhone",
    "companyName", "contactAccuracyScore", "managementLevel", "externalUrls",
]

_TOKEN = {"token": None, "expires_at": 0.0}
_TOKEN_LOCK = threading.Lock()
_WORKING_ENRICH_ENVELOPE = {"idx": None}   # remembered across calls in this process

_HEADERS_BASE = {"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "TMS-LeadGen/2.0"}


class ZoomInfoError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.ZOOMINFO_CLIENT_ID and config.ZOOMINFO_CLIENT_SECRET)


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_token() -> str:
    with _TOKEN_LOCK:
        if _TOKEN["token"] and _TOKEN["expires_at"] > time.time() + 60:
            return _TOKEN["token"]
        r = requests.post(
            config.ZOOMINFO_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={"grant_type": "client_credentials",
                  "client_id": config.ZOOMINFO_CLIENT_ID,
                  "client_secret": config.ZOOMINFO_CLIENT_SECRET},
            timeout=15,
        )
        if r.status_code >= 400:
            STATS["http_errors"] += 1
            STATS["last_error"] = f"token HTTP {r.status_code}: {r.text[:300]}"
            raise ZoomInfoError(STATS["last_error"])
        j = r.json()
        _TOKEN["token"] = j["access_token"]
        _TOKEN["expires_at"] = time.time() + float(j.get("expires_in", 3600))
        return _TOKEN["token"]


def _post(path: str, payload: dict, params: dict | None = None, timeout: int = 20) -> requests.Response:
    url = f"{config.ZOOMINFO_BASE_URL}{path}"
    for attempt in range(3):
        headers = dict(_HEADERS_BASE)
        headers["Authorization"] = f"Bearer {get_token()}"
        r = requests.post(url, headers=headers, json=payload, params=params, timeout=timeout)
        if r.status_code == 401 and attempt == 0:
            _TOKEN["token"] = None
            continue
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(min(float(r.headers.get("Retry-After", 2 ** attempt)), 20))
            continue
        return r
    return r


# ── Search ────────────────────────────────────────────────────────────────────

def search_people(company_name: str, domain: str, titles: list[str],
                  management_levels: list[str] | None = None, page_size: int = 5) -> list[dict]:
    """Return candidate people (dicts with at least ``id``) at the company,
    best contactAccuracyScore first. Tries website first, then company name."""
    attempts = []
    if domain:
        attempts.append({"companyWebsite": _website(domain)})
    if company_name:
        attempts.append({"companyName": company_name})

    for base in attempts:
        attrs = dict(base)
        if titles:
            attrs["jobTitleList"] = [_clean_title(t) for t in titles][:25]
        if management_levels:
            attrs["managementLevelList"] = management_levels
        attrs["requiredFieldsList"] = ["email"]
        people = _search_once(attrs, page_size)
        if people:
            return people
        # Same company, no title match → widen to management level only
        if titles:
            attrs.pop("jobTitleList", None)
            attrs["managementLevelList"] = management_levels or ["C Level Exec", "VP Level Exec", "Director", "Manager"]
            people = _search_once(attrs, page_size)
            if people:
                return people
    return []


def _search_once(attrs: dict, page_size: int) -> list[dict]:
    STATS["search_calls"] += 1
    payload = {"data": {"type": "ContactSearch", "attributes": attrs}}
    r = _post("/data/v1/contacts/search", payload,
              params={"page[size]": page_size, "sort": "-contactAccuracyScore"})
    if r.status_code >= 400:
        STATS["http_errors"] += 1
        STATS["last_error"] = f"search HTTP {r.status_code}: {r.text[:400]}"
        print(f"[ZI] {STATS['last_error']} attrs={attrs}")
        return []
    people = _people_from(r.json())
    print(f"[ZI] search {attrs.get('companyWebsite') or attrs.get('companyName')!r} "
          f"titles={len(attrs.get('jobTitleList', []))} → {len(people)} people")
    if people:
        STATS["search_hits"] += 1
    return people


def _people_from(body) -> list[dict]:
    """Normalise a JSON:API search response to flat dicts with ``id``."""
    out = []
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        data = data.get("data") or data.get("result") or [data]
    for item in data or []:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else item
        pid = item.get("id") or attrs.get("id") or attrs.get("personId")
        if not pid:
            continue
        out.append({
            "id": str(pid),
            "firstName": attrs.get("firstName", ""),
            "lastName": attrs.get("lastName", ""),
            "jobTitle": attrs.get("jobTitle", ""),
            "contactAccuracyScore": attrs.get("contactAccuracyScore"),
            "hasEmail": attrs.get("hasEmail"),
            "managementLevel": attrs.get("managementLevel", ""),
        })
    out.sort(key=lambda p: (p.get("contactAccuracyScore") or 0), reverse=True)
    return out


# ── Enrich ────────────────────────────────────────────────────────────────────

def _enrich_envelopes(match: dict) -> list[dict]:
    inner = {"matchPersonInput": [match], "outputFields": OUTPUT_FIELDS}
    return [
        {"data": {"type": "ContactEnrich", "attributes": inner}},
        {"data": {"type": "EnrichContact", "attributes": inner}},
        {"data": {"attributes": inner}},
        {"data": inner},
        inner,
    ]


def enrich_person(person_id: str | None = None, **match_fields) -> dict | None:
    """Enrich one person (by personId, or by name+company) → normalised contact dict."""
    match = {"personId": person_id} if person_id else dict(match_fields)
    envelopes = _enrich_envelopes(match)
    order = list(range(len(envelopes)))
    if _WORKING_ENRICH_ENVELOPE["idx"] is not None:
        order.remove(_WORKING_ENRICH_ENVELOPE["idx"])
        order.insert(0, _WORKING_ENRICH_ENVELOPE["idx"])

    STATS["enrich_calls"] += 1
    last = None
    for idx in order:
        r = _post("/data/v1/contacts/enrich", envelopes[idx])
        last = r
        if r.status_code == 400:
            print(f"[ZI] enrich envelope #{idx} rejected (400): {r.text[:200]}")
            continue
        if r.status_code >= 400:
            STATS["http_errors"] += 1
            STATS["last_error"] = f"enrich HTTP {r.status_code}: {r.text[:400]}"
            print(f"[ZI] {STATS['last_error']}")
            return None
        _WORKING_ENRICH_ENVELOPE["idx"] = idx
        person = extract_person(r.json())
        if person and (person.get("firstName") or person.get("lastName")):
            STATS["enrich_hits"] += 1
            return person
        print(f"[ZI] enrich {match}: no match in response")
        return None

    STATS["http_errors"] += 1
    STATS["last_error"] = f"enrich: every envelope rejected; last {last.status_code if last else '?'}: {last.text[:300] if last else ''}"
    print(f"[ZI] {STATS['last_error']}")
    return None


def extract_person(obj) -> dict | None:
    """Walk any response shape and return the first record with name/email fields."""
    if isinstance(obj, dict):
        lower = {k.lower(): v for k, v in obj.items()}
        if ("firstname" in lower or "lastname" in lower) and ("email" in lower or "jobtitle" in lower):
            ext = lower.get("externalurls") or {}
            linkedin = ""
            if isinstance(ext, dict):
                linkedin = ext.get("linkedin") or ext.get("linkedIn") or ""
            elif isinstance(ext, list):
                linkedin = next((u for u in ext if isinstance(u, str) and "linkedin" in u.lower()), "")
            return {
                "firstName": lower.get("firstname") or "",
                "lastName": lower.get("lastname") or "",
                "jobTitle": lower.get("jobtitle") or lower.get("title") or "",
                "email": lower.get("email") or "",
                "phone": lower.get("phone") or "",
                "mobilePhone": lower.get("mobilephone") or "",
                "contactAccuracyScore": lower.get("contactaccuracyscore"),
                "managementLevel": lower.get("managementlevel") or "",
                "linkedin": linkedin,
                "personId": lower.get("id") or lower.get("personid"),
            }
        for v in obj.values():
            found = extract_person(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = extract_person(v)
            if found:
                return found
    return None


# ── High level ────────────────────────────────────────────────────────────────

def find_contact(company_name: str, domain: str, titles: list[str]) -> dict | None:
    """Search → pick best person → enrich. Returns the MOD-05 contact dict or None."""
    if not configured():
        return None
    try:
        people = search_people(company_name, domain, titles)
        if not people:
            return None
        # Prefer people whose search record says an email exists.
        people.sort(key=lambda p: (bool(p.get("hasEmail")), p.get("contactAccuracyScore") or 0), reverse=True)
        for person in people[:2]:   # at most two credits per company
            enriched = enrich_person(person_id=person["id"])
            if enriched and enriched.get("email"):
                full_name = f"{enriched['firstName']} {enriched['lastName']}".strip()
                return {
                    "full_name": full_name,
                    "title": enriched.get("jobTitle") or person.get("jobTitle", ""),
                    "email": enriched.get("email", ""),
                    "phone": enriched.get("phone") or enriched.get("mobilePhone") or "",
                    "enrichment_source": "zoominfo",
                    "enrichment_raw": {**enriched, "search_record": person},
                }
        return None
    except ZoomInfoError as e:
        print(f"[ZI] {e}")
        return None
    except requests.RequestException as e:
        STATS["http_errors"] += 1
        STATS["last_error"] = f"network: {e}"
        print(f"[ZI] network error: {e}")
        return None


def diagnose(company_name: str, domain: str, titles: list[str]) -> dict:
    """Run the full flow once and return every raw request/response for the admin page."""
    out: dict = {"configured": configured(), "company": company_name, "domain": domain, "titles": titles}
    if not configured():
        return out
    try:
        get_token()
        out["token_ok"] = True
    except Exception as e:
        out["token_ok"] = False
        out["token_error"] = str(e)
        return out

    attrs = {"companyWebsite": _website(domain)} if domain else {"companyName": company_name}
    attrs["jobTitleList"] = [_clean_title(t) for t in titles]
    attrs["requiredFieldsList"] = ["email"]
    payload = {"data": {"type": "ContactSearch", "attributes": attrs}}
    r = _post("/data/v1/contacts/search", payload, params={"page[size]": 5, "sort": "-contactAccuracyScore"})
    out["search"] = {"request": payload, "status": r.status_code, "response": _safe_json(r)}
    people = _people_from(r.json()) if r.status_code < 400 else []
    out["people"] = people
    if people:
        match = {"personId": people[0]["id"]}
        tries = []
        for idx, env in enumerate(_enrich_envelopes(match)):
            rr = _post("/data/v1/contacts/enrich", env)
            tries.append({"envelope": idx, "request": env, "status": rr.status_code, "response": _safe_json(rr)})
            if rr.status_code != 400:
                out["person"] = extract_person(rr.json()) if rr.status_code < 400 else None
                break
        out["enrich_attempts"] = tries
    out["stats"] = dict(STATS)
    return out


# ── helpers ───────────────────────────────────────────────────────────────────

def _website(domain: str) -> str:
    d = (domain or "").strip()
    return d if d.startswith("http") else f"https://{d}"


def _clean_title(t: str) -> str:
    # jobTitleList only allows letters and spaces
    return "".join(ch for ch in t if ch.isalpha() or ch == " ").strip()


def _safe_json(r: requests.Response):
    try:
        return r.json()
    except Exception:
        return r.text[:2000]
