#!/usr/bin/env python3
"""
Проверка индексируемости сайта: robots.txt · sitemap.xml · canonical · noindex · дубли URL.

Запуск:
    cd seo-agent
    python3 scripts/audit_indexability.py --site https://site.ru
    python3 scripts/audit_indexability.py --site https://site.ru --limit 100
    python3 scripts/audit_indexability.py            # возьмёт M2_SITE_ROOT из окружения

Что делает:
    1. Читает robots.txt и проверяет, не закрыты ли важные разделы (отдельно для Yandex).
    2. Ищет sitemap.xml (в robots.txt и по стандартным адресам), валидирует,
       спрашивает Яндекс.Вебмастер, знает ли он про эту карту (нужен YANDEX_WEBMASTER_TOKEN).
    3. Обходит выборку страниц и смотрит canonical: свой домен, живой адрес, без цепочек.
    4. Ловит noindex — и в <meta name="robots">, и в HTTP-заголовке X-Robots-Tag.
    5. Проверяет дубли: зеркала (http/https, www, слеш, index.php, utm), одинаковый
       контент/title/description на разных адресах.

Отчёт: data/audits/YYYY-MM-DD/indexability.{json,md} + вывод в консоль.
Ничего не меняет на сайте и никуда не пишет, кроме своей папки data/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
AGENT_DIR = THIS_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from modules import indexability as ix  # noqa: E402

log = logging.getLogger("indexability")

DATA_DIR = AGENT_DIR / "data" / "audits"

SEVERITY_ORDER = ("critical", "high", "medium", "low")
SEVERITY_LABEL = {
    "critical": "🔴 срочно",
    "high": "🟠 важно",
    "medium": "🟡 по возможности",
    "low": "⚪ мелочь",
}

# Служебные страницы, для которых noindex — норма, а не ошибка.
DEFAULT_EXPECTED_NOINDEX = (
    "/policy", "/privacy", "/soglasie", "/offer", "/litsenziya",
    "/thanks", "/spasibo", "/search", "/poisk", "/cart", "/korzina",
)


def pick_sample(urls: list[str], limit: int) -> list[str]:
    """Выборка страниц для обхода: равномерно по всей карте, а не первые N подряд."""
    uniq = list(dict.fromkeys(urls))
    if len(uniq) <= limit:
        return uniq
    step = len(uniq) / limit
    return [uniq[int(i * step)] for i in range(limit)]


def run(site: str, limit: int, skip_yandex: bool, skip_mirrors: bool) -> dict:
    site = site.rstrip("/")
    if not site.startswith("http"):
        site = "https://" + site
    today = dt.date.today().isoformat()
    issues: list[ix.Issue] = []

    # 1. robots.txt
    log.info("1/5 robots.txt — %s/robots.txt", site)
    robots = ix.load_robots(site)

    # 2. sitemap.xml
    log.info("2/5 sitemap.xml — ищу карту сайта")
    sitemap = ix.load_sitemaps(site, robots)
    sitemap_paths = sorted({
        "/" + (ix.urlparse(u).path or "/").strip("/").split("/")[0]
        for u in sitemap.urls
    } - {"/"})
    issues += ix.check_robots(robots, site, sample_paths=sitemap_paths)
    issues += ix.check_sitemap(sitemap, robots, site, today=today)
    log.info("    карта: %s · URL в ней: %d", sitemap.found_url or "не найдена", len(sitemap.entries))

    yandex: dict = {"available": False, "reason": "пропущено ключом --skip-yandex"}
    if not skip_yandex:
        log.info("    спрашиваю Яндекс.Вебмастер про карту сайта")
        yandex, ya_issues = ix.check_yandex_sitemap_submission(site)
        issues += ya_issues

    # 3-4. canonical и noindex постранично
    own_urls = [u for u in sitemap.urls
                if ix.registrable_host(u) == ix.registrable_host(site)]
    skipped_foreign = len(sitemap.urls) - len(own_urls)
    if skipped_foreign:
        log.info("    %d чужих URL из карты не обхожу (о них уже есть замечание)", skipped_foreign)
    targets = [site + "/"] + pick_sample(own_urls, limit)
    targets = list(dict.fromkeys(targets))
    log.info("3/5 canonical + 4/5 noindex — обхожу %d страниц", len(targets))

    sitemap_norm = {ix.normalize_url(u) for u in sitemap.urls}
    facts_list: list[ix.PageFacts] = []
    for i, url in enumerate(targets, 1):
        if i == 1 or i % 10 == 0 or i == len(targets):
            log.info("    [%d/%d] %s", i, len(targets), url)
        facts = ix.page_facts(url)
        facts_list.append(facts)
        if facts.error:
            issues.append(ix.Issue(url, "network_error", "high", facts.error))
        elif facts.status >= 500:
            issues.append(ix.Issue(url, "http_5xx", "critical", f"HTTP {facts.status}"))
        elif facts.status >= 400:
            issues.append(ix.Issue(url, "http_4xx", "high", f"HTTP {facts.status} — битая ссылка в карте сайта"))
        else:
            issues += ix.check_canonical_facts(facts, site, sitemap_norm)
            issues += ix.check_noindex_facts(facts, robots, DEFAULT_EXPECTED_NOINDEX)
        ix.time.sleep(ix.DELAY)

    issues += ix.check_canonical_targets(facts_list, site)

    # 5. Дубли URL
    log.info("5/5 дубли URL")
    probes: list[dict] = []
    if not skip_mirrors:
        probes, mirror_issues = ix.check_mirrors(site)
        issues += mirror_issues
    issues += ix.check_duplicate_urls(facts_list)

    payload = {
        "date": today,
        "site": site,
        "robots": {
            "url": robots.url,
            "status": robots.status,
            "exists": robots.exists,
            "groups": {ua: [{"kind": r.kind, "pattern": r.pattern, "line": r.line_no}
                            for r in rules] for ua, rules in robots.groups.items()},
            "sitemaps": robots.sitemaps,
            "hosts": robots.hosts,
            "clean_params": robots.clean_params,
            "crawl_delays": robots.crawl_delays,
        },
        "sitemap": {
            "found_url": sitemap.found_url,
            "files": sitemap.checked,
            "urls_total": len(sitemap.entries),
        },
        "yandex_sitemaps": yandex,
        "mirrors": probes,
        "pages_checked": [
            {"url": f.url, "status": f.status, "final_url": f.final_url,
             "canonical": f.canonical, "meta_robots": f.meta_robots,
             "x_robots_tag": f.x_robots_tag, "title": f.title}
            for f in facts_list
        ],
        "issues": [i.to_dict() for i in issues],
    }
    return payload


# ───── Отчёт ─────────────────────────────────────────────────────────

BLOCKS = {
    "robots": ("1. robots.txt — не закрыты ли важные разделы",
               lambda c: c.startswith("robots_")),
    "sitemap": ("2. sitemap.xml — есть ли карта и знает ли о ней Яндекс",
                lambda c: c.startswith("sitemap_") or c.startswith("yandex_sitemap")),
    "canonical": ("3. Canonical — не указывают ли страницы на неверный адрес",
                  lambda c: c.startswith("canonical_")),
    "noindex": ("4. noindex — нет ли случайного запрета индексации",
                lambda c: c.startswith("noindex") or c.startswith("nofollow")),
    "duplicates": ("5. Дубли URL",
                   lambda c: c.startswith("duplicate_")),
    "other": ("Прочее (доступность страниц)", lambda c: True),
}


def group_issues(issues: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {k: [] for k in BLOCKS}
    for issue in issues:
        for name, (_, matcher) in BLOCKS.items():
            if matcher(issue["check"]):
                grouped[name].append(issue)
                break
    return grouped


def render_markdown(payload: dict) -> str:
    site = payload["site"].replace("https://", "").replace("http://", "")
    issues = payload["issues"]
    by_sev = Counter(i["severity"] for i in issues)
    grouped = group_issues(issues)

    out: list[str] = []
    out.append(f"# Индексируемость сайта {site} — {payload['date']}\n")
    verdict = ("Ничего критичного не нашлось." if not by_sev["critical"]
               else f"Есть критичные проблемы: {by_sev['critical']}. Их чинить в первую очередь.")
    out.append(f"**Итог:** {verdict}\n")
    out.append("| Важность | Сколько |")
    out.append("|---|---|")
    for sev in SEVERITY_ORDER:
        out.append(f"| {SEVERITY_LABEL[sev]} | {by_sev[sev]} |")
    out.append("")

    # Факты — чтобы отчёт читался без запуска скрипта.
    robots = payload["robots"]
    sm = payload["sitemap"]
    ya = payload["yandex_sitemaps"]
    out.append("## Что проверено\n")
    out.append(f"- **robots.txt:** "
               + (f"есть, HTTP {robots['status']}, секций: {len(robots['groups'])}, "
                  f"директив Sitemap: {len(robots['sitemaps'])}"
                  if robots["exists"] else f"недоступен (HTTP {robots['status']})"))
    out.append(f"- **sitemap.xml:** "
               + (f"{sm['found_url']}, файлов: {len(sm['files'])}, URL в карте: {sm['urls_total']}"
                  if sm["found_url"] else "не найдена"))
    if ya.get("available"):
        known = (ya.get("sitemaps") or []) + (ya.get("user_added") or [])
        if known:
            out.append(f"- **Яндекс.Вебмастер:** знает {len(known)} карт(ы):")
            for s in known[:5]:
                out.append(f"    - `{s.get('sitemap_url')}` — URL: {s.get('urls_count', '?')}, "
                           f"ошибок: {s.get('errors', 0)}, "
                           f"последнее обращение: {(s.get('last_access_date') or '—')[:10]}")
        else:
            out.append("- **Яндекс.Вебмастер:** карт сайта нет в списке")
    else:
        out.append(f"- **Яндекс.Вебмастер:** проверить не удалось ({ya.get('reason') or '—'})")
    out.append(f"- **Страниц обойдено:** {len(payload['pages_checked'])}")
    if payload.get("mirrors"):
        out.append(f"- **Зеркал проверено:** {len(payload['mirrors'])} вариантов главной")
    out.append("")

    for name, (title, _) in BLOCKS.items():
        block = grouped[name]
        if name == "other" and not block:
            continue
        out.append(f"## {title}\n")
        if not block:
            out.append("✅ Проблем не найдено.\n")
            continue
        for sev in SEVERITY_ORDER:
            items = [i for i in block if i["severity"] == sev]
            if not items:
                continue
            out.append(f"**{SEVERITY_LABEL[sev]} — {len(items)}**\n")
            for i in items:
                out.append(f"- `{i['url']}`  \n  {i['detail']}")
            out.append("")

    if payload.get("mirrors"):
        out.append("## Приложение: как отвечают зеркала главной страницы\n")
        out.append("| Адрес | Код | Куда привёл | Редиректов | canonical |")
        out.append("|---|---|---|---|---|")
        for p in payload["mirrors"]:
            status = "не открылся" if p["error"] else p["status"]
            out.append(f"| `{p['url']}` | {status} | `{p['final_url']}` | "
                       f"{p['redirects']} | `{p['canonical'] or '—'}` |")
        out.append("")

    out.append("---")
    out.append("_Отчёт собран автоматически: seo-agent/scripts/audit_indexability.py_")
    return "\n".join(out) + "\n"


def render_console(payload: dict) -> str:
    grouped = group_issues(payload["issues"])
    lines: list[str] = []
    for name, (title, _) in BLOCKS.items():
        block = grouped[name]
        if name == "other" and not block:
            continue
        lines.append(f"\n=== {title} ===")
        if not block:
            lines.append("  ✅ проблем не найдено")
            continue
        for sev in SEVERITY_ORDER:
            for i in [x for x in block if x["severity"] == sev]:
                lines.append(f"  {SEVERITY_LABEL[sev]}  {i['url']}")
                lines.append(f"      {i['detail']}")
    return "\n".join(lines)


def save(payload: dict) -> Path:
    audit_dir = DATA_DIR / payload["date"]
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "indexability.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = audit_dir / "indexability.md"
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return md_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="robots.txt / sitemap.xml / canonical / noindex / дубли URL")
    parser.add_argument("--site", default=os.environ.get("M2_SITE_ROOT", ""),
                        help="Корень сайта, например https://site.ru (по умолчанию M2_SITE_ROOT)")
    parser.add_argument("--limit", type=int, default=40,
                        help="Сколько страниц из карты обойти (по умолчанию 40)")
    parser.add_argument("--skip-yandex", action="store_true",
                        help="Не ходить в API Яндекс.Вебмастера")
    parser.add_argument("--skip-mirrors", action="store_true",
                        help="Не проверять зеркала главной (http/https/www/index.php)")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="Дополнительно сохранить JSON по этому пути")
    args = parser.parse_args()

    if not args.site or "example.com" in args.site:
        parser.error("укажите реальный домен: --site https://ваш-сайт.ru "
                     "(или задайте M2_SITE_ROOT)")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    payload = run(args.site, args.limit, args.skip_yandex, args.skip_mirrors)

    md_path = save(payload)
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(render_console(payload))
    counts = Counter(i["severity"] for i in payload["issues"])
    print(f"\nВсего замечаний: {len(payload['issues'])} "
          f"(срочно {counts['critical']}, важно {counts['high']}, "
          f"по возможности {counts['medium']}, мелочь {counts['low']})")
    print(f"Отчёт: {md_path}")
    return 1 if counts["critical"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
