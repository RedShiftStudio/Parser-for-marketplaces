"""Сбор общедоступных карточек поиска маркетплейсов в CSV/JSON."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import Browser, Page, sync_playwright


@dataclass
class Listing:
    site: str
    query: str
    title: str
    price: str
    url: str
    image: str


BASE = {
    "wb": "https://www.wildberries.ru",
    "ozon": "https://www.ozon.ru",
    "yandex": "https://market.yandex.ru",
    "avito": "https://www.avito.ru",
}


class BlockedError(RuntimeError):
    """Площадка запросила проверку человека или ограничила доступ."""


def browser_options() -> dict:
    """Опциональный корпоративный HTTP(S)-прокси для доступа из нужного региона."""
    proxy = os.getenv("BROWSER_PROXY_URL", "").strip()
    if not proxy:
        return {}
    options = {"server": proxy}
    if os.getenv("BROWSER_PROXY_USER"):
        options["username"] = os.environ["BROWSER_PROXY_USER"]
        options["password"] = os.getenv("BROWSER_PROXY_PASSWORD", "")
    return {"proxy": options}

CARDS = {
    "wb": "article.product-card, .product-card",
    "ozon": '[data-index], [data-widget="searchResultsV2"] > div',
    "yandex": '[data-auto="searchOrganic"], [data-zone-name="productSnippet"]',
    "avito": '[data-marker="item"], [data-marker="catalog-serp"] [itemtype*="Product"]',
}

LINKS = {
    "wb": 'a[href*="/catalog/"]',
    "ozon": 'a[href*="/product/"]',
    "yandex": 'a[href*="/product--"], a[href*="/product/"]',
    "avito": 'a[href*="_"], a[href*="/items/"]',
}


def search_url(site: str, query: str, page: int) -> str:
    if site == "wb":
        return f'{BASE[site]}/catalog/0/search.aspx?{urlencode({"search": query, "page": page})}'
    if site == "ozon":
        return f'{BASE[site]}/search/?{urlencode({"text": query, "page": page})}'
    if site == "yandex":
        return f'{BASE[site]}/search?{urlencode({"text": query, "page": page - 1})}'
    return f'{BASE[site]}/all?q={quote(query)}&p={page}'


def text(element: Tag, selectors: str) -> str:
    for selector in selectors.split("|"):
        node = element.select_one(selector.strip())
        if node:
            value = node.get_text(" ", strip=True)
            if value:
                return value
    return ""


def normalize_price(raw: str) -> str:
    match = re.search(r"\d[\d\s\u00a0\u202f]*", raw)
    return re.sub(r"\D", "", match.group()) if match else ""


def extract(site: str, query: str, html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[Listing] = []
    seen: set[str] = set()
    title_selectors = {
        "wb": ".product-card__name | .product-card__brand | [title]",
        "ozon": '[data-widget="webProductHeading"] | span[title] | a[href*="/product/"] span',
        "yandex": '[data-auto="productCardTitle"] | [data-auto="snippet-title"] | h3',
        "avito": '[itemprop="name"] | [data-marker="item-title"] | h3',
    }
    price_selectors = {
        "wb": ".price__lower-price | .price__lower-price-wallet | ins",
        "ozon": '[data-widget="webPrice"] | [class*="price"]',
        "yandex": '[data-auto="snippet-price-current"] | [data-auto="price-value"]',
        "avito": '[data-marker="item-price"] | [itemprop="price"]',
    }
    for card in soup.select(CARDS[site]):
        link = card.select_one(LINKS[site])
        if not link or not link.get("href"):
            continue
        url = urljoin(BASE[site], str(link["href"]).split("?", 1)[0])
        if urlparse(url).hostname not in {urlparse(BASE[site]).hostname, urlparse(BASE[site]).hostname.removeprefix("www.")}:
            continue
        if url in seen:
            continue
        title = text(card, title_selectors[site]) or str(link.get("title", "")) or link.get_text(" ", strip=True)
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        price_node = card.select_one(price_selectors[site].replace(" | ", ", "))
        raw_price = ""
        if price_node:
            raw_price = str(price_node.get("content") or price_node.get_text(" ", strip=True))
        image_node = card.select_one("img")
        image = ""
        if image_node:
            image = str(image_node.get("src") or image_node.get("data-src") or "")
            if image.startswith("//"):
                image = "https:" + image
        found.append(Listing(site, query, title, normalize_price(raw_price), url, image))
        seen.add(url)
    return found


def load_page(page: Page, url: str) -> str:
    response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    if response and response.status in (403, 429):
        raise BlockedError(f"HTTP {response.status}: доступ ограничен")
    try:
        page.locator("article, [data-index], [data-marker='item'], [data-auto='searchOrganic']").first.wait_for(timeout=12000)
    except Exception:
        pass  # Некоторые площадки используют другую разметку или возвращают пустую выдачу.
    for _ in range(3):
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(500)
    html = page.content()
    if re.search(r"captcha|smartcaptcha|verify you are human|access denied|докажите, что вы не робот|подтвердите, что вы не робот", page.title(), re.I):
        raise BlockedError("Обнаружена проверка человека; сбор остановлен для этой страницы")
    return html


def write_output(path: Path, rows: list[Listing]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        if path.suffix.lower() == ".json":
            json.dump([asdict(row) for row in rows], file, ensure_ascii=False, indent=2)
        else:
            writer = csv.DictWriter(file, fieldnames=list(Listing.__dataclass_fields__))
            writer.writeheader()
            writer.writerows(asdict(row) for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("--sites", nargs="+", choices=list(BASE), default=list(BASE))
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("results.csv"))
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.pages <= 10 or args.delay < 0:
        parser.error("--pages должен быть от 1 до 10, --delay не может быть отрицательным")
    if args.output.suffix.lower() not in {".csv", ".json"}:
        parser.error("--output должен заканчиваться на .csv или .json")

    results: dict[str, Listing] = {}
    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch(headless=not args.headed, **browser_options())
        try:
            context = browser.new_context(locale="ru-RU", viewport={"width": 1366, "height": 900})
            page = context.new_page()
            for site in args.sites:
                for number in range(1, args.pages + 1):
                    try:
                        html = load_page(page, search_url(site, args.query, number))
                        rows = extract(site, args.query, html)
                        for row in rows:
                            results.setdefault(row.url, row)
                        print(f"{site}, страница {number}: {len(rows)} карточек", file=sys.stderr)
                    except Exception as exc:
                        print(f"{site}, страница {number}: {exc}", file=sys.stderr)
                    if number < args.pages:
                        time.sleep(args.delay)
            context.close()
        finally:
            browser.close()
    write_output(args.output, list(results.values()))
    print(f"Сохранено {len(results)} карточек: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
