#!/usr/bin/env python3
"""
Fetch openly licensed household test photos from Wikimedia Commons.

    python3 dgx/fetch_testpics.py            # into dgx/testpics/
    python3 dgx/fetch_testpics.py --per 4    # photos per subject

For timing and plumbing tests of the checker before Sidd's own photos
exist. These are not glasses frames from his home, so they say nothing
about accuracy on the real question set; the eval capture (item F) is
for that.

Only files whose Commons licence is public domain, CC0, CC BY or CC BY-SA
are kept, and dgx/testpics/ATTRIBUTION.md records title, author,
licence and source page for each, which CC BY and CC BY-SA require when
the files sit in the public repo. Downloads the 1280-pixel rendition,
not the original. Standard library only.
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://commons.wikimedia.org/w/api.php"
UA = "truss-testpics/1.0 (https://github.com/ClimbOutLabs/truss)"
OUT = Path(__file__).resolve().parent / "testpics"

# subject -> Commons search text. The last two are deliberately
# uninformative for every question (the cannot_tell probes).
SUBJECTS = {
    "stove": "gas stove knobs kitchen",
    "door": "door deadbolt lock",
    "pills": "weekly pill organizer",
    "tv": "television living room",
    "floor": "wooden floor texture",
    "ceiling": "white ceiling",
}

OK_LICENCES = re.compile(r"^(public domain|pd\b|cc0|cc[- ]by(-sa)?[- ]?\d)", re.I)


def get_json(params):
    url = API + "?" + urllib.parse.urlencode(dict(params, format="json"))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def plain(value):
    """Commons extmetadata values are HTML snippets."""
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def candidates(search, limit):
    data = get_json({
        "action": "query",
        "generator": "search",
        "gsrsearch": f"{search} filetype:bitmap",
        "gsrnamespace": 6,
        "gsrlimit": limit,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|mime",
        "iiurlwidth": 1280,
    })
    pages = (data.get("query") or {}).get("pages") or {}
    for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        licence = plain((meta.get("LicenseShortName") or {}).get("value"))
        if info.get("mime") != "image/jpeg" or not OK_LICENCES.match(licence):
            continue
        yield {
            "title": page["title"],
            "url": info.get("thumburl") or info.get("url"),
            "page": info.get("descriptionurl"),
            "licence": licence,
            "artist": plain((meta.get("Artist") or {}).get("value")) or "unknown",
        }


def main():
    parser = argparse.ArgumentParser(description="Fetch open-licence test photos from Wikimedia Commons.")
    parser.add_argument("--per", type=int, default=3, help="photos per subject (default 3)")
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    credits = ["# Test photo credits", "", "From Wikimedia Commons; each keeps its own licence.", ""]
    for subject, search in SUBJECTS.items():
        got = 0
        try:
            found = list(candidates(search, 20))
        except Exception as e:
            print(f"{subject}: search failed ({e})", file=sys.stderr)
            continue
        for c in found:
            if got >= args.per:
                break
            name = f"{subject}-{got + 1}.jpg"
            req = urllib.request.Request(c["url"], headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    (OUT / name).write_bytes(resp.read())
            except Exception as e:
                print(f"{subject}: skipped {c['title']} ({e})", file=sys.stderr)
                continue
            got += 1
            credits.append(f"- `{name}`: {c['title'][5:]} by {c['artist']}, {c['licence']}, {c['page']}")
            time.sleep(0.5)  # be polite to Commons
        print(f"{subject}: {got} photos", file=sys.stderr)
    (OUT / "ATTRIBUTION.md").write_text("\n".join(credits) + "\n")
    print(f"Saved to {OUT}. Look through them before committing: search results can be off-topic.")


if __name__ == "__main__":
    main()
