#!/usr/bin/env python3
"""
Panels comic feed updater.

Fetches comics from each source and writes/updates JSON files in data/.
Designed to run in GitHub Actions on a schedule, but works locally too.

JSON format per file:
{
  "source": "xkcd",
  "updatedAt": "2026-05-25T12:00:00Z",
  "comics": [
    {
      "id": "xkcd-1",
      "title": "Barrel - Part 1",
      "pageUrl": "https://xkcd.com/1/",
      "imageUrl": "https://imgs.xkcd.com/comics/barrel_cropped_(1).jpg",
      "altText": "Don't we all.",
      "publishDate": "2006-01-01",
      "sortIndex": 1136073600
    },
    ...
  ]
}

Comics are sorted newest-first (highest sortIndex first) so clients can
stop reading early once they reach a sortIndex they've already seen.
"""

import json
import os
import sys
import time
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
SCRIPT_DIR = Path(__file__).parent

UA = "PanelsFeedBot/1.0 (https://github.com/PolymerTheory/panels-privacy; comic reader app feed updater)"

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def fetch(url: str, timeout: int = 20) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        print(f"  WARN: fetch failed for {url}: {e}", file=sys.stderr)
        return None


def fetch_text(url: str) -> str | None:
    data = fetch(url)
    return data.decode("utf-8", errors="replace") if data else None


def fetch_json(url: str) -> dict | None:
    data = fetch(url)
    if not data:
        return None
    try:
        return json.loads(data)
    except Exception as e:
        print(f"  WARN: JSON parse failed for {url}: {e}", file=sys.stderr)
        return None


# ── JSON feed helpers ─────────────────────────────────────────────────────────

def load_existing(source_id: str) -> dict:
    path = DATA_DIR / f"{source_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"source": source_id, "updatedAt": "", "comics": []}


def save_feed(source_id: str, comics: list[dict]):
    feed = {
        "source": source_id,
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "comics": sorted(comics, key=lambda c: c["sortIndex"], reverse=True),
    }
    path = DATA_DIR / f"{source_id}.json"
    path.write_text(json.dumps(feed, ensure_ascii=False, indent=2))
    print(f"  Saved {len(comics)} comics → {path.name}")


def merge_comics(existing: list[dict], new_comics: list[dict]) -> tuple[list[dict], int]:
    """Merge new_comics into existing, deduplicating by id. Returns (merged, added_count)."""
    by_id = {c["id"]: c for c in existing}
    added = 0
    for c in new_comics:
        if c["id"] not in by_id:
            by_id[c["id"]] = c
            added += 1
        else:
            # Update if new data has more fields filled in
            existing_entry = by_id[c["id"]]
            for key in ("imageUrl", "altText", "publishDate", "title"):
                if c.get(key) and not existing_entry.get(key):
                    existing_entry[key] = c[key]
    return list(by_id.values()), added


# ── Date parsing ──────────────────────────────────────────────────────────────

def rfc822_to_epoch(date_str: str) -> int | None:
    if not date_str:
        return None
    try:
        return int(parsedate_to_datetime(date_str).timestamp())
    except Exception:
        pass
    # Fallback: try common patterns
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%d %b %Y %H:%M:%S %z"):
        try:
            return int(datetime.strptime(date_str.strip(), fmt).timestamp())
        except ValueError:
            pass
    return None


def epoch_to_date(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


# ── RSS parser ────────────────────────────────────────────────────────────────

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
}


def parse_rss(xml_text: str) -> list[dict]:
    """Parse RSS XML, return list of item dicts with keys: title, link, pubDate, description, contentEncoded, guid."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError as e:
        print(f"  WARN: RSS parse error: {e}", file=sys.stderr)
        return []

    items = []
    channel = root.find("channel")
    if channel is None:
        channel = root
    for item in channel.findall("item"):
        def text(tag, ns=None):
            el = item.find(tag) if ns is None else item.find(tag, ns)
            return (el.text or "").strip() if el is not None else ""

        content_encoded = text("{http://purl.org/rss/1.0/modules/content/}encoded")
        items.append({
            "title":          text("title"),
            "link":           text("link"),
            "guid":           text("guid"),
            "pubDate":        text("pubDate"),
            "description":    text("description"),
            "contentEncoded": content_encoded,
        })
    return items


def extract_img_src(html: str) -> str | None:
    """Extract first img src from an HTML fragment."""
    m = re.search(r'<img\b[^>]+\bsrc=["\']([^"\']+)["\']', html, re.IGNORECASE)
    return m.group(1) if m else None


def extract_img_title(html: str) -> str | None:
    """Extract img title attribute (often used as alt text)."""
    m = re.search(r'<img\b[^>]+\btitle=["\']([^"\']*)["\']', html, re.IGNORECASE)
    return m.group(1) if m else None


def http_to_https(url: str | None) -> str | None:
    if url and url.startswith("http://"):
        return "https://" + url[7:]
    return url


# ── XKCD ─────────────────────────────────────────────────────────────────────

def update_xkcd():
    print("XKCD:")
    existing = load_existing("xkcd")
    comics_by_id = {c["id"]: c for c in existing.get("comics", [])}

    # Find current max comic number
    latest = fetch_json("https://xkcd.com/info.0.json")
    if not latest:
        print("  WARN: could not fetch latest XKCD, skipping")
        return
    max_num = latest["num"]
    known_nums = {int(c["id"].split("-")[1]) for c in comics_by_id.values()}

    # Fetch any missing comics (newest first so we get updates quickly)
    missing = sorted([n for n in range(1, max_num + 1) if n not in known_nums and n != 404],
                     reverse=True)
    print(f"  Latest #{max_num}, known: {len(known_nums)}, missing: {len(missing)}")

    added = 0
    for num in missing:
        data = fetch_json(f"https://xkcd.com/{num}/info.0.json")
        if not data:
            time.sleep(0.5)
            continue
        epoch = int(datetime(int(data["year"]), int(data["month"]), int(data["day"]),
                             tzinfo=timezone.utc).timestamp())
        comic_id = f"xkcd-{num}"
        comics_by_id[comic_id] = {
            "id":          comic_id,
            "title":       data.get("safe_title") or data.get("title", f"#{num}"),
            "pageUrl":     f"https://xkcd.com/{num}/",
            "imageUrl":    data.get("img", ""),
            "altText":     data.get("alt", ""),
            "publishDate": f"{data['year']}-{int(data['month']):02d}-{int(data['day']):02d}",
            "sortIndex":   epoch,
        }
        added += 1
        if added % 100 == 0:
            print(f"  ...fetched {added}/{len(missing)}")
        time.sleep(0.15)  # be polite

    save_feed("xkcd", list(comics_by_id.values()))
    print(f"  Done. Added {added} new comics.")


# ── SMBC ─────────────────────────────────────────────────────────────────────

def smbc_extract_prev_url(html: str) -> str | None:
    # cc-prev anchor is in static HTML: <a class="cc-prev" ... href="...">
    m = re.search(r'<a\b[^>]*\bclass="[^"]*cc-prev[^"]*"[^>]*>', html, re.IGNORECASE)
    if not m:
        return None
    href_m = re.search(r'\bhref="([^"]+)"', m.group(0), re.IGNORECASE)
    return href_m.group(1) if href_m else None


def smbc_extract_first_url(html: str) -> str | None:
    m = re.search(r'<a\b[^>]*\bclass="[^"]*cc-first[^"]*"[^>]*>', html, re.IGNORECASE)
    if not m:
        return None
    href_m = re.search(r'\bhref="([^"]+)"', m.group(0), re.IGNORECASE)
    return href_m.group(1) if href_m else None


def smbc_extract_comic(html: str, page_url: str) -> dict | None:
    img_tag = re.search(r'<img\s[^>]*id="cc-comic"[^>]*>', html, re.IGNORECASE)
    if not img_tag:
        img_tag = re.search(r'<img\s[^>]*class="[^"]*cc-comic[^"]*"[^>]*>', html, re.IGNORECASE)
    if not img_tag:
        return None

    tag = img_tag.group(0)
    src_m = re.search(r'\bsrc="([^"]+)"', tag)
    title_m = re.search(r'\btitle="([^"]*)"', tag)
    image_url = src_m.group(1) if src_m else None
    if image_url and not image_url.startswith("http"):
        image_url = "https://www.smbc-comics.com" + image_url
    alt_text = title_m.group(1) if title_m else None

    page_title = re.search(r'Saturday Morning Breakfast Cereal\s*[-–]\s*([^<\n]+)', html)
    title = page_title.group(1).strip() if page_title else page_url.rstrip("/").rsplit("/", 1)[-1]

    slug = page_url.rstrip("/").rsplit("/", 1)[-1]

    # Try the page's cc-publishtime element first ("Posted Month D, YYYY at …")
    pub_m = re.search(r'cc-publishtime[^>]*>\s*Posted\s+(\w+ \d{1,2},?\s+\d{4})', html)
    if pub_m:
        try:
            dt = datetime.strptime(pub_m.group(1).replace(",", "").strip(), "%B %d %Y")
            publish_date = dt.strftime("%Y-%m-%d")
            sort_index   = int(dt.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            pub_m = None

    if not pub_m:
        # Fall back to date slug (YYYY-MM-DD) — used by very early SMBC strips
        date_m = re.search(r"(\d{4})-(\d{2})-(\d{2})", slug)
        if date_m:
            y, mo, d = int(date_m.group(1)), int(date_m.group(2)), int(date_m.group(3))
            sort_index   = int(datetime(y, mo, d, tzinfo=timezone.utc).timestamp())
            publish_date = f"{y}-{mo:02d}-{d:02d}"
        else:
            # Non-date slug with no page date — sortIndex filled in by caller
            sort_index   = None
            publish_date = None

    return {
        "id":           f"smbc-{slug}",
        "title":        title,
        "pageUrl":      page_url,
        "imageUrl":     image_url,
        "altText":      alt_text,
        "publishDate":  publish_date,
        "sortIndex":    sort_index,
        # The "after" bonus panel is on the same page — always set so the red button appears
        "bonusPageUrl": page_url,
    }


def update_smbc():
    print("SMBC:")
    existing = load_existing("smbc")
    comics_by_id = {c["id"]: c for c in existing.get("comics", [])}

    # First fetch latest from RSS (fast, gets recent comics with correct data).
    # NOTE: the RSS feed does NOT include the img title attribute that SMBC uses
    # as alt text — those are filled in by the page-fetch repair pass below.
    rss_text = fetch_text("https://www.smbc-comics.com/comic/rss")
    if rss_text:
        items = parse_rss(rss_text)
        rss_added = 0
        for item in items:
            link = item["link"] or item["guid"]
            if not link:
                continue
            slug = link.rstrip("/").rsplit("/", 1)[-1]
            comic_id = f"smbc-{slug}"
            epoch = rfc822_to_epoch(item["pubDate"])
            img = extract_img_src(item["description"]) or extract_img_src(item["contentEncoded"])
            alt = extract_img_title(item["description"])
            # Strip the redundant "Saturday Morning Breakfast Cereal - " prefix so
            # RSS-sourced titles are consistent with archive-sourced short titles.
            rss_title = item["title"] or ""
            short_title = re.sub(r"^Saturday Morning Breakfast Cereal\s*[-–]\s*", "", rss_title).strip()
            if comic_id not in comics_by_id:
                if epoch is None:
                    continue
                comics_by_id[comic_id] = {
                    "id":          comic_id,
                    "title":       short_title or rss_title,
                    "pageUrl":     link,
                    "imageUrl":    img,
                    "altText":     alt,
                    "publishDate": epoch_to_date(epoch),
                    "sortIndex":   epoch,
                    "bonusPageUrl": link,
                }
                rss_added += 1
        print(f"  RSS: +{rss_added} new comics")

    # ── Title normalisation (no network) ────────────────────────────────────────
    # Strip "Saturday Morning Breakfast Cereal - " prefix from existing entries;
    # RSS-sourced titles had this prefix but archive-sourced ones don't.
    SMBC_PREFIX = re.compile(r"^Saturday Morning Breakfast Cereal\s*[-–]\s*", re.IGNORECASE)
    normalised = 0
    for comic in comics_by_id.values():
        t = comic.get("title", "")
        stripped = SMBC_PREFIX.sub("", t).strip()
        if stripped != t:
            comic["title"] = stripped
            normalised += 1
    if normalised:
        print(f"  Normalised {normalised} titles (stripped SMBC prefix)")

    # ── Alt-text repair pass ──────────────────────────────────────────────────
    # RSS doesn't carry the img title attribute (= SMBC alt text). Fetch the page
    # for any comic that is still missing it and fill it in. Self-healing: once
    # every comic has altText this list is empty and the loop is a no-op.
    missing_alt = [c for c in comics_by_id.values() if not c.get("altText") and c.get("pageUrl")]
    if missing_alt:
        print(f"  Fetching pages for {len(missing_alt)} comics missing altText…")
        for comic in missing_alt:
            html = fetch_text(comic["pageUrl"])
            if html:
                full = smbc_extract_comic(html, comic["pageUrl"])
                if full:
                    if full.get("altText"):
                        comic["altText"] = full["altText"]
                    if full.get("imageUrl") and not comic.get("imageUrl"):
                        comic["imageUrl"] = full["imageUrl"]
                    if full.get("publishDate") and not comic.get("publishDate"):
                        comic["publishDate"] = full["publishDate"]
                        comic["sortIndex"]   = full["sortIndex"] or comic["sortIndex"]
            time.sleep(0.25)

    # ── Date repair pass (opt-in, slow) ──────────────────────────────────────
    # Set SMBC_REPAIR_DATES=1 to fetch pages for all comics missing publishDate.
    # ~7700 pages at 0.25 s each takes roughly 30-40 minutes. Run once to backfill
    # the archive; after that this list stays empty and the env var has no effect.
    if os.environ.get("SMBC_REPAIR_DATES"):
        missing_date = [c for c in comics_by_id.values()
                        if not c.get("publishDate") and c.get("pageUrl")]
        if missing_date:
            print(f"  SMBC_REPAIR_DATES: fetching dates for {len(missing_date)} comics…")
            for i, comic in enumerate(missing_date, 1):
                html = fetch_text(comic["pageUrl"])
                if html:
                    full = smbc_extract_comic(html, comic["pageUrl"])
                    if full and full.get("publishDate"):
                        comic["publishDate"] = full["publishDate"]
                        comic["sortIndex"]   = full["sortIndex"] or comic["sortIndex"]
                if i % 200 == 0:
                    print(f"    …{i}/{len(missing_date)} dates fetched")
                time.sleep(0.25)
            print(f"  Date repair complete")

    # Walk backwards to fill archive gaps
    # Find the oldest comic currently in DB to start walking from
    all_comics = list(comics_by_id.values())
    dated = [c for c in all_comics if c.get("sortIndex") and c.get("pageUrl")]
    if not dated:
        print("  No starting point for walk, skipping archive walk")
        save_feed("smbc", list(comics_by_id.values()))
        return

    oldest = min(dated, key=lambda c: c["sortIndex"])
    print(f"  Oldest in DB: {oldest['id']} (sortIndex {oldest['sortIndex']})")

    # Check if we've reached the first comic (2002-09-05)
    first_epoch = int(datetime(2002, 9, 5, tzinfo=timezone.utc).timestamp())
    if oldest["sortIndex"] <= first_epoch + 86400:
        print("  Archive appears complete (reached 2002-09-05)")
        save_feed("smbc", list(comics_by_id.values()))
        return

    # Walk backwards from oldest, up to MAX_WALK per run
    MAX_WALK = int(os.environ.get("SMBC_MAX_WALK", "200"))
    current_url = oldest["pageUrl"]
    current_html = fetch_text(current_url)
    walked = 0
    non_date_counter = oldest["sortIndex"]  # fallback sortIndex for non-date slugs

    while walked < MAX_WALK and current_html:
        prev_url = smbc_extract_prev_url(current_html)
        if not prev_url:
            print(f"  Reached first comic at {current_url}")
            break

        prev_html = fetch_text(prev_url)
        if not prev_html:
            print(f"  WARN: failed to fetch {prev_url}")
            break

        comic = smbc_extract_comic(prev_html, prev_url)
        if comic:
            if comic["sortIndex"] is None:
                # Non-date slug: derive a sort index just before the current oldest
                non_date_counter -= 86400  # one day earlier per step
                comic["sortIndex"] = non_date_counter
                comic["bonusPageUrl"] = prev_url

            if comic["id"] not in comics_by_id:
                comics_by_id[comic["id"]] = comic
                walked += 1
            else:
                # Already have it — fill in any missing fields then stop walking
                # (we've caught up to already-known territory)
                existing_c = comics_by_id[comic["id"]]
                for k in ("imageUrl", "altText", "title"):
                    if comic.get(k) and not existing_c.get(k):
                        existing_c[k] = comic[k]
                print(f"  Caught up at {comic['id']} after walking {walked} comics")
                break

        current_html = prev_html
        current_url = prev_url
        time.sleep(0.3)  # polite

    print(f"  Walk: +{walked} comics")
    save_feed("smbc", list(comics_by_id.values()))


# ── Generic RSS source ────────────────────────────────────────────────────────

def update_rss_source(
    source_id: str,
    display_name: str,
    feed_url: str,
    slug_fn=None,       # fn(item) -> str slug; default = last path segment of link
    image_fn=None,      # fn(item) -> str|None image URL; default = first img in description
    bonus_url_fn=None,  # fn(item) -> str|None bonus URL
):
    print(f"{display_name}:")
    existing = load_existing(source_id)
    comics_by_id = {c["id"]: c for c in existing.get("comics", [])}

    rss_text = fetch_text(feed_url)
    if not rss_text:
        print(f"  WARN: could not fetch {feed_url}")
        return

    items = parse_rss(rss_text)
    added = 0
    for item in items:
        link = item["link"] or item["guid"]
        if not link:
            continue
        epoch = rfc822_to_epoch(item["pubDate"])
        if not epoch:
            continue

        slug = slug_fn(item) if slug_fn else (link.rstrip("/").rsplit("/", 1)[-1] or item["title"][:40])
        comic_id = f"{source_id}-{slug}"

        img = None
        if image_fn:
            img = image_fn(item)
        else:
            img = (extract_img_src(item["description"])
                   or extract_img_src(item["contentEncoded"]))
        img = http_to_https(img)

        bonus = bonus_url_fn(item) if bonus_url_fn else None

        entry = {
            "id":          comic_id,
            "title":       item["title"],
            "pageUrl":     link,
            "imageUrl":    img,
            "altText":     None,
            "publishDate": epoch_to_date(epoch),
            "sortIndex":   epoch,
        }
        if bonus:
            entry["bonusPageUrl"] = bonus

        if comic_id not in comics_by_id:
            comics_by_id[comic_id] = entry
            added += 1
        else:
            # Update missing imageUrl if we now have it
            if img and not comics_by_id[comic_id].get("imageUrl"):
                comics_by_id[comic_id]["imageUrl"] = img

    print(f"  +{added} new comics (total {len(comics_by_id)})")
    save_feed(source_id, list(comics_by_id.values()))


# ── Abstruse Goose ────────────────────────────────────────────────────────────

def update_abstrusegoose():
    """
    The original site is offline. Full archive is in:
    https://github.com/s-macke/Abstruse-Goose-Archive
    We generate the list once from that repo's tree API.
    After the first run this is a no-op (nothing new to add).
    """
    print("Abstruse Goose:")
    existing = load_existing("abstrusegoose")
    comics_by_id = {c["id"]: c for c in existing.get("comics", [])}

    if len(comics_by_id) >= 500:
        print(f"  Already have {len(comics_by_id)} comics, skipping (archive is complete)")
        return

    # Download archive repo ZIP and parse MD files
    import zipfile, io
    BASE = "https://raw.githubusercontent.com/s-macke/Abstruse-Goose-Archive/master/comics"
    # BASE_EPOCH: 2008-01-01 UTC, STEP: ~3 days per comic
    BASE_EPOCH = 1_199_145_600
    STEP = 258_068

    zip_data = fetch("https://github.com/s-macke/Abstruse-Goose-Archive/archive/refs/heads/master.zip")
    if not zip_data:
        print("  WARN: could not download archive")
        return

    added = 0
    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        md_files = [n for n in zf.namelist()
                    if n.startswith("Abstruse-Goose-Archive-master/comics/")
                    and n.endswith(".md")
                    and n.split("/")[-1][:-3].isdigit()]
        md_files.sort(key=lambda n: int(n.split("/")[-1][:-3]))
        for name in md_files:
            num = int(name.split("/")[-1][:-3])
            content = zf.read(name).decode("utf-8", errors="replace")
            title_m = re.search(r'^##\s+(.+)', content, re.MULTILINE)
            title = title_m.group(1).strip() if title_m else f"#{num}"
            img_m = re.search(r'!\[image\]\(([^)]+)\)', content)
            if not img_m:
                continue
            img_file = urllib.parse.quote(img_m.group(1).strip())
            image_url = f"{BASE}/{img_file}"
            sort_index = BASE_EPOCH + num * STEP
            comic_id = f"abstrusegoose-{num}"
            if comic_id not in comics_by_id:
                comics_by_id[comic_id] = {
                    "id":          comic_id,
                    "title":       title,
                    "pageUrl":     f"https://abstrusegoose.com/{num}",
                    "imageUrl":    image_url,
                    "altText":     None,
                    "publishDate": epoch_to_date(sort_index),
                    "sortIndex":   sort_index,
                }
                added += 1

    print(f"  +{added} new comics (total {len(comics_by_id)})")
    save_feed("abstrusegoose", list(comics_by_id.values()))


# ── PBF Archive Walk ──────────────────────────────────────────────────────────

def update_pbf_archive():
    """
    Walk PBF (pbfcomics.com) backwards via rel=prev links to build the full archive.

    Each comic page URL is the canonical key. Date is extracted from /uploads/YYYY/MM/.
    Stops after MAX_CONSECUTIVE_KNOWN known pages in a row.

    Image extraction uses the div#comic container so it works on both old-style
    single-quoted lazy-load images (PBF001-Stiff_Breeze.png) and new-style double-quoted
    ones (PBF-Endgame.png), without accidentally picking up sidebar thumbnails.

    Safe to run repeatedly — pass an empty comics list to force a full re-crawl.
    """
    print("PBF (archive walk):")
    existing = load_existing("pbf")
    comics_by_id = {c["id"]: c for c in existing.get("comics", [])}
    known_page_urls = {c["pageUrl"].rstrip("/") for c in comics_by_id.values()}

    url = "https://pbfcomics.com/"
    consecutive_known = 0
    MAX_CONSECUTIVE_KNOWN = 5
    added = 0

    while url and consecutive_known < MAX_CONSECUTIVE_KNOWN:
        html = fetch_text(url)
        if not html:
            break

        # ── Comic image extraction ────────────────────────────────────────────
        # Primary: search inside div#comic (avoids sidebar thumbnails entirely).
        # The img uses data-src (lazy-load) or src, with single or double quotes.
        # Old numbered comics (PBF001-…) use single quotes; new ones use double.
        image_url = None
        comic_marker = re.search(r'id=["\']comic["\']', html)
        if comic_marker:
            search_area = html[comic_marker.start():comic_marker.start() + 3000]
            img_m = re.search(
                r'(?:data-src|src)=["\']([^"\']+/wp-content/uploads/\d{4}/\d{2}/[^"\']+\.(?:png|jpg|gif|webp))["\']',
                search_area, re.IGNORECASE,
            )
            if img_m and "thumb" not in img_m.group(1).lower():
                image_url = img_m.group(1)

        # Fallback: scan full page but require YYYY/MM path and exclude thumbs
        if not image_url:
            img_m = re.search(
                r'(?:data-src|src)=["\']([^"\']+/wp-content/uploads/\d{4}/\d{2}/PBF[^"\']+\.(?:png|jpg|gif|webp))["\']',
                html, re.IGNORECASE,
            )
            if img_m and "thumb" not in img_m.group(1).lower():
                image_url = img_m.group(1)

        # ── Title ─────────────────────────────────────────────────────────────
        title_m = (re.search(r'<h1[^>]*class="[^"]*pbf-comic-title[^"]*"[^>]*>\s*([^<]+)', html)
                   or re.search(r'<title>\s*([^|<]+)', html))
        title = (title_m.group(1).strip()
                 .replace("Perry Bible Fellowship", "").strip(" -|")
                 if title_m else "")

        # ── ID / page URL ─────────────────────────────────────────────────────
        page_url = url.rstrip("/")
        slug = page_url.rsplit("/", 1)[-1]
        # For the homepage (slug is empty or the domain), fall back to image stem
        if not slug or "." in slug:
            import os as _os
            slug = _os.path.splitext(_os.path.basename(image_url or ""))[0] or "home"
        comic_id = f"pbf-{slug}"

        # ── Date ──────────────────────────────────────────────────────────────
        date_m = re.search(r"/uploads/(\d{4})/(\d{2})/", image_url or "")
        if date_m:
            y, mo = int(date_m.group(1)), int(date_m.group(2))
            sort_index = int(datetime(y, mo, 1, tzinfo=timezone.utc).timestamp())
            publish_date = f"{y}-{mo:02d}-01"
        else:
            sort_index = 0
            publish_date = None

        # ── Store / skip ──────────────────────────────────────────────────────
        if page_url in known_page_urls:
            consecutive_known += 1
        elif image_url:
            comics_by_id[comic_id] = {
                "id":          comic_id,
                "title":       title or comic_id,
                "pageUrl":     page_url,
                "imageUrl":    image_url,
                "altText":     None,
                "publishDate": publish_date,
                "sortIndex":   sort_index,
            }
            known_page_urls.add(page_url)
            added += 1
            consecutive_known = 0

        # ── Next (older) page ─────────────────────────────────────────────────
        prev_m = (re.search(r'<a\b[^>]*\brel=["\']prev["\'][^>]*\bhref=["\']([^"\']+)["\']', html)
                  or re.search(r'\bhref=["\']([^"\']+)["\'][^>]*\brel=["\']prev["\']', html)
                  or re.search(r'<link[^>]+rel=["\']prev["\'][^>]+href=["\']([^"\']+)["\']', html))
        url = prev_m.group(1) if prev_m else None
        time.sleep(0.15)

    print(f"  Archive walk: +{added} new (total {len(comics_by_id)})")
    save_feed("pbf", list(comics_by_id.values()))


# ── PhD Comics Archive Walk ───────────────────────────────────────────────────

def update_phdcomics_archive():
    """
    Walk PhD Comics (phdcomics.com) by sequential comic ID: comicid=1 .. current_max.

    Incremental: skips entries that already have imageUrl set. This means a fresh
    run (or a repair run after a botched scrape) will fill in missing image URLs
    without re-fetching comics that are already complete.
    """
    print("PhD Comics (archive walk):")
    existing = load_existing("phdcomics")
    comics_by_id = {c["id"]: c for c in existing.get("comics", [])}

    max_known = max(
        (int(m.group(1)) for c in comics_by_id
         if (m := re.search(r"phdcomics-(\d+)$", c))), default=0
    )

    home = fetch_text("https://phdcomics.com/comics.php")
    current_max = max_known
    if home:
        m = re.search(r"comicid=(\d+)", home)
        if m:
            current_max = max(current_max, int(m.group(1)))

    # Count how many existing entries are missing imageUrl (need repair)
    missing_image = sum(
        1 for c in comics_by_id.values() if not c.get("imageUrl")
    )
    print(f"  Known: {len(comics_by_id)}, max_id={max_known}, site_max~{current_max}, missing_image={missing_image}")

    # ── Smoke-test: probe a few pages before committing to the full run ───────
    # If the regex can't find an image on any of the probe pages, the site markup
    # has changed and we'd waste minutes scraping 2000+ pages uselessly.
    if missing_image > 0:
        probe_ids = [n for n in [10, 50, 200, 500, 1000] if n <= current_max]
        probe_hits = 0
        print(f"  Probing {len(probe_ids)} pages to validate regex before full run…")
        for probe_n in probe_ids:
            probe_html = fetch_text(f"https://phdcomics.com/comics/archive.php?comicid={probe_n}")
            if probe_html and len(probe_html) >= 500:
                m = re.search(
                    r'\bsrc=(https?://[^\s>]+phdcomics\.com/comics/archive/[^\s>]+\.(?:gif|jpg|png|jpeg))',
                    probe_html, re.IGNORECASE,
                )
                if not m:
                    m = (re.search(r'<img[^>]+\bid=["\']?comic["\']?[^>]+\bsrc=["\']?([^"\'>\s]+)', probe_html, re.IGNORECASE)
                         or re.search(r'\bsrc=["\']?([^"\'>\s]+)["\']?[^>]+\bid=["\']?comic["\']?(?!\w)', probe_html, re.IGNORECASE))
                if m:
                    probe_hits += 1
            time.sleep(0.3)
        if probe_hits == 0:
            print(f"  ERROR: image regex matched 0/{len(probe_ids)} probe pages — site markup may have changed.")
            print(f"  Aborting without modifying phdcomics.json.")
            return
        print(f"  Probe OK: {probe_hits}/{len(probe_ids)} pages matched. Starting full run…")

    added = 0
    updated = 0
    consecutive_misses = 0   # track regex failures during the run
    for n in range(1, current_max + 1):
        comic_id = f"phdcomics-{n}"
        existing_entry = comics_by_id.get(comic_id)

        # Skip entries that already have a valid imageUrl
        if existing_entry and existing_entry.get("imageUrl"):
            continue

        url = f"https://phdcomics.com/comics/archive.php?comicid={n}"
        html = fetch_text(url)
        if not html:
            time.sleep(0.5)
            continue

        # Guard against empty/redirect pages
        if len(html) < 500:
            continue

        # PhD Comics uses unquoted attributes: src=http://... (no quotes around the URL)
        img_m = re.search(
            r'\bsrc=(https?://[^\s>]+phdcomics\.com/comics/archive/[^\s>]+\.(?:gif|jpg|png|jpeg))',
            html, re.IGNORECASE,
        )
        if not img_m:
            # Fallback: any img with id=comic (handles future markup changes)
            img_m = (re.search(r'<img[^>]+\bid=["\']?comic["\']?[^>]+\bsrc=["\']?([^"\'>\s]+)', html, re.IGNORECASE)
                     or re.search(r'\bsrc=["\']?([^"\'>\s]+)["\']?[^>]+\bid=["\']?comic["\']?(?!\w)', html, re.IGNORECASE))
        image_url = img_m.group(1) if img_m else None
        if image_url:
            # Normalise to https
            if image_url.startswith("//"):
                image_url = "https:" + image_url
            elif image_url.startswith("http://"):
                image_url = "https://" + image_url[7:]
            elif not image_url.startswith("http"):
                image_url = "https://phdcomics.com/" + image_url.lstrip("/")

        title_m = re.search(r"<title>[^:]+:\s*([^<|]+)", html)
        title = title_m.group(1).strip() if title_m else f"PhD Comics #{n}"

        # Extract date from the image filename.
        # PhD Comics filenames follow phd[MMDDYY][suffix].ext (e.g. phd091004s.gif = Sep 10, 2004).
        # Early comics (#1-~10) use 4-digit filenames with no date; those fall back to n*86400.
        # The page body dates are news-sidebar dates, not the comic's own date, so we never
        # scrape them from the HTML body.
        publish_date = None
        sort_index   = n * 86400  # fallback: stable proxy ordering
        if image_url:
            fn_m = re.search(r'/phd(\d{2})(\d{2})(\d{2})[a-z]*\.', image_url, re.IGNORECASE)
            if fn_m:
                mm, dd, yy = int(fn_m.group(1)), int(fn_m.group(2)), int(fn_m.group(3))
                year = 2000 + yy if yy < 70 else 1900 + yy
                if 1 <= mm <= 12 and 1 <= dd <= 31:
                    publish_date = f"{year}-{mm:02d}-{dd:02d}"
                    try:
                        sort_index = int(datetime.strptime(publish_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
                    except ValueError:
                        pass

        if existing_entry:
            if image_url:
                # Only repair if we actually got a valid imageUrl this time
                existing_entry["imageUrl"]    = image_url
                existing_entry["sortIndex"]   = sort_index
                existing_entry["publishDate"] = publish_date
                existing_entry["title"]       = title
                updated += 1
                consecutive_misses = 0
            else:
                # page had no image — leave entry unchanged, retry next run
                consecutive_misses += 1
        else:
            if image_url:
                consecutive_misses = 0
            else:
                consecutive_misses += 1
            comics_by_id[comic_id] = {
                "id":          comic_id,
                "title":       title,
                "pageUrl":     url,
                "imageUrl":    image_url,
                "altText":     None,
                "publishDate": publish_date,
                "sortIndex":   sort_index,
            }
            added += 1

        # Abort early if the regex stops working mid-run (site change / rate-limit)
        if consecutive_misses >= 20:
            print(f"  ERROR: image regex failed on {consecutive_misses} consecutive pages (last: #{n}).")
            print(f"  Saving {updated} repaired + {added} new comics collected so far, then aborting.")
            break

        if n % 100 == 0:
            print(f"  PhD Comics: {n}/{current_max}… (+{added} new, {updated} repaired)")
        time.sleep(0.15)

    print(f"  Archive walk: +{added} new, {updated} repaired (total {len(comics_by_id)})")
    save_feed("phdcomics", list(comics_by_id.values()))


# ── Dinosaur Comics Archive Walk ──────────────────────────────────────────────

def update_dinosaurcomics_archive():
    """
    Walk Dinosaur Comics (qwantz.com) by sequential comic number: comic=1 .. current_max.

    Date is extracted from the image filename (dinosaur-comics-YYYY-MM-DD.png).
    Alt text is also extracted (Ryan North writes great hover text).
    Incremental: starts from max known ID + 1.
    """
    print("Dinosaur Comics (archive walk):")
    existing = load_existing("dinosaurcomics")
    comics_by_id = {c["id"]: c for c in existing.get("comics", [])}

    max_known = max(
        (int(m.group(1)) for c in comics_by_id
         if (m := re.search(r"dinosaurcomics-(\d+)$", c))), default=0
    )

    home = fetch_text("https://qwantz.com/")
    current_max = max_known
    if home:
        m = re.search(r"index\.php\?comic=(\d+)", home)
        if m:
            current_max = max(current_max, int(m.group(1)))

    print(f"  Known: {len(comics_by_id)}, max_id={max_known}, site_max~{current_max}")

    added = 0
    for n in range(max_known + 1, current_max + 1):
        url = f"https://qwantz.com/index.php?comic={n}"
        html = fetch_text(url)
        if not html:
            time.sleep(0.5)
            continue

        img_m = (re.search(r'<img[^>]+src="(comics/[^"]+)"[^>]*class="comic"', html)
                 or re.search(r'class="comic"[^>]+src="(comics/[^"]+)"', html)
                 or re.search(r'<img[^>]+src="(comics/dinosaur[^"]+)"', html))
        image_url = ("https://qwantz.com/" + img_m.group(1)) if img_m else None

        date_m = re.search(r"(\d{4}-\d{2}-\d{2})", img_m.group(1) if img_m else "")
        publish_date = date_m.group(1) if date_m else None
        if publish_date:
            try:
                sort_index = int(datetime.strptime(publish_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                sort_index = n * 86400  # stable ordering when date is unparseable
        else:
            sort_index = n * 86400  # qwantz filenames don't embed dates; use comic # as proxy

        alt_m = (re.search(r'class="comic"[^>]+alt="([^"]*)"', html)
                 or re.search(r'alt="([^"]*)"[^>]+class="comic"', html))
        alt_text = alt_m.group(1).strip() or None if alt_m else None

        title_m = re.search(r"<title>([^<]+)</title>", html)
        title = re.sub(r"\s*[-|].*$", "", title_m.group(1).strip()).strip() if title_m else ""
        title = title or f"Dinosaur Comics #{n}"

        comic_id = f"dinosaurcomics-{n}"
        if comic_id not in comics_by_id:
            comics_by_id[comic_id] = {
                "id":          comic_id,
                "title":       title,
                "pageUrl":     url,
                "imageUrl":    image_url,
                "altText":     alt_text,
                "publishDate": publish_date,
                "sortIndex":   sort_index,
            }
            added += 1

        if n % 100 == 0:
            print(f"  Dinosaur Comics: {n}/{current_max}…")
        time.sleep(0.15)

    print(f"  Archive walk: +{added} new (total {len(comics_by_id)})")
    save_feed("dinosaurcomics", list(comics_by_id.values()))


# ── Source registry ───────────────────────────────────────────────────────────

def update_all():
    DATA_DIR.mkdir(exist_ok=True)

    update_xkcd()

    update_smbc()

    update_pbf_archive()

    update_phdcomics_archive()

    update_dinosaurcomics_archive()

    update_rss_source(
        source_id    = "commitstrip",
        display_name = "CommitStrip",
        feed_url     = "https://www.commitstrip.com/en/feed/",
        image_fn     = lambda item: (extract_img_src(item["contentEncoded"])
                                     or extract_img_src(item["description"])),
    )

    update_abstrusegoose()

    print("\nDone.")


if __name__ == "__main__":
    sources = sys.argv[1:] if len(sys.argv) > 1 else []
    if not sources:
        update_all()
    else:
        fn_map = {
            "xkcd":           update_xkcd,
            "smbc":           update_smbc,
            "pbf":            update_pbf_archive,
            "phdcomics":      update_phdcomics_archive,
            "dinosaurcomics": update_dinosaurcomics_archive,
            "abstrusegoose":  update_abstrusegoose,
            "commitstrip":    lambda: update_rss_source(
                                  "commitstrip", "CommitStrip",
                                  "https://www.commitstrip.com/en/feed/",
                                  image_fn=lambda item: (extract_img_src(item["contentEncoded"])
                                                         or extract_img_src(item["description"])),
                              ),
        }
        for s in sources:
            if s in fn_map:
                fn_map[s]()
            else:
                print(f"Unknown source: {s}", file=sys.stderr)
