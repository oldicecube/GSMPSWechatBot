"""Request-scoped identity evidence; no hand-written character/artist mappings."""
import csv
import logging
import re
from functools import lru_cache
from pathlib import Path

import requests

LOG = logging.getLogger(__name__)
ARTIST_RE = re.compile(r"@([A-Za-z0-9_-]+)")
VOCAB = Path(__file__).parent / "vendor" / "good_anima" / "anima-1.0.csv"


def normalized(value):
    return str(value).replace(r"\(", "(").replace(r"\)", ")").replace("_", " ").strip().casefold().lstrip("@")


@lru_cache(maxsize=1)
def vocabulary():
    lookup, stems = {}, {}
    with VOCAB.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.reader(file):
            if len(row)<3 or row[1] not in {"1", "3", "4"}: continue
            category=int(row[1]); name=row[0]
            for key in [name]+(row[3].split(",") if len(row)>3 else []):
                if not key: continue
                lookup.setdefault((category,normalized(key)),set()).add(name)
            if category==4:
                stem=normalized(name).split(" (")[0]
                stems.setdefault(stem,set()).add(name)
    return lookup,stems


def exact_tag(value, category):
    matches=vocabulary()[0].get((category,normalized(value)),set())
    return next(iter(matches)) if len(matches)==1 else None


def is_character(value):
    key=normalized(value)
    return bool(vocabulary()[0].get((4,key)) or vocabulary()[1].get(key))


@lru_cache(maxsize=128)
def public_character(source):
    """Exact public wiki alias + model vocabulary intersection, never fuzzy popularity."""
    if not isinstance(source,str) or not 1<=len(source)<=40: return None
    try:
        response=requests.get("https://danbooru.donmai.us/wiki_pages.json",
            params={"search[other_names_match]":source,"limit":5},
            headers={"User-Agent":"Anima-Identity-Resolver/1.0"},timeout=5)
        response.raise_for_status()
        matches=[]
        for page in response.json():
            if not isinstance(page,dict) or page.get("is_deleted"):continue
            aliases=[page.get("title","")]+page.get("other_names",[])
            if not any(normalized(a)==normalized(source) for a in aliases): continue
            canonical=exact_tag(page.get("title",""),4)
            if not canonical:continue
            # Only a parenthesized suffix already present in the verified title.
            suffix=re.search(r"\(([^()]*)\)$",normalized(canonical))
            series=exact_tag(suffix.group(1),3) if suffix else None
            matches.append({"source_text":source,"canonical_tag":normalized(canonical),
                "series_tag":normalized(series) if series else "",
                "evidence_url":"https://danbooru.donmai.us/wiki_pages/"+str(page["id"]),
                "evidence_kind":"exact_public_alias_and_model_vocabulary"})
        return matches[0] if len(matches)==1 else None
    except (requests.RequestException,ValueError,TypeError,KeyError):
        LOG.info("Public identity lookup unavailable; no identity assumed")
        return None


def request_hints(original, allow_lookup=False):
    artists=[]
    for source in ARTIST_RE.findall(original):
        tag=exact_tag(source,1)
        # Preserve explicit unknown names, but never invent a different artist id.
        value=normalized(tag) if tag else source.lower()
        if value not in artists:artists.append(value)
    remainder=ARTIST_RE.sub("",original).strip()
    characters=[]
    # Only the short candidate identity, not an entire private scene description.
    if allow_lookup and artists and re.fullmatch(r"[\w ()-]{1,40}",remainder,re.UNICODE):
        found=public_character(remainder)
        if found:characters.append(found)
    return {"artists":artists,"characters":characters,"identity_only":bool(characters)}
