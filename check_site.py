#!/usr/bin/env python3
"""Crawl a whole site and report SEO, GEO and technical defects, comparing pages with each other.

https://github.com/nicolas-datapet/check-site · MIT licence · Standard library only. Read-only: GET requests, one at a time, with a short pause.

Usage:
  python check_site.py https://beta.example.com [options]

Options:
  --auth USER:PASS      HTTP basic auth (protected beta). Or env CHECK_SITE_AUTH.
  --prod                Production checks: noindex and robots "Disallow: /" are errors,
                        http and www variants must answer one 301 to the canonical form.
  --seeds FILE          Extra start URLs: Screaming Frog export (column "Address") or one URL per line.
                        Pages found only there and never linked internally are reported as orphans.
  --arbo FILE           Site tree in Markdown: the first table with an "URL" column and a keyword column
                        ("Keyword" or "Mot-clé principal"): duplicate keywords = cannibalisation.
  --redirections FILE   Redirect plan, CSV separated by ";" with the columns old_url;new_url;code
                        (or ancienne_url;nouvelle_url;code): each line is tested.
  --max-pages N         Crawl limit (default 500).
  --delay S             Pause between requests in seconds (default 0.2).
  --out FILE            Write the Markdown report to FILE (default: stdout).
  --json                JSON report (schema check-site/1, described in the README) instead of Markdown;
                        each finding id starts with the check number (SEO-01 to SEO-43).
  --passive             Only what a search engine robot does: respects the robots.txt Disallow rules
                        for the user-agent in use (the home page, robots.txt and the sitemaps excepted).
  --user-agent UA       User-Agent sent (default: "check_site/1.0 (+site audit, read-only)").

Exit code: 0 no error, 1 at least one error, 2 usage or network failure on the home page.
With --json: 0 whenever the crawl ran, 2 on a fatal error ({"schema": "check-site/1", "fatal": "…"}).
"""
from __future__ import annotations

__version__ = "2.2.0"

import argparse
import base64
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser

UA = "check_site/1.0 (+site audit, read-only)"
SCHEMA = "check-site/1"
GENERIC_HEADINGS = {
    "faq", "questions fréquentes", "foire aux questions", "nos services", "services", "contact",
    "contactez-nous", "à propos", "en savoir plus", "découvrez", "pourquoi nous ?", "pourquoi nous choisir ?",
    "nos atouts", "nos réalisations", "réalisations", "témoignages", "avis", "liens utiles", "suivez-nous",
    "newsletter", "nos offres", "tarifs",
}
GENERIC_ANCHORS = {"cliquez ici", "ici", "en savoir plus", "lire la suite", "voir plus", "plus", "click here",
                   "read more", "learn more", "suite", "découvrir", "voir"}
# Leftover template text. French phrases (« à compléter »…) also occur in real sentences (« formulaire à remplir »):
# they only count between brackets or as the whole text of an element. TODO/FIXME are matched in capitals only.
PLACEHOLDER = re.compile(r"\blorem ipsum\b|\bdolor sit amet\b", re.I)
PLACEHOLDER_CODE = re.compile(r"\b(?:TODO|FIXME)\b|\bXX(?: ?XX){3}\b")
PLACEHOLDER_FR = r"(?:à compléter|à remplir|à définir|à venir|texte provisoire|votre texte ici)"
PLACEHOLDER_BRACKET = re.compile(r"[\[(<{]\s*" + PLACEHOLDER_FR + r"\s*[\])>}]", re.I)
PLACEHOLDER_ALONE = re.compile(r"^\W*" + PLACEHOLDER_FR + r"\W*$", re.I)
EXT_FONTS = re.compile(r"fonts\.(?:googleapis|gstatic)\.com|use\.typekit\.net", re.I)
HEAVY_IMAGE = 200_000   # bytes
STATIC_EXT = re.compile(r"\.(?:css|js|mjs|png|jpe?g|gif|webp|avif|svg|ico|pdf|zip|woff2?|ttf|mp4|webm|xml|txt|json)$", re.I)

# --json: check numbers and finding ids -> (category, stable title)
GRID = {
    "SEO-01": "Erreurs serveur ou réseau", "SEO-02": "Liens internes cassés",
    "SEO-03": "Liens internes via une redirection", "SEO-04": "Chaînes de redirections",
    "SEO-05": "Temps de réponse", "SEO-06": "Page inexistante en 404", "SEO-07": "Pages en noindex",
    "SEO-08": "Balises canonical", "SEO-09": "Beta ouverte aux moteurs", "SEO-10": "robots.txt",
    "SEO-11": "Sitemap XML", "SEO-12": "Balises title", "SEO-13": "Meta descriptions", "SEO-14": "H1",
    "SEO-15": "Hiérarchie des titres", "SEO-16": "Intertitres répétés entre pages",
    "SEO-17": "Intertitres génériques", "SEO-18": "Questions de FAQ répétées", "SEO-19": "Contenu faible",
    "SEO-20": "Arborescence et mots-clés", "SEO-21": "Pages orphelines", "SEO-22": "Textes de lien",
    "SEO-23": "Attribut alt des images", "SEO-24": "Dimensions des images", "SEO-25": "Formats d'image",
    "SEO-26": "Langue de la page", "SEO-27": "Balise viewport", "SEO-28": "Open Graph",
    "SEO-29": "nosnippet et max-snippet", "SEO-30": "Données structurées JSON-LD", "SEO-31": "hreflang",
    "SEO-32": "Formes d'URL (http, www)", "SEO-33": "HSTS", "SEO-34": "Plan de redirections",
    "SEO-35": "Texte de remplissage", "SEO-36": "Polices tierces", "SEO-37": "Fichiers liés",
    "SEO-38": "Poids des images", "SEO-39": "Page 404 personnalisée",
    "SEO-40": "Paramètres d'URL", "SEO-41": "Pages vides en 200", "SEO-42": "Pagination",
    "SEO-43": "Contenus retirés redirigés",
}
FINDINGS = {
    "SEO-01.server-errors": ("seo", "Erreurs serveur ou réseau"),
    "SEO-02.broken-internal-links": ("seo", "Liens internes cassés"),
    "SEO-03.links-via-redirect": ("seo", "Liens internes qui passent par une redirection"),
    "SEO-04.redirect-chains": ("seo", "Chaînes de redirections"),
    "SEO-05.slow-responses": ("performance", "Réponses lentes (> 2 s)"),
    "SEO-06.soft-404": ("seo", "Une page inexistante ne répond pas 404 (soft 404)"),
    "SEO-07.noindex": ("seo", "Pages en noindex"),
    "SEO-08.canonical-issues": ("seo", "Canonicals absentes, relatives ou vers une autre page"),
    "SEO-09.beta-open": ("seo", "Beta accessible sans mot de passe et sans noindex"),
    "SEO-10.robots-missing": ("seo", "robots.txt absent"),
    "SEO-10.robots-no-sitemap": ("seo", "robots.txt ne déclare pas le sitemap"),
    "SEO-10.robots-disallow-all": ("seo", "robots.txt contient Disallow: /"),
    "SEO-10.robots-blocks-assets": ("seo", "robots.txt bloque des CSS ou JS"),
    "SEO-11.sitemap-missing": ("seo", "Sitemap absent ou vide"),
    "SEO-11.sitemap-unreadable": ("seo", "Sitemap illisible ou en erreur"),
    "SEO-11.sitemap-priority-changefreq": ("seo", "Sitemap avec priority ou changefreq (ignorés par Google)"),
    "SEO-11.sitemap-non-200": ("seo", "URLs du sitemap qui ne répondent pas 200 directement"),
    "SEO-11.sitemap-noindex": ("seo", "URLs du sitemap en noindex"),
    "SEO-11.sitemap-non-canonical": ("seo", "URLs du sitemap non canoniques"),
    "SEO-11.sitemap-missing-pages": ("seo", "Pages indexables absentes du sitemap"),
    "SEO-12.title-duplicate": ("seo", "Titles en double"),
    "SEO-12.title-missing": ("seo", "Pages sans title"),
    "SEO-12.title-too-long": ("seo", "Titles de plus de 65 caractères"),
    "SEO-13.meta-description-duplicate": ("seo", "Meta descriptions en double"),
    "SEO-13.meta-description-missing": ("seo", "Pages sans meta description"),
    "SEO-14.h1-duplicate": ("seo", "H1 en double"),
    "SEO-14.h1-count": ("seo", "Pages sans H1 ou avec plusieurs H1"),
    "SEO-15.heading-level-skip": ("accessibilite", "Sauts de niveau de titre"),
    "SEO-16.heading-sitewide": ("seo", "Intertitres présents sur plus de la moitié des pages"),
    "SEO-16.h2-repeated": ("seo", "H2 identiques sur plusieurs pages"),
    "SEO-16.h3-repeated": ("seo", "H3 à H6 identiques sur plusieurs pages"),
    "SEO-17.heading-generic": ("seo", "Intertitres génériques"),
    "SEO-18.faq-repeated": ("seo", "Questions de FAQ répétées entre pages"),
    "SEO-19.thin-content": ("seo", "Pages de moins de 200 mots"),
    "SEO-20.keyword-cannibalisation": ("seo", "Même mot-clé principal pour plusieurs pages"),
    "SEO-20.arbo-page-missing": ("seo", "Pages de l'arborescence introuvables"),
    "SEO-21.orphan-pages": ("seo", "Pages orphelines"),
    "SEO-22.generic-anchors": ("seo", "Textes de lien non descriptifs"),
    "SEO-23.img-alt-missing": ("accessibilite", "Images sans attribut alt"),
    "SEO-24.img-dimensions-missing": ("performance", "Images sans width/height"),
    "SEO-25.img-legacy-format": ("performance", "Images JPEG/PNG sans srcset"),
    "SEO-26.html-lang-missing": ("accessibilite", "Pages sans attribut lang"),
    "SEO-27.viewport-missing": ("seo", "Pages sans balise viewport"),
    "SEO-28.open-graph-incomplete": ("seo", "Open Graph incomplet"),
    "SEO-29.nosnippet": ("seo", "Pages avec nosnippet ou max-snippet restrictif"),
    "SEO-30.jsonld-invalid": ("seo", "JSON-LD invalide"),
    "SEO-30.jsonld-self-rating": ("seo", "Auto-notation dans le JSON-LD"),
    "SEO-31.hreflang-inconsistent": ("seo", "hreflang incohérents"),
    "SEO-32.url-variant-no-redirect": ("seo", "Variante d'URL servie sans redirection (contenu en double)"),
    "SEO-32.url-variant-redirect": ("seo", "Variante d'URL mal redirigée"),
    "SEO-33.hsts-missing": ("securite", "En-tête HSTS absent"),
    "SEO-34.redirection-plan-fail": ("seo", "Redirections du plan non conformes"),
    "SEO-30.jsonld-home-missing": ("seo", "Accueil sans données structurées JSON-LD"),
    "SEO-35.placeholder-text": ("seo", "Texte de remplissage visible"),
    "SEO-36.third-party-fonts": ("performance", "Polices chargées depuis un service tiers"),
    "SEO-37.linked-file-broken": ("seo", "Fichiers liés introuvables"),
    "SEO-37.linked-file-elsewhere": ("seo", "Documents hébergés sur un autre site"),
    "SEO-38.heavy-images": ("performance", "Images de plus de 200 Ko"),
    "SEO-39.raw-404-page": ("seo", "Page 404 brute du serveur"),
    "SEO-12.title-too-short": ("seo", "Titles d'un seul mot"),
    "SEO-12.title-wrong-language": ("seo", "Titles dans une autre langue que la page"),
    "SEO-13.meta-description-wrong-language": ("seo", "Meta descriptions dans une autre langue que la page"),
    "SEO-21.orphan-store-pages": ("seo", "Pages de boutique ou d'établissement orphelines"),
    "SEO-40.param-explosion": ("seo", "Explosion d'URL à paramètres (piège à robots)"),
    "SEO-40.param-reencoded": ("seo", "Paramètres d'URL ré-encodés en boucle"),
    "SEO-40.alternate-to-params": ("seo", "hreflang ou canonical vers des URL à paramètres"),
    "SEO-41.empty-listing": ("seo", "Pages vides servies en 200 (soft 404)"),
    "SEO-42.hreflang-to-page-one": ("seo", "hreflang des pages 2 et suivantes vers la page 1"),
    "SEO-42.page-one-duplicate": ("seo", "URL de page 1 qui duplique la liste"),
    "SEO-43.removed-content-redirect": ("seo", "Contenus retirés redirigés vers une page sans rapport"),
}
SEVERITY = {"erreur": "important", "avertissement": "recommended", "info": "info"}
SEVERITY_RANK = {"critical": 0, "important": 1, "recommended": 2, "info": 3}
URL_RE = re.compile(r"https?://[^\s,;«»<>\"'`()]+")

# SEO-40: query-string variants of one path. Past MAX_PARAM_VARIANTS crawled variants of a path, the others
# are only counted (a faceted filter can produce thousands); PARAM_EXPLOSION variants found = finding.
MAX_PARAM_VARIANTS = 10
PARAM_EXPLOSION = 20
PAGE_PARAMS = {"page", "p", "paged", "pg", "pagenum"}
# A query re-encoded in a loop: "&amp;" left in the URL after HTML decoding, %2526 (= %26 encoded again),
# "amp;" glued to a parameter name, any %25xx (an escape escaped twice)
REENCODED = re.compile(r"&amp;|%26amp%3b|%25[0-9a-f]{2}|(?:[?&]|^)amp;", re.I)
# SEO-41: messages of an empty product or article list (PrestaShop, WooCommerce, WordPress, prestablog…)
EMPTY_LISTING = re.compile(
    r"aucun(?:e)?\s+(?:produit|article|résultat|offre)s?\s+(?:n['’]est\s+)?(?:disponibles?|trouvée?s?|à afficher|"
    r"dans cette catégorie|pour (?:le moment|cette catégorie)|ne correspond)"
    r"|il n['’]y a (?:pas|aucun) (?:de )?produits? dans cette catégorie|cette catégorie (?:est vide|ne contient aucun)"
    r"|no products? (?:were )?(?:found|available|to display|in this category|matched)|there are no products"
    r"|no posts? (?:were )?found", re.I)
# Function words, to tell the language of a title or a description (no dependency). Words shared by two
# languages ("de", "en", "a", "on"…) are left out.
LANG_WORDS = {
    "fr": set("le la les des du une et pour avec nos notre vos votre est sur dans aux au qui que par ou cette ces "
              "ses leur leurs chez sans très tout tous toutes à être".split()),
    "en": set("the and for with our your of to is are from by this that these you we how what online best its at "
              "into about".split()),
    "es": set("el los las del para con nuestro nuestros nuestra y una por más sobre".split()),
    "de": set("der die das und für mit unsere unser ist von zu den dem ein eine auf".split()),
    "it": set("il gli della delle dei per con nostro nostri nostra è di una sono".split()),
}
FR_ACCENTS = re.compile(r"[éèêàçùûôîœ]", re.I)
STORE_TYPES = re.compile(r"Store$|^LocalBusiness$|Shop$")


class Robots:
    """robots.txt rules for one user-agent, Google style: most specific group, longest rule wins
    (Allow on a tie), * and $ wildcards. urllib.robotparser supports neither wildcards nor merging."""

    def __init__(self, body: str, ua: str):
        token = ua.split("/")[0].strip().lower()
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        in_rules = False
        for line in body.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (x.strip() for x in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if in_rules:
                    groups.append((agents, rules))
                    agents, rules, in_rules = [], [], False
                agents.append(value.lower())
            elif key in ("allow", "disallow"):
                in_rules = True
                if value:  # empty Disallow = everything allowed
                    rules.append((key == "allow", value))
        if agents:
            groups.append((agents, rules))
        specific = [r for a, r in groups if any(x != "*" and x and x in token for x in a)]
        chosen = specific or [r for a, r in groups if "*" in a]
        self.rules = [(allow, pattern, self._regex(pattern)) for r in chosen for allow, pattern in r]

    @staticmethod
    def _regex(pattern: str):
        anchored = pattern.endswith("$")
        body = re.escape(pattern.rstrip("$")).replace(r"\*", ".*")
        return re.compile(body + ("$" if anchored else ""))

    def allowed(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        best = None
        for allow, pattern, rx in self.rules:
            if rx.match(path) and (best is None or len(pattern) > best[0] or (len(pattern) == best[0] and allow)):
                best = (len(pattern), allow)
        return best is None or best[1]


# --- Fetching -----------------------------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@dataclass
class Response:
    status: int
    headers: dict
    body: str
    elapsed: float
    error: str = ""


class Fetcher:
    def __init__(self, auth: str | None, delay: float, ua: str = UA):
        self.opener = urllib.request.build_opener(NoRedirect)
        self.headers = {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        if auth:
            self.headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
        self.delay = delay
        self.cache: dict[str, Response] = {}
        self.requests = 0

    def get(self, url: str) -> Response:
        if url in self.cache:
            return self.cache[url]
        self.requests += 1
        time.sleep(self.delay)
        start = time.monotonic()
        # Encode like a browser (spaces, accents in hrefs) without touching existing %xx escapes
        req = urllib.request.Request(urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~"), headers=self.headers)
        try:
            with self.opener.open(req, timeout=30) as r:
                raw = r.read(5_000_000)
                resp = Response(r.status, {k.lower(): v for k, v in r.headers.items()},
                                raw.decode(r.headers.get_content_charset() or "utf-8", "replace"),
                                time.monotonic() - start)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read(2_000_000).decode("utf-8", "replace")
            except Exception:
                pass
            resp = Response(e.code, {k.lower(): v for k, v in e.headers.items()}, body, time.monotonic() - start)
        except Exception as e:  # network error, timeout, TLS
            resp = Response(0, {}, "", time.monotonic() - start, str(e))
        self.cache[url] = resp
        return resp

    def resolve(self, url: str, max_hops: int = 10) -> tuple[Response, list[tuple[int, str]]]:
        """Follow redirects by hand; return final response and the chain [(code, target), …]."""
        chain: list[tuple[int, str]] = []
        current = url
        for _ in range(max_hops):
            r = self.get(current)
            loc = r.headers.get("location")
            if r.status in (301, 302, 303, 307, 308) and loc:
                current = urllib.parse.urljoin(current, loc)
                chain.append((r.status, current))
                continue
            return r, chain
        return Response(0, {}, "", 0, "trop de redirections"), chain


# --- HTML parsing -------------------------------------------------------------

@dataclass
class Page:
    url: str
    status: int = 0
    chain: list = field(default_factory=list)
    elapsed: float = 0.0
    error: str = ""
    content_type: str = ""
    x_robots: str = ""
    title: str = ""
    titles: int = 0
    meta_desc: str | None = None
    robots: str = ""
    canonical: str | None = None
    lang: str = ""
    viewport: bool = False
    placeholders: list = field(default_factory=list)  # leftover template text
    ext_fonts: bool = False                           # fonts loaded from a third party
    og: dict = field(default_factory=dict)
    headings: list = field(default_factory=list)       # [(level, text)]
    links: list = field(default_factory=list)          # [(absolute url, anchor text)]
    images: list = field(default_factory=list)         # [dict attrs]
    jsonld: list = field(default_factory=list)         # raw strings
    hreflang: list = field(default_factory=list)       # [(lang, url)]
    summaries: list = field(default_factory=list)      # <summary> texts (visible FAQ)
    words: int = 0
    empty_msg: str = ""                                # "no products" message of an empty list


class PageParser(HTMLParser):
    def __init__(self, page: Page):
        super().__init__(convert_charrefs=True)
        self.p = page
        self.stack: list[str] = []
        self.capture: str | None = None     # title | h1..h6 | a | script-ld | summary
        self.buf: list[str] = []
        self.href = ""
        self.skip = 0                       # inside script/style/noscript
        self.text: list[str] = []
        self.svg = 0                        # inside an inline <svg> (its <title> is an icon label)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        p = self.p
        if tag == "svg":
            self.svg += 1
        if tag == "html":
            p.lang = a.get("lang", "")
        elif tag == "title" and not self.svg:
            p.titles += 1
            self._start("title")
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name == "description":
                p.meta_desc = a.get("content", "")
            elif name == "robots":
                p.robots = a.get("content", "").lower()
            elif name == "viewport":
                p.viewport = True
            elif name.startswith("og:"):
                p.og[name] = a.get("content", "")
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            if "canonical" in rel:
                p.canonical = a.get("href", "")
            if "alternate" in rel and a.get("hreflang"):
                p.hreflang.append((a["hreflang"], a.get("href", "")))
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._start(tag)
        elif tag == "a" and a.get("href"):
            self.href = a["href"]
            self._start("a")
        elif tag == "img":
            p.images.append(a)
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._start("ld")
        elif tag == "summary":
            self._start("summary")
        if tag in ("script", "style", "noscript", "template") and self.capture != "ld":
            self.skip += 1

    def handle_endtag(self, tag):
        p = self.p
        if tag == "svg":
            self.svg = max(0, self.svg - 1)
        if tag in ("script", "style", "noscript", "template") and self.capture != "ld":
            self.skip = max(0, self.skip - 1)
        if self.capture is None:
            return
        text = " ".join("".join(self.buf).split())
        if self.capture == "title" and tag == "title":
            p.title = text
        elif self.capture in ("h1", "h2", "h3", "h4", "h5", "h6") and tag == self.capture:
            p.headings.append((int(tag[1]), text))
        elif self.capture == "a" and tag == "a":
            p.links.append((self.href, text))
        elif self.capture == "ld" and tag == "script":
            p.jsonld.append("".join(self.buf))
        elif self.capture == "summary" and tag == "summary":
            if text:
                p.summaries.append(text)
        else:
            return
        self.capture = None
        self.buf = []

    def handle_data(self, data):
        if self.capture:
            self.buf.append(data)
        if not self.skip and self.capture != "ld":
            self.text.append(data)

    def _start(self, what):
        # Nested captures (heading inside a link…): keep the outer one, the inner text is collected too
        if self.capture is None:
            self.capture = what
            self.buf = []


# --- Helpers ------------------------------------------------------------------

def norm(url: str) -> str:
    """Drop fragment; keep query (it may be a distinct page)."""
    return urllib.parse.urldefrag(url)[0]


def same_site(url: str, host: str) -> bool:
    return urllib.parse.urlparse(url).hostname == host


def page_number(url: str) -> tuple[int | None, str]:
    """Pagination of a listing URL: (page number, URL of the list without it), (None, url) if not paginated.
    Forms: ?page=N (p, paged…), /page/N/, prestablog -<offset>pN- (blog only)."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    for key, value in query:
        if key.lower() in PAGE_PARAMS and value.isdigit():
            rest = urllib.parse.urlencode([(k, v) for k, v in query if k != key])
            return int(value), urllib.parse.urlunsplit(parts._replace(query=rest))
    m = re.search(r"/page/(\d+)/?$", parts.path)
    if m:
        return int(m.group(1)), urllib.parse.urlunsplit(parts._replace(path=parts.path[:m.start()] + "/"))
    m = re.search(r"-\d+p(\d+)(?=-|$)", parts.path)
    if m and "blog" in parts.path.lower():
        return int(m.group(1)), urllib.parse.urlunsplit(parts._replace(path=parts.path[:m.start()] + parts.path[m.end():]))
    return None, url


def non_page_query(url: str) -> str:
    """Query string of an URL without its pagination parameter ("" = clean URL or plain pagination)."""
    query = urllib.parse.urlsplit(url).query
    return "&".join(x for x in query.split("&") if x and x.split("=")[0].lower() not in PAGE_PARAMS)


def guess_lang(text: str) -> dict[str, int]:
    """Function-word count per language (LANG_WORDS). Capitalised words are skipped: in a title they are
    mostly names ("Le Point Chanvre", "The Fork")."""
    words = [w for w in re.findall(r"[^\W\d_]+", text) if w.islower()]
    return {lang: sum(1 for w in words if w in vocab) for lang, vocab in LANG_WORDS.items()}


def other_language(text: str, lang: str, need: int) -> str | None:
    """Language other than lang that the text is written in, None when unsure (short text, no clue)."""
    if lang not in LANG_WORDS or len(re.findall(r"[^\W\d_]+", text)) < 2:
        return None
    scores = guess_lang(text)
    if lang == "fr" and FR_ACCENTS.search(text):
        scores["fr"] += 1
    best = max((l for l in scores if l != lang), key=lambda l: scores[l])
    return best if scores[lang] == 0 and scores[best] >= need else None


def load_seeds(path: str) -> list[str]:
    text = open(path, encoding="utf-8-sig", errors="replace").read()
    if "address" in text.splitlines()[0].lower():
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = csv.DictReader(text.splitlines(), dialect=dialect)
        key = next(k for k in rows.fieldnames or [] if k.strip().lower() == "address")
        return [r[key].strip() for r in rows if r.get(key, "").strip().startswith("http")]
    return [l.strip() for l in text.splitlines() if l.strip().startswith("http")]


def sitemap_urls(fetch: Fetcher, url: str, depth: int = 0) -> tuple[list[str], list[str]]:
    """Return (urls, notes) from a sitemap or sitemap index."""
    r, _ = fetch.resolve(url)
    if r.status != 200 or not r.body.strip():
        return [], [f"sitemap {url} : code {r.status or r.error}"]
    try:
        root = ET.fromstring(r.body.encode("utf-8"))
    except ET.ParseError as e:
        return [], [f"sitemap {url} illisible : {e}"]
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    notes = []
    if root.find(".//s:priority", ns) is not None or root.find(".//s:changefreq", ns) is not None:
        notes.append("`priority` ou `changefreq` présents : Google les ignore")
    if root.tag.endswith("sitemapindex") and depth < 2:
        urls: list[str] = []
        for loc in root.findall("s:sitemap/s:loc", ns):
            u, n = sitemap_urls(fetch, loc.text.strip(), depth + 1)
            urls += u
            notes += n
        return urls, notes
    return [loc.text.strip() for loc in root.findall("s:url/s:loc", ns) if loc.text], notes


def parse_arbo(path: str) -> list[tuple[str, str]]:
    """Read (url, keyword) from the first Markdown table having URL and keyword columns."""
    rows: list[tuple[str, str]] = []
    cols = None
    for line in open(path, encoding="utf-8"):
        if not line.strip().startswith("|"):
            cols = None if not rows else cols
            continue
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if cols is None:
            low = [c.lower() for c in cells]
            iu = next((i for i, c in enumerate(low) if c == "url"), None)
            ik = next((i for i, c in enumerate(low) if "mot-clé" in c or "mot clé" in c or "keyword" in c), None)
            if iu is not None and ik is not None:
                cols = (iu, ik)
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        if max(cols) < len(cells) and cells[cols[0]] and cells[cols[1]]:
            rows.append((cells[cols[0]], cells[cols[1]]))
    return rows


# --- Report -------------------------------------------------------------------

class Report:
    LEVELS = {"erreur": 0, "avertissement": 1, "info": 2}

    def __init__(self):
        self.items: dict[str, list[tuple[str, str, list[str]]]] = defaultdict(list)
        self.found: list[tuple[str, str | None, str, str, list[str]]] = []   # --json findings

    def add(self, section: str, level: str, message: str, evidence: list[str] | None = None,
            fid: str | None = None, subject: str | None = None):
        """fid: finding id for --json (FINDINGS); None keeps the line out of the JSON findings."""
        self.items[section].append((level, message, evidence or []))
        if fid:
            assert fid in FINDINGS, fid
            self.found.append((fid, subject, level, message, evidence or []))

    def findings(self) -> list[dict]:
        """Findings merged by id + subject, truncated as the contract says."""
        merged: dict[tuple, dict] = {}
        for fid, subject, level, message, evidence in self.found:
            category, title = FINDINGS[fid]
            lines = list(evidence) or ([message] if message != title else [])
            f = merged.get((fid, subject))
            if f is None:
                f = merged[(fid, subject)] = {
                    "id": fid, "grid": fid.split(".")[0], "category": category, "severity": SEVERITY[level],
                    "level_raw": level, "title": title, "subject": subject, "count": 0, "urls": [], "evidence": []}
            elif SEVERITY_RANK[SEVERITY[level]] < SEVERITY_RANK[f["severity"]]:
                f["severity"], f["level_raw"] = SEVERITY[level], level
            f["count"] += len(evidence) or 1
            f["evidence"] += lines
            f["urls"] += [u.rstrip(".:") for line in lines for u in URL_RE.findall(line)]
        out = []
        for f in merged.values():
            f["urls"] = list(dict.fromkeys(f["urls"]))[:50]
            f["evidence"] = [e[:300] for e in f["evidence"][:30]]
            out.append(f)
        return sorted(out, key=lambda f: (SEVERITY_RANK[f["severity"]], f["id"], f["subject"] or ""))

    def count(self, level: str) -> int:
        return sum(1 for v in self.items.values() for l, _, _ in v if l == level)

    def render(self, base: str, pages: int) -> str:
        out = [f"# Contrôle du site {base}", "",
               f"{time.strftime('%d/%m/%Y %H:%M')} · {pages} URLs explorées · "
               f"{self.count('erreur')} erreur(s), {self.count('avertissement')} avertissement(s), "
               f"{self.count('info')} info(s).", "",
               "Contrôles automatiques seulement : la pertinence des intertitres, des réponses et des "
               "preuves se juge à la lecture.", ""]
        for section, items in self.items.items():
            out += [f"## {section}", ""]
            for level, msg, ev in sorted(items, key=lambda i: self.LEVELS[i[0]]):
                out.append(f"- **{level}** : {msg}")
                for e in ev[:15]:
                    out.append(f"  - {e}")
                if len(ev) > 15:
                    out.append(f"  - … et {len(ev) - 15} autre(s)")
            out.append("")
        if not self.items:
            out.append("Aucun défaut détecté par les contrôles automatiques.")
        return "\n".join(out)


# --- Main ---------------------------------------------------------------------

def emit_json(data: dict, out: str | None) -> None:
    """--json: the JSON goes to --out or stdout, nothing else is written on stdout."""
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"Rapport JSON écrit dans {out}", file=sys.stderr)
    else:
        print(text)


class JsonArgumentParser(argparse.ArgumentParser):
    """With --json, a usage error is also reported as a fatal JSON on stdout."""

    def error(self, message):
        if "--json" in sys.argv[1:]:
            print(json.dumps({"schema": SCHEMA, "fatal": message}, ensure_ascii=False))
        super().error(message)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console (cp1252)
    ap = JsonArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base")
    ap.add_argument("--auth", default=os.environ.get("CHECK_SITE_AUTH"))
    ap.add_argument("--prod", action="store_true")
    ap.add_argument("--seeds")
    ap.add_argument("--arbo")
    ap.add_argument("--redirections")
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--delay", type=float, default=0.2)
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--passive", action="store_true")
    ap.add_argument("--user-agent", default=UA)
    args = ap.parse_args()
    started = dt.datetime.now().astimezone()

    base = args.base.rstrip("/") + "/"
    host = urllib.parse.urlparse(base).hostname
    fetch = Fetcher(args.auth, args.delay, args.user_agent)
    rep = Report()

    home, chain = fetch.resolve(base)
    if home.status != 200:
        msg = (f"Accueil inaccessible : {base} → {home.status or home.error}"
               + (" (protégé ? utiliser --auth)" if home.status == 401 else ""))
        if args.json:
            emit_json({"schema": SCHEMA, "fatal": msg}, args.out)
        else:
            print(msg)
        return 2
    if chain:
        base = chain[-1][1]
        host = urllib.parse.urlparse(base).hostname

    # Start URLs: home, sitemap, seeds
    robots = fetch.get(urllib.parse.urljoin(base, "/robots.txt"))
    sm_declared = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots.body) if robots.status == 200 else []
    sm_list = sm_declared or [urllib.parse.urljoin(base, "/sitemap.xml")]
    # --passive: Disallow rules of robots.txt for our user-agent (home, robots.txt and sitemaps excepted)
    robots_all = Robots(robots.body if robots.status == 200 else "", args.user_agent)   # reporting (SEO-40)
    rules = robots_all if args.passive else None
    blocked: set[str] = set()
    plan_ok = False
    sitemap: list[str] = []
    for sm in sm_list:
        u, notes = sitemap_urls(fetch, sm)
        sitemap += [norm(x) for x in u]
        for n in notes:
            bad_sm = "illisible" in n or "code" in n
            rep.add("Sitemap et robots.txt", "avertissement" if bad_sm else "info", n,
                    fid="SEO-11.sitemap-unreadable" if bad_sm else "SEO-11.sitemap-priority-changefreq")
    seeds = [norm(u) for u in load_seeds(args.seeds)] if args.seeds else []

    queue = [norm(base)] + sitemap + seeds
    seen: set[str] = set()
    pages: dict[str, Page] = {}
    incoming: dict[str, set[str]] = defaultdict(set)
    link_targets: dict[str, list[tuple[str, str]]] = defaultdict(list)   # target -> [(from, anchor)]
    listed = set(sitemap) | set(seeds)
    variants_crawled: Counter = Counter()   # path -> query-string variants crawled (SEO-40)
    variants_skipped: set[str] = set()

    def visit(url: str) -> None:
        """Fetch and parse one page, queue its internal links."""
        r, ch = fetch.resolve(url)
        page = Page(url, r.status, ch, r.elapsed, r.error, r.headers.get("content-type", ""),
                    r.headers.get("x-robots-tag", "").lower())
        pages[url] = page
        if r.status != 200 or "html" not in page.content_type:
            return
        final = ch[-1][1] if ch else url
        if ch and not same_site(final, host):
            return
        parser = PageParser(page)
        try:
            parser.feed(r.body)
        except Exception as e:
            page.error = f"HTML illisible : {e}"
        page.words = len(" ".join(parser.text).split())
        text = " ".join(parser.text)
        found = {m.group(0).lower() for m in PLACEHOLDER.finditer(text)}
        found |= {m.group(0) for m in PLACEHOLDER_CODE.finditer(text)}
        found |= {m.group(0).lower() for m in PLACEHOLDER_BRACKET.finditer(text)}
        found |= {t.strip().lower() for t in parser.text if PLACEHOLDER_ALONE.match(t.strip())}
        page.placeholders = sorted(found)
        m = EMPTY_LISTING.search(text)
        if m:
            page.empty_msg = m.group(0)
        page.ext_fonts = bool(EXT_FONTS.search(r.body))
        abs_links, new = [], []
        for href, text in page.links:
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            target = norm(urllib.parse.urljoin(final, href))
            abs_links.append((target, text))
            if same_site(target, host):
                incoming[target].add(url)
                link_targets[target].append((url, text))
                if target not in seen:
                    new.append(target)
        page.links = abs_links
        if url == norm(base):
            queue[:0] = new     # menu and footer pages first: the crawl limit keeps the pages the site puts forward
        else:
            queue.extend(new)

    while queue and len(pages) < args.max_pages:
        url = queue.pop(0)
        if url in seen or not same_site(url, host) or STATIC_EXT.search(urllib.parse.urlparse(url).path):
            continue
        seen.add(url)
        if rules and url != norm(base) and not rules.allowed(url):
            blocked.add(url)
            continue
        # Faceted filters, sorts…: a few variants per path are enough, the rest would eat the crawl budget
        if non_page_query(url) and url not in listed:
            path_key = urllib.parse.urlsplit(url)._replace(query="").geturl()
            if variants_crawled[path_key] >= MAX_PARAM_VARIANTS:
                variants_skipped.add(url)
                continue
            variants_crawled[path_key] += 1
        visit(url)

    # A few pages beyond --max-pages for SEO-40 and SEO-42, which need a parameter variant and a page 2
    # crawled even when the limit was reached on the sitemap: 3 variants of the 3 most varied paths, 5 page 2+.
    def open_url(u: str) -> bool:
        return (u not in seen and same_site(u, host) and not STATIC_EXT.search(urllib.parse.urlparse(u).path)
                and not (rules and not rules.allowed(u)))

    unseen = defaultdict(list)
    for t in link_targets:
        if open_url(t) and non_page_query(t):
            unseen[urllib.parse.urlsplit(t)._replace(query="").geturl()].append(t)
    probes = []
    for path_key, us in sorted(unseen.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:3]:
        # A filter page links to further combinations: crawling 3 of them shows whether the variants multiply
        if len(us) + variants_crawled[path_key] >= MAX_PARAM_VARIANTS and variants_crawled[path_key] < 3:
            probes += sorted(us)[:3 - variants_crawled[path_key]]
    probes += sorted((t for t in link_targets if open_url(t) and (page_number(t)[0] or 0) >= 2),
                     key=lambda t: (page_number(t)[0], t))[:5]
    for url in probes:
        seen.add(url)
        visit(url)

    ok = {u: p for u, p in pages.items() if p.status == 200 and not p.chain and "html" in p.content_type}

    def indexable(p: Page) -> bool:
        return "noindex" not in p.robots and "noindex" not in p.x_robots

    # Language versions of one page (hreflang) form a cluster: same H1 or FAQ across them is expected
    cluster: dict[str, str] = {}
    for u, p in ok.items():
        members = {u} | {norm(urllib.parse.urljoin(u, h)) for _, h in p.hreflang}
        key = min(cluster.get(m, m) for m in members)
        for m in members:
            cluster[m] = key

    def clusters(urls) -> int:
        return len({cluster.get(u, u) for u in urls})

    # --- Status, redirects, speed
    S = "Codes HTTP et redirections"
    errors5 = [f"{u} → {p.status or p.error}" for u, p in pages.items() if p.status >= 500 or p.status == 0]
    if errors5:
        rep.add(S, "erreur", "Erreurs serveur ou réseau", errors5, fid="SEO-01.server-errors")
    broken = []
    via_redirect = []
    for target, sources in link_targets.items():
        p = pages.get(target)
        if not p:
            continue
        where = f"{target} (depuis {sources[0][0]}" + (f" et {len(sources) - 1} autre(s))" if len(sources) > 1 else ")")
        if p.status >= 400 or p.status == 0:
            broken.append(f"{where} → {p.status or p.error}")
        elif p.chain:
            via_redirect.append(f"{where} → {p.chain[-1][1]}")
    if broken:
        rep.add(S, "erreur", "Liens internes cassés", broken, fid="SEO-02.broken-internal-links")
    if via_redirect:
        rep.add(S, "avertissement", "Liens internes qui passent par une redirection (pointer la cible finale)", via_redirect,
                fid="SEO-03.links-via-redirect")
    chains = [f"{u} : " + " → ".join(f"{c} {t}" for c, t in p.chain) for u, p in pages.items() if len(p.chain) > 1]
    if chains:
        rep.add(S, "avertissement", "Chaînes de redirections (une seule 301 attendue)", chains, fid="SEO-04.redirect-chains")
    slow = [f"{u} : {p.elapsed:.1f} s" for u, p in ok.items() if p.elapsed > 2]
    if slow:
        rep.add(S, "avertissement", "Réponses lentes (> 2 s)", slow, fid="SEO-05.slow-responses")
    probe = urllib.parse.urljoin(base if base.endswith("/") else base + "/", "check-site-page-inexistante-404")
    r404, ch404 = fetch.resolve(probe)
    if r404.status != 404 and r404.status != 410:
        rep.add(S, "erreur", f"Une page inexistante répond {r404.status}"
                + (f" après redirection vers {ch404[-1][1]}" if ch404 else "") + " au lieu de 404 (soft 404)",
                fid="SEO-06.soft-404")
    elif "<a " not in r404.body.lower() or re.search(r"<center>\s*nginx|apache/\d|<address>", r404.body, re.I):
        rep.add(S, "avertissement", "Page 404 brute (page du serveur, sans menu ni lien vers l'accueil)", [probe],
                fid="SEO-39.raw-404-page")

    # --- Indexing
    S = "Indexation"
    noidx = [u for u, p in ok.items() if not indexable(p)]
    if noidx:
        rep.add(S, "erreur" if args.prod else "info",
                "Pages en noindex" + (" en production" if args.prod else " (normal sur une beta, à retirer en prod)"), noidx,
                fid="SEO-07.noindex")
    can_issues = []
    for u, p in ok.items():
        if not indexable(p):
            continue
        if p.canonical is None:
            can_issues.append(f"{u} : pas de canonical")
        elif not p.canonical.startswith("https://") and base.startswith("https://"):
            can_issues.append(f"{u} : canonical non absolue ou non https ({p.canonical})")
        elif norm(p.canonical) != u:
            can_issues.append(f"{u} : canonical vers {p.canonical}")
    if can_issues:
        rep.add(S, "avertissement", "Canonicals absentes, relatives ou vers une autre page (à vérifier)", can_issues,
                fid="SEO-08.canonical-issues")
    if not args.auth and not args.prod and not noidx and urllib.parse.urlparse(base).hostname and "beta" in host:
        rep.add(S, "erreur", "Beta accessible sans mot de passe et sans noindex", fid="SEO-09.beta-open")

    # --- Sitemap
    S = "Sitemap et robots.txt"
    if robots.status != 200:
        rep.add(S, "avertissement", f"robots.txt absent (code {robots.status})", fid="SEO-10.robots-missing")
    else:
        if not sm_declared:
            rep.add(S, "avertissement", "robots.txt ne déclare pas le sitemap (ligne `Sitemap:`)", fid="SEO-10.robots-no-sitemap")
        if re.search(r"(?im)^\s*disallow:\s*/\s*$", robots.body):
            rep.add(S, "erreur" if args.prod else "info", "robots.txt contient `Disallow: /` (bloque tout le site pour au moins un robot : vérifier le groupe)",
                    fid="SEO-10.robots-disallow-all")
        if re.search(r"(?im)^\s*disallow:.*\.(css|js)\b", robots.body):
            rep.add(S, "erreur", "robots.txt bloque des CSS ou JS : Google ne peut pas afficher les pages",
                    fid="SEO-10.robots-blocks-assets")
    if not sitemap:
        rep.add(S, "erreur", "Sitemap absent ou vide", fid="SEO-11.sitemap-missing")
    else:
        bad = [f"{u} → {pages[u].status}" + (f" ({pages[u].chain[-1][1]})" if pages[u].chain else "")
               for u in sitemap if u in pages and (pages[u].status != 200 or pages[u].chain)]
        if bad:
            rep.add(S, "erreur", "URLs du sitemap qui ne répondent pas 200 directement", bad, fid="SEO-11.sitemap-non-200")
        nidx = [u for u in sitemap if u in ok and not indexable(ok[u])]
        if nidx:
            rep.add(S, "erreur" if args.prod else "info", "URLs du sitemap en noindex", nidx, fid="SEO-11.sitemap-noindex")
        noncanon = [f"{u} (canonical {ok[u].canonical})" for u in sitemap
                    if u in ok and ok[u].canonical and norm(ok[u].canonical) != u]
        if noncanon:
            rep.add(S, "avertissement", "URLs du sitemap non canoniques", noncanon, fid="SEO-11.sitemap-non-canonical")
        missing = [u for u, p in ok.items() if indexable(p) and u not in set(sitemap)
                   and not (p.canonical and norm(p.canonical) != u)]
        if missing:
            rep.add(S, "avertissement", "Pages indexables absentes du sitemap", missing, fid="SEO-11.sitemap-missing-pages")

    # --- Titles, descriptions, H1
    S = "Titles, descriptions et H1"
    idx = {u: p for u, p in ok.items() if indexable(p)}

    def dupes(values: dict[str, str], label: str, fid: str):
        groups = defaultdict(list)
        for u, v in values.items():
            if v:
                groups[v.strip().lower()].append(u)
        found = [f"« {v} » : " + ", ".join(us) for v, us in groups.items() if clusters(us) > 1]
        if found:
            rep.add(S, "erreur", f"{label} en double", found, fid=fid)

    dupes({u: p.title for u, p in idx.items()}, "Titles", "SEO-12.title-duplicate")
    dupes({u: p.meta_desc or "" for u, p in idx.items()}, "Meta descriptions", "SEO-13.meta-description-duplicate")
    dupes({u: next((t for l, t in p.headings if l == 1), "") for u, p in idx.items()}, "H1", "SEO-14.h1-duplicate")
    no_title = [u for u, p in idx.items() if not p.title]
    if no_title:
        rep.add(S, "erreur", "Pages sans title", no_title, fid="SEO-12.title-missing")
    long_title = [f"{u} ({len(p.title)} car.)" for u, p in idx.items() if len(p.title) > 65]
    if long_title:
        rep.add(S, "info", "Titles de plus de 65 caractères (préférence : ≈ 60, Google tronque ou réécrit)", long_title,
                fid="SEO-12.title-too-long")
    no_desc = [u + (" (balise présente mais vide)" if p.meta_desc is not None else "")
               for u, p in idx.items() if not (p.meta_desc or "").strip()]
    if no_desc:
        rep.add(S, "avertissement", "Pages sans meta description", no_desc, fid="SEO-13.meta-description-missing")
    short_title = [f"{u} : « {p.title} »" for u, p in idx.items()
                   if p.title and len(re.findall(r"[^\W_]+", p.title)) == 1]
    if short_title:
        rep.add(S, "avertissement", "Titles d'un seul mot ou d'une seule lettre (ni sujet ni marque pour Google)",
                short_title, fid="SEO-12.title-too-short")

    def page_lang(u: str, p: Page) -> str:
        own = p.lang or next((l for l, h in p.hreflang if norm(urllib.parse.urljoin(u, h)) == u and l != "x-default"), "")
        return own.split("-")[0].split("_")[0].lower()

    # Site name in the titles ("… - Popets et compagnie"): a part found in many titles is not the page's own text
    title_sep = re.compile(r"\s+[-|–—·:]\s+|\s*\|\s*")
    parts = Counter(x.strip().lower() for p in idx.values() for x in set(title_sep.split(p.title)) if x.strip())
    brand = {x for x, c in parts.items() if c >= 3 and c >= len(idx) / 5}

    def own_title(p: Page) -> str:
        return " - ".join(x for x in title_sep.split(p.title) if x.strip() and x.strip().lower() not in brand)

    for label, getter, need, fid in (("Titles", own_title, 1, "SEO-12.title-wrong-language"),
                                     ("Meta descriptions", lambda p: p.meta_desc or "", 2,
                                      "SEO-13.meta-description-wrong-language")):
        wrong = []
        for u, p in idx.items():
            lang = page_lang(u, p)
            other = other_language(getter(p), lang, need)
            if other:
                full = p.title if fid.startswith("SEO-12") else getter(p)
                wrong.append(f"{u} (page en {lang}) : « {full[:120]} » semble en {other}")
        if wrong:
            rep.add(S, "avertissement", f"{label} dans une autre langue que la page (traduction oubliée ?)", wrong, fid=fid)
    h1_count = [f"{u} : {sum(1 for l, _ in p.headings if l == 1)} H1" for u, p in idx.items()
                if sum(1 for l, _ in p.headings if l == 1) != 1]
    if h1_count:
        rep.add(S, "avertissement", "Pages sans H1 ou avec plusieurs H1 (préférence d'accessibilité, pas une règle Google)", h1_count,
                fid="SEO-14.h1-count")
    skips = []
    for u, p in idx.items():
        prev = 0
        for l, t in p.headings:
            if prev and l > prev + 1:
                skips.append(f"{u} : H{prev} → H{l} « {t[:60]} »")
                break
            prev = l
    if skips:
        rep.add(S, "info", "Sauts de niveau de titre (accessibilité)", skips, fid="SEO-15.heading-level-skip")

    # --- Headings and FAQ across the site
    S = "Intertitres et FAQ (comparaison entre pages)"
    by_heading = defaultdict(set)
    level_of: dict[str, int] = {}
    for u, p in idx.items():
        anchors = {" ".join(t.split()).lower() for _, t in p.links}
        for l, t in p.headings:
            key = t.strip().lower()
            # A heading that is also a link text on the page is a card or teaser title (list of
            # case studies, articles…): repeating it across listing pages is expected
            if l >= 2 and t and not any(key in a for a in anchors):
                by_heading[key].add(u)
                level_of[key] = min(level_of.get(key, l), l)
    n = max(clusters(idx), 1)
    everywhere = [f"« {t} » sur {len(us)} pages" for t, us in by_heading.items()
                  if clusters(us) > n / 2 and clusters(us) > 2]
    if everywhere:
        rep.add(S, "avertissement", "Intertitres présents sur plus de la moitié des pages (en-tête, pied de page ou bloc répété en Hn ?)", everywhere,
                fid="SEO-16.heading-sitewide")
    for lvl_test, level, label, fid in ((lambda l: l == 2, "avertissement", "H2 identiques sur plusieurs pages (sections de gabarit ? dire le sujet de la page)", "SEO-16.h2-repeated"),
                                        (lambda l: l > 2, "info", "H3 à H6 identiques sur plusieurs pages", "SEO-16.h3-repeated")):
        repeated = [f"« {t} » : " + ", ".join(sorted(us)) for t, us in by_heading.items()
                    if lvl_test(level_of[t]) and 1 < clusters(us) <= max(n / 2, 2)]
        if repeated:
            rep.add(S, level, label, repeated, fid=fid)
    generic = sorted({f"« {t} » ({max(len(by_heading[t.strip().lower()]), 1)} page(s))" for p in idx.values()
                      for l, t in p.headings if l >= 2 and t.strip().lower().rstrip(" :") in GENERIC_HEADINGS})
    if generic:
        rep.add(S, "avertissement", "Intertitres génériques (dire le sujet : « Questions fréquentes sur … »)", generic,
                fid="SEO-17.heading-generic")
    questions = defaultdict(set)
    for u, p in idx.items():
        qs = set(p.summaries)
        for raw in p.jsonld:
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            for node in (data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []):
                if isinstance(node, dict) and node.get("@type") == "FAQPage":
                    for q in node.get("mainEntity", []):
                        if isinstance(q, dict) and q.get("name"):
                            qs.add(" ".join(q["name"].split()))
        for q in qs:
            questions[q.lower()].add(u)
    rep_q = [f"« {q} » : " + ", ".join(sorted(us)) for q, us in questions.items() if clusters(us) > 1]
    if rep_q:
        rep.add(S, "erreur", "Questions de FAQ (ou éléments repliables) répétées entre pages", rep_q,
                fid="SEO-18.faq-repeated")

    # --- Thin content
    S = "Contenu"
    thin = [f"{u} : {p.words} mots" for u, p in idx.items() if p.words < 200]
    if thin:
        rep.add(S, "info", "Pages de moins de 200 mots (contenu faible ? à juger)", thin, fid="SEO-19.thin-content")
    if args.arbo:
        arbo = parse_arbo(args.arbo)
        kw = defaultdict(list)
        for u, k in arbo:
            kw[k.strip().lower()].append(u)
        cannib = [f"« {k} » : " + ", ".join(us) for k, us in kw.items() if len(us) > 1]
        if cannib:
            rep.add(S, "erreur", "Même mot-clé principal pour plusieurs pages dans l'arborescence (cannibalisation)", cannib,
                    fid="SEO-20.keyword-cannibalisation")
        missing_arbo = [u for u, _ in arbo if norm(urllib.parse.urljoin(base, u)) not in ok]
        if missing_arbo:
            rep.add(S, "avertissement", "Pages de l'arborescence introuvables en 200 sur le site", missing_arbo,
                    fid="SEO-20.arbo-page-missing")

    # --- Links
    S = "Maillage interne"
    orphans = [u for u in ok if u != norm(base) and not incoming.get(u)]
    if orphans:
        rep.add(S, "avertissement", "Pages orphelines (aucun lien interne vers elles ; trouvées par le sitemap ou les seeds)", orphans,
                fid="SEO-21.orphan-pages")
    # Store / establishment pages (JSON-LD Store, LocalBusiness…) found only through the sitemap: local SEO
    # pages nobody reaches. A type carried by most pages (site-wide block) does not single a page out.
    store_pages: dict[str, set[str]] = {}
    for u, p in idx.items():
        found_types = set()
        for raw in p.jsonld:
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            for node in (data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []):
                t = node.get("@type") if isinstance(node, dict) else None
                found_types |= {str(x) for x in (t if isinstance(t, list) else [t]) if x and STORE_TYPES.search(str(x))}
        if found_types:
            store_pages[u] = found_types
    sitewide = {t for t in set().union(*store_pages.values()) if sum(t in s for s in store_pages.values()) > len(idx) / 2} \
        if store_pages else set()
    store_orphans = [f"{u} ({', '.join(sorted(store_pages[u] - sitewide))})" for u in orphans
                     if u in store_pages and store_pages[u] - sitewide]
    if store_orphans:
        rep.add(S, "avertissement", "Pages de boutique ou d'établissement (JSON-LD Store, LocalBusiness) jamais liées : "
                "seul le sitemap y mène", store_orphans, fid="SEO-21.orphan-store-pages")
    gen_anchor = sorted({f"« {t} » sur {u}" for u, p in idx.items() for _, t in p.links
                         if t.strip().lower().rstrip(" .…>→") in GENERIC_ANCHORS})
    if gen_anchor:
        rep.add(S, "avertissement", "Textes de lien non descriptifs", gen_anchor, fid="SEO-22.generic-anchors")

    # --- Images
    S = "Images"
    alt_pages: dict[str, list[str]] = defaultdict(list)
    dim_pages: dict[str, list[str]] = defaultdict(list)
    old_fmt = set()
    for u, p in idx.items():
        for img in p.images:
            src = img.get("src", "") or img.get("data-src", "")
            if "alt" not in img:
                alt_pages[src].append(u)
            if not (img.get("width") and img.get("height")):
                dim_pages[src].append(u)
            if re.search(r"\.(jpe?g|png)(\?|$)", src, re.I) and not img.get("srcset"):
                old_fmt.add(src)
    def per_image(pages: dict[str, list[str]]) -> list[str]:
        # One line per image (count = distinct images), most widespread first
        return [f"{src} sur {len(us)} page(s), dont {us[0]}" if len(us) > 1 else f"{src} sur {us[0]}"
                for src, us in sorted(pages.items(), key=lambda kv: (-len(kv[1]), kv[0]))]

    no_alt, no_dim = per_image(alt_pages), per_image(dim_pages)
    if no_alt:
        rep.add(S, "erreur", "Images sans attribut alt (alt=\"\" si décorative)", no_alt, fid="SEO-23.img-alt-missing")
    if no_dim:
        rep.add(S, "info", "Images sans width/height (risque de décalage de mise en page)", no_dim,
                fid="SEO-24.img-dimensions-missing")
    if old_fmt:
        rep.add(S, "info", "Images JPEG/PNG sans srcset (WebP ou AVIF ?)", sorted(old_fmt),
                fid="SEO-25.img-legacy-format")

    # --- Technical tags
    S = "Balises techniques"
    for label, test, level, fid in [
        ("Pages sans `lang` sur <html>", lambda p: not p.lang, "erreur", "SEO-26.html-lang-missing"),
        ("Pages sans balise viewport", lambda p: not p.viewport, "erreur", "SEO-27.viewport-missing"),
        ("Pages sans Open Graph complet (og:title, og:description, og:image)",
         lambda p: not all(k in p.og for k in ("og:title", "og:description", "og:image")), "info",
         "SEO-28.open-graph-incomplete"),
        ("Pages avec nosnippet ou max-snippet restrictif (réduit la reprise par Google et ses aperçus IA)",
         lambda p: "nosnippet" in p.robots or re.search(r"max-snippet:\s*(\d+)", p.robots) and
         int(re.search(r"max-snippet:\s*(\d+)", p.robots).group(1)) < 50, "avertissement", "SEO-29.nosnippet"),
    ]:
        hits = [u for u, p in idx.items() if test(p)]
        if hits:
            rep.add(S, level, label, hits, fid=fid)
    bad_ld, self_rating, types = [], [], Counter()
    for u, p in idx.items():
        for raw in p.jsonld:
            try:
                data = json.loads(raw)
            except ValueError as e:
                bad_ld.append(f"{u} : {e}")
                continue
            nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                t = node.get("@type")
                for tt in (t if isinstance(t, list) else [t]):
                    if tt:
                        types[str(tt)] += 1
                if str(t) in ("Organization", "LocalBusiness", "ProfessionalService") or "Business" in str(t):
                    if "aggregateRating" in node or "review" in node:
                        self_rating.append(f"{u} : {t}")
    if bad_ld:
        rep.add(S, "erreur", "JSON-LD invalide", bad_ld, fid="SEO-30.jsonld-invalid")
    if self_rating:
        rep.add(S, "erreur", "Auto-notation : aggregateRating ou review sur l'entreprise du site (non éligible, jugé auto-promotionnel)", self_rating,
                fid="SEO-30.jsonld-self-rating")
    if types:
        rep.add(S, "info", "Types JSON-LD trouvés : " + ", ".join(f"{t} ×{c}" for t, c in types.most_common()))
    # Pages 2+ whose hreflang point to page 1 (SEO-42): reported there, not also as a missing self-reference
    hl_page1, page1_pages = [], set()
    for u, p in idx.items():
        n, _ = page_number(u)
        if n and n >= 2:
            bad = [(l, h) for l, raw in p.hreflang for h in [norm(urllib.parse.urljoin(u, raw))] if (page_number(h)[0] or 1) == 1]
            if bad:
                page1_pages.add(u)
                hl_page1.append(f"{u} (page {n}) : hreflang {bad[0][0]} vers {bad[0][1]}"
                                + (f" (et {len(bad) - 1} autre(s))" if len(bad) > 1 else ""))
    if hl_page1:
        rep.add(S, "avertissement", "hreflang des pages 2 et suivantes vers la page 1 (chaque page de la liste doit "
                "pointer vers la même page dans l'autre langue)", hl_page1, fid="SEO-42.hreflang-to-page-one")
    hl = []
    for u, p in idx.items():
        if p.hreflang and u not in page1_pages:
            urls = {norm(urllib.parse.urljoin(u, h)) for _, h in p.hreflang}
            if u not in urls:
                hl.append(f"{u} : hreflang sans auto-référence")
            for h in urls:
                other = ok.get(h)
                if other is None:
                    # Not crawled (page limit, not linked): check the answer, reciprocity cannot be checked
                    if rules and not rules.allowed(h):
                        continue
                    r_h, ch_h = fetch.resolve(h)
                    if ch_h or r_h.status != 200:
                        got = " → ".join(f"{c} {t}" for c, t in ch_h) + f" → {r_h.status}" if ch_h else str(r_h.status or r_h.error)
                        hl.append(f"{u} : hreflang vers {h} (pas en 200 : {got})")
                elif u not in {norm(urllib.parse.urljoin(h, x)) for _, x in other.hreflang}:
                    hl.append(f"{u} : {h} ne renvoie pas vers elle")
    if hl:
        rep.add(S, "erreur", "hreflang incohérents", hl, fid="SEO-31.hreflang-inconsistent")

    # --- Production: URL forms
    if args.prod:
        S = "Formes d'URL (production)"
        parsed = urllib.parse.urlparse(base)
        bare = parsed.hostname.removeprefix("www.")
        variants = {f"http://{bare}/", f"http://www.{bare}/", f"https://{bare}/", f"https://www.{bare}/"} - {base}
        for v in sorted(variants):
            r, ch = fetch.resolve(v)
            if r.status == 0 and not ch:
                continue  # variant not served at all (no DNS): fine
            if not ch:
                rep.add(S, "erreur", f"{v} répond {r.status} sans rediriger : contenu en double",
                        fid="SEO-32.url-variant-no-redirect", subject=v)
            elif len(ch) > 1 or ch[0][0] not in (301, 308) or norm(ch[-1][1]) != norm(base):
                rep.add(S, "avertissement", f"{v} : " + " → ".join(f"{c} {t}" for c, t in ch) + " (une seule 301 vers la forme canonique attendue)",
                        fid="SEO-32.url-variant-redirect", subject=v)
        if parsed.scheme == "https":
            r = fetch.get(base)
            if "strict-transport-security" not in r.headers:
                rep.add(S, "avertissement", "En-tête HSTS absent", fid="SEO-33.hsts-missing")

    # --- Redirection plan
    if args.redirections:
        S = "Plan de redirections"
        fails = []
        with open(args.redirections, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh, delimiter=";"):
                old = (row.get("old_url") or row.get("ancienne_url") or "").strip()
                if not old:
                    continue
                if rules and not rules.allowed(urllib.parse.urljoin(base, old)):
                    blocked.add(urllib.parse.urljoin(base, old))
                    continue
                want = (row.get("code") or "301").strip()
                new = (row.get("new_url") or row.get("nouvelle_url") or "").strip()
                r, ch = fetch.resolve(urllib.parse.urljoin(base, old))
                if want in ("404", "410"):
                    if ch or str(r.status) not in ("404", "410"):
                        fails.append(f"{old} : attendu {want}, obtenu " + (" → ".join(f"{c} {t}" for c, t in ch) or str(r.status)))
                    continue
                target = norm(urllib.parse.urljoin(base, new))
                if not ch or str(ch[0][0]) != want or norm(ch[-1][1]) != target or len(ch) > 1 or r.status != 200:
                    got = " → ".join(f"{c} {t}" for c, t in ch) + f" → {r.status}" if ch else str(r.status)
                    fails.append(f"{old} : attendu {want} vers {new}, obtenu {got}")
        if fails:
            rep.add(S, "erreur", "Redirections du plan non conformes", fails, fid="SEO-34.redirection-plan-fail")
        else:
            rep.add(S, "info", "Toutes les redirections du plan sont conformes")
            plan_ok = True

    # --- Content and resources: leftover text, third-party fonts, linked files, image weight
    S = "Contenu et ressources"
    hits = [f"{u} : " + ", ".join(p.placeholders) for u, p in ok.items() if p.placeholders]
    if hits:
        rep.add(S, "erreur", "Texte de remplissage visible (à compléter, lorem ipsum, TODO…)", hits,
                fid="SEO-35.placeholder-text")
    hits = [u for u, p in ok.items() if p.ext_fonts]
    if hits:
        rep.add(S, "avertissement", "Polices chargées depuis un service tiers (Google Fonts…) : à auto-héberger (RGPD, performance)",
                hits, fid="SEO-36.third-party-fonts")
    files_broken, files_elsewhere = set(), set()
    for u, p in ok.items():
        for target, _ in p.links:
            path = urllib.parse.urlparse(target).path
            if not STATIC_EXT.search(path):
                continue
            if not same_site(target, host):
                if path.lower().endswith(".pdf"):
                    files_elsewhere.add(target)
                continue
            if rules and not rules.allowed(target):
                blocked.add(target)
                continue
            r, _ = fetch.resolve(target)
            if r.status != 200:
                files_broken.add(f"{target} (depuis {u}) → {r.status or r.error}")
    if files_broken:
        rep.add(S, "erreur", "Fichiers liés introuvables (PDF, documents…)", sorted(files_broken),
                fid="SEO-37.linked-file-broken")
    if files_elsewhere:
        rep.add(S, "avertissement", "Documents hébergés sur un autre site : ils casseront si ce site change (les copier sur le site)",
                sorted(files_elsewhere), fid="SEO-37.linked-file-elsewhere")
    heavy = set()
    for u, p in ok.items():
        for img in p.images:
            src = img.get("src", "") or img.get("data-src", "")
            target = urllib.parse.urljoin(u, src) if src and not src.startswith("data:") else ""
            if not target or not same_site(target, host) or (rules and not rules.allowed(target)):
                continue
            r = fetch.get(target)
            size = int(r.headers.get("content-length") or 0) or len(r.body.encode("utf-8", "ignore"))
            if r.status == 200 and size > HEAVY_IMAGE:
                heavy.add(f"{target} : {size // 1000} Ko")
    if heavy:
        rep.add(S, "avertissement", f"Images de plus de {HEAVY_IMAGE // 1000} Ko (compresser, WebP ou AVIF, srcset ; "
                "si c'est le repli d'un <picture>, vérifier seulement la source WebP)", sorted(heavy), fid="SEO-38.heavy-images")
    home = ok.get(norm(base))
    if home is not None and not home.jsonld:
        rep.add(S, "avertissement", "Accueil sans données structurées JSON-LD (LocalBusiness ou type plus précis, Organization)",
                [norm(base)], fid="SEO-30.jsonld-home-missing")

    # --- URL parameters (robot trap), empty lists, pagination, removed content
    S = "Paramètres d'URL, pagination et contenus retirés"
    sm_set = set(sitemap)
    discovered = set(link_targets) | set(pages) | variants_skipped
    for u, p in ok.items():
        discovered |= {norm(urllib.parse.urljoin(u, h)) for _, h in p.hreflang}
    discovered = {u for u in discovered if same_site(u, host)}
    by_path: dict[str, set[str]] = defaultdict(set)
    param_names: dict[str, Counter] = defaultdict(Counter)
    for u in discovered:
        q = non_page_query(u)
        if q:
            path_key = urllib.parse.urlsplit(u)._replace(query="").geturl()
            by_path[path_key].add(u)
            for x in q.split("&"):
                param_names[path_key][urllib.parse.unquote_plus(x.split("=")[0])] += 1
    explosion_open, explosion_blocked = [], []
    for path_key, us in sorted(by_path.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(us) < PARAM_EXPLOSION:
            continue
        n_blocked = sum(1 for x in us if not robots_all.allowed(x))
        line = (f"{path_key} : {len(us)} variantes à paramètres trouvées (paramètres "
                + ", ".join(k for k, _ in param_names[path_key].most_common(4))
                + f" ; {n_blocked} interdite(s) par robots.txt), ex. {sorted(us)[0]}")
        (explosion_blocked if n_blocked == len(us) else explosion_open).append(line)
    if explosion_open:
        rep.add(S, "avertissement", f"Explosion d'URL à paramètres (filtres, tris…) : au moins {PARAM_EXPLOSION} variantes "
                "d'une même page, explorables par les robots (piège à robots, budget d'exploration gaspillé)",
                explosion_open, fid="SEO-40.param-explosion")
    if explosion_blocked:
        rep.add(S, "info", f"URL à paramètres en nombre ({PARAM_EXPLOSION} variantes ou plus) mais toutes interdites par "
                "robots.txt (Search Console les compte en « bloquées » : retirer les liens si possible)",
                explosion_blocked, fid="SEO-40.param-explosion")
    loops: dict[str, str] = {}
    for u, p in ok.items():
        raws = [t for t, _ in p.links] + [urllib.parse.urljoin(u, h) for _, h in p.hreflang]
        raws += [urllib.parse.urljoin(u, p.canonical)] if p.canonical else []
        for x in raws:
            if same_site(x, host) and x not in loops and REENCODED.search(x.split("://", 1)[-1]):
                loops[x] = u
    if loops:
        rep.add(S, "avertissement", "Paramètres d'URL ré-encodés en boucle (&amp;amp;, %2526…) : chaque passage crée une "
                "nouvelle URL", [f"{x} (depuis {u})" for x, u in sorted(loops.items())], fid="SEO-40.param-reencoded")
    # A parameter URL is fine as its own canonical (?lang=en, ?page_id=12). Wrong: a canonical that sends to
    # another page's parameter URL, or hreflang carrying parameters while the canonical of the page has none.
    alt_params: dict[str, list[str]] = defaultdict(list)
    for u, p in idx.items():
        canon = norm(urllib.parse.urljoin(u, p.canonical)) if p.canonical else u
        bad = [("canonical", canon)] if canon != u and non_page_query(canon) else []
        if not non_page_query(canon):
            bad += [(f"hreflang {l}", h) for l, raw in p.hreflang for h in [urllib.parse.urljoin(u, raw)] if non_page_query(h)]
        if bad:
            alt_params[urllib.parse.urlsplit(u)._replace(query="").geturl()].append(
                f"{u} : {bad[0][0]} vers {bad[0][1]}" + (f" (et {len(bad) - 1} autre(s))" if len(bad) > 1 else ""))
    if alt_params:
        rep.add(S, "avertissement", "hreflang ou canonical vers des URL à paramètres (à réserver aux URL propres et "
                "canoniques)", [lst[0] + (f" ; même défaut sur {len(lst) - 1} autre(s) variante(s)" if len(lst) > 1 else "")
                                for _, lst in sorted(alt_params.items())], fid="SEO-40.alternate-to-params")

    empty_sm = [f"{u} : « {p.empty_msg} »" for u, p in idx.items() if p.empty_msg and not non_page_query(u) and u in sm_set]
    empty_other = [f"{u} : « {p.empty_msg} »" for u, p in idx.items()
                   if p.empty_msg and not non_page_query(u) and u not in sm_set]
    if empty_sm:
        rep.add(S, "erreur", "Pages vides du sitemap servies en 200 (soft 404 : remplir, rediriger vers la page parente "
                "ou répondre 404/410, et sortir du sitemap)", empty_sm, fid="SEO-41.empty-listing")
    if empty_other:
        rep.add(S, "avertissement", "Pages vides servies en 200 (liste sans produit ni article : soft 404)", empty_other,
                fid="SEO-41.empty-listing")

    page_one = {}
    for u in discovered:
        n, list_url = page_number(u)
        if n == 1 and norm(list_url) != u:
            page_one[u] = norm(list_url)
    dup_one = []
    for u, list_url in sorted(page_one.items())[:20]:
        p = ok.get(u)
        if p is None:
            if u in pages or (rules and not rules.allowed(u)):
                continue   # redirected or in error (fine), or not ours to fetch
            r, ch = fetch.resolve(u)
            if ch or r.status != 200 or "html" not in r.headers.get("content-type", ""):
                continue
            p = Page(u, r.status, x_robots=r.headers.get("x-robots-tag", "").lower())
            try:
                PageParser(p).feed(r.body)
            except Exception:
                continue
        if indexable(p) and not (p.canonical and norm(urllib.parse.urljoin(u, p.canonical)) == list_url):
            dup_one.append(f"{u} : répond 200 et duplique {list_url}"
                           + (f" (canonical vers elle-même)" if p.canonical else " (sans canonical)"))
    if dup_one:
        rep.add(S, "avertissement", "URL de page 1 qui duplique la liste (lier la page de liste elle-même, ou rediriger "
                "en 301 vers elle)", dup_one, fid="SEO-42.page-one-duplicate")

    # Content removed and redirected to a generic page: home or language root, a parent page, or one page that
    # collects several unrelated redirects. Google treats them as soft 404; the old ranking is lost.
    lang_root = re.compile(r"^/(?:[a-z]{2}(?:[-_][a-z]{2})?/?)?$", re.I)
    blog_root = re.compile(r"/(?:blog|actualites?|news|articles?|journal|magazine)/?$", re.I)
    auth_page = re.compile(r"login|connexion|authenti|compte|account|panier|cart|checkout|commande", re.I)
    redirected = {u: p for u, p in pages.items() if p.chain and p.status == 200 and (u in sm_set or u in link_targets)}
    collecting = Counter(norm(p.chain[-1][1]) for p in redirected.values())
    removed = []
    for u, p in sorted(redirected.items()):
        final = norm(p.chain[-1][1])
        src, dst = urllib.parse.urlsplit(u).path, urllib.parse.urlsplit(final).path
        if (not same_site(final, host) or src.rstrip("/").lower() == dst.rstrip("/").lower() or lang_root.match(src)
                or auth_page.search(dst)):
            continue
        parent = dst.rstrip("/") and src.lower().startswith(dst.rstrip("/").lower() + "/")
        if lang_root.match(dst) or parent or collecting[final] >= 3 or blog_root.search(dst):
            where = "dans le sitemap" if u in sm_set else f"lié depuis {link_targets[u][0][0]}"
            removed.append(f"{u} → {final} ({p.chain[0][0]}, {where})")
    if removed:
        rep.add(S, "avertissement", "Contenus retirés redirigés vers une page générique (accueil, rubrique) : Google les "
                "traite en soft 404 ; rediriger vers un contenu équivalent ou répondre 410, et retirer les liens",
                removed, fid="SEO-43.removed-content-redirect")

    if variants_skipped:
        rep.add("Exploration", "info", f"{len(variants_skipped)} URL(s) à paramètres non explorée(s) : au-delà de "
                f"{MAX_PARAM_VARIANTS} variantes par page (filtres, tris), elles sont seulement comptées (SEO-40)")
    if len(pages) >= args.max_pages:
        rep.add("Exploration", "avertissement", f"Limite de {args.max_pages} URLs atteinte : exploration incomplète (--max-pages)")
    if blocked:
        rep.add("Exploration", "info", f"{len(blocked)} URL(s) non explorée(s) : interdites par robots.txt pour "
                f"« {args.user_agent} » (--passive)")

    if args.json:
        skipped = {"SEO-20": None if args.arbo else "option --arbo non fournie (arborescence du projet)",
                   "SEO-32": None if args.prod else "option --prod non utilisée",
                   "SEO-33": None if args.prod else "option --prod non utilisée",
                   "SEO-34": None if args.redirections else "option --redirections non fournie (plan de redirections)"}
        findings = rep.findings()
        flagged = {f["grid"] for f in findings}
        details = {"SEO-34": "Toutes les redirections du plan sont conformes"}
        if types:
            details["SEO-30"] = "JSON-LD valide, types trouvés : " + ", ".join(f"{t} ×{c}" for t, c in types.most_common())
        passed = [{"grid": g, "detail": details.get(g, f"{GRID[g]} : aucun problème détecté par ce test")}
                  for g in GRID if g not in flagged and not skipped.get(g) and (g != "SEO-34" or plan_ok)]
        unverified = [{"grid": g, "reason": reason} for g, reason in skipped.items() if reason]
        emit_json({
            "schema": SCHEMA, "tool": "check_site", "version": __version__, "target": base,
            "mode": "passive" if args.passive else "active",
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "stats": {"pages": len(pages), "requests": fetch.requests, "truncated": len(pages) >= args.max_pages,
                      "robots_blocked": len(blocked)},
            "findings": findings, "passed": passed, "unverified": unverified,
        }, args.out)
        return 0

    text = rep.render(base, len(pages))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"Rapport écrit dans {args.out} : {rep.count('erreur')} erreur(s), "
              f"{rep.count('avertissement')} avertissement(s), {len(pages)} URLs.")
    else:
        print(text)
    return 1 if rep.count("erreur") else 0


if __name__ == "__main__":
    sys.exit(main())
