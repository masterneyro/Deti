"""
Проверка индексируемости сайта: robots.txt, sitemap.xml, canonical, noindex, дубли URL.

Пять блоков — ровно те вопросы, которые задают при приёмке сайта:

  1. robots.txt   — существует ли, не закрыты ли важные разделы (для Яндекса отдельно).
  2. sitemap.xml  — существует ли карта, валидна ли, отправлена ли в Яндекс.Вебмастер.
  3. canonical    — не указывают ли страницы на чужой/битый/неверный URL.
  4. noindex      — нет ли случайного запрета индексации (meta robots и X-Robots-Tag).
  5. дубли URL    — зеркала (http/https, www), слеш, index.php, utm, повторы в sitemap.

Модуль ничего не отправляет и ничего не чинит — только собирает факты и Issue.
Запуск: seo-agent/scripts/audit_indexability.py (CLI) или orchestrator.py indexability.

ENV (опционально, только для блока «отправлена ли карта в Яндекс»):
    YANDEX_WEBMASTER_TOKEN
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; seo-agent/1.0; +https://github.com/)"
TIMEOUT = 30
DELAY = 0.25

# Параметры-метки, которые не меняют контент: страница с ними — дубль.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "utm_referrer", "yclid", "gclid", "fbclid", "ysclid", "_openstat",
    "from", "ref", "referrer", "roistat", "gbraid", "wbraid", "msclkid",
}

# Разделы, закрытие которых в robots.txt почти всегда — ошибка.
IMPORTANT_PATH_HINTS = (
    "/blog", "/stati", "/articles", "/news", "/novosti", "/catalog", "/katalog",
    "/uslugi", "/services", "/kursy", "/programmy", "/product", "/tovar",
)

# Ресурсы, без которых робот не отрисует страницу (Google/Яндекс это ругают).
RENDER_RESOURCE_HINTS = (".css", ".js", "/static", "/assets", "/_next", "/media", "/upload")


# ───── Issue (совместим с modules/audit_checks.Issue) ────────────────

@dataclass
class Issue:
    url: str
    check: str
    severity: str
    detail: str

    def key(self) -> tuple[str, str, str]:
        return (self.url, self.check, self.detail)

    def to_dict(self) -> dict:
        return {"url": self.url, "check": self.check,
                "severity": self.severity, "detail": self.detail}


# ───── HTTP ──────────────────────────────────────────────────────────

@dataclass
class Fetched:
    url: str
    status: int = 0
    final_url: str = ""
    redirects: list[str] = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    text: str = ""
    content: bytes = b""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def fetch(url: str, *, allow_redirects: bool = True, ua: str = USER_AGENT,
          timeout: int = TIMEOUT) -> Fetched:
    """GET без исключений: сетевую ошибку кладём в .error, а не роняем аудит."""
    try:
        r = requests.get(
            url, timeout=timeout, allow_redirects=allow_redirects,
            headers={"User-Agent": ua, "Accept-Language": "ru,en;q=0.5"},
        )
        text = ""
        ctype = r.headers.get("content-type", "")
        if any(t in ctype for t in ("text/", "xml", "json")) or not ctype:
            text = r.text
        return Fetched(
            url=url, status=r.status_code, final_url=r.url,
            redirects=[h.url for h in r.history],
            headers={k.lower(): v for k, v in r.headers.items()},
            text=text, content=r.content,
        )
    except requests.RequestException as e:
        return Fetched(url=url, final_url=url, error=str(e))


# ───── URL-нормализация ──────────────────────────────────────────────

def normalize_url(url: str, *, drop_tracking: bool = True,
                  ignore_scheme: bool = True, ignore_www: bool = True,
                  ignore_trailing_slash: bool = True,
                  ignore_index: bool = True,
                  collapse_slashes: bool = True) -> str:
    """Свести URL к «содержательному» ключу, чтобы поймать дубли.

    https://www.site.ru/blog/?utm_source=vk  →  site.ru/blog
    """
    p = urlparse(url)
    host = (p.netloc or "").lower()
    if ignore_www and host.startswith("www."):
        host = host[4:]
    path = p.path or "/"
    if ignore_index:
        path = re.sub(r"/(index|default)\.(html?|php|aspx?)$", "/", path, flags=re.I)
    if collapse_slashes:
        path = re.sub(r"/{2,}", "/", path)
    if ignore_trailing_slash and len(path) > 1:
        path = path.rstrip("/") or "/"
    query = ""
    if p.query:
        pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not (drop_tracking and k.lower() in TRACKING_PARAMS)]
        query = urlencode(sorted(pairs))
    scheme = "" if ignore_scheme else f"{p.scheme}://"
    return f"{scheme}{host}{path}" + (f"?{query}" if query else "")


def same_normalized(a: str, b: str) -> bool:
    return normalize_url(a) == normalize_url(b)


def strip_query(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def registrable_host(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    return host[4:] if host.startswith("www.") else host


# ───── 1. robots.txt ─────────────────────────────────────────────────

def _pattern_to_regex(pattern: str) -> re.Pattern:
    """robots-паттерн → regex. Поддержаны * и $ (Google + Яндекс)."""
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    parts = [re.escape(chunk) for chunk in body.split("*")]
    rx = ".*".join(parts)
    return re.compile("^" + rx + ("$" if anchored_end else ""))


@dataclass
class RobotsRule:
    kind: str          # "allow" | "disallow"
    pattern: str
    line_no: int


@dataclass
class RobotsTxt:
    url: str
    status: int
    exists: bool
    raw: str = ""
    groups: dict[str, list[RobotsRule]] = field(default_factory=dict)
    sitemaps: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    clean_params: list[str] = field(default_factory=list)
    crawl_delays: dict[str, str] = field(default_factory=dict)
    unknown: list[tuple[int, str]] = field(default_factory=list)

    def group_for(self, ua: str) -> tuple[str, list[RobotsRule]]:
        """Какая секция robots.txt применяется к боту. Точнее — длиннее совпадение."""
        ua_l = ua.lower()
        best_name, best_len = None, -1
        for name in self.groups:
            if name == "*":
                continue
            if ua_l.startswith(name) or name.startswith(ua_l):
                if len(name) > best_len:
                    best_name, best_len = name, len(name)
        if best_name:
            return best_name, self.groups[best_name]
        return "*", self.groups.get("*", [])

    def is_allowed(self, path: str, ua: str = "yandex") -> tuple[bool, Optional[RobotsRule]]:
        """Разрешён ли путь. Правило самое длинное; при равной длине выигрывает Allow."""
        _, rules = self.group_for(ua)
        winner: Optional[RobotsRule] = None
        winner_len = -1
        for rule in rules:
            if not rule.pattern:
                # «Disallow:» без значения = разрешено всё, правилом не считается
                continue
            if _pattern_to_regex(rule.pattern).match(path):
                plen = len(rule.pattern)
                if plen > winner_len or (plen == winner_len and rule.kind == "allow"):
                    winner, winner_len = rule, plen
        if winner is None:
            return True, None
        return (winner.kind == "allow"), winner


def parse_robots(text: str, url: str, status: int) -> RobotsTxt:
    robots = RobotsTxt(url=url, status=status, exists=200 <= status < 300, raw=text)
    current: list[str] = []
    expecting_new_group = True

    for line_no, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            robots.unknown.append((line_no, raw_line.strip()))
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "user-agent":
            if not expecting_new_group:
                current = []
                expecting_new_group = True
            ua = value.lower()
            current.append(ua)
            robots.groups.setdefault(ua, [])
        elif field_name in ("allow", "disallow"):
            expecting_new_group = False
            if not current:
                current = ["*"]
                robots.groups.setdefault("*", [])
            for ua in current:
                robots.groups[ua].append(RobotsRule(field_name, value, line_no))
        elif field_name == "sitemap":
            robots.sitemaps.append(value)
        elif field_name == "host":
            robots.hosts.append(value)
        elif field_name == "clean-param":
            robots.clean_params.append(value)
        elif field_name == "crawl-delay":
            expecting_new_group = False
            for ua in (current or ["*"]):
                robots.crawl_delays[ua] = value
        else:
            robots.unknown.append((line_no, raw_line.strip()))

    return robots


def load_robots(site_root: str) -> RobotsTxt:
    url = site_root.rstrip("/") + "/robots.txt"
    r = fetch(url)
    if r.error:
        return RobotsTxt(url=url, status=0, exists=False, raw="")
    ctype = r.headers.get("content-type", "")
    if r.ok and "html" in ctype:
        # Сервер отдал 200 + HTML-страницу вместо robots.txt — это «мягкая 404».
        return RobotsTxt(url=url, status=r.status, exists=False, raw=r.text[:2000])
    return parse_robots(r.text if r.ok else "", url, r.status)


def check_robots(robots: RobotsTxt, site_root: str,
                 sample_paths: Iterable[str] = ()) -> list[Issue]:
    """Разбор robots.txt: блокировки важных разделов, зеркала, sitemap-директива."""
    issues: list[Issue] = []
    url = robots.url

    if not robots.exists:
        if robots.status == 0:
            issues.append(Issue(url, "robots_unreachable", "high",
                                "robots.txt не открывается (сетевая ошибка или таймаут)"))
        elif 200 <= robots.status < 300:
            issues.append(Issue(url, "robots_not_plain_text", "high",
                                "по /robots.txt отдаётся HTML-страница, а не текстовый файл"))
        elif robots.status == 404:
            issues.append(Issue(url, "robots_missing", "medium",
                                "robots.txt отсутствует (404) — сайт индексируется целиком, "
                                "включая служебные страницы"))
        else:
            issues.append(Issue(url, "robots_bad_status", "high",
                                f"robots.txt отдаёт HTTP {robots.status}"))
        return issues

    # Полная блокировка сайта — самая дорогая ошибка.
    for ua, rules in robots.groups.items():
        for rule in rules:
            if rule.kind == "disallow" and rule.pattern.strip() in ("/", "/*"):
                sev = "critical"
                who = "для всех роботов" if ua == "*" else f"для {ua}"
                issues.append(Issue(url, "robots_blocks_whole_site", sev,
                                    f"строка {rule.line_no}: «Disallow: {rule.pattern}» {who} — "
                                    "весь сайт закрыт от индексации"))

    # Секция для Яндекса строже общей.
    if "yandex" in robots.groups:
        for path in ("/",) + tuple(IMPORTANT_PATH_HINTS):
            allowed_all, _ = robots.is_allowed(path, "*")
            allowed_ya, rule_ya = robots.is_allowed(path, "yandex")
            if allowed_all and not allowed_ya:
                issues.append(Issue(url, "robots_yandex_stricter", "high",
                                    f"{path} открыт для всех, но закрыт в секции User-agent: Yandex"
                                    + (f" (строка {rule_ya.line_no}: Disallow: {rule_ya.pattern})"
                                       if rule_ya else "")))

    # Важные разделы под Disallow. Разделы из карты сайта существуют точно —
    # для них это critical. Типовые пути проверяем «на всякий случай», мягче.
    real_paths = [p if p.startswith("/") else "/" + p for p in sample_paths]
    checked: set[str] = set()
    for path in real_paths + [p for p in IMPORTANT_PATH_HINTS if p not in real_paths]:
        if path in checked:
            continue
        checked.add(path)
        is_real = path in real_paths
        for ua in ("*", "yandex", "googlebot"):
            allowed, rule = robots.is_allowed(path, ua)
            if not allowed and rule:
                detail = (f"раздел из карты сайта закрыт для {ua} правилом строки {rule.line_no}: "
                          f"Disallow: {rule.pattern}") if is_real else (
                    f"типовой раздел закрыт для {ua} правилом строки {rule.line_no}: "
                    f"Disallow: {rule.pattern} — проверьте, нужен ли он в поиске")
                issues.append(Issue(urljoin(site_root, path), "robots_blocks_section",
                                    "critical" if is_real else "high", detail))
                break

    # Ресурсы для рендеринга.
    for rules in robots.groups.values():
        for rule in rules:
            if rule.kind != "disallow" or not rule.pattern:
                continue
            low = rule.pattern.lower()
            if any(hint in low for hint in RENDER_RESOURCE_HINTS):
                issues.append(Issue(url, "robots_blocks_assets", "medium",
                                    f"строка {rule.line_no}: «Disallow: {rule.pattern}» закрывает "
                                    "стили/скрипты — робот увидит страницу не так, как пользователь"))
                break

    # Директива Sitemap.
    if not robots.sitemaps:
        issues.append(Issue(url, "robots_no_sitemap_directive", "medium",
                            "в robots.txt нет строки «Sitemap:» — роботу негде взять карту сайта"))
    else:
        site_host = registrable_host(site_root)
        for sm in robots.sitemaps:
            if not sm.lower().startswith("http"):
                issues.append(Issue(url, "robots_sitemap_relative", "medium",
                                    f"«Sitemap: {sm}» — нужен абсолютный URL с https://"))
            elif registrable_host(sm) != site_host:
                issues.append(Issue(url, "robots_sitemap_foreign_host", "high",
                                    f"«Sitemap: {sm}» ведёт на чужой домен"))

    # Host: Яндекс отменил директиву в 2018-м, главное зеркало задаётся 301 + canonical.
    if robots.hosts:
        issues.append(Issue(url, "robots_host_directive_obsolete", "low",
                            f"директива «Host: {robots.hosts[0]}» больше не учитывается Яндексом — "
                            "главное зеркало задаётся 301-редиректом и canonical"))

    # Crawl-delay замедляет обход; Яндекс учитывает, Google игнорирует.
    for ua, delay in robots.crawl_delays.items():
        try:
            if float(delay.replace(",", ".")) >= 2:
                issues.append(Issue(url, "robots_crawl_delay_high", "medium",
                                    f"Crawl-delay: {delay} для «{ua}» — Яндекс будет обходить сайт медленно"))
        except ValueError:
            issues.append(Issue(url, "robots_crawl_delay_invalid", "low",
                                f"Crawl-delay: {delay!r} — нечисловое значение"))

    if robots.unknown:
        line_no, text = robots.unknown[0]
        issues.append(Issue(url, "robots_unknown_directive", "low",
                            f"строка {line_no}: непонятная директива {text!r}"
                            + (f" (и ещё {len(robots.unknown) - 1})" if len(robots.unknown) > 1 else "")))

    return issues


# ───── 2. sitemap.xml ────────────────────────────────────────────────

SITEMAP_FALLBACKS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                     "/sitemap.xml.gz", "/sitemap1.xml")


@dataclass
class SitemapEntry:
    loc: str
    lastmod: Optional[str] = None
    source: str = ""


@dataclass
class SitemapReport:
    checked: list[dict] = field(default_factory=list)   # {url, status, kind, count, bytes}
    entries: list[SitemapEntry] = field(default_factory=list)
    found_url: Optional[str] = None

    @property
    def urls(self) -> list[str]:
        return [e.loc for e in self.entries]


def _decode_sitemap(resp: Fetched) -> str:
    if resp.url.endswith(".gz") or resp.headers.get("content-type", "").endswith("gzip"):
        try:
            return gzip.decompress(resp.content).decode("utf-8", "replace")
        except (OSError, EOFError):
            pass
    return resp.text


def load_sitemaps(site_root: str, robots: RobotsTxt, *, max_files: int = 50) -> SitemapReport:
    """Собрать все URL из карты сайта. Sitemap-index разворачивается рекурсивно."""
    report = SitemapReport()
    candidates: list[str] = list(robots.sitemaps)
    for path in SITEMAP_FALLBACKS:
        candidate = site_root.rstrip("/") + path
        if candidate not in candidates:
            candidates.append(candidate)

    seen: set[str] = set()
    queue = list(candidates)
    files_read = 0

    while queue and files_read < max_files:
        sm_url = queue.pop(0)
        if sm_url in seen:
            continue
        seen.add(sm_url)

        resp = fetch(sm_url)
        entry = {"url": sm_url, "status": resp.status, "kind": "?",
                 "count": 0, "bytes": len(resp.content), "error": resp.error}
        if resp.error or not resp.ok:
            entry["kind"] = "unavailable"
            report.checked.append(entry)
            continue

        files_read += 1
        text = _decode_sitemap(resp)
        soup = BeautifulSoup(text, "xml")

        if soup.find("sitemapindex"):
            entry["kind"] = "index"
            children = [loc.text.strip() for loc in soup.find_all("loc")]
            entry["count"] = len(children)
            queue.extend(children)
        elif soup.find("urlset"):
            entry["kind"] = "urlset"
            for url_tag in soup.find_all("url"):
                loc_tag = url_tag.find("loc")
                if not loc_tag or not loc_tag.text.strip():
                    continue
                lastmod_tag = url_tag.find("lastmod")
                report.entries.append(SitemapEntry(
                    loc=loc_tag.text.strip(),
                    lastmod=lastmod_tag.text.strip() if lastmod_tag else None,
                    source=sm_url,
                ))
                entry["count"] += 1
        else:
            entry["kind"] = "not_xml"

        report.checked.append(entry)
        if report.found_url is None and entry["kind"] in ("index", "urlset"):
            report.found_url = sm_url
        time.sleep(DELAY)

    return report


_LASTMOD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")


def check_sitemap(report: SitemapReport, robots: RobotsTxt, site_root: str,
                  today: str = "") -> list[Issue]:
    issues: list[Issue] = []
    root_sitemap = site_root.rstrip("/") + "/sitemap.xml"

    if not report.found_url:
        tried = ", ".join(c["url"] for c in report.checked[:5]) or root_sitemap
        issues.append(Issue(root_sitemap, "sitemap_missing", "critical",
                            f"карта сайта не найдена: проверены {tried}"))
        return issues

    for entry in report.checked:
        if entry["kind"] == "unavailable" and entry["url"] in robots.sitemaps:
            issues.append(Issue(entry["url"], "sitemap_in_robots_unavailable", "high",
                                f"указана в robots.txt, но отдаёт "
                                f"{entry['error'] or 'HTTP ' + str(entry['status'])}"))
        if entry["kind"] == "not_xml":
            issues.append(Issue(entry["url"], "sitemap_not_xml", "high",
                                "файл открывается, но это не XML-карта (нет <urlset>/<sitemapindex>)"))
        if entry["kind"] == "urlset" and entry["count"] > 50000:
            issues.append(Issue(entry["url"], "sitemap_too_many_urls", "high",
                                f"{entry['count']} URL в одном файле — лимит 50 000, нужен sitemap-index"))
        if entry["bytes"] > 50 * 1024 * 1024:
            issues.append(Issue(entry["url"], "sitemap_too_big", "high",
                                f"{entry['bytes'] // (1024*1024)} МБ — лимит 50 МБ без сжатия"))

    if not report.entries:
        issues.append(Issue(report.found_url, "sitemap_empty", "critical",
                            "карта сайта найдена, но в ней нет ни одного <loc>"))
        return issues

    if not robots.sitemaps and robots.exists:
        issues.append(Issue(report.found_url, "sitemap_not_in_robots", "medium",
                            "карта существует, но не указана в robots.txt строкой «Sitemap:»"))

    site_host = registrable_host(site_root)
    root_scheme = urlparse(site_root).scheme or "https"

    seen_norm: dict[str, str] = {}
    foreign, wrong_scheme, dupes, no_lastmod, bad_lastmod, blocked = [], [], [], [], [], []

    for e in report.entries:
        if registrable_host(e.loc) != site_host:
            foreign.append(e.loc)
            continue
        if urlparse(e.loc).scheme != root_scheme:
            wrong_scheme.append(e.loc)
        key = normalize_url(e.loc)
        if key in seen_norm and seen_norm[key] != e.loc:
            dupes.append(f"{seen_norm[key]} ↔ {e.loc}")
        elif key in seen_norm:
            dupes.append(f"{e.loc} (повторяется)")
        else:
            seen_norm[key] = e.loc
        if not e.lastmod:
            no_lastmod.append(e.loc)
        elif not _LASTMOD_RE.match(e.lastmod):
            bad_lastmod.append(f"{e.loc} → lastmod={e.lastmod!r}")
        elif today and e.lastmod[:10] > today:
            bad_lastmod.append(f"{e.loc} → lastmod={e.lastmod[:10]} (дата в будущем)")
        if robots.exists:
            allowed, rule = robots.is_allowed(urlparse(e.loc).path or "/", "yandex")
            if not allowed and rule:
                blocked.append(f"{e.loc} (Disallow: {rule.pattern})")

    def _bulk(sample: list[str], check: str, severity: str, template: str) -> None:
        if not sample:
            return
        head = sample[0]
        tail = f" (и ещё {len(sample) - 1})" if len(sample) > 1 else ""
        issues.append(Issue(report.found_url, check, severity,
                            template.format(n=len(sample), head=head) + tail))

    _bulk(foreign, "sitemap_foreign_urls", "high",
          "{n} URL из карты ведут на другой домен, например {head}")
    _bulk(wrong_scheme, "sitemap_scheme_mismatch", "high",
          "{n} URL в карте с другим протоколом, чем сам сайт, например {head}")
    _bulk(dupes, "sitemap_duplicate_urls", "medium",
          "{n} повторов/дублей URL внутри карты, например {head}")
    _bulk(blocked, "sitemap_url_blocked_by_robots", "critical",
          "{n} URL есть в карте, но закрыты в robots.txt — противоречие, например {head}")
    _bulk(bad_lastmod, "sitemap_bad_lastmod", "low",
          "{n} записей с некорректным lastmod, например {head}")
    if no_lastmod and len(no_lastmod) == len(report.entries):
        issues.append(Issue(report.found_url, "sitemap_no_lastmod", "low",
                            "ни у одной записи нет <lastmod> — роботу сложнее заметить обновления"))

    return issues


def check_yandex_sitemap_submission(host_url: str) -> tuple[dict, list[Issue]]:
    """Отправлена ли карта в Яндекс.Вебмастер. Нужен YANDEX_WEBMASTER_TOKEN.

    Возвращает (данные, issues). Без токена — пустой результат и Issue-подсказка,
    аудит на этом не падает.
    """
    import os

    result: dict = {"available": False, "reason": "", "host_id": None,
                    "sitemaps": [], "user_added": []}
    issues: list[Issue] = []

    if not os.environ.get("YANDEX_WEBMASTER_TOKEN", "").strip():
        result["reason"] = "нет YANDEX_WEBMASTER_TOKEN"
        issues.append(Issue(host_url, "yandex_sitemap_unknown", "medium",
                            "не удалось проверить, отправлена ли карта в Яндекс.Вебмастер: "
                            "не задан YANDEX_WEBMASTER_TOKEN"))
        return result, issues

    try:
        from modules.yandex_webmaster import (
            yw_get_user_id, yw_resolve_host_id, yw_list_sitemaps,
            yw_list_user_added_sitemaps,
        )
        user_id = yw_get_user_id()
        user_id, host_id = yw_resolve_host_id(user_id, registrable_host(host_url))
        result["host_id"] = host_id
        result["sitemaps"] = yw_list_sitemaps(user_id, host_id)
        result["user_added"] = yw_list_user_added_sitemaps(user_id, host_id)
        result["available"] = True
    except Exception as e:  # noqa: BLE001 — сеть/токен/права, аудит продолжается
        result["reason"] = str(e)
        issues.append(Issue(host_url, "yandex_sitemap_unknown", "medium",
                            f"не удалось спросить Яндекс.Вебмастер про карту сайта: {e}"))
        return result, issues

    known = result["sitemaps"] + result["user_added"]
    if not known:
        issues.append(Issue(host_url, "yandex_sitemap_not_submitted", "high",
                            "Яндекс.Вебмастер не знает ни одной карты сайта — "
                            "добавьте её в «Индексирование → Файлы Sitemap»"))
        return result, issues

    for sm in known:
        errors = (sm.get("errors") or 0)
        if errors:
            issues.append(Issue(sm.get("sitemap_url", host_url), "yandex_sitemap_errors", "high",
                                f"Яндекс нашёл {errors} ошибок в карте {sm.get('sitemap_url')}"))
        if sm.get("urls_count") == 0:
            issues.append(Issue(sm.get("sitemap_url", host_url), "yandex_sitemap_zero_urls", "high",
                                f"Яндекс прочитал карту {sm.get('sitemap_url')}, но взял из неё 0 URL"))

    return result, issues


# ───── 3-4. canonical и noindex (постранично) ────────────────────────

_XROBOTS_NOINDEX = re.compile(r"\bnone\b|\bnoindex\b", re.I)


@dataclass
class PageFacts:
    url: str
    status: int
    final_url: str
    redirects: list[str]
    canonical: Optional[str] = None
    meta_robots: Optional[str] = None
    x_robots_tag: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    text_hash: Optional[str] = None
    error: Optional[str] = None


def page_facts(url: str) -> PageFacts:
    r = fetch(url)
    facts = PageFacts(url=url, status=r.status, final_url=r.final_url or url,
                      redirects=r.redirects, error=r.error)
    facts.x_robots_tag = r.headers.get("x-robots-tag")
    if not r.ok or not r.text:
        return facts

    soup = BeautifulSoup(r.text, "html.parser")
    link = soup.find("link", rel="canonical")
    if link and (link.get("href") or "").strip():
        facts.canonical = urljoin(facts.final_url, link["href"].strip())
    robots_meta = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
    if robots_meta:
        facts.meta_robots = (robots_meta.get("content") or "").strip()
    else:
        ya = soup.find("meta", attrs={"name": re.compile(r"^yandex$", re.I)})
        if ya:
            facts.meta_robots = (ya.get("content") or "").strip()
    title_tag = soup.find("title")
    if title_tag:
        facts.title = title_tag.get_text(strip=True)
    desc = soup.find("meta", attrs={"name": "description"})
    if desc:
        facts.description = (desc.get("content") or "").strip()

    body = soup.body or soup
    for tag in body.find_all(["script", "style", "noscript"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", body.get_text(" ", strip=True)).strip().lower()
    if len(text) >= 200:
        facts.text_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return facts


def check_canonical_facts(facts: PageFacts, site_root: str,
                          sitemap_urls: Optional[set[str]] = None) -> list[Issue]:
    """Canonical: есть ли, свой ли домен, не ведёт ли на другую/битую страницу."""
    if not facts.error and facts.status and not (200 <= facts.status < 300):
        return []
    issues: list[Issue] = []
    url = facts.url

    if not facts.canonical:
        issues.append(Issue(url, "canonical_missing", "high",
                            "нет <link rel=canonical> — поисковик сам выберет главную версию страницы"))
        return issues

    canonical = facts.canonical
    if registrable_host(canonical) != registrable_host(site_root):
        issues.append(Issue(url, "canonical_foreign_domain", "critical",
                            f"canonical ведёт на другой домен: {canonical}"))
        return issues

    if same_normalized(canonical, facts.final_url):
        # Совпадает по смыслу — проверим точность записи (протокол/www/слеш).
        exact = normalize_url(canonical, ignore_scheme=False, ignore_www=False,
                              ignore_trailing_slash=False, drop_tracking=False)
        actual = normalize_url(facts.final_url, ignore_scheme=False, ignore_www=False,
                               ignore_trailing_slash=False, drop_tracking=False)
        if exact != actual:
            issues.append(Issue(url, "canonical_format_mismatch", "medium",
                                f"canonical={canonical} отличается от адреса страницы "
                                f"{facts.final_url} протоколом/www/слешем"))
        if urlparse(canonical).scheme != (urlparse(site_root).scheme or "https"):
            issues.append(Issue(url, "canonical_wrong_scheme", "high",
                                f"canonical на {urlparse(canonical).scheme}:// — сайт работает по "
                                f"{urlparse(site_root).scheme}://"))
        return issues

    # Canonical указывает на другую страницу — законно только для дублей.
    severity = "critical" if (sitemap_urls and normalize_url(url) in sitemap_urls) else "high"
    issues.append(Issue(url, "canonical_points_elsewhere", severity,
                        f"canonical={canonical} — страница отдаёт индексацию другому адресу"))
    return issues


def check_canonical_targets(facts_list: list[PageFacts], site_root: str) -> list[Issue]:
    """Куда ведут canonical: живой ли адрес, не цепочка ли, не закрыт ли noindex'ом."""
    issues: list[Issue] = []
    by_norm = {normalize_url(f.url): f for f in facts_list}
    checked: dict[str, Fetched] = {}

    for facts in facts_list:
        canonical = facts.canonical
        if not canonical or registrable_host(canonical) != registrable_host(site_root):
            continue
        if same_normalized(canonical, facts.final_url):
            continue

        target = by_norm.get(normalize_url(canonical))
        if target is None:
            if canonical not in checked:
                checked[canonical] = fetch(canonical)
                time.sleep(DELAY)
            resp = checked[canonical]
            if resp.error or resp.status >= 400:
                issues.append(Issue(facts.url, "canonical_target_broken", "critical",
                                    f"canonical ведёт на недоступный адрес {canonical} "
                                    f"({resp.error or 'HTTP ' + str(resp.status)})"))
            elif resp.redirects:
                issues.append(Issue(facts.url, "canonical_target_redirects", "high",
                                    f"canonical={canonical} редиректит на {resp.final_url} — "
                                    "нужно указывать конечный адрес"))
            continue

        if target.canonical and not same_normalized(target.canonical, target.final_url):
            issues.append(Issue(facts.url, "canonical_chain", "high",
                                f"цепочка canonical: {facts.url} → {canonical} → {target.canonical}"))
        if target.meta_robots and "noindex" in target.meta_robots.lower():
            issues.append(Issue(facts.url, "canonical_target_noindex", "critical",
                                f"canonical ведёт на {canonical}, а та страница закрыта noindex"))
        if target.redirects:
            issues.append(Issue(facts.url, "canonical_target_redirects", "high",
                                f"canonical={canonical} редиректит на {target.final_url}"))
    return issues


def check_noindex_facts(facts: PageFacts, robots: RobotsTxt,
                        expected_noindex: Iterable[str] = ()) -> list[Issue]:
    """noindex в meta robots и в HTTP-заголовке X-Robots-Tag."""
    issues: list[Issue] = []
    path = (urlparse(facts.url).path or "/").rstrip("/") or "/"
    expected = {p.rstrip("/") or "/" for p in expected_noindex}
    is_expected = path in expected

    meta = (facts.meta_robots or "").lower()
    if "noindex" in meta or re.search(r"\bnone\b", meta):
        if not is_expected:
            issues.append(Issue(facts.url, "noindex_on_public_page", "critical",
                                f"<meta name=robots content=\"{facts.meta_robots}\"> — "
                                "страница запрещена к индексации"))
    elif "nofollow" in meta and not is_expected:
        issues.append(Issue(facts.url, "nofollow_on_public_page", "high",
                            f"<meta name=robots content=\"{facts.meta_robots}\"> — "
                            "робот не пойдёт по ссылкам со страницы"))

    header = facts.x_robots_tag or ""
    if header and _XROBOTS_NOINDEX.search(header) and not is_expected:
        issues.append(Issue(facts.url, "noindex_http_header", "critical",
                            f"HTTP-заголовок X-Robots-Tag: {header} — запрет индексации "
                            "на уровне сервера (в HTML его не видно)"))

    # noindex + Disallow одновременно: робот не зайдёт и не увидит noindex.
    if ("noindex" in meta or (header and _XROBOTS_NOINDEX.search(header))) and robots.exists:
        allowed, rule = robots.is_allowed(urlparse(facts.url).path or "/", "yandex")
        if not allowed and rule:
            issues.append(Issue(facts.url, "noindex_and_disallow", "high",
                                f"страница и закрыта в robots.txt (Disallow: {rule.pattern}), "
                                "и помечена noindex — робот не зайдёт и не увидит noindex, "
                                "страница может остаться в выдаче"))
    return issues


# ───── 5. Дубли URL ──────────────────────────────────────────────────

# Варианты главной страницы и требования к ним.
#   strict — обязан быть 301 на основной адрес (иначе два «сайта» в индексе);
#   soft   — 301 желателен, но правильный canonical проблему закрывает;
#   param  — 200 это норма, важен только canonical.
MIRROR_VARIANTS = (
    ("http://{host}/", "strict", "http-версия"),
    ("https://{host}/", "strict", "https-версия"),
    ("http://www.{host}/", "strict", "www-версия по http"),
    ("https://www.{host}/", "strict", "www-версия"),
    ("{scheme}://{host}/index.php", "soft", "/index.php"),
    ("{scheme}://{host}/index.html", "soft", "/index.html"),
    ("{scheme}://{host}//", "soft", "двойной слеш"),
    ("{scheme}://{host}/?utm_source=seo-agent-test", "param", "адрес с utm-меткой"),
)


def _literal(url: str) -> str:
    """Точное написание адреса: без «умной» нормализации, только регистр хоста."""
    return normalize_url(url, drop_tracking=False, ignore_scheme=False, ignore_www=False,
                         ignore_trailing_slash=False, ignore_index=False,
                         collapse_slashes=False)


def check_mirrors(site_root: str) -> tuple[list[dict], list[Issue]]:
    """Классические зеркала: http/https, www/без www, слеш, index.php, utm.

    Правильно — всё склеено 301-редиректом на один адрес. Если вариант отдаёт 200,
    спасти положение может только canonical на основной адрес.
    """
    host = registrable_host(site_root)
    scheme = urlparse(site_root).scheme or "https"
    canonical_home = f"{scheme}://{host}/"

    probes: list[dict] = []
    issues: list[Issue] = []

    for template, kind, label in MIRROR_VARIANTS:
        variant = template.format(host=host, scheme=scheme)
        r = fetch(variant)
        canonical = None
        text_hash = None
        if r.ok and r.text:
            soup = BeautifulSoup(r.text, "html.parser")
            link = soup.find("link", rel="canonical")
            if link and (link.get("href") or "").strip():
                canonical = urljoin(r.final_url, link["href"].strip())
            body = soup.body or soup
            for tag in body.find_all(["script", "style", "noscript"]):
                tag.decompose()
            text = re.sub(r"\s+", " ", body.get_text(" ", strip=True)).strip().lower()
            if len(text) >= 200:
                text_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()
        probes.append({
            "url": variant, "kind": kind, "label": label, "status": r.status,
            "final_url": r.final_url, "redirects": len(r.redirects),
            "canonical": canonical, "error": r.error, "text_hash": text_hash,
        })
        time.sleep(DELAY)

    home_hash = next((p["text_hash"] for p in probes
                      if _literal(p["url"]) == _literal(canonical_home) and p["text_hash"]), None)

    for probe in probes:
        if probe["error"] or not probe["status"]:
            continue
        variant, kind, label = probe["url"], probe["kind"], probe["label"]
        if _literal(variant) == _literal(canonical_home):
            continue                              # это и есть основной адрес
        if probe["status"] >= 400:
            continue                              # 404 на /index.php — тоже нормально
        if _literal(probe["final_url"]) == _literal(canonical_home):
            continue                              # склеено редиректом — как надо

        canonical_ok = probe["canonical"] and same_normalized(probe["canonical"], canonical_home)
        if kind == "param":
            if canonical_ok:
                continue
            issues.append(Issue(variant, "duplicate_mirror", "high",
                                f"{label} отдаёт 200, а canonical "
                                f"{'ведёт на ' + probe['canonical'] if probe['canonical'] else 'отсутствует'} — "
                                "utm-метки создадут дубли в индексе"))
            continue

        if canonical_ok:
            severity = "high" if kind == "strict" else "medium"
            note = ("склеено только через canonical, 301-редиректа нет"
                    if kind == "strict" else "отдаётся 200 вместо редиректа, но canonical верный")
        else:
            severity = "critical" if kind == "strict" else "high"
            note = (f"отдаётся 200 без редиректа на {canonical_home}, canonical "
                    + (f"ведёт на {probe['canonical']}" if probe["canonical"] else "отсутствует"))
        if home_hash and probe["text_hash"] == home_hash:
            note += "; содержимое полностью совпадает с главной"
        issues.append(Issue(variant, "duplicate_mirror", severity, f"{label}: {note}"))

    return probes, issues


def check_duplicate_urls(facts_list: list[PageFacts]) -> list[Issue]:
    """Дубли среди просканированных страниц: адрес, title, description, текст."""
    issues: list[Issue] = []
    live = [f for f in facts_list if 200 <= f.status < 300]

    by_norm: dict[str, list[str]] = {}
    for f in live:
        by_norm.setdefault(normalize_url(f.final_url), []).append(f.url)
    for norm, urls in by_norm.items():
        uniq = sorted(set(urls))
        if len(uniq) > 1:
            issues.append(Issue(uniq[0], "duplicate_url_variants", "high",
                                "один и тот же адрес доступен в разных написаниях: "
                                + ", ".join(uniq[:4])))

    by_hash: dict[str, list[PageFacts]] = {}
    for f in live:
        if f.text_hash:
            by_hash.setdefault(f.text_hash, []).append(f)
    for _, group in by_hash.items():
        if len(group) < 2:
            continue
        norms = {normalize_url(f.final_url) for f in group}
        if len(norms) < 2:
            continue
        canonicals = {normalize_url(f.canonical) for f in group if f.canonical}
        sev = "medium" if len(canonicals) == 1 else "high"
        note = ("контент совпадает, но canonical у всех один — поисковик склеит"
                if len(canonicals) == 1 else
                "полностью совпадающий текст на разных URL без общего canonical")
        issues.append(Issue(group[0].url, "duplicate_content", sev,
                            f"{note}: " + ", ".join(f.url for f in group[:4])))

    by_title: dict[str, list[str]] = {}
    by_desc: dict[str, list[str]] = {}
    for f in live:
        if f.title:
            by_title.setdefault(f.title.strip(), []).append(f.url)
        if f.description:
            by_desc.setdefault(f.description.strip(), []).append(f.url)
    for title, urls in by_title.items():
        if len(urls) >= 2:
            issues.append(Issue(urls[0], "duplicate_title", "high",
                                f"одинаковый title «{title[:60]}» на {len(urls)} страницах: "
                                + ", ".join(urls[:4])))
    for desc, urls in by_desc.items():
        if len(urls) >= 2:
            issues.append(Issue(urls[0], "duplicate_description", "medium",
                                f"одинаковое описание на {len(urls)} страницах: "
                                + ", ".join(urls[:4])))
    return issues
