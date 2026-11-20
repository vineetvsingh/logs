#!/usr/bin/env python3
"""
Leonida Logs image collector.

Collects official GTA 6 images (Rockstar Games, PlayStation, Xbox) into this repo:
  official/<source>/<date>-<hash>.jpg   + manifest.json (credit, source page, size, phash)

Runs daily on GitHub Actions (see .github/workflows/collect.yml).
Only official sources: no leaks, no other creators' screenshots.
Respects robots.txt, waits between requests, skips small images (logos/icons) and near-duplicates.

Local run:  pip install -r requirements.txt && python -m playwright install chromium && python collect.py
Static-only (no browser):  python collect.py --no-render
"""
import argparse, hashlib, io, json, os, re, sys, time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib import robotparser

import requests
from bs4 import BeautifulSoup
from PIL import Image
import imagehash
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(ROOT, "manifest.json")
LIB_DIR = "official"            # --library compare switches to compare/ + compare.json
UA = "Mozilla/5.0 (compatible; LeonidaLogsCollector/1.0; +https://github.com)"
IMG_RE = re.compile(r"https?://[^\s\"'<>()\\]+?\.(?:jpe?g|png|webp)(?:\?[^\s\"'<>()\\]*)?", re.I)
GTA6_RE = re.compile(r"grand[-\s]theft[-\s]auto[-\s](?:vi|6)\b|\bgta[-\s]?(?:vi|6)\b", re.I)

# Each source: pages to read, which linked articles to follow, and the credit line.
SOURCES = [
    {
        "name": "rockstar-vi",
        "credit": "Rockstar Games",
        "start": ["https://www.rockstargames.com/VI"],
        "follow": r"^https://www\.rockstargames\.com/VI(/|$)(?!media/artwork)",
        "require_gta6_title": False,   # every image on the GTA VI site is GTA 6
        "render": True,
    },
    {
        "name": "rockstar-newswire",
        "credit": "Rockstar Games",
        "start": ["https://www.rockstargames.com/newswire"],
        "follow": r"^https://www\.rockstargames\.com/newswire/article/",
        "require_gta6_title": True,
        "render": True,
    },
    {
        "name": "playstation-blog",
        "credit": "PlayStation",
        "start": ["https://blog.playstation.com/?s=grand+theft+auto+vi"],
        "follow": r"^https://blog\.playstation\.com/\d{4}/\d{2}/\d{2}/",
        "require_gta6_title": True,
        "render": False,
    },
]

# Official screenshots of OTHER Rockstar games, kept apart from the GTA 6 library
# (compare/ + compare.json) and used only for side-by-side comparison posts.
COMPARE_SOURCES = [
    {
        "name": "rockstar-gta-v",
        "credit": "Rockstar Games",
        "start": ["https://www.rockstargames.com/gta-v"],
        "follow": r"^https://www\.rockstargames\.com/gta-v(/|$)",
        "require_gta6_title": False,
        "render": True,
    },
    {
        "name": "rockstar-rdr2",
        "credit": "Rockstar Games",
        "start": ["https://www.rockstargames.com/reddeadredemption2"],
        "follow": r"^https://www\.rockstargames\.com/reddeadredemption2(/|$)",
        "require_gta6_title": False,
        "render": True,
    },
    {
        "name": "rockstar-trilogy",
        "credit": "Rockstar Games",
        "start": ["https://www.rockstargames.com/gta-the-trilogy"],
        "follow": r"^https://www\.rockstargames\.com/gta-the-trilogy(/|$)",
        "require_gta6_title": False,
        "render": True,
    },
]

MIN_W, MIN_H = 1200, 650        # smaller = logos, icons, thumbnails
MAX_ASPECT = 2.6                # wider than this = banners
SQUARE = (0.85, 1.15)           # square-ish = album covers, logos
MAX_FLAT = 0.45                 # share of perfectly smooth 8x8 blocks; logos/cut-outs score 0.6+, screenshots < 0.35
DUP_DISTANCE = 6                # phash distance treated as the same image
MAX_SIDE = 2560                 # stored images are downscaled to this long side
PER_SOURCE = 60                 # new images per source per run


def flat_fraction(im):
    """Share of 8x8 blocks with no texture. Screenshots have grain even in dark/sky areas;
    logos, flat backgrounds and transparent cut-outs do not."""
    a = np.asarray(im.convert("RGB").resize((384, 216)), dtype=np.float32)
    H, W = a.shape[0] // 8 * 8, a.shape[1] // 8 * 8
    b = a[:H, :W].reshape(H // 8, 8, W // 8, 8, 3).transpose(0, 2, 1, 3, 4).reshape(H // 8, W // 8, 64, 3)
    return float((b.std(axis=2).max(axis=-1) < 2.0).mean())


def looks_like_screenshot(im):
    w, h = im.size
    if w < MIN_W or h < MIN_H or max(w / h, h / w) > MAX_ASPECT:
        return False
    if SQUARE[0] <= w / h <= SQUARE[1]:
        return False
    if im.mode in ("RGBA", "LA", "P") and "transparency" in im.info or im.mode in ("RGBA", "LA"):
        alpha = im.convert("RGBA").getchannel("A")
        if alpha.getextrema()[0] < 250:          # any see-through area = cut-out art
            return False
    return flat_fraction(im) <= MAX_FLAT


class Fetcher:
    def __init__(self, render, delay):
        self.render, self.delay = render, delay
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.robots, self._pw, self._browser = {}, None, None

    def allowed(self, url):
        host = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if host not in self.robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.s.get(host + "/robots.txt", timeout=20)
                rp.parse(r.text.splitlines() if r.ok else [])
            except requests.RequestException:
                rp.parse([])
            self.robots[host] = rp
        return self.robots[host].can_fetch(UA, url)

    def page(self, url, render):
        if not self.allowed(url):
            log(f"  robots.txt disallows {url}, skipping")
            return ""
        time.sleep(self.delay)
        if render and self.render:
            try:
                return self._render(url)
            except Exception as e:  # browser missing or page failed: fall back to plain HTML
                log(f"  render failed ({e.__class__.__name__}), using plain HTML")
        try:
            r = self.s.get(url, timeout=30)
            return r.text if r.ok else ""
        except requests.RequestException as e:
            log(f"  fetch failed: {e}")
            return ""

    def _render(self, url):
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        pg = self._browser.new_page(user_agent=UA)
        try:
            pg.goto(url, wait_until="networkidle", timeout=60000)
            for _ in range(8):                       # scroll to trigger lazy-loaded galleries
                pg.mouse.wheel(0, 4000)
                pg.wait_for_timeout(600)
            return pg.content()
        finally:
            pg.close()

    def image(self, url):
        if not self.allowed(url):
            return None
        time.sleep(self.delay / 2)
        try:
            r = self.s.get(url, timeout=60)
            if r.ok and r.headers.get("content-type", "").startswith("image/"):
                return r.content
        except requests.RequestException:
            pass
        return None

    def close(self):
        if self._browser:
            self._browser.close()
            self._pw.stop()


def log(msg):
    print(msg, flush=True)


def best_from_srcset(srcset):
    best, best_w = None, -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                w = int(bits[1][:-1])
            except ValueError:
                pass
        if w >= best_w:
            best, best_w = bits[0], w
    return best


def extract(html, base):
    """Return (image_urls, links, title) from a page."""
    soup = BeautifulSoup(html, "html.parser")
    imgs = set()
    for tag in soup.find_all(["img", "source"]):
        for attr in ("src", "data-src", "data-lazy-src"):
            if tag.get(attr):
                imgs.add(urljoin(base, tag[attr]))
        for attr in ("srcset", "data-srcset"):
            if tag.get(attr):
                b = best_from_srcset(tag[attr])
                if b:
                    imgs.add(urljoin(base, b))
    for m in soup.find_all("meta", attrs={"property": "og:image"}) + soup.find_all("meta", attrs={"name": "twitter:image"}):
        if m.get("content"):
            imgs.add(urljoin(base, m["content"]))
    unescaped = html.replace("\\/", "/").replace("\\u002F", "/").replace("&quot;", '"')
    for m in IMG_RE.finditer(unescaped):                  # URLs inside JSON state / inline styles
        imgs.add(m.group(0))
    links = {urljoin(base, a["href"]).split("#")[0] for a in soup.find_all("a", href=True)}
    title = (soup.title.string or "").strip() if soup.title else ""
    h1 = soup.find("h1")
    if h1:
        title += " " + h1.get_text(" ", strip=True)
    return imgs, links, title


def load_manifest():
    if os.path.exists(MANIFEST):
        with open(MANIFEST) as f:
            return json.load(f)
    return {"images": []}


def save_image(data, src, manifest, hashes, seen_urls, img_url, page_url):
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return False
    if not looks_like_screenshot(im):
        return False
    w, h = im.size
    ph = imagehash.phash(im.convert("RGB"))
    if any(ph - old <= DUP_DISTANCE for old in hashes):
        seen_urls.add(img_url)
        return False
    sha = hashlib.sha1(data).hexdigest()[:10]
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    rel = os.path.join(LIB_DIR, src["name"], f"{day}-{sha}.jpg")
    os.makedirs(os.path.join(ROOT, os.path.dirname(rel)), exist_ok=True)
    out = im.convert("RGB")
    if max(out.size) > MAX_SIDE:
        out.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out.save(os.path.join(ROOT, rel), "JPEG", quality=88, optimize=True)
    w, h = out.size
    manifest["images"].append({
        "file": rel, "credit": src["credit"], "source": src["name"],
        "page": page_url, "image_url": img_url, "width": w, "height": h,
        "phash": str(ph), "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    hashes.append(ph)
    seen_urls.add(img_url)
    return True


def run(sources, render, delay, max_pages, max_new):
    manifest = load_manifest()
    hashes = [imagehash.hex_to_hash(i["phash"]) for i in manifest["images"]]
    seen_urls = {i["image_url"] for i in manifest["images"]}
    seen_urls.update(manifest.get("rejected_urls", []))
    rejected = set(manifest.get("rejected_urls", []))
    f = Fetcher(render, delay)
    added = 0
    try:
        for src in sources:
            log(f"[{src['name']}]")
            src_added = 0
            follow = re.compile(src["follow"])
            queue, visited = list(src["start"]), set()
            while queue and len(visited) < max_pages and added < max_new and src_added < PER_SOURCE:
                url = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                html = f.page(url, src["render"])
                if not html:
                    continue
                imgs, links, title = extract(html, url)
                is_start = url in src["start"]
                if src["require_gta6_title"] and not is_start and not GTA6_RE.search(title + " " + url):
                    continue
                for l in sorted(links):
                    if follow.match(l) and l not in visited and l not in queue:
                        queue.append(l)
                if src["require_gta6_title"] and is_start:
                    continue                      # listing/search pages: only follow links
                for img_url in sorted(imgs):
                    if img_url in seen_urls or added >= max_new or src_added >= PER_SOURCE:
                        continue
                    data = f.image(img_url)
                    if data and save_image(data, src, manifest, hashes, seen_urls, img_url, url):
                        added += 1
                        src_added += 1
                        log(f"  + {manifest['images'][-1]['file']}")
                    else:
                        rejected.add(img_url)
                        seen_urls.add(img_url)
    finally:
        f.close()
    manifest["rejected_urls"] = sorted(rejected)[-5000:]
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(MANIFEST, "w") as fh:
        json.dump(manifest, fh, indent=1)
    log(f"Done: {added} new image(s), {len(manifest['images'])} total.")
    return added


def clean():
    """Re-check every stored image with the current filter; delete failures, shrink oversized ones."""
    manifest = load_manifest()
    rejected = set(manifest.get("rejected_urls", []))
    kept, removed = [], 0
    for e in manifest["images"]:
        path = os.path.join(ROOT, e["file"])
        if not os.path.exists(path):
            continue
        im = Image.open(path); im.load()
        if not looks_like_screenshot(im):
            os.remove(path); rejected.add(e["image_url"]); removed += 1
            continue
        if max(im.size) > MAX_SIDE:
            im = im.convert("RGB"); im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
            im.save(path, "JPEG", quality=88, optimize=True)
            e["width"], e["height"] = im.size
        kept.append(e)
    manifest["images"], manifest["rejected_urls"] = kept, sorted(rejected)[-5000:]
    with open(MANIFEST, "w") as fh:
        json.dump(manifest, fh, indent=1)
    log(f"Clean: removed {removed}, kept {len(kept)}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-render", action="store_true", help="plain HTML only (no headless browser)")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between page requests")
    ap.add_argument("--max-pages", type=int, default=40, help="pages per source per run")
    ap.add_argument("--max-new", type=int, default=200, help="new images per run (all sources)")
    ap.add_argument("--sources", help="JSON file overriding SOURCES (for testing)")
    ap.add_argument("--clean", action="store_true", help="re-filter and shrink the existing library, then exit")
    ap.add_argument("--library", choices=["gta6", "compare"], default="gta6",
                    help="gta6: official/ + manifest.json; compare: other Rockstar games in compare/ + compare.json")
    a = ap.parse_args()
    global MANIFEST, LIB_DIR
    sources = SOURCES
    if a.library == "compare":
        MANIFEST, LIB_DIR, sources = os.path.join(ROOT, "compare.json"), "compare", COMPARE_SOURCES
    if a.clean:
        return clean()
    if a.sources:
        with open(a.sources) as fh:
            sources = json.load(fh)
    run(sources, not a.no_render, a.delay, a.max_pages, a.max_new)


if __name__ == "__main__":
    sys.exit(main())
