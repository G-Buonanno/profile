#!/usr/bin/env python3
"""Refresh the publications list and the "Recent publications" sidebar in
index.html from the Scopus API.

Environment:
  SCOPUS_API_KEY     (required) Elsevier API key from https://dev.elsevier.com
  SCOPUS_INSTTOKEN   (optional) Elsevier institutional token; needed for full
                     author lists when running outside the university network
  SCOPUS_AUTHOR_ID   (optional) defaults to 7006985475
"""
import datetime
import html
import json
import os
import pathlib
import re
import sys

import requests

AUTHOR_ID = os.environ.get("SCOPUS_AUTHOR_ID", "7006985475")
API = "https://api.elsevier.com/content/search/scopus"
INDEX = pathlib.Path(__file__).resolve().parent.parent / "index.html"
RECENT_COUNT = 3
MIN_RATIO = 0.9  # refuse to publish if Scopus returns <90% of what we already have


# --------------------------------------------------------------------------
# Scopus
# --------------------------------------------------------------------------
def fetch(view):
    headers = {"X-ELS-APIKey": os.environ["SCOPUS_API_KEY"], "Accept": "application/json"}
    if os.environ.get("SCOPUS_INSTTOKEN"):
        headers["X-ELS-Insttoken"] = os.environ["SCOPUS_INSTTOKEN"]
    params = {
        "query": f"AU-ID({AUTHOR_ID})",
        "view": view,
        "count": 25 if view == "COMPLETE" else 200,
        "cursor": "*",
        "sort": "-coverDate",
    }
    entries = []
    while True:
        r = requests.get(API, params=params, headers=headers, timeout=60)
        r.raise_for_status()
        sr = r.json()["search-results"]
        batch = [e for e in sr.get("entry", []) if "error" not in e]
        if not batch:
            break
        entries += batch
        total = int(sr.get("opensearch:totalResults", 0))
        nxt = sr.get("cursor", {}).get("@next")
        if not nxt or len(entries) >= total:
            break
        params["cursor"] = nxt
    return entries


def fetch_all():
    try:
        return fetch("COMPLETE")  # includes full author lists
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code in (401, 403):
            print("WARNING: COMPLETE view not permitted for this key; using STANDARD "
                  "view (author lists will be kept from the previous data).")
            return fetch("STANDARD")
        raise


# --------------------------------------------------------------------------
# Conversion to the format used by the page
# --------------------------------------------------------------------------
def clean(s):
    s = re.sub(r"</?(inf|sup|sub|i|b)>", "", s or "")
    return " ".join(s.split())


def eid_of(url):
    """Scopus ID from either link style (…eid=2-s2.0-123… or …scp=123…)."""
    m = re.search(r"eid=(2-s2\.0-\d+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"scp=(\d+)", url or "")
    return f"2-s2.0-{m.group(1)}" if m else None


def to_records(entries, old_records):
    old_by_eid = {eid_of(o.get("l")): o for o in old_records if eid_of(o.get("l"))}
    old_by_doi = {o["d"].lower(): o for o in old_records if o.get("d")}
    seen, out = set(), []
    for e in entries:
        eid = e.get("eid", "")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        doi = clean(e.get("prism:doi"))
        old = old_by_eid.get(eid) or (old_by_doi.get(doi.lower()) if doi else None) or {}

        names = [clean(a.get("authname")) for a in (e.get("author") or []) if a.get("authname")]
        if names:
            authors = "; ".join(names)
        elif old.get("a"):
            authors = old["a"]
        else:
            first = clean(e.get("dc:creator"))
            authors = f"{first} et al." if first else ""

        vol, issue = clean(e.get("prism:volume")), clean(e.get("prism:issueIdentifier"))
        volume = f"{vol}({issue})" if vol and issue else vol
        pages = clean(e.get("prism:pageRange")).replace("-", "–") or clean(e.get("article-number"))

        link = next((l["@href"] for l in e.get("link", []) if l.get("@ref") == "scopus"), "")
        link = link or f"https://www.scopus.com/record/display.uri?eid={eid}&origin=inward"

        date = e.get("prism:coverDate", "")
        out.append({
            "_date": date,
            "a": authors,
            "t": clean(e.get("dc:title")),
            "y": int(date[:4]) if date[:4].isdigit() else 0,
            "s": clean(e.get("prism:publicationName")),
            "v": volume,
            "p": pages,
            "c": int(e.get("citedby-count") or 0),
            "d": doi,
            "l": link,
        })
    out.sort(key=lambda r: r["_date"], reverse=True)
    for r in out:
        del r["_date"]
    return out


# --------------------------------------------------------------------------
# index.html rewriting
# --------------------------------------------------------------------------
PUBS_RE = re.compile(r"/\*PUBS_START\*/.*?/\*PUBS_END\*/", re.S)
RECENT_RE = re.compile(r"<!--RECENT_START-->.*?<!--RECENT_END-->", re.S)
UPD_RE = re.compile(r"<!--UPD-->.*?<!--/UPD-->", re.S)


def read_old_records(text):
    m = PUBS_RE.search(text)
    if not m:
        sys.exit("ERROR: PUBS_START/PUBS_END markers not found in index.html")
    payload = m.group(0)[len("/*PUBS_START*/const D="):-len(";/*PUBS_END*/")]
    return json.loads(payload)


def recent_html(records):
    items = []
    for r in records[:RECENT_COUNT]:
        href = f"https://doi.org/{r['d']}" if r["d"] else r["l"]
        items.append(
            f'        <li><a href="{html.escape(href, quote=True)}" target="_blank" '
            f'rel="noopener">{html.escape(r["t"])}</a></li>'
        )
    return "<!--RECENT_START--><ul>\n" + "\n".join(items) + "\n      </ul><!--RECENT_END-->"


def rewrite(text, records, stamp):
    data = json.dumps(records, ensure_ascii=False)
    data = re.sub(r"</(script)", r"<\\u002f\1", data, flags=re.I)  # JSON escape for "/"
    text = PUBS_RE.sub(lambda m: f"/*PUBS_START*/const D={data};/*PUBS_END*/", text, count=1)
    text = RECENT_RE.sub(lambda m: recent_html(records), text, count=1)
    return UPD_RE.sub(lambda m: f"<!--UPD-->{stamp}<!--/UPD-->", text, count=1)


def main():
    original = INDEX.read_bytes().decode("utf-8")
    old = read_old_records(original)

    records = to_records(fetch_all(), old)
    print(f"Scopus returned {len(records)} records; page currently has {len(old)}.")

    if len(records) < MIN_RATIO * len(old):
        sys.exit("ERROR: Scopus returned far fewer records than the page has. "
                 "Not updating (check API key / quota / author ID).")

    # Compare without the date stamp so an unchanged list produces no commit.
    m = UPD_RE.search(original)
    current_stamp = re.sub(r"<!--/?UPD-->", "", m.group(0)) if m else ""
    if rewrite(original, records, current_stamp) == original:
        print("No changes.")
        return

    stamp = " Last updated " + datetime.date.today().strftime("%-d %B %Y") + "."
    INDEX.write_bytes(rewrite(original, records, stamp).encode("utf-8"))

    old_ids = {eid_of(o["l"]) or o["d"] for o in old}
    new = [r for r in records if (eid_of(r["l"]) or r["d"]) not in old_ids]
    print(f"Updated index.html ({len(new)} new publication(s)).")
    for r in new[:20]:
        print("  +", r["y"], r["t"])


if __name__ == "__main__":
    main()
