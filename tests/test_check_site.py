"""End-to-end test: crawl a small local site with known defects and check the JSON report.

Run: python -m unittest discover -s tests
"""
from __future__ import annotations

import functools
import http.server
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><meta name="description" content="{desc}">
<link rel="canonical" href="{base}{path}"></head>
<body><h1>{h1}</h1><p>{text}</p>{extra}</body></html>"""


def write_site(root: Path, base: str) -> None:
    """Three pages: same title on two of them, an image without alt, a broken internal link."""
    words = " ".join(["mot"] * 250)
    pages = {
        "/": ("Accueil", "Page d'accueil.", "Accueil", '<a href="/a.html">Page A</a> <a href="/b.html">Page B</a>'),
        "/a.html": ("Titre en double", "Description A.", "Page A", '<img src="/x.png" width="10" height="10">'),
        "/b.html": ("Titre en double", "Description B.", "Page B", '<a href="/manquante.html">Page manquante</a>'),
    }
    for path, (title, desc, h1, extra) in pages.items():
        html = PAGE.format(title=title, desc=desc, base=base, path=path, h1=h1, text=words, extra=extra)
        (root / ("index.html" if path == "/" else path.lstrip("/"))).write_text(html, encoding="utf-8")
    (root / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n", encoding="utf-8")
    urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in pages)
    (root / "sitemap.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        encoding="utf-8")
    (root / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass


class CheckSiteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        handler = functools.partial(QuietHandler, directory=cls.tmp.name)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        write_site(Path(cls.tmp.name), cls.base)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def run_tool(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "check_site.py"), self.base + "/", "--delay", "0", *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def test_json_report_lists_known_defects(self):
        proc = self.run_tool("--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["schema"], "check-site/1")
        ids = {f["id"] for f in report["findings"]}
        self.assertIn("SEO-12.title-duplicate", ids)
        self.assertIn("SEO-23.img-alt-missing", ids)
        self.assertIn("SEO-02.broken-internal-links", ids)

    def test_markdown_report_exits_1_on_errors(self):
        proc = self.run_tool()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("# Contrôle du site", proc.stdout)


if __name__ == "__main__":
    unittest.main()
