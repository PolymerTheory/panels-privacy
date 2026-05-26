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
    # Parse date slug (YYYY-MM-DD) for sortIndex
    date_m = re.search(r"(\d{4})-(\d{2})-(\d{2})", slug)
    if date_m:
        y, mo, d = int(date_m.group(1)), int(date_m.group(2)), int(date_m.group(3))
        sort_index = int(datetime(y, mo, d, tzinfo=timezone.utc).timestamp())
        publish_date = f"{y}-{mo:02d}-{d:02d}"
    else:
        # Non-date slug — use a stable hash offset so order is preserved across runs
        sort_index = None
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

    # First fetch latest from RSS (fast, gets recent comics with correct data)
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
            if comic_id not in comics_by_id:
                if epoch is None:
                    continue
                comics_by_id[comic_id] = {
                    "id":          comic_id,
                    "title":       item["title"],
                    "pageUrl":     link,
                    "imageUrl":    img,
                    "altText":     alt,
                    "publishDate": epoch_to_date(epoch),
                    "sortIndex":   epoch,
                    "bonusPageUrl": link,
                }
                rss_added += 1
        print(f"  RSS: +{rss_added} new comics")

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

    Each comic's page URL is used as the canonical key (more stable than image filename).
    Date is extracted from the WordPress upload path (uploads/YYYY/MM/).
    Stops when it hits MAX_CONSECUTIVE_KNOWN already-known pages in a row.

    Safe to run repeatedly: incremental — stops as soon as it reaches known content.
    Initial run fetches ~230 pages; subsequent runs are nearly instantaneous.
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

        # Comic image — prefer lazy-load data-src, fall back to src
        img_m = re.search(
            r'(?:data-src|src)="(https://pbfcomics\.com/wp-content/uploads/[^"]*PBF-[^"]+\.\w+)"',
            html, re.IGNORECASE,
        )
        image_url = img_m.group(1) if img_m else None

        # Title — WordPress <h1> with pbf-comic-title class, or <title> fallback
        title_m = (re.search(r'<h1[^>]*class="[^"]*pbf-comic-title[^"]*"[^>]*>\s*([^<]+)', html)
                   or re.search(r'<title>\s*([^|<]+)', html))
        title = (title_m.group(1).strip()
                 .replace("Perry Bible Fellowship", "").strip(" -|")
                 if title_m else url.rstrip("/").rsplit("/", 1)[-1])

        page_url = url.rstrip("/")
        slug = page_url.rsplit("/", 1)[-1] or "home"
        comic_id = f"pbf-{slug}"

        # Date from /uploads/YYYY/MM/ in the image URL
        date_m = re.search(r"/uploads/(\d{4})/(\d{2})/", image_url or "")
        if date_m:
            y, mo = int(date_m.group(1)), int(date_m.group(2))
            sort_index = int(datetime(y, mo, 1, tzinfo=timezone.utc).timestamp())
            publish_date = f"{y}-{mo:02d}-01"
        else:
            sort_index = 0
            publish_date = None

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

        # Older comic link: <a rel="prev" href="..."> or <link rel="prev" href="...">
        prev_m = (re.search(r'<a\b[^>]*\brel="prev"[^>]*\bhref="([^"]+)"', html)
                  or re.search(r'\bhref="([^"]+)"[^>]*\brel="prev"', html)
                  or re.search(r'<link[^>]+rel="prev"[^>]+href="([^"]+)"', html))
        url = prev_m.group(1) if prev_m else None
        time.sleep(0.15)

    print(f"  Archive walk: +{added} new (total {len(comics_by_id)})")
    save_feed("pbf", list(comics_by_id.values()))


# ── PhD Comics Archive Walk ───────────────────────────────────────────────────

def update_phdcomics_archive():
    """
    Walk PhD Comics (phdcomics.com) by sequential comic ID: comicid=1 .. current_max.

    Incremental: starts from max known ID + 1. On a fresh run this fetches all
    ~2000+ comics; subsequent runs only pick up new strips (Jorge Cham posts infrequently).
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

    print(f"  Known: {len(comics_by_id)}, max_id={max_known}, site_max~{current_max}")

    added = 0
    for n in range(max_known + 1, current_max + 1):
        url = f"https://phdcomics.com/comics/archive.php?comicid={n}"
        html = fetch_text(url)
        if not html:
            time.sleep(0.5)
            continue

        # Guard against empty/redirect pages
        if len(html) < 500:
            continue

        img_m = (re.search(r'<img[^>]+id="comic"[^>]+src="([^"]+)"', html)
                 or re.search(r'src="([^"]+)"[^>]+id="comic"', html))
        image_url = img_m.group(1) if img_m else None
        if image_url:
            if image_url.startswith("//"):
                image_url = "https:" + image_url
            elif not image_url.startswith("http"):
                image_url = "https://phdcomics.com/" + image_url.lstrip("/")

        title_m = re.search(r"<title>[^:]+:\s*([^<|]+)", html)
        title = title_m.group(1).strip() if title_m else f"PhD Comics #{n}"

        date_m = re.search(r"(\d{2})/(\d{2})/(\d{4})", html)
        if date_m:
            publish_date = f"{date_m.group(3)}-{date_m.group(1)}-{date_m.group(2)}"
            try:
                sort_index = int(datetime.strptime(publish_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                sort_index = n * 86400
        else:
            publish_date = None
            sort_index = n * 86400  # stable ordering when date is missing

        comic_id = f"phdcomics-{n}"
        if comic_id not in comics_by_id:
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

        if n % 100 == 0:
            print(f"  PhD Comics: {n}/{current_max}…")
        time.sleep(0.15)

    print(f"  Archive walk: +{added} new (total {len(comics_by_id)})")
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
                sort_index = 0
        else:
            sort_index = 0

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
