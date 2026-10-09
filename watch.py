#!/usr/bin/env python3
"""
Hi-fi watch
-----------
Searches second-hand marketplaces for the components listed in config.yaml,
opens each matching listing to check photos, remote control and location,
ranks everything, and writes a static page to site/index.html.

    python watch.py          # real run
    python watch.py --demo   # sample data, no network: preview the page
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import math
import os
import random
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

try:
    from PIL import Image
except ImportError:  # duplicate-photo detection is skipped without Pillow
    Image = None

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.yaml"
TEMPLATE_FILE = ROOT / "template.html"
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
STATE_FILE = DATA_DIR / "seen.json"
LISTINGS_FILE = DATA_DIR / "listings.json"

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
NOW = datetime.now(timezone.utc)
log = logging.getLogger("hifi-watch")


# =========================================================================
# Text helpers
# =========================================================================
def fold(text: str, keep_case: bool = False) -> str:
    """Strip accents (and lowercase unless keep_case): 'Hľadám' -> 'hladam'."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return t if keep_case else t.lower()


def compact(text: str) -> str:
    """Uppercase letters and digits only: 'TA-F 770 ES' -> 'TAF770ES'."""
    return re.sub(r"[^A-Z0-9]", "", fold(text).upper())


def clean(node) -> str:
    if node is None:
        return ""
    text = node.get_text(" ") if hasattr(node, "get_text") else str(node)
    return re.sub(r"\s+", " ", text).strip()


def parse_price(text: str) -> float | None:
    """'1.299,00 €' -> 1299, '8 500 Kč' -> 8500, 'Dohodou' -> None."""
    if not text:
        return None
    m = re.search(r"\d[\d\s\u00a0.']*(?:,\d{1,2})?", text)
    if not m:
        return None
    raw = re.sub(r",\d{1,2}$", "", m.group(0).strip())
    digits = re.sub(r"\D", "", raw)
    return float(digits) if digits else None


def has_any(text: str, keywords: list[str]) -> bool:
    t = fold(text)
    return any(fold(k) in t for k in keywords)


def to_iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else None


def from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def pause(cfg: dict) -> None:
    time.sleep(random.uniform(*cfg["settings"].get("request_delay_seconds", [2, 4])))


# =========================================================================
# Remote control detection (Slovak, Czech, German, English)
# =========================================================================
_REMOTE_WORDS = r"(?:dialkov\w*|dalkov\w*|ovladac(?!i)\w*|ovladani\w*|ovladanim|fernbedienung|remote)"
REMOTE_NO = [
    re.compile(r"\bbez\s+(?:\w+\s+)?" + _REMOTE_WORDS),
    re.compile(r"\bchyba\w*\s+(?:\w+\s+)?" + _REMOTE_WORDS),
    re.compile(_REMOTE_WORDS + r"\s+(?:\w+\s+)?(?:chyba|nie je|neni|nemam|nemame|nie su)"),
    re.compile(r"\bohne\s+(?:\w+\s+)?(?:fernbedienung|fb)\b"),
    re.compile(r"\b(?:no|without)\s+remote"),
]
REMOTE_NO_CASED = [re.compile(r"\b[Bb]ez\s+(?:[a-z]+\w*\s+)?DO\b"), re.compile(r"\bohne\s+FB\b")]
REMOTE_YES = [
    re.compile(r"\b(?:dialkov\w*|dalkov\w*)\s*(?:ovlada\w*)?"),
    re.compile(r"\bovladac(?!i)\w*"),
    re.compile(r"\bfernbedienung\b"),
    re.compile(r"\b(?:with|incl\w*|including)\s+(?:\w+\s+)?remote"),
    re.compile(r"\bremote\s+(?:control\s+)?included"),
]
REMOTE_YES_CASED = [
    re.compile(r"(?:\b[Ss]o?|\+|\b[Vv]ratane|\b[Vv]cetne|\b[Aa]j|\b[Oo]rigin\w*|\b[Pp]ovodn\w*)\s+(?:[a-z]+\w*\s+)?DO\b"),
    re.compile(r"\b(?:mit|inkl\.?)\s+FB\b"),
    re.compile(r"\bRM-?[A-Z]{1,3}\d{2,4}\b"),   # Sony remote model codes, e.g. RM-S703
]


REMOTE_NOT_MADE = re.compile(r"remote(?: control)?\s*:\s*(?:no|none|nein)\b")  # spec sheet line


def remote_status(text: str) -> str | None:
    """'yes', 'no', 'not_made' (model never had one), or None when the listing doesn't say."""
    low, cased = fold(text), fold(text, keep_case=True)
    if REMOTE_NOT_MADE.search(low):
        return "not_made"
    if any(p.search(low) for p in REMOTE_NO) or any(p.search(cased) for p in REMOTE_NO_CASED):
        return "no"
    if any(p.search(low) for p in REMOTE_YES) or any(p.search(cased) for p in REMOTE_YES_CASED):
        return "yes"
    return None


# =========================================================================
# Location: coordinates, postal-code lookup, distance
# =========================================================================
def km_between(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


POSTAL_PATTERNS = {"SK": r"\b(\d{3})\s?(\d{2})\b", "CZ": r"\b(\d{3})\s?(\d{2})\b",
                   "DE": r"\b(\d{5})\b", "AT": r"\b(\d{4})\b"}


class PostalGeo:
    """Postal code -> coordinates, from GeoNames (free, CC BY 4.0). Loaded per country on demand."""

    def __init__(self):
        self.tables: dict[str, dict[str, tuple[float, float]]] = {}

    def _load(self, cc: str) -> dict:
        table: dict[str, tuple[float, float]] = {}
        try:
            r = requests.get(f"https://download.geonames.org/export/zip/{cc}.zip", timeout=60)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                for line in z.read(f"{cc}.txt").decode("utf-8").splitlines():
                    cols = line.split("\t")
                    if len(cols) > 10 and cols[9] and cols[10]:
                        table.setdefault(cols[1].replace(" ", ""), (float(cols[9]), float(cols[10])))
            log.info("Postal codes loaded for %s (%d)", cc, len(table))
        except Exception as exc:  # noqa: BLE001
            log.warning("Postal codes for %s unavailable: %s", cc, exc)
        return table

    def lookup(self, cc: str | None, text: str) -> tuple[float, float] | None:
        if not cc or cc not in POSTAL_PATTERNS or not text:
            return None
        m = re.search(POSTAL_PATTERNS[cc], text)
        if not m:
            return None
        if cc not in self.tables:
            self.tables[cc] = self._load(cc)
        return self.tables[cc].get("".join(m.groups()))


# =========================================================================
# Photos: duplicate detection and optional Claude check
# =========================================================================
def dhash(img_bytes: bytes) -> str | None:
    if Image is None:
        return None
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("L").resize((9, 8))
        px = list(im.getdata())
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
        return f"{bits:016x}"
    except Exception:  # noqa: BLE001
        return None


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


PHOTO_PROMPT = (
    "This is the main photo of a second-hand marketplace listing for a hi-fi component. "
    "Is it a real photo taken by the seller (home, shelf, floor or garage, uneven light, visible "
    "surroundings) or a stock or catalogue image (manufacturer press shot, clean studio background, "
    "screenshot of a website, another shop's watermark)? Reply with JSON only: "
    '{"stock": true or false, "reason": "max 8 words"}'
)


def claude_photo_check(img_bytes: bytes, media_type: str, api_key: str) -> dict | None:
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 100,
                  "messages": [{"role": "user", "content": [
                      {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                   "data": base64.b64encode(img_bytes).decode()}},
                      {"type": "text", "text": PHOTO_PROMPT}]}]},
            timeout=60,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as exc:  # noqa: BLE001
        log.warning("Claude photo check failed: %s", exc)
        return None


# =========================================================================
# Exchange rates (ECB daily reference rates, free, no key)
# =========================================================================
def load_rates(fallback_czk: float) -> dict[str, float]:
    rates = {"EUR": 1.0, "CZK": fallback_czk}
    try:
        r = requests.get("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml", timeout=15)
        r.raise_for_status()
        for cube in ET.fromstring(r.content).iter():
            if cube.get("currency") and cube.get("rate"):
                rates[cube.get("currency")] = float(cube.get("rate"))
        log.info("ECB rates loaded (CZK per EUR: %.2f)", rates["CZK"])
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load ECB rates, using fallback: %s", exc)
    return rates


# =========================================================================
# Sources (search results). If a source starts returning nothing, its
# marketplace probably changed its HTML: the CSS selectors are here.
# =========================================================================
BAZOS_DATE = re.compile(r"\[(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\]")


def fetch_bazos(session, host: str, source: str, currency: str, country: str, query: str):
    r = session.get(f"https://{host}/search.php",
                    params={"hledat": query, "rubriky": "www", "hlokalita": "", "humkreis": "",
                            "cenaod": "", "cenado": "", "kitx": "ano"}, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for ad in soup.select("div.inzeraty"):
        link = ad.select_one("h2.nadpis a") or ad.select_one("a[href*='/inzerat/']")
        if not link or not link.get("href"):
            continue
        posted = None
        m = BAZOS_DATE.search(ad.get_text(" "))
        if m:
            day, month, year = map(int, m.groups())
            try:
                posted = datetime(year, month, day, 12, tzinfo=timezone.utc)
            except ValueError:
                pass
        img = ad.select_one("img")
        out.append({
            "source": source, "country": country, "kind": "bazos",
            "url": urljoin(r.url, link["href"]),
            "title": clean(link),
            "description": clean(ad.select_one(".popis")),
            "price": parse_price(clean(ad.select_one(".inzeratycena"))),
            "currency": currency,
            "location": clean(ad.select_one(".inzeratylok")),
            "posted": posted,
            "image": urljoin(r.url, img["src"]) if img and img.get("src") else None,
            "auction": False,
        })
    return out


def parse_german_date(text: str) -> datetime | None:
    t = fold(text)
    today = NOW.replace(hour=12, minute=0, second=0, microsecond=0)
    if "heute" in t:
        return today
    if "gestern" in t:
        return today - timedelta(days=1)
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", t)
    if m:
        try:
            return datetime(int(m[3]), int(m[2]), int(m[1]), 12, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def fetch_kleinanzeigen(session, query: str):
    slug = re.sub(r"[^a-z0-9]+", "-", fold(query)).strip("-")
    r = session.get(f"https://www.kleinanzeigen.de/s-{slug}/k0", timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for art in soup.select("article.aditem"):
        href = art.get("data-href") or (art.select_one("a[href]") or {}).get("href")
        title_node = art.select_one("h2 a") or art.select_one(".ellipsis")
        if not href or not title_node:
            continue
        img = art.select_one("img")
        out.append({
            "source": "Kleinanzeigen", "country": "DE", "kind": "kleinanzeigen",
            "url": urljoin("https://www.kleinanzeigen.de", href),
            "title": clean(title_node),
            "description": clean(art.select_one(".aditem-main--middle--description")),
            "price": parse_price(clean(art.select_one(".aditem-main--middle--price-shipping--price"))),
            "currency": "EUR",
            "location": clean(art.select_one(".aditem-main--top--left")),
            "posted": parse_german_date(clean(art.select_one(".aditem-main--top--right"))),
            "image": img.get("src") if img else None,
            "auction": False,
        })
    return out


VINTAGEHIFI = "https://www.vintagehifi.cz"
VINTAGEHIFI_SEARCH = f"{VINTAGEHIFI}/1364688365/e-search"  # shop id in the path; see SearchAction on the homepage


def fetch_vintagehifi(session, query: str):
    """vintagehifi.cz is a dealer e-shop: fixed CZK prices, shipping, no listing dates."""
    r = session.get(VINTAGEHIFI_SEARCH, params={"q": query}, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for prod in soup.select("div[data-selector='product'][data-id]"):
        link = prod.select_one("a[data-selector='name']")
        if not link or not link.get("href"):
            continue
        if not prod.select_one(".in-stock"):
            continue
        try:
            price = float(prod.get("data-price") or "") or None
        except ValueError:
            price = None
        img = prod.select_one("img")
        out.append({
            "source": "VintageHifi.cz", "country": "CZ", "kind": "vintagehifi",
            "url": urljoin(VINTAGEHIFI, link["href"]),
            "title": clean(link),
            "description": "",
            "price": price,
            "currency": "CZK",
            "location": "E-shop (shipping)",
            "posted": None,
            "image": urljoin(VINTAGEHIFI, img["src"]) if img and img.get("src") else None,
            "auction": False,
        })
    return out


def ebay_token(client_id: str, client_secret: str) -> str:
    r = requests.post("https://api.ebay.com/identity/v1/oauth2/token", auth=(client_id, client_secret),
                      data={"grant_type": "client_credentials",
                            "scope": "https://api.ebay.com/oauth/api_scope"}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


EBAY_NAMES = {"EBAY_DE": "eBay.de", "EBAY_AT": "eBay.at", "EBAY_GB": "eBay.co.uk",
              "EBAY_FR": "eBay.fr", "EBAY_IT": "eBay.it", "EBAY_NL": "eBay.nl"}


def fetch_ebay(session, token: str, marketplace: str, delivery_country: str | None, query: str):
    params = {"q": query, "limit": 50}
    if delivery_country:
        params["filter"] = f"deliveryCountry:{delivery_country}"
    r = session.get("https://api.ebay.com/buy/browse/v1/item_summary/search", params=params,
                    headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace},
                    timeout=25)
    r.raise_for_status()
    out = []
    for it in r.json().get("itemSummaries", []):
        price = it.get("price") or it.get("currentBidPrice") or {}
        loc = it.get("itemLocation") or {}
        out.append({
            "source": EBAY_NAMES.get(marketplace, marketplace), "country": loc.get("country"),
            "kind": "ebay",
            "url": it.get("itemWebUrl"),
            "title": it.get("title", ""),
            "description": it.get("shortDescription", "") or "",
            "condition": it.get("condition", ""),
            "price": float(price["value"]) if price.get("value") else None,
            "currency": price.get("currency", "EUR"),
            "location": " ".join(x for x in [loc.get("postalCode"), loc.get("country")] if x),
            "posted": from_iso(it.get("itemCreationDate")),
            "image": (it.get("image") or {}).get("imageUrl"),
            "photo_count": (1 if it.get("image") else 0) + len(it.get("additionalImages") or []),
            "auction": "AUCTION" in (it.get("buyingOptions") or []),
        })
    return out


def build_fetchers(cfg: dict, session):
    src = cfg.get("sources", {})
    fetchers = {}
    if src.get("bazos_sk", {}).get("enabled"):
        fetchers["Bazoš.sk"] = lambda q: fetch_bazos(session, "www.bazos.sk", "Bazoš.sk", "EUR", "SK", q)
    if src.get("bazos_cz", {}).get("enabled"):
        fetchers["Bazoš.cz"] = lambda q: fetch_bazos(session, "www.bazos.cz", "Bazoš.cz", "CZK", "CZ", q)
    if src.get("kleinanzeigen", {}).get("enabled"):
        fetchers["Kleinanzeigen"] = lambda q: fetch_kleinanzeigen(session, q)
    if src.get("vintagehifi_cz", {}).get("enabled"):
        fetchers["VintageHifi.cz"] = lambda q: fetch_vintagehifi(session, q)
    ebay_cfg = src.get("ebay", {})
    if ebay_cfg.get("enabled"):
        cid, secret = os.getenv("EBAY_CLIENT_ID"), os.getenv("EBAY_CLIENT_SECRET")
        if cid and secret:
            try:
                token = ebay_token(cid, secret)
                for mp in ebay_cfg.get("marketplaces", ["EBAY_DE"]):
                    fetchers[EBAY_NAMES.get(mp, mp)] = (lambda q, mp=mp: fetch_ebay(
                        session, token, mp, ebay_cfg.get("delivery_country"), q))
            except Exception as exc:  # noqa: BLE001
                log.warning("eBay login failed, skipping eBay: %s", exc)
        else:
            log.info("eBay skipped: EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set")
    return fetchers


# =========================================================================
# Listing details (Bazoš detail page: full text, photo count, map pin)
# =========================================================================
def fetch_bazos_detail(session, url: str) -> dict:
    r = session.get(url, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    ad_id = (re.search(r"/inzerat/(\d+)/", url) or [None, ""])[1]
    photos = {int(n) for n, i in re.findall(r"/img/(\d+)t?/\d{3}/(\d+)\.jpg", r.text) if i == ad_id}
    desc = clean(soup.select_one(".popisdetail"))
    if not desc:
        meta = soup.select_one('meta[property="og:description"], meta[name="description"]')
        desc = meta.get("content", "") if meta else ""
    pin = re.search(r"maps/place/(-?\d+\.\d+),(-?\d+\.\d+)", r.text)
    og_img = soup.select_one('meta[property="og:image"]')
    return {
        "description": desc,
        "photo_count": len(photos),
        "lat": float(pin[1]) if pin else None,
        "lon": float(pin[2]) if pin else None,
        "main_image": og_img.get("content") if og_img else None,
    }


def fetch_vintagehifi_detail(session, url: str) -> dict:
    r = session.get(url, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    og_img = soup.select_one('meta[property="og:image"]')
    main = og_img.get("content") if og_img else None
    pid = (re.search(r"_vyr_(\d+)_", main or "") or [None, None])[1]
    photos = set(re.findall(rf"/fotos/(_vyr_{pid}_\d+|_vyrp\d+_{pid}\d\d)\.", r.text)) if pid else set()
    desc = clean(soup.select_one("#detail-anchor-description"))
    return {"description": desc, "photo_count": len(photos), "main_image": main}


def enrich(listings: list[dict], cfg: dict, session, cache: dict, offline: bool = False) -> None:
    """Adds remote status, photo verdict and distance. Results are cached per URL across runs."""
    home = cfg.get("home") or {}
    geo = PostalGeo()
    api_key = os.getenv("ANTHROPIC_API_KEY") if cfg.get("photos", {}).get("claude_check") else None

    for l in listings:
        d = dict(cache.get(l["url"], {}).get("detail") or {})
        if not d and not offline:
            d["checked"] = to_iso(NOW)
            if l["kind"] == "bazos":
                try:
                    d.update(fetch_bazos_detail(session, l["url"]))
                    pause(cfg)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Detail page failed %s: %s", l["url"], exc)
            elif l["kind"] == "vintagehifi":
                try:
                    d.update(fetch_vintagehifi_detail(session, l["url"]))
                    pause(cfg)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Detail page failed %s: %s", l["url"], exc)
            text = f"{l['title']} {d.get('description') or l.get('description', '')}"
            d["remote"] = remote_status(text)
            d["desc_fault"] = has_any(d.get("description") or l.get("description", ""),
                                      cfg.get("defect_keywords", []))
            if d.get("lat") is None:
                ll = geo.lookup(l.get("country"), l.get("location", ""))
                if ll:
                    d["lat"], d["lon"] = ll
            img_url = d.get("main_image") or l.get("image")
            if img_url:
                try:
                    ir = session.get(img_url, timeout=25)
                    ir.raise_for_status()
                    d["dhash"] = dhash(ir.content)
                    if api_key:
                        mt = ir.headers.get("content-type", "image/jpeg").split(";")[0]
                        if mt not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                            mt = "image/jpeg"
                        d["claude"] = claude_photo_check(ir.content, mt, api_key)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Image check failed %s: %s", img_url, exc)
            d.pop("description", None)  # keep the cache small
        elif not d:  # offline demo: derive what we can from the item itself
            d = {"remote": remote_status(f"{l['title']} {l.get('description', '')}"),
                 "desc_fault": has_any(l.get("description", ""), cfg.get("defect_keywords", [])),
                 "lat": l.get("lat"), "lon": l.get("lon"), "photo_count": l.get("photo_count"),
                 "claude": l.get("claude"), "dhash": l.get("dhash")}

        if l.get("photo_count") and not d.get("photo_count"):
            d["photo_count"] = l["photo_count"]
        l["_detail"] = d
        l["remote"] = d.get("remote")
        l["photo_count"] = d.get("photo_count")
        l["desc_fault"] = d.get("desc_fault", False)
        l["distance_km"] = (round(km_between(home["lat"], home["lon"], d["lat"], d["lon"]))
                            if home.get("lat") and d.get("lat") is not None else None)

    # Same picture in listings from different places = probably borrowed from the internet
    known = [(l["url"], l["_detail"].get("dhash"), l.get("location", "")) for l in listings]
    known += [(u, e.get("detail", {}).get("dhash"), e.get("location", "")) for u, e in cache.items()]
    for l in listings:
        h = l["_detail"].get("dhash")
        l["photo_duplicate"] = bool(h) and any(
            u != l["url"] and oh and loc != l.get("location", "") and hamming(h, oh) <= 6
            for u, oh, loc in known)

        verdict = (l["_detail"].get("claude") or {})
        if verdict.get("stock") is True or l["photo_duplicate"]:
            l["photo_kind"] = "stock"
            l["photo_reason"] = verdict.get("reason") if verdict.get("stock") else "Same photo in another seller's listing"
        elif (l["photo_count"] or 0) >= 2 or verdict.get("stock") is False:
            l["photo_kind"] = "own"
        elif l["photo_count"] == 1:
            l["photo_kind"] = "single"
        else:
            l["photo_kind"] = None


# =========================================================================
# Matching, filtering, scoring
# =========================================================================
def build_matchers(models):
    pairs = [(compact(a), m) for m in models for a in (m.get("aliases") or [m["id"]])]
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def match_models(text: str, matchers) -> list[dict]:
    c = compact(text)
    found: list[dict] = []
    for key, model in matchers:
        if key in c and all(model["id"] != f["id"] for f in found):
            found.append(model)
    return found


def prefilter(item: dict, model: dict, cfg: dict) -> bool:
    """Title-based checks that don't need the detail page. False = drop."""
    title, ref, price_eur = item["title"], float(model["reference_eur"]), item.get("price_eur")
    if has_any(title, cfg.get("wanted_ad_keywords", [])):
        return False
    if has_any(title, cfg.get("defect_keywords", [])) and cfg["settings"].get("exclude_defective", True):
        return False
    if has_any(title, cfg.get("accessory_keywords", [])) and price_eur is not None and price_eur < 0.3 * ref:
        return False
    return True


def score_listing(item: dict, model: dict, cfg: dict) -> dict:
    sc = cfg.get("scoring", {})
    ref = float(model["reference_eur"])
    price_eur = item.get("price_eur")
    notes: list[str] = []
    parts: list[list] = []

    if has_any(item["title"], cfg.get("defect_keywords", [])):
        notes.append("Title says faulty or for parts")
    elif item.get("desc_fault"):
        notes.append("Description mentions a fault, read it first")

    deal_pct = deal_label = None
    if item.get("bundle"):
        notes.append("Sold together with other items, price covers the set")
    elif item.get("auction"):
        notes.append("Auction: price is the current bid")
    elif price_eur is None:
        notes.append("No price given")
    elif price_eur < 0.2 * ref:
        notes.append("Price looks unusually low, check the listing")
    else:
        ratio = price_eur / ref
        deal_pct = round((1 - ratio) * 100)
        deal_label = ("Great price" if ratio <= 0.75 else "Good price" if ratio <= 0.95 else
                      "Fair price" if ratio <= 1.15 else "Above market")
    parts.append(["Price", deal_pct] if deal_pct is not None else ["Price unknown", sc.get("unknown_price", -15)])

    tier = int(model.get("tier", 3))
    tb = (sc.get("tier_bonus") or {}).get(tier, 0)
    if tb:
        parts.append(["Wish list", tb])

    if model.get("remote_exists", True):
        if item.get("remote") == "yes":
            parts.append(["Remote included", sc.get("remote_included", 8)])
        elif item.get("remote") == "no":
            parts.append(["No remote", sc.get("remote_missing", -8)])

    if item.get("photo_kind") == "own":
        parts.append(["Own photos", sc.get("own_photos", 6)])
    elif item.get("photo_kind") == "stock":
        parts.append(["Stock photo", sc.get("stock_photo", -15)])

    km = item.get("distance_km")
    if km is not None:
        zero = float(sc.get("distance_zero_km", 250))
        factor = max(float(sc.get("distance_min_factor", -0.8)), min(1.0, 1 - km / zero))
        parts.append(["Distance", round(sc.get("distance_bonus", 15) * factor)])

    item.update({
        "model": model["id"], "brand": model.get("brand", ""), "category": model.get("category", "Other"),
        "tier": tier, "reference_eur": ref, "deal_pct": deal_pct, "deal_label": deal_label,
        "score": sum(v for _, v in parts), "score_parts": parts, "notes": notes,
    })
    return item


# =========================================================================
# Pipeline
# =========================================================================
def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "sk,cs;q=0.9,de;q=0.8,en;q=0.7"})
    return s


def collect(cfg: dict, session) -> tuple[list[dict], list[dict]]:
    fetchers = build_fetchers(cfg, session)
    status = {n: {"name": n, "ok": 0, "failed": 0, "found": 0} for n in fetchers}
    raw: list[dict] = []
    for model in cfg["models"]:
        for query in model.get("queries") or [model["id"]]:
            for name, fetch in fetchers.items():
                try:
                    results = fetch(query)
                    status[name]["ok"] += 1
                    for r in results:
                        r["_query_model"] = model["id"]
                    raw.extend(results)
                    log.info("%-14s %-22s %3d results", name, query, len(results))
                except Exception as exc:  # noqa: BLE001
                    status[name]["failed"] += 1
                    log.warning("%-14s %-22s failed: %s", name, query, exc)
                pause(cfg)
    return raw, list(status.values())


def match_and_filter(raw: list[dict], cfg: dict, rates: dict) -> list[dict]:
    matchers = build_matchers(cfg["models"])
    by_id = {m["id"]: m for m in cfg["models"]}
    max_age = timedelta(days=cfg["settings"].get("max_listing_age_days", 60))
    out: dict[str, dict] = {}
    for item in raw:
        if not item.get("url") or item["url"] in out:
            continue
        title_models = match_models(item["title"], matchers)
        models = title_models or match_models(item.get("description", ""), matchers)
        if not models:
            continue
        queried = by_id.get(item.pop("_query_model", None))
        model = queried if queried and any(m["id"] == queried["id"] for m in models) else models[0]
        item["bundle"] = len(title_models) > 1
        item["_model"] = model
        if item.get("posted") and NOW - item["posted"] > max_age:
            continue
        rate = rates.get(item.get("currency") or "EUR")
        item["price_eur"] = round(item["price"] / rate, 2) if item.get("price") and rate else None
        if prefilter(item, model, cfg):
            out[item["url"]] = item
    return list(out.values())


def apply_state(listings: list[dict], status: list[dict], state: dict) -> list[dict]:
    """Marks new listings and price drops; keeps old listings of a source that failed."""
    previous = json.loads(LISTINGS_FILE.read_text()).get("listings", []) if LISTINGS_FILE.exists() else []
    first_run = not state
    failed = {s["name"] for s in status if s["ok"] == 0 and s["failed"] > 0}
    urls = {l["url"] for l in listings}
    for old in previous:
        if old["source"] in failed and old["url"] not in urls:
            old["notes"] = [n for n in old.get("notes", []) if not n.startswith("Not rechecked")]
            old["notes"].append("Not rechecked this run (source unavailable)")
            old["is_new"] = False
            old["_carried"] = True
            listings.append(old)
            urls.add(old["url"])

    now_iso = to_iso(NOW)
    for l in listings:
        entry = state.get(l["url"])
        l["is_new"] = entry is None and not first_run
        if l.get("_carried"):
            continue
        l["price_drop_eur"] = None
        if entry:
            l["first_seen"] = entry["first_seen"]
            last = entry.get("price_eur")
            if last and l.get("price_eur") and l["price_eur"] < last - 1:
                l["price_drop_eur"] = round(last - l["price_eur"])
            elif entry.get("price_drop_eur") and l.get("price_eur") == last:
                l["price_drop_eur"] = entry["price_drop_eur"]
        else:
            l["first_seen"] = now_iso
        state[l["url"]] = {"first_seen": l["first_seen"], "last_seen": now_iso,
                           "price_eur": l.get("price_eur"), "price_drop_eur": l.get("price_drop_eur"),
                           "location": l.get("location", ""), "detail": l.get("_detail", {})}

    cutoff = NOW - timedelta(days=90)
    for k in [k for k, v in state.items() if (from_iso(v.get("last_seen")) or NOW) < cutoff]:
        del state[k]
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    return listings


PUBLIC_FIELDS = ("source", "url", "title", "model", "brand", "category", "tier", "price", "currency",
                 "price_eur", "reference_eur", "deal_pct", "deal_label", "score", "score_parts",
                 "location", "distance_km", "posted", "first_seen", "image", "photo_count",
                 "photo_kind", "photo_reason", "remote", "is_new", "price_drop_eur", "notes", "auction")


def render(listings: list[dict], status: list[dict], cfg: dict, save_data: bool = True) -> None:
    out = []
    for l in listings:
        if isinstance(l.get("posted"), datetime):
            l["posted"] = to_iso(l["posted"])
        out.append({k: l.get(k) for k in PUBLIC_FIELDS})
    payload = {
        "generated": to_iso(NOW),
        "home": (cfg.get("home") or {}).get("name"),
        "scoring": cfg.get("scoring", {}),
        "listings": out,
        "sources": status,
        "models": [{k: m.get(k) for k in ("id", "brand", "category", "tier", "reference_eur")}
                   for m in cfg["models"]],
    }
    if save_data:
        LISTINGS_FILE.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE_FILE.read_text(encoding="utf-8").replace("__DATA_JSON__", data)
    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(html, encoding="utf-8")
    log.info("Wrote %d listings to %s", len(out), SITE_DIR / "index.html")


def demo_data():
    """Sample listings modelled on real Bazoš finds, so the page can be previewed offline."""
    B = "https://elektro.bazos.sk/inzerat/"
    S = [  # url, source, title, description, price, cur, location, lat, lon, photos, days, claude_stock
        # real listings from Oct 2026 (details as they were when checked)
        (B + "196209540/sony-ta-fa30es-spickovy-zosilnovac-zo-serie-es-bez-do.php", "Bazoš.sk",
         "Sony TA-FA30ES – špičkový zosilňovač zo série ES (bez DO", "100% funkčný, zvuk je čistý",
         350, "EUR", "Levice 934 05", 48.2044, 18.6110, 4, 2, None),
        (B + "195908522/sony-ta-fa30es-w.php", "Bazoš.sk", "SONY TA-FA30ES",
         "Je v 100 % funkčnom stave. S diaľkovým ovládaním. Originál stav.",
         380, "EUR", "Košice-okolie 044 02", 48.7000, 21.2000, 4, 11, None),
        (B + "195228514/predam-krasny-sony-ta-fa5es-s-originalnym-do.php", "Bazoš.sk",
         "Predám krásny Sony TA-FA5ES s originálnym DO", "V cene je originálne diaľkové Sony RM-S703",
         798, "EUR", "Liptovský Mikuláš 031 01", 49.0868, 19.6102, 10, 33, None),
        (B + "196129405/sony-ta-f770es.php", "Bazoš.sk", "Sony TA-F770ES",
         "Predám zosilňovač Sony TA-F770ES.", 699, "EUR", "Dunajská Streda 929 01",
         47.9924, 17.6171, 1, 4, None),
        (B + "194945606/sony-ta-f630esd-esprit.php", "Bazoš.sk", "Sony TA-F630ESD Nový DA prevodník",
         "plne funkčný, po prečistení. Remote control: No", 380, "EUR", "Banská Bystrica 974 05",
         48.7208, 19.1330, 6, 42, None),
        # made-up examples to show the other cases
        ("https://example.com/1", "Bazoš.sk", "Sony TA-F 590 ES", "foto z internetu", 190, "EUR",
         "Bratislava 821 01", 48.1486, 17.1077, 1, 3, True),
        ("https://example.com/2", "Bazoš.cz", "Sony TA-F670ES ES series", "Bez dálkového ovládání",
         11500, "CZK", "Brno 602 00", 49.1951, 16.6068, 4, 2, None),
        ("https://example.com/3", "Kleinanzeigen", "Sony TA-FA50ES Vollverstärker silber",
         "mit Fernbedienung RM-S50", 330, "EUR", "80331 München", 48.1372, 11.5755, None, 0, None),
        ("https://example.com/4", "eBay.de", "SONY CDP-XA30ES CD Player Top Zustand", "", 410, "EUR",
         "10115 DE", 52.5323, 13.3846, 8, 3, None),
        ("https://example.com/5", "Bazoš.sk", "Sony TC-KA6ES kazetový deck", "aj s DO", 320, "EUR",
         "Zvolen 960 01", 48.5762, 19.1371, 5, 0, None),
        ("https://example.com/6", "Bazoš.sk", "Hľadám Sony TA-F770ES", "", None, "EUR",
         "Košice 040 01", 48.72, 21.26, 0, 1, None),
    ]
    raw = []
    for url, src, title, desc, price, cur, loc, lat, lon, photos, days, stock in S:
        raw.append({"source": src, "kind": "demo", "country": None, "url": url,
                    "title": title, "description": desc, "price": price, "currency": cur,
                    "location": loc, "lat": lat, "lon": lon, "photo_count": photos,
                    "claude": {"stock": stock, "reason": "Catalogue shot on white background"} if stock is not None else None,
                    "posted": NOW - timedelta(days=days), "image": None, "auction": False})
    status = [{"name": s, "ok": 19, "failed": 0, "found": 0}
              for s in ("Bazoš.sk", "Bazoš.cz", "Kleinanzeigen", "eBay.de", "eBay.at")]
    return raw, status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="use sample data, no network")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    cfg.setdefault("settings", {})
    DATA_DIR.mkdir(exist_ok=True)
    session = new_session()
    state = {} if args.demo else (json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {})

    if args.demo:
        raw, status = demo_data()
        rates = {"EUR": 1.0, "CZK": cfg["settings"].get("czk_per_eur_fallback", 25.0)}
    else:
        rates = load_rates(cfg["settings"].get("czk_per_eur_fallback", 25.0))
        raw, status = collect(cfg, session)

    listings = match_and_filter(raw, cfg, rates)
    enrich(listings, cfg, session, state, offline=args.demo)
    listings = [score_listing(l, l.pop("_model"), cfg) for l in listings]
    for s in status:
        s["found"] = sum(1 for l in listings if l["source"] == s["name"])

    if args.demo:
        for i, l in enumerate(listings):
            l.update(is_new=i % 3 != 2, first_seen=to_iso(NOW), price_drop_eur=40 if i == 3 else None)
    else:
        listings = apply_state(listings, status, state)
    render(listings, status, cfg, save_data=not args.demo)

    if not args.demo and status and all(s["ok"] == 0 for s in status):
        log.error("Every source failed. Check the log above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
