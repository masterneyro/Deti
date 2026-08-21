"""
Тесты проверок индексируемости. Сети не требуют — всё на синтетических данных.

Запуск:
    cd seo-agent && python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from modules import indexability as ix  # noqa: E402

SITE = "https://site.ru"


def robots_of(text: str, status: int = 200) -> ix.RobotsTxt:
    return ix.parse_robots(text, f"{SITE}/robots.txt", status)


def checks(issues) -> set[str]:
    return {i.check for i in issues}


class TestRobotsParsing(unittest.TestCase):
    def test_groups_and_directives(self):
        r = robots_of(
            "User-agent: *\n"
            "Disallow: /admin\n"
            "Allow: /admin/help\n\n"
            "User-agent: Yandex\n"
            "Disallow: /blog\n"
            "Clean-param: utm_source&utm_medium /\n"
            "Crawl-delay: 1.5\n\n"
            "Sitemap: https://site.ru/sitemap.xml\n"
            "Host: site.ru\n"
        )
        self.assertTrue(r.exists)
        self.assertEqual(set(r.groups), {"*", "yandex"})
        self.assertEqual(r.sitemaps, ["https://site.ru/sitemap.xml"])
        self.assertEqual(r.hosts, ["site.ru"])
        self.assertEqual(r.crawl_delays["yandex"], "1.5")
        self.assertEqual(len(r.clean_params), 1)

    def test_two_user_agents_share_one_group(self):
        r = robots_of("User-agent: Yandex\nUser-agent: Googlebot\nDisallow: /secret\n")
        self.assertFalse(r.is_allowed("/secret", "yandex")[0])
        self.assertFalse(r.is_allowed("/secret", "googlebot")[0])

    def test_longest_rule_wins_and_allow_beats_disallow(self):
        r = robots_of("User-agent: *\nDisallow: /catalog\nAllow: /catalog/hits\n")
        self.assertFalse(r.is_allowed("/catalog/all", "*")[0])
        self.assertTrue(r.is_allowed("/catalog/hits/1", "*")[0])

    def test_wildcards(self):
        r = robots_of("User-agent: *\nDisallow: /*?\nDisallow: /*.pdf$\n")
        self.assertFalse(r.is_allowed("/blog?page=2", "*")[0])
        self.assertFalse(r.is_allowed("/files/doc.pdf", "*")[0])
        self.assertTrue(r.is_allowed("/files/doc.pdf.html", "*")[0])

    def test_empty_disallow_allows_everything(self):
        r = robots_of("User-agent: *\nDisallow:\n")
        self.assertTrue(r.is_allowed("/anything", "*")[0])

    def test_yandex_group_overrides_star(self):
        r = robots_of("User-agent: *\nDisallow:\n\nUser-agent: Yandex\nDisallow: /\n")
        self.assertTrue(r.is_allowed("/blog", "googlebot")[0])
        self.assertFalse(r.is_allowed("/blog", "yandex")[0])


class TestRobotsChecks(unittest.TestCase):
    def test_whole_site_closed_is_critical(self):
        issues = ix.check_robots(robots_of("User-agent: *\nDisallow: /\n"), SITE)
        self.assertIn("robots_blocks_whole_site", checks(issues))
        self.assertEqual(
            [i.severity for i in issues if i.check == "robots_blocks_whole_site"], ["critical"])

    def test_important_section_closed(self):
        issues = ix.check_robots(
            robots_of("User-agent: *\nDisallow: /blog\nSitemap: https://site.ru/sitemap.xml\n"),
            SITE)
        self.assertIn("robots_blocks_section", checks(issues))

    def test_section_from_sitemap_paths_closed_is_critical(self):
        issues = ix.check_robots(
            robots_of("User-agent: *\nDisallow: /razvitie\nSitemap: https://site.ru/sitemap.xml\n"),
            SITE, sample_paths=["/razvitie"])
        blocked = [i for i in issues if i.check == "robots_blocks_section"]
        self.assertEqual([i.severity for i in blocked], ["critical"])

    def test_generic_hint_path_closed_is_softer(self):
        # /catalog нет в карте сайта — возможно, такого раздела на сайте просто нет
        issues = ix.check_robots(
            robots_of("User-agent: *\nDisallow: /catalog\nSitemap: https://site.ru/sitemap.xml\n"),
            SITE, sample_paths=["/razvitie"])
        blocked = [i for i in issues if i.check == "robots_blocks_section"]
        self.assertEqual([i.severity for i in blocked], ["high"])

    def test_yandex_stricter_than_star(self):
        issues = ix.check_robots(
            robots_of("User-agent: *\nDisallow:\n\nUser-agent: Yandex\nDisallow: /blog\n"), SITE)
        self.assertIn("robots_yandex_stricter", checks(issues))

    def test_missing_sitemap_directive_and_assets_block(self):
        issues = ix.check_robots(robots_of("User-agent: *\nDisallow: /static/\n"), SITE)
        self.assertIn("robots_no_sitemap_directive", checks(issues))
        self.assertIn("robots_blocks_assets", checks(issues))

    def test_foreign_sitemap_host(self):
        issues = ix.check_robots(
            robots_of("User-agent: *\nDisallow:\nSitemap: https://other.ru/sitemap.xml\n"), SITE)
        self.assertIn("robots_sitemap_foreign_host", checks(issues))

    def test_clean_robots_has_no_issues(self):
        clean = ("User-agent: *\nDisallow: /admin/\nDisallow: /cart/\n"
                 "Sitemap: https://site.ru/sitemap.xml\n")
        self.assertEqual(checks(ix.check_robots(robots_of(clean), SITE)), set())

    def test_missing_robots_file(self):
        issues = ix.check_robots(robots_of("", 404), SITE)
        self.assertIn("robots_missing", checks(issues))


class TestNormalizeUrl(unittest.TestCase):
    def test_variants_collapse(self):
        base = ix.normalize_url("https://site.ru/blog")
        for variant in (
            "http://site.ru/blog",
            "https://www.site.ru/blog/",
            "https://site.ru/blog/index.php",
            "https://site.ru//blog//",
            "https://site.ru/blog?utm_source=vk&utm_medium=cpc",
        ):
            self.assertEqual(ix.normalize_url(variant), base, variant)

    def test_meaningful_query_kept(self):
        self.assertNotEqual(ix.normalize_url("https://site.ru/blog?page=2"),
                            ix.normalize_url("https://site.ru/blog"))

    def test_query_order_ignored(self):
        self.assertEqual(ix.normalize_url("https://site.ru/a?b=1&c=2"),
                         ix.normalize_url("https://site.ru/a?c=2&b=1"))


def facts(url, **kw) -> ix.PageFacts:
    base = dict(status=200, final_url=url, redirects=[])
    base.update(kw)
    return ix.PageFacts(url=url, **base)


class TestCanonical(unittest.TestCase):
    def test_missing_canonical(self):
        issues = ix.check_canonical_facts(facts(f"{SITE}/a"), SITE)
        self.assertIn("canonical_missing", checks(issues))

    def test_self_canonical_is_clean(self):
        page = facts(f"{SITE}/a", canonical=f"{SITE}/a")
        self.assertEqual(checks(ix.check_canonical_facts(page, SITE)), set())

    def test_foreign_domain_canonical(self):
        page = facts(f"{SITE}/a", canonical="https://other.ru/a")
        issues = ix.check_canonical_facts(page, SITE)
        self.assertIn("canonical_foreign_domain", checks(issues))
        self.assertEqual(issues[0].severity, "critical")

    def test_www_and_scheme_mismatch_flagged_softly(self):
        page = facts(f"{SITE}/a", canonical="http://www.site.ru/a/")
        found = checks(ix.check_canonical_facts(page, SITE))
        self.assertIn("canonical_format_mismatch", found)
        self.assertIn("canonical_wrong_scheme", found)

    def test_points_elsewhere_is_critical_for_sitemap_page(self):
        page = facts(f"{SITE}/blog/post", canonical=f"{SITE}/blog")
        issues = ix.check_canonical_facts(page, SITE, {ix.normalize_url(f"{SITE}/blog/post")})
        self.assertEqual(issues[0].check, "canonical_points_elsewhere")
        self.assertEqual(issues[0].severity, "critical")

    def test_canonical_chain_detected(self):
        a = facts(f"{SITE}/a", canonical=f"{SITE}/b")
        b = facts(f"{SITE}/b", canonical=f"{SITE}/c")
        self.assertIn("canonical_chain", checks(ix.check_canonical_targets([a, b], SITE)))

    def test_canonical_to_noindex_page(self):
        a = facts(f"{SITE}/a", canonical=f"{SITE}/b")
        b = facts(f"{SITE}/b", canonical=f"{SITE}/b", meta_robots="noindex, follow")
        self.assertIn("canonical_target_noindex", checks(ix.check_canonical_targets([a, b], SITE)))


class TestNoindex(unittest.TestCase):
    def setUp(self):
        self.open_robots = robots_of("User-agent: *\nDisallow:\n")

    def test_meta_noindex_on_article(self):
        page = facts(f"{SITE}/blog/post", meta_robots="noindex, follow")
        issues = ix.check_noindex_facts(page, self.open_robots)
        self.assertIn("noindex_on_public_page", checks(issues))
        self.assertEqual(issues[0].severity, "critical")

    def test_meta_none_counts_as_noindex(self):
        page = facts(f"{SITE}/blog/post", meta_robots="none")
        self.assertIn("noindex_on_public_page",
                      checks(ix.check_noindex_facts(page, self.open_robots)))

    def test_yandex_meta_tag_is_read(self):
        page = ix.PageFacts(url=f"{SITE}/blog/post", status=200,
                            final_url=f"{SITE}/blog/post", redirects=[],
                            meta_robots="noindex")
        self.assertIn("noindex_on_public_page",
                      checks(ix.check_noindex_facts(page, self.open_robots)))

    def test_x_robots_tag_header(self):
        page = facts(f"{SITE}/blog/post", x_robots_tag="noindex")
        self.assertIn("noindex_http_header",
                      checks(ix.check_noindex_facts(page, self.open_robots)))

    def test_expected_noindex_page_is_silent(self):
        page = facts(f"{SITE}/policy", meta_robots="noindex, nofollow")
        self.assertEqual(
            checks(ix.check_noindex_facts(page, self.open_robots, ["/policy"])), set())

    def test_noindex_plus_disallow_conflict(self):
        blocked = robots_of("User-agent: *\nDisallow: /blog\n")
        page = facts(f"{SITE}/blog/post", meta_robots="noindex")
        self.assertIn("noindex_and_disallow", checks(ix.check_noindex_facts(page, blocked)))

    def test_index_follow_is_clean(self):
        page = facts(f"{SITE}/blog/post", meta_robots="index, follow")
        self.assertEqual(checks(ix.check_noindex_facts(page, self.open_robots)), set())


class TestSitemap(unittest.TestCase):
    def _report(self, urls, found="https://site.ru/sitemap.xml", lastmod="2026-08-01"):
        rep = ix.SitemapReport()
        rep.found_url = found
        rep.checked = [{"url": found, "status": 200, "kind": "urlset",
                        "count": len(urls), "bytes": 1000, "error": None}]
        rep.entries = [ix.SitemapEntry(loc=u, lastmod=lastmod, source=found) for u in urls]
        return rep

    def test_missing_sitemap_is_critical(self):
        rep = ix.SitemapReport()
        rep.checked = [{"url": f"{SITE}/sitemap.xml", "status": 404, "kind": "unavailable",
                        "count": 0, "bytes": 0, "error": None}]
        issues = ix.check_sitemap(rep, robots_of("User-agent: *\nDisallow:\n"), SITE)
        self.assertEqual(issues[0].check, "sitemap_missing")
        self.assertEqual(issues[0].severity, "critical")

    def test_url_blocked_by_robots_is_critical(self):
        rep = self._report([f"{SITE}/blog/post-1"])
        robots = robots_of("User-agent: *\nDisallow: /blog\nSitemap: https://site.ru/sitemap.xml\n")
        issues = ix.check_sitemap(rep, robots, SITE)
        self.assertIn("sitemap_url_blocked_by_robots", checks(issues))

    def test_foreign_and_scheme_and_dupes(self):
        rep = self._report([
            f"{SITE}/a", "https://other.ru/b", "http://site.ru/c", f"{SITE}/a/",
        ])
        robots = robots_of("User-agent: *\nDisallow:\nSitemap: https://site.ru/sitemap.xml\n")
        found = checks(ix.check_sitemap(rep, robots, SITE))
        self.assertIn("sitemap_foreign_urls", found)
        self.assertIn("sitemap_scheme_mismatch", found)
        self.assertIn("sitemap_duplicate_urls", found)

    def test_not_referenced_in_robots(self):
        rep = self._report([f"{SITE}/a"])
        issues = ix.check_sitemap(rep, robots_of("User-agent: *\nDisallow:\n"), SITE)
        self.assertIn("sitemap_not_in_robots", checks(issues))

    def test_future_lastmod(self):
        rep = self._report([f"{SITE}/a"], lastmod="2099-01-01")
        robots = robots_of("User-agent: *\nDisallow:\nSitemap: https://site.ru/sitemap.xml\n")
        issues = ix.check_sitemap(rep, robots, SITE, today="2026-08-20")
        self.assertIn("sitemap_bad_lastmod", checks(issues))

    def test_clean_sitemap_has_no_issues(self):
        rep = self._report([f"{SITE}/a", f"{SITE}/b"])
        robots = robots_of("User-agent: *\nDisallow:\nSitemap: https://site.ru/sitemap.xml\n")
        self.assertEqual(checks(ix.check_sitemap(rep, robots, SITE, today="2026-08-20")), set())


class TestDuplicates(unittest.TestCase):
    def test_url_variants(self):
        pages = [facts(f"{SITE}/blog", final_url=f"{SITE}/blog"),
                 facts(f"{SITE}/blog/", final_url=f"{SITE}/blog/")]
        self.assertIn("duplicate_url_variants", checks(ix.check_duplicate_urls(pages)))

    def test_same_content_without_common_canonical(self):
        a = facts(f"{SITE}/a", text_hash="deadbeef", canonical=f"{SITE}/a")
        b = facts(f"{SITE}/b", text_hash="deadbeef", canonical=f"{SITE}/b")
        issues = ix.check_duplicate_urls([a, b])
        dup = [i for i in issues if i.check == "duplicate_content"]
        self.assertTrue(dup)
        self.assertEqual(dup[0].severity, "high")

    def test_same_content_with_shared_canonical_is_softer(self):
        a = facts(f"{SITE}/a", text_hash="deadbeef", canonical=f"{SITE}/a")
        b = facts(f"{SITE}/b", text_hash="deadbeef", canonical=f"{SITE}/a")
        dup = [i for i in ix.check_duplicate_urls([a, b]) if i.check == "duplicate_content"]
        self.assertEqual(dup[0].severity, "medium")

    def test_duplicate_title_and_description(self):
        a = facts(f"{SITE}/a", title="Развитие ребёнка", description="Одно и то же описание")
        b = facts(f"{SITE}/b", title="Развитие ребёнка", description="Одно и то же описание")
        found = checks(ix.check_duplicate_urls([a, b]))
        self.assertIn("duplicate_title", found)
        self.assertIn("duplicate_description", found)

    def test_unique_pages_are_clean(self):
        a = facts(f"{SITE}/a", title="A", description="Описание А", text_hash="1")
        b = facts(f"{SITE}/b", title="B", description="Описание Б", text_hash="2")
        self.assertEqual(checks(ix.check_duplicate_urls([a, b])), set())


class TestMirrors(unittest.TestCase):
    """check_mirrors ходит в сеть — подменяем ix.fetch картой «адрес → ответ»."""

    HOME_HTML = ("<html><head><title>Главная</title>"
                 "<link rel='canonical' href='https://site.ru/'></head>"
                 "<body><p>" + "текст " * 60 + "</p></body></html>")

    def _run(self, responses: dict) -> set[str]:
        def fake_fetch(url, **kw):
            spec = responses.get(url)
            if spec is None:
                return ix.Fetched(url=url, final_url=url, error="connection refused")
            status, final_url, html = spec
            return ix.Fetched(url=url, status=status, final_url=final_url,
                              redirects=[url] if final_url != url else [],
                              headers={}, text=html or "",
                              content=(html or "").encode())

        saved_fetch, saved_sleep = ix.fetch, ix.time.sleep
        try:
            ix.fetch, ix.time.sleep = fake_fetch, lambda *_: None
            _, issues = ix.check_mirrors(SITE)
        finally:
            ix.fetch, ix.time.sleep = saved_fetch, saved_sleep
        return {(i.check, i.severity, i.url) for i in issues}

    def test_all_variants_redirect_to_home_is_clean(self):
        home = f"{SITE}/"
        responses = {v.format(host="site.ru", scheme="https"): (200, home, self.HOME_HTML)
                     for v, _, _ in ix.MIRROR_VARIANTS}
        self.assertEqual(self._run(responses), set())

    def test_www_serving_200_is_critical(self):
        home = f"{SITE}/"
        html_no_canonical = "<html><head><title>Главная</title></head><body><p>" + "т " * 200 + "</p></body></html>"
        responses = {
            home: (200, home, self.HOME_HTML),
            "https://www.site.ru/": (200, "https://www.site.ru/", html_no_canonical),
        }
        found = self._run(responses)
        self.assertIn(("duplicate_mirror", "critical", "https://www.site.ru/"), found)

    def test_index_php_with_correct_canonical_is_medium(self):
        home = f"{SITE}/"
        responses = {
            home: (200, home, self.HOME_HTML),
            f"{SITE}/index.php": (200, f"{SITE}/index.php", self.HOME_HTML),
        }
        found = self._run(responses)
        self.assertIn(("duplicate_mirror", "medium", f"{SITE}/index.php"), found)

    def test_utm_variant_with_correct_canonical_is_clean(self):
        home = f"{SITE}/"
        utm = f"{SITE}/?utm_source=seo-agent-test"
        responses = {home: (200, home, self.HOME_HTML), utm: (200, utm, self.HOME_HTML)}
        self.assertEqual(self._run(responses), set())

    def test_utm_variant_without_canonical_is_high(self):
        home = f"{SITE}/"
        utm = f"{SITE}/?utm_source=seo-agent-test"
        bare = "<html><head><title>Главная</title></head><body><p>" + "т " * 200 + "</p></body></html>"
        responses = {home: (200, home, self.HOME_HTML), utm: (200, utm, bare)}
        self.assertIn(("duplicate_mirror", "high", utm), self._run(responses))

    def test_index_php_404_is_not_an_issue(self):
        home = f"{SITE}/"
        responses = {home: (200, home, self.HOME_HTML),
                     f"{SITE}/index.php": (404, f"{SITE}/index.php", "<html>404</html>")}
        self.assertEqual(self._run(responses), set())


class TestPageParsing(unittest.TestCase):
    def test_page_facts_reads_html(self):
        html = (
            "<html><head><title>Развитие ребёнка</title>"
            "<meta name='description' content='Описание'>"
            "<link rel='canonical' href='/razvitie/'>"
            "<meta name='robots' content='noindex'></head>"
            "<body><p>" + "текст " * 60 + "</p><script>x=1</script></body></html>"
        )

        class FakeResp(ix.Fetched):
            pass

        saved = ix.fetch
        try:
            ix.fetch = lambda url, **kw: FakeResp(  # type: ignore[assignment]
                url=url, status=200, final_url=url, redirects=[],
                headers={"x-robots-tag": "noarchive"}, text=html, content=html.encode())
            f = ix.page_facts(f"{SITE}/razvitie/")
        finally:
            ix.fetch = saved

        self.assertEqual(f.title, "Развитие ребёнка")
        self.assertEqual(f.canonical, f"{SITE}/razvitie/")
        self.assertEqual(f.meta_robots, "noindex")
        self.assertEqual(f.x_robots_tag, "noarchive")
        self.assertTrue(f.text_hash)


if __name__ == "__main__":
    unittest.main()
