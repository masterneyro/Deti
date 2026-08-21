"""
Человеческий язык для отчётов seo-agent.

Владелец сайта — маркетолог, а не разработчик. Отчёты в Telegram должны читаться как
письмо от живого SEO-специалиста, а не как лог: без кодов модулей (M1–M6),
без английских critical/high, без дампов длинных URL и без жаргона
(query, snapshot, граф узлов, ИКС без расшифровки).

Этот модуль — единая «словарная» прослойка. Все render-функции модулей
импортируют отсюда severity_ru(), check_ru(), short_url() и помощники.
"""

from __future__ import annotations

from urllib.parse import urlparse

# ── Уровни важности: эмодзи + слово вместо critical/high/medium/low ──
_SEVERITY = {
    "critical": ("🔴", "срочно"),
    "high": ("🟠", "важно"),
    "medium": ("🟡", "по возможности"),
    "low": ("⚪", "мелочь"),
    "info": ("ℹ️", "к сведению"),
}


def severity_emoji(sev: str) -> str:
    return _SEVERITY.get(sev, ("•", sev))[0]


def severity_word(sev: str) -> str:
    return _SEVERITY.get(sev, ("•", sev))[1]


def severity_label(sev: str) -> str:
    """'🔴 срочно' — для строк-счётчиков."""
    e, w = _SEVERITY.get(sev, ("•", sev))
    return f"{e} {w}"


# ── Типы тех. проблем → понятная формулировка ──
# Ключи совпадают с Issue.check из modules/audit_checks.py и m2_audit.py.
_CHECK = {
    "title_missing": "нет заголовка вкладки (title)",
    "title_too_long": "заголовок вкладки длинноват",
    "title_too_short": "заголовок вкладки коротковат",
    "description_missing": "нет описания страницы для поисковика",
    "description_too_long": "описание для поисковика длинновато",
    "description_too_short": "описание для поисковика коротковато",
    "canonical_missing": "не указан основной адрес страницы (canonical)",
    "canonical_mismatch": "основной адрес страницы (canonical) не тот",
    "h1_missing": "нет главного заголовка на странице (H1)",
    "h1_multiple": "на странице несколько главных заголовков (H1)",
    "h1_empty": "главный заголовок страницы (H1) пустой",
    "h1_too_long": "главный заголовок (H1) длинноват",
    "noindex_on_public_page": "рабочая страница случайно закрыта от поиска",
    "img_no_alt": "у картинок нет текстового описания (alt)",
    "json_ld_missing": "нет спецразметки для поисковика (JSON-LD)",
    "json_ld_empty": "спецразметка для поисковика пустая",
    "json_ld_invalid": "спецразметка для поисковика с ошибкой",
    "json_ld_no_type": "в спецразметке не указан тип",
    "json_ld_tbd_marker": "в спецразметке осталась заглушка «TBD»",
    "network_error": "страница не открылась",
    "http_5xx": "ошибка на стороне сервера",
    "http_4xx": "страница недоступна (битая ссылка)",
    "psi_lcp_slow_mobile": "медленно грузится на телефоне",
    "psi_inp_slow_mobile": "тормозит при нажатиях на телефоне",
    "psi_cls_bad_mobile": "вёрстка «прыгает» при загрузке на телефоне",
    "psi_performance_low_mobile": "низкая оценка скорости на телефоне",

    # robots.txt
    "robots_missing": "нет файла robots.txt",
    "robots_unreachable": "robots.txt не открывается",
    "robots_bad_status": "robots.txt отдаёт ошибку",
    "robots_not_plain_text": "вместо robots.txt отдаётся обычная страница",
    "robots_blocks_whole_site": "весь сайт закрыт от индексации в robots.txt",
    "robots_blocks_section": "важный раздел закрыт от поиска в robots.txt",
    "robots_yandex_stricter": "для Яндекса раздел закрыт, а для остальных открыт",
    "robots_blocks_assets": "в robots.txt закрыты стили и скрипты",
    "robots_no_sitemap_directive": "в robots.txt не указана карта сайта",
    "robots_sitemap_relative": "адрес карты сайта в robots.txt указан не полностью",
    "robots_sitemap_foreign_host": "карта сайта в robots.txt ведёт на чужой домен",
    "robots_host_directive_obsolete": "устаревшая строка Host в robots.txt",
    "robots_crawl_delay_high": "робот вынужден обходить сайт медленно",
    "robots_crawl_delay_invalid": "некорректное значение Crawl-delay",
    "robots_unknown_directive": "непонятная строка в robots.txt",

    # sitemap.xml
    "sitemap_missing": "нет карты сайта (sitemap.xml)",
    "sitemap_empty": "карта сайта пустая",
    "sitemap_not_xml": "карта сайта в неверном формате",
    "sitemap_in_robots_unavailable": "карта, указанная в robots.txt, не открывается",
    "sitemap_not_in_robots": "карта сайта не прописана в robots.txt",
    "sitemap_too_many_urls": "в одной карте слишком много адресов",
    "sitemap_too_big": "файл карты сайта слишком большой",
    "sitemap_foreign_urls": "в карте сайта есть чужие адреса",
    "sitemap_scheme_mismatch": "в карте сайта адреса с другим протоколом",
    "sitemap_duplicate_urls": "повторы адресов в карте сайта",
    "sitemap_url_blocked_by_robots": "адрес есть в карте, но закрыт в robots.txt",
    "sitemap_bad_lastmod": "неверная дата обновления в карте сайта",
    "sitemap_no_lastmod": "в карте сайта нет дат обновления",
    "yandex_sitemap_not_submitted": "карта сайта не добавлена в Яндекс.Вебмастер",
    "yandex_sitemap_errors": "Яндекс нашёл ошибки в карте сайта",
    "yandex_sitemap_zero_urls": "Яндекс не взял из карты ни одного адреса",
    "yandex_sitemap_unknown": "не удалось проверить карту в Яндекс.Вебмастере",

    # canonical
    "canonical_foreign_domain": "основной адрес страницы указан на чужой сайт",
    "canonical_format_mismatch": "основной адрес отличается протоколом или слешем",
    "canonical_wrong_scheme": "основной адрес указан с неверным протоколом",
    "canonical_points_elsewhere": "страница отдаёт индексацию другому адресу",
    "canonical_chain": "цепочка основных адресов (canonical → canonical)",
    "canonical_target_broken": "основной адрес ведёт на несуществующую страницу",
    "canonical_target_redirects": "основной адрес ведёт на редирект",
    "canonical_target_noindex": "основной адрес ведёт на закрытую от поиска страницу",

    # noindex
    "noindex_http_header": "страница закрыта от поиска на уровне сервера",
    "nofollow_on_public_page": "робот не пойдёт по ссылкам со страницы",
    "noindex_and_disallow": "страница закрыта дважды — и в robots.txt, и через noindex",

    # дубли
    "duplicate_mirror": "сайт открывается по нескольким адресам (зеркала)",
    "duplicate_url_variants": "один адрес доступен в разных написаниях",
    "duplicate_content": "одинаковый текст на разных страницах",
    "duplicate_title": "одинаковый заголовок вкладки на разных страницах",
    "duplicate_description": "одинаковое описание на разных страницах",
}


def check_ru(check: str) -> str:
    """Понятное название типа проблемы. Неизвестный код — мягко расшифровываем."""
    if check in _CHECK:
        return _CHECK[check]
    return check.replace("_", " ")


def short_url(url: str) -> str:
    """Только путь, без https://домена — так читабельнее. '/' → 'главная'."""
    try:
        path = urlparse(url).path or "/"
    except Exception:
        return url
    return "главная" if path == "/" else path


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 статья, 2 статьи, 5 статей."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many
