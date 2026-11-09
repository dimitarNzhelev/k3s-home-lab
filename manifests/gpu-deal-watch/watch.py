#!/usr/bin/env python3
"""GPU deal watch: ping Discord when an RTX 3090 is listed cheaply on OLX.bg or Bazar.bg.

Runs every few minutes as a Kubernetes CronJob (cronjob.yaml). Standard library only,
so the stock python image runs it without installing anything.

Settings (environment variables):
  DISCORD_WEBHOOK_URL  Discord webhook; Alertmanager's ".../slack" form is fine too
  MAX_EUR / MIN_EUR    alert on prices in this range, inclusive (default 1000 / 300; the
                       floor skips coolers, empty boxes and "1 лв" placeholder prices)
  MAX_ALERTS           at most this many messages per run (default 10); the rest go next run
  STATE_FILE           where alerted listings are remembered (default /data/seen.json)
  DRY_RUN=1            only print what would be alerted; send and remember nothing
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable

BGN_PER_EUR = 1.95583  # the lev's fixed euro conversion rate

QUERY = "3090"
OLX_PAGE_SIZE = 50  # the most the OLX API returns per request
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
SITE_NAMES = {"olx": "OLX", "bazar": "Bazar"}

# OLX's CloudFront firewall answers 403 to the default TLS handshake of OpenSSL 3.5
# (as in python:3.13-alpine, which offers the post-quantum X25519MLKEM768 key exchange)
# and 200 when only classic X25519 is offered. Verified from the cluster on 2026-11-09.
SITE_TLS = ssl.create_default_context()
SITE_TLS.set_ecdh_curve("X25519")

MODEL_RE = re.compile(r"(?<!\d)3090(?!\d)")
# A GPU word must be in the title too: plenty of other products carry "3090" in their
# model name (Aiwa AF-3090, Kenwood KR-3090, a JN-3090 compressor, nail lamps, car parts).
GPU_RE = re.compile(
    r"rtx|geforce|nvidia|\bgpu\b|видео\s?карт|графичн|gddr6x|24\s?gb|24\s?гб|founders|\bfe\b"
    r"|asus|\bmsi\b|gigabyte|aorus|zotac|evga|palit|gainward|inno3d|\bpny\b|galax|kfa2|colorful"
    r"|manli|strix|\btuf\b|suprim|ventus|trinity|ichill|phoenix|gamerock",
    re.IGNORECASE)
SKIP_RE = re.compile(
    r"(?<!не )\b(?:купувам|търся|търсим|изкупувам)\b"               # wanted ads ("не търся бартер" is fine)
    r"|\bза части\b|\bне работи\b|\bнеработещ|\bповреден|\bизгоря"  # broken cards
    r"|optiplex",                                                    # Dell OptiPlex 3090 desktops
    re.IGNORECASE)
PRICE_RE = re.compile(r"(\d[\d\s.,]*)\s*(€|eur|лв|bgn)", re.IGNORECASE)


@dataclass(frozen=True)
class Listing:
    site: str
    id: str
    title: str
    price_eur: float | None
    url: str
    location: str = ""

    @property
    def key(self) -> str:
        return f"{self.site}:{self.id}"


def looks_like_3090(title: str) -> bool:
    return bool(MODEL_RE.search(title) and GPU_RE.search(title)) and not SKIP_RE.search(title)


def _number(raw: str) -> float | None:
    """'1 400' / '1.400' / '1,400.50' / '999,99' -> float."""
    s = re.sub(r"\s", "", raw).rstrip(".,")
    if "," in s and "." in s:  # both used: the last one is the decimal separator
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s or "." in s:  # one kind: three digits after it means thousands
        sep = "," if "," in s else "."
        whole, _, tail = s.rpartition(sep)
        s = s.replace(sep, "") if len(tail) == 3 else whole.replace(sep, "") + "." + tail
    try:
        return float(s)
    except ValueError:
        return None


def parse_price(text: str) -> float | None:
    """Price text such as '1 400 €' or '2 738 лв.' -> euros; None when there is no price."""
    match = PRICE_RE.search(text)
    if not match:
        return None
    amount = _number(match.group(1))
    if not amount or amount <= 0:
        return None
    return round(amount / BGN_PER_EUR, 2) if match.group(2).lower() in ("лв", "bgn") else amount


def _olx_price(offer: dict) -> float | None:
    for param in offer.get("params", []):
        if param.get("key") != "price":
            continue
        value = param.get("value") or {}
        amount, currency = value.get("value"), (value.get("currency") or "").upper()
        if value.get("type", "price") != "price" or not amount or amount <= 0:
            return None  # "по договаряне", exchange, free
        if currency == "EUR":
            return float(amount)
        if currency == "BGN":
            return round(amount / BGN_PER_EUR, 2)
        return None
    return None


def parse_olx(payload: dict) -> list[Listing]:
    """Offers from https://www.olx.bg/api/v1/offers/ (the JSON API the OLX site itself uses)."""
    listings: dict[str, Listing] = {}
    for offer in payload.get("data", []):
        offer_id = str(offer["id"])
        if offer_id in listings:
            continue  # promoted ads can repeat on a page
        city = ((offer.get("location") or {}).get("city") or {}).get("name", "")
        listings[offer_id] = Listing("olx", offer_id, (offer.get("title") or "").strip(),
                                     _olx_price(offer), offer.get("url", ""), city)
    return list(listings.values())


class _BazarParser(HTMLParser):
    """Collects the ads on a bazar.bg search page: <a class="listItemLink" data-id=...>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ads: list[dict[str, str]] = []
        self._ad: dict[str, str] | None = None
        self._field: str | None = None  # "price" / "location" while inside that span
        self._depth = 0                 # spans nested inside that field (the currency span)

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if tag == "a" and "listItemLink" in classes and (attr.get("data-id") or "").isdigit():
            self._ad = {"id": attr["data-id"], "url": attr.get("href") or "",
                        "title": attr.get("title") or "", "price": "", "location": ""}
            self._field = None
        elif self._ad is not None and tag == "span":
            if self._field:
                self._depth += 1
            elif "price" in classes or "location" in classes:
                self._field = "price" if "price" in classes else "location"
                self._depth = 0

    def handle_endtag(self, tag):
        if self._ad is None:
            return
        if tag == "span" and self._field:
            if self._depth:
                self._depth -= 1
            else:
                self._field = None
        elif tag == "a":
            self.ads.append(self._ad)
            self._ad = self._field = None

    def handle_data(self, data):
        if self._ad is not None and self._field:
            self._ad[self._field] += data


def parse_bazar(html: str) -> list[Listing]:
    parser = _BazarParser()
    parser.feed(html)
    parser.close()
    listings: dict[str, Listing] = {}
    for ad in parser.ads:
        if ad["id"] not in listings:  # every ad appears more than once on the page
            listings[ad["id"]] = Listing(
                "bazar", ad["id"], " ".join(ad["title"].split()), parse_price(ad["price"]),
                urllib.parse.urljoin("https://bazar.bg/", ad["url"]), " ".join(ad["location"].split()))
    return list(listings.values())


def _sent_record(item: Listing) -> dict:
    return {"url": item.url, "title": item.title, "price_eur": item.price_eur}


def already_sent(item: Listing, state: dict) -> bool:
    """True when this listing was already sent with the same link, title and price."""
    prev = state.get(item.key)
    if isinstance(prev, (int, float)):  # state written before 2026-11-09 kept only the price
        return prev == item.price_eur
    return isinstance(prev, dict) and all(prev.get(k) == v for k, v in _sent_record(item).items())


def pick_alerts(listings: list[Listing], state: dict, min_eur: float, max_eur: float) -> list[Listing]:
    """3090s priced in [min_eur, max_eur] not yet sent with this link, title and price; cheapest first."""
    deals = [item for item in listings
             if item.price_eur is not None and min_eur <= item.price_eur <= max_eur
             and looks_like_3090(item.title) and not already_sent(item, state)]
    return sorted(deals, key=lambda item: item.price_eur)


def _eur(amount: float) -> str:
    """1400.0 -> '1 400 €', 715.81 -> '715.81 €'."""
    text = f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.2f}"
    return text.replace(",", " ") + " €"


def discord_payload(listing: Listing) -> dict:
    where = f" · {listing.location}" if listing.location else ""
    content = (f"**{_eur(listing.price_eur)}** · {SITE_NAMES.get(listing.site, listing.site)}{where}\n"
               f"{listing.title}\n{listing.url}")
    # allowed_mentions: an ad titled "@everyone ..." must not ping the server.
    return {"username": "GPU deal watch", "content": content[:2000], "allowed_mentions": {"parse": []}}


def native_webhook(url: str) -> str:
    """The Alertmanager secret holds Discord's Slack-compatible URL (.../slack); use the native one."""
    return url.strip().removesuffix("/slack")


def send_discord(webhook: str, payload: dict) -> None:
    request = urllib.request.Request(
        webhook, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "gpu-deal-watch/1.0"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=20):
                return
        except urllib.error.HTTPError as err:
            if err.code != 429 or attempt == 2:
                raise
            try:  # rate limited: wait as long as Discord asks, then retry once
                wait = float(json.loads(err.read()).get("retry_after", 2))
            except ValueError:
                wait = 2.0
            time.sleep(min(wait, 30))


def load_state(path: str) -> dict:
    """Alerts already sent: {"olx:123": {"url": ..., "title": ..., "price_eur": ...}}."""
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError as err:
        print(f"WARNING: {path} is unreadable ({err}); starting with an empty memory", file=sys.stderr)
        return {}
    return state if isinstance(state, dict) else {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)  # atomic, so a crash never leaves a half-written file


def run(fetchers: dict[str, Callable[[], list[Listing]]], state_path: str,
        send: Callable[[Listing], None] | None, *, min_eur: float, max_eur: float,
        max_alerts: int = 10) -> int:
    """One check of every site; send=None is a dry run. Returns the exit code: non-zero when
    a site or Discord failed, so the Kubernetes Job fails and the KubeJobFailed alert fires."""
    state = load_state(state_path)
    failed = False
    listings: list[Listing] = []
    for site, fetch in fetchers.items():
        try:
            found = fetch()
            if not found:
                raise RuntimeError("no listings parsed - blocked, or the page layout changed")
        except Exception as err:  # one broken site must not hide deals on the other
            print(f"ERROR {site}: {err}", file=sys.stderr)
            failed = True
            continue
        print(f"{site}: {len(found)} listings, {sum(looks_like_3090(i.title) for i in found)} look like a 3090")
        listings += found

    deals = pick_alerts(listings, state, min_eur, max_eur)
    print(f"{len(deals)} new deal(s) at {_eur(min_eur)} - {_eur(max_eur)}")
    for sent, deal in enumerate(deals):
        line = f"{_eur(deal.price_eur)} | {deal.site} | {deal.title} | {deal.url}"
        if send is None:
            print(f"WOULD ALERT {line}")
            continue
        if sent >= max_alerts:
            print(f"{len(deals) - sent} more deal(s) left for the next run")
            break
        try:
            send(deal)
        except Exception as err:
            print(f"ERROR discord: {err}", file=sys.stderr)
            return 1
        state[deal.key] = _sent_record(deal)
        save_state(state_path, state)
        print(f"ALERTED {line}")
    return 1 if failed else 0


def _get(url: str, accept: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": BROWSER_UA, "Accept": accept, "Accept-Language": "bg,en;q=0.8"})
    with urllib.request.urlopen(request, timeout=20, context=SITE_TLS) as response:
        return response.read()


def _olx_url(offset: int) -> str:
    return "https://www.olx.bg/api/v1/offers/?" + urllib.parse.urlencode(
        {"offset": offset, "limit": OLX_PAGE_SIZE, "query": QUERY, "sort_by": "created_at:desc"})


def _bazar_url(page: int) -> str:
    params = {"q": QUERY, "sort": "date", **({"page": page} if page > 1 else {})}
    return "https://bazar.bg/obiavi?" + urllib.parse.urlencode(params)


def fetch_olx(get: Callable[[str, str], bytes] = _get, pause: float = 1.0,
              max_pages: int = 5) -> list[Listing]:
    """Every page of OLX results, newest first: older ads matter too (price drops, first run)."""
    listings: dict[str, Listing] = {}
    for page in range(max_pages):
        if page:
            time.sleep(pause)
        payload = json.loads(get(_olx_url(page * OLX_PAGE_SIZE), "application/json"))
        for listing in parse_olx(payload):
            listings.setdefault(listing.key, listing)
        if not (payload.get("links") or {}).get("next"):
            break
    return list(listings.values())


def fetch_bazar(get: Callable[[str, str], bytes] = _get, pause: float = 1.0,
                max_pages: int = 5) -> list[Listing]:
    """Every page of Bazar results, newest first. A page past the last one answers 404."""
    listings: dict[str, Listing] = {}
    first_page_size = 0
    for page in range(1, max_pages + 1):
        if page > 1:
            time.sleep(pause)
        try:
            html = get(_bazar_url(page), "text/html")
        except urllib.error.HTTPError as err:
            if err.code == 404 and page > 1:
                break
            raise
        found = parse_bazar(html.decode("utf-8", "replace"))
        new = [listing for listing in found if listing.key not in listings]
        listings.update((listing.key, listing) for listing in new)
        first_page_size = first_page_size or len(found)
        if not new or len(found) < first_page_size:
            break  # a short page is the last one
    return list(listings.values())


def main() -> int:
    env = os.environ
    dry_run = env.get("DRY_RUN") == "1"
    webhook = native_webhook(env.get("DISCORD_WEBHOOK_URL", ""))
    if not webhook and not dry_run:
        print("ERROR: set DISCORD_WEBHOOK_URL (or DRY_RUN=1)", file=sys.stderr)
        return 2

    def send(deal: Listing) -> None:
        send_discord(webhook, discord_payload(deal))
        time.sleep(1)  # stay far below Discord's webhook rate limit

    return run({"olx": fetch_olx, "bazar": fetch_bazar},
               env.get("STATE_FILE", "/data/seen.json"),
               None if dry_run else send,
               min_eur=float(env.get("MIN_EUR", "300")),
               max_eur=float(env.get("MAX_EUR", "1000")),
               max_alerts=int(env.get("MAX_ALERTS", "10")))


if __name__ == "__main__":
    sys.exit(main())
