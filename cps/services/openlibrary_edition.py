import re
import requests
from functools import lru_cache
from cps import constants

HEADERS = {"User-Agent": constants.USER_AGENT}   # add a contact email later for 3 req/s
_LOC = re.compile(r"^marc:([^/]+)/([^:]+):(\d+):(\d+)$")

class OLUnavailable(Exception):          # timeout / 403 / 429 / 5xx, as opposed to "no such book"
    pass

@lru_cache(maxsize=128)
def fetch_edition(isbn13):
    try:
        r = requests.get(f"https://openlibrary.org/isbn/{isbn13}.json",
                         headers=HEADERS, timeout=(10, 30))
    except requests.RequestException as exc:
        raise OLUnavailable(type(exc).__name__) from exc
    if r.status_code == 404:
        return None
    if r.status_code in (403, 429) or r.status_code >= 500:
        raise OLUnavailable(f"HTTP {r.status_code}")
    r.raise_for_status()
    return r.json()

def marc_locators(edition):
    return [m.groups() for s in edition.get("source_records", []) if (m := _LOC.match(s))]

@lru_cache(maxsize=128)
def fetch_marc(item, fname, off, ln):
    off, ln = int(off), int(ln)
    r = requests.get(f"https://archive.org/download/{item}/{fname}",
                     headers={**HEADERS, "Range": f"bytes={off}-{off+ln-1}"},
                     stream=True, timeout=(10, 30))
    if r.status_code != 206:             # a 200 would mean the whole ~200 MB file
        r.close()
        raise OLUnavailable(f"archive.org HTTP {r.status_code}")
    data = r.content
    if int(data[:5]) != len(data) or data[-1:] != b"\x1d":
        raise ValueError("incomplete MARC record")
    return data

def parse_marc(data):
    """Binary MARC21 to {tag: [str | {subfield: [values]}]}. UTF-8 records only."""
    if data[9:10] != b"a":                # leader/09: 'a' = UTF-8, blank = MARC-8
        raise ValueError("MARC-8 record: store the raw file, skip parsing")
    base = int(data[12:17])
    out, directory = {}, data[24:base - 1]
    for i in range(0, len(directory), 12):
        tag = directory[i:i+3].decode()
        ln, st = int(directory[i+3:i+7]), int(directory[i+7:i+12])
        field = data[base + st: base + st + ln - 1]
        if tag < "010":
            out.setdefault(tag, []).append(field.decode("utf-8"))
        else:
            subs = {}
            for part in field[2:].split(b"\x1f"):
                if part:
                    subs.setdefault(chr(part[0]), []).append(part[1:].decode("utf-8"))
            out.setdefault(tag, []).append(subs)
    return out

def series_from_marc(rec):
    for tag in ("830", "490"):            # 830 = authorised form, 490 = as printed
        for f in rec.get(tag, []):
            name = re.sub(r"\s*\([^)]*\)", "", (f.get("a") or [""])[0]).strip(" ;,.:/")
            m = re.search(r"\d+(?:\.\d+)?", (f.get("v") or [""])[0])
            if name:
                return name, float(m.group()) if m else None
    return None, None