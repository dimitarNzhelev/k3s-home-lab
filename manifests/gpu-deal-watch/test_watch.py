"""Unit tests for watch.py (stdlib only, no network).

Run from this directory:  python3 -m unittest -v test_watch
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

import watch
from watch import Listing


def olx_offer(offer_id, title, price=None, currency="EUR", price_type="price", city="София"):
    """One offer shaped like https://www.olx.bg/api/v1/offers/ returns it."""
    params = [{"key": "state", "value": {"key": "used", "label": "Използвано"}}]
    if price is not None:
        params.append({"key": "price", "type": "price", "value": {
            "value": price, "type": price_type, "currency": currency,
            "arranged": price_type == "arranged", "label": f"{price} {currency}"}})
    return {"id": offer_id, "title": title,
            "url": f"https://www.olx.bg/d/ad/x-ID{offer_id}.html",
            "params": params, "location": {"city": {"name": city}}}


def deal(listing_id, price, title="RTX 3090 24GB Gaming OC", site="olx"):
    return Listing(site, str(listing_id), title, price, f"https://example.test/{listing_id}", "")


# Trimmed copy of a real bazar.bg search page: one ad, the same ad repeated, an ad
# with no price, a client-side template, and an ad inside a <script> template.
BAZAR_HTML = """
<div id="dataContainer" data-ads-list>
<div class="listItemContainer listItemContainerV2  ">
    <a title="Asus TUF RTX 3090 OC 24GB" href="https://bazar.bg/obiava-56873010/asus-tuf-rtx-3090-oc-24gb"
    data-id="56873010" data-search-source="text" data-paid="false" data-step=""
    class="listItemLink  ">
        <img class="cover " alt="Asus TUF RTX 3090 OC 24GB" width="241" height="245" src="//cdn5.focus.bg/x.webp" />
        <div class="title">
            <span class="title">
                Asus TUF RTX 3090 OC 24GB
            </span>
            <span class="location">гр. София, Лозенец</span>
            <span class="date">днес</span>
            <span class="price">1 350 <span class="currency">€</span> </span>
        </div>
    </a>
    <div class="fav  tmb" title="Добави в Любими" data-ad-id="56873010" data-id="56873010"></div>
</div>
<div class="listItemContainer listItemContainerV2  ">
    <a title="Asus TUF RTX 3090 OC 24GB" href="https://bazar.bg/obiava-56873010/asus-tuf-rtx-3090-oc-24gb"
    data-id="56873010" data-search-source="text" class="listItemLink  ">
        <div class="title"><span class="price">1 350 <span class="currency">€</span></span></div>
    </a>
</div>
<div class="listItemContainer listItemContainerV2  ">
    <a title="RTX 3090 Founders &amp; box" href="/obiava-56871873/rtx-3090-founders"
    data-id="56871873" data-search-source="text" class="listItemLink  ">
        <div class="title">
            <span class="title">RTX 3090 Founders &amp; box</span>
            <span class="location">гр. Пловдив</span>
            <span class="price">Договаряне</span>
        </div>
    </a>
</div>
<div class="listItemContainer listItemContainerV2 {{#if ad_already_seen}}seen{{/if}}">
    <a title="{{title}}" href="{{url}}" data-id="{{id}}" class="listItemLink {{#if is_promo}}listTop{{/if}} ">
        <span class="price">{{price}}</span>
    </a>
</div>
<script type="text/x-handlebars-template">
    <a title="RTX 3090 in a template" href="/obiava-1/x" data-id="1" class="listItemLink">
        <span class="price">1 €</span>
    </a>
</script>
</div>
"""


class LooksLike3090Test(unittest.TestCase):
    def test_accepts_3090_cards(self):
        for title in ["1г Гаранция! RTX 3090 24GB Gainward Phoenix",
                      "NVIDIA GeForce RTX 3090 iCHILL X3 24GB",
                      "Asus TUF 3090 OC",
                      "rtx3090 strix",
                      "RTX 3090 Ti Founders Edition",
                      "RTX 3090 - перфектна, не търся бартери"]:
            with self.subTest(title=title):
                self.assertTrue(watch.looks_like_3090(title))

    def test_accepts_real_titles_seen_on_2026_11_09(self):
        for title in ["Rtx 3090 Asus TUF",
                      "Видео карта nVidia RTX 3090 24GB Palit GamingPro",
                      "INNO3D GeForce RTX3090 iCHILL X4,24GB",
                      "Геймърски PC RTX 3090 FE, 128GB RAM, 7TB, водно охлаждане EK"]:
            with self.subTest(title=title):
                self.assertTrue(watch.looks_like_3090(title))

    def test_rejects_non_gpu_products_with_3090_in_the_model_name(self):
        for title in ["Aiwa AF-3090",
                      "Глава с електродвигател комплект за компресор, модел JN-3090",
                      "Kenwood KR-3090 ресивър.",
                      "3090 UV лампа за маникюр и педикюр",
                      "Мобилен терминал с баркод четец Motorola Mc 3090",
                      "Нова раница за DSLR фотоапарат, обективи и лаптоп и дрон CAREELL C3090",
                      "259600-3090 моторче задна чистачкa SUZUKI BALENO 2016-2022"]:
            with self.subTest(title=title):
                self.assertFalse(watch.looks_like_3090(title))

    def test_rejects_other_cards_wanted_ads_broken_cards_and_desktops(self):
        for title in ["Gigabyte RTX 3070 Vision",
                      "RTX 30900",
                      "Купувам RTX 3090",
                      "ТЪРСЯ видеокарта 3090",
                      "RTX 3090 за части",
                      "RTX 3090 - не работи",
                      "Неработеща RTX 3090",
                      "Повредена 3090",
                      "Dell OptiPlex 3090 Micro"]:
            with self.subTest(title=title):
                self.assertFalse(watch.looks_like_3090(title))


class ParsePriceTest(unittest.TestCase):
    def test_euro_prices(self):
        cases = {"80 €": 80.0, "1 400 €": 1400.0, "1 400 €": 1400.0, "1.400 €": 1400.0,
                 "999,99 €": 999.99, "1 350 € ": 1350.0, "950 EUR": 950.0}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(watch.parse_price(text), expected)

    def test_leva_are_converted_at_the_fixed_rate(self):
        self.assertAlmostEqual(watch.parse_price("1955,83 лв."), 1000.0, places=2)

    def test_no_price(self):
        for text in ["Договаряне", "", "Подарявам", "1400"]:
            with self.subTest(text=text):
                self.assertIsNone(watch.parse_price(text))


class ParseOlxTest(unittest.TestCase):
    def test_reads_title_price_url_and_city(self):
        got = watch.parse_olx({"data": [olx_offer(149568548, "RTX 3090 24GB Gainward Phoenix", 1400)]})
        self.assertEqual(got, [Listing("olx", "149568548", "RTX 3090 24GB Gainward Phoenix", 1400.0,
                                       "https://www.olx.bg/d/ad/x-ID149568548.html", "София")])

    def test_converts_leva(self):
        got = watch.parse_olx({"data": [olx_offer(1, "RTX 3090", 1700, currency="BGN")]})
        self.assertAlmostEqual(got[0].price_eur, 1700 / 1.95583, places=2)

    def test_no_price_when_missing_or_negotiable(self):
        got = watch.parse_olx({"data": [olx_offer(1, "RTX 3090"),
                                        olx_offer(2, "RTX 3090", 0, price_type="arranged")]})
        self.assertEqual([listing.price_eur for listing in got], [None, None])

    def test_drops_repeated_promoted_offers(self):
        offer = olx_offer(7, "RTX 3090", 900)
        self.assertEqual(len(watch.parse_olx({"data": [offer, offer]})), 1)


class ParseBazarTest(unittest.TestCase):
    def test_reads_listings_and_skips_duplicates_and_templates(self):
        self.assertEqual(watch.parse_bazar(BAZAR_HTML), [
            Listing("bazar", "56873010", "Asus TUF RTX 3090 OC 24GB", 1350.0,
                    "https://bazar.bg/obiava-56873010/asus-tuf-rtx-3090-oc-24gb", "гр. София, Лозенец"),
            Listing("bazar", "56871873", "RTX 3090 Founders & box", None,
                    "https://bazar.bg/obiava-56871873/rtx-3090-founders", "гр. Пловдив"),
        ])


class FakeGet:
    """Stands in for the HTTP GET: hands out the given responses in order, records URLs."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url, accept):
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def olx_page(ids, more):
    links = {"self": {"href": "x"}, **({"next": {"href": "y"}} if more else {})}
    return json.dumps({"data": [olx_offer(i, f"RTX 3090 #{i}", 900) for i in ids],
                       "links": links}).encode()


def bazar_page(*ids):
    return "".join(f'<a class="listItemLink" data-id="{i}" href="/obiava-{i}/x" title="RTX 3090 #{i}">'
                   f'<span class="price">900 €</span></a>' for i in ids).encode()


def not_found():
    return urllib.error.HTTPError("https://bazar.bg/obiavi", 404, "Not Found", None, None)


class SiteTlsTest(unittest.TestCase):
    def test_site_requests_use_the_x25519_only_tls_context(self):
        # OLX's CloudFront firewall 403s OpenSSL 3.5's default handshake (seen 2026-11-09).
        seen = {}

        def fake_urlopen(request, timeout=None, context=None):
            seen["context"] = context
            return io.BytesIO(b"{}")

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            watch._get("https://www.olx.bg/api/v1/offers/", "application/json")
        self.assertIs(seen["context"], watch.SITE_TLS)


class FetchOlxTest(unittest.TestCase):
    def test_follows_next_links_and_merges_pages(self):
        get = FakeGet(olx_page([1, 2], more=True), olx_page([2, 3], more=True), olx_page([4], more=False))
        self.assertEqual([l.id for l in watch.fetch_olx(get=get, pause=0)], ["1", "2", "3", "4"])
        self.assertEqual(len(get.urls), 3)
        self.assertIn("offset=50", get.urls[1])

    def test_stops_after_max_pages(self):
        get = FakeGet(*[olx_page([i], more=True) for i in range(10)])
        watch.fetch_olx(get=get, pause=0, max_pages=3)
        self.assertEqual(len(get.urls), 3)


class FetchBazarTest(unittest.TestCase):
    def test_reads_pages_until_a_shorter_page(self):
        get = FakeGet(bazar_page(1, 2, 3), bazar_page(4))
        self.assertEqual([l.id for l in watch.fetch_bazar(get=get, pause=0)], ["1", "2", "3", "4"])
        self.assertIn("sort=date", get.urls[0])
        self.assertNotIn("page=", get.urls[0])
        self.assertIn("page=2", get.urls[1])

    def test_404_after_the_last_page_ends_paging(self):
        get = FakeGet(bazar_page(1, 2), bazar_page(3, 4), not_found())
        self.assertEqual([l.id for l in watch.fetch_bazar(get=get, pause=0)], ["1", "2", "3", "4"])

    def test_404_on_the_first_page_is_an_error(self):
        with self.assertRaises(urllib.error.HTTPError):
            watch.fetch_bazar(get=FakeGet(not_found()), pause=0)


class PickAlertsTest(unittest.TestCase):
    def pick(self, listings, state=None):
        return [d.id for d in watch.pick_alerts(listings, state or {}, min_eur=300, max_eur=1000)]

    def test_3090s_between_floor_and_ceiling_inclusive_cheapest_first(self):
        listings = [deal(1, 950), deal(2, 1000), deal(3, 300), deal(4, 1000.5), deal(5, 80),
                    deal(6, None), deal(7, 500, title="RTX 3080 10GB")]
        self.assertEqual(self.pick(listings), ["3", "1", "2"])

    def test_already_alerted_only_again_when_the_price_drops(self):
        state = {"olx:1": 950.0, "olx:2": 950.0}
        self.assertEqual(self.pick([deal(1, 950), deal(2, 900)], state), ["2"])


class DiscordTest(unittest.TestCase):
    def test_payload_has_price_site_place_title_link_and_never_pings(self):
        payload = watch.discord_payload(Listing("bazar", "5", "@everyone RTX 3090", 1350.0,
                                                "https://bazar.bg/obiava-5/x", "гр. София"))
        self.assertEqual(payload["username"], "GPU deal watch")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        for part in ["1 350 €", "Bazar", "гр. София", "@everyone RTX 3090", "https://bazar.bg/obiava-5/x"]:
            with self.subTest(part=part):
                self.assertIn(part, payload["content"])

    def test_payload_keeps_cents_of_converted_prices(self):
        self.assertIn("715.81 €", watch.discord_payload(deal(1, 715.81))["content"])

    def test_native_webhook_strips_the_slack_suffix(self):
        url = "https://discord.com/api/webhooks/1/abc"
        self.assertEqual(watch.native_webhook(url + "/slack"), url)
        self.assertEqual(watch.native_webhook(url), url)
        self.assertEqual(watch.native_webhook(""), "")


class StateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "seen.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_is_empty(self):
        self.assertEqual(watch.load_state(self.path), {})

    def test_round_trip(self):
        watch.save_state(self.path, {"olx:1": 950.0})
        self.assertEqual(watch.load_state(self.path), {"olx:1": 950.0})

    def test_corrupt_file_starts_fresh(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(watch.load_state(self.path), {})


class RunTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.dir.name, "seen.json")
        self.sent = []

    def tearDown(self):
        self.dir.cleanup()

    def run_once(self, fetchers, send="record", max_alerts=10):
        sender = self.sent.append if send == "record" else send
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = watch.run(fetchers, self.state, sender, min_eur=300, max_eur=1000, max_alerts=max_alerts)
        return code, out.getvalue()

    def test_alerts_once_per_deal(self):
        fetchers = {"olx": lambda: [deal(1, 950)]}
        self.assertEqual(self.run_once(fetchers)[0], 0)
        self.assertEqual(self.run_once(fetchers)[0], 0)
        self.assertEqual([d.id for d in self.sent], ["1"])

    def test_broken_site_fails_the_run_but_other_sites_still_alert(self):
        def blocked():
            raise OSError("HTTP Error 403: Forbidden")
        code, log = self.run_once({"olx": blocked, "bazar": lambda: [deal(2, 900, site="bazar")]})
        self.assertEqual(code, 1)
        self.assertEqual([d.key for d in self.sent], ["bazar:2"])
        self.assertIn("403", log)

    def test_site_with_no_listings_fails_the_run(self):
        self.assertEqual(self.run_once({"olx": lambda: []})[0], 1)

    def test_dry_run_sends_nothing_and_remembers_nothing(self):
        code, log = self.run_once({"olx": lambda: [deal(1, 950)]}, send=None)
        self.assertEqual(code, 0)
        self.assertIn("WOULD ALERT", log)
        self.assertFalse(os.path.exists(self.state))

    def test_caps_alerts_per_run_and_sends_the_rest_next_run(self):
        fetchers = {"olx": lambda: [deal(1, 900), deal(2, 800), deal(3, 700)]}
        self.run_once(fetchers, max_alerts=2)
        self.assertEqual([d.id for d in self.sent], ["3", "2"])
        self.run_once(fetchers, max_alerts=2)
        self.assertEqual([d.id for d in self.sent], ["3", "2", "1"])

    def test_failed_send_fails_the_run_and_retries_next_run(self):
        def discord_down(_listing):
            raise OSError("discord down")
        self.assertEqual(self.run_once({"olx": lambda: [deal(1, 950)]}, send=discord_down)[0], 1)
        self.assertEqual(watch.load_state(self.state), {})


if __name__ == "__main__":
    unittest.main()
