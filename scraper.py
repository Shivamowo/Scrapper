import json
import re
import csv
import os
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import cycle
import requests
from bs4 import BeautifulSoup
import pandas as pd

BASE_URL = "https://www.ambitionbox.com"
LISTING_URL = "https://www.ambitionbox.com/list-of-companies?campaign=desktop_nav&page={page}"
TOTAL_PAGES = 5
MAX_COMPANIES = 50
MAX_WORKERS = 10
OUTPUT_CSV = "companies.csv"
TIMEOUT = 20
DELAY_MIN = 0.8
DELAY_MAX = 1.8
RATING_SUFFIX = "Rating"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]
_ua = cycle(USER_AGENTS)

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "max-age=0",
        "Upgrade-Insecure-Requests": "1",
        "Referer": BASE_URL,
    })
    return s

def _get_ua() -> str:
    return next(_ua)

def fetch_html(url: str, session: requests.Session) -> str | None:
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
    session.headers["User-Agent"] = _get_ua()
    session.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    try:
        r = session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text
    except requests.RequestException:
        return None

def fetch_json(url: str, session: requests.Session) -> dict | None:
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
    session.headers["User-Agent"] = _get_ua()
    session.headers["Accept"] = "application/json, text/plain, */*"
    try:
        r = session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, json.JSONDecodeError):
        return None

def extract_next_data(html: str) -> dict:
    try:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if tag and tag.string:
            return json.loads(tag.string)
    except Exception:
        pass
    return {}

def get_build_id(session: requests.Session) -> str | None:
    html = fetch_html(BASE_URL, session)
    if not html:
        return None
    nd = extract_next_data(html)
    return nd.get("buildId")

def iter_companies_from_listing(session: requests.Session):
    seen: set[str] = set()
    count: int = 0

    for page in range(1, TOTAL_PAGES + 1):
        url = LISTING_URL.format(page=page)
        print(f"Scraping listing page {page} -> {url}")
        
        html = fetch_html(url, session)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")
        wrappers = soup.select("div.companyCardWrapper")
        cards = [c for c in wrappers if c.find("meta", itemprop="url")]

        if not cards:
            continue

        for card in cards:
            try:
                url_meta = card.find("meta", itemprop="url")
                profile_url = (url_meta or {}).get("content", "N/A")
                if profile_url and not profile_url.startswith("http"):
                    profile_url = BASE_URL + profile_url

                if profile_url in seen or profile_url == "N/A":
                    continue
                seen.add(profile_url)

                m = re.search(r"/overview/(.+?)-overview$", profile_url)
                slug = m.group(1) if m else None
                if not slug:
                    continue

                h2 = card.find("h2", class_="companyCardWrapper__companyName")
                name = (h2.get("title") or h2.get_text(strip=True)) if h2 else "N/A"
                if name == "N/A":
                    alt = card.find("meta", itemprop="alternateName")
                    name = alt["content"] if alt else slug.replace("-", " ").title()

                yield name, profile_url, slug
                count += 1
                if count >= MAX_COMPANIES:
                    return

            except Exception:
                pass

def scrape_company_profile(name: str, profile_url: str, slug: str, build_id: str, session: requests.Session) -> dict:
    data: dict = {
        "company_name": name,
        "profile_url": profile_url,
        "overall_rating": "N/A",
        "total_reviews": "N/A",
        "industry": "N/A",
        "description": "N/A",
    }

    api_url = f"{BASE_URL}/_next/data/{build_id}/overview/{slug}-overview.json"
    payload = fetch_json(api_url, session)
    
    if not payload:
        return data

    page_props = payload.get("pageProps", {})
    ccd_list = page_props.get("compareCompaniesData", [])
    company = ccd_list[0] if ccd_list else {}
    
    header_data = page_props.get("companyHeaderData", {})
    agg_ratings = page_props.get("aggregatedRatingsData", {})

    try:
        cname = (
            company.get("name") 
            or company.get("companyName") 
            or page_props.get("companyName")
            or name
        )
        data["company_name"] = str(cname).strip()
    except Exception:
        pass

    try:
        r = (
            company.get("ratingOneDecimal")
            or company.get("rating")
            or company.get("ambitionScore")
            or header_data.get("rating")
        )
        if r is not None:
            data["overall_rating"] = str(r)
        else:
            ocr = company.get("overallCompanyRating", {})
            if isinstance(ocr, dict):
                trend = ocr.get("trend", {})
                if trend:
                    data["overall_rating"] = str(list(trend.values())[-1])
    except Exception:
        pass

    try:
        rv = (
            company.get("companyReviewsLive")
            or company.get("totalReviews")
            or company.get("reviewCount")
            or header_data.get("reviewsCount")
        )
        if rv is not None:
            data["total_reviews"] = str(rv)
    except Exception:
        pass

    try:
        ind = (
            company.get("primaryIndustryName")
            or company.get("industryName")
            or company.get("industry")
        )
        if not ind:
            industry_arr = header_data.get("industry", [])
            if industry_arr and isinstance(industry_arr, list):
                ind = industry_arr[0].get("name")
        if ind:
            data["industry"] = str(ind).strip()
    except Exception:
        pass

    try:
        about = (
            company.get("aboutUs") 
            or company.get("description") 
            or company.get("about")
            or header_data.get("aboutCompany")
        )
        if about:
            clean = re.sub(r"<[^>]+>", " ", str(about))
            data["description"] = " ".join(clean.split())
    except Exception:
        pass

    try:
        ratings_source = {}
        if company:
            ratings_source = company
        else:
            rd = agg_ratings.get("ratingDistribution", {}).get("data", {})
            ratings_source = rd.get("ratingsTwoDecimal") or rd.get("ratings") or {}

        for key, value in ratings_source.items():
            if not isinstance(key, str) or not key.endswith(RATING_SUFFIX):
                continue
            if key in ("overallCompanyRating",):
                continue
            try:
                label = re.sub(r"Rating$", "", key)            
                label = re.sub(r"([A-Z])", r"_\1", label)      
                col = "rating_" + label.lower().strip("_")   

                if isinstance(value, dict):
                    trend = value.get("trendTwoDecimal") or value.get("trend") or {}
                    if trend:
                        latest_val = list(trend.values())[-1]
                        data[col] = str(round(float(latest_val), 2))
                elif value is not None:
                    data[col] = str(round(float(value), 2))
            except Exception:
                continue
    except Exception:
        pass

    return data

_header_written = False

def save_row_to_csv(row: dict, filepath: str, lock: threading.Lock) -> None:
    global _header_written
    with lock:
        write_header = not os.path.isfile(filepath) or not _header_written
        with open(filepath, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()), extrasaction="ignore")
            if write_header:
                w.writeheader()
                _header_written = True
            w.writerow(row)

def normalise_and_save(rows: list[dict], filepath: str) -> None:
    df = pd.DataFrame(rows).fillna("N/A")

    fixed = ["company_name", "profile_url", "overall_rating", "total_reviews", "industry", "description"]
    rating = sorted(c for c in df.columns if c.startswith("rating_"))
    other = [c for c in df.columns if c not in fixed and c not in rating]
    order = [c for c in fixed if c in df.columns] + rating + other

    df.reindex(columns=order).to_csv(filepath, index=False, encoding="utf-8")

def _worker(args: tuple, build_id: str, lock: threading.Lock) -> dict:
    name, profile_url, slug = args
    session = make_session()
    row = scrape_company_profile(name, profile_url, slug, build_id, session)
    save_row_to_csv(row, OUTPUT_CSV, lock)
    return row

def main() -> None:
    if os.path.isfile(OUTPUT_CSV):
        os.remove(OUTPUT_CSV)

    bootstrap = make_session()
    build_id = get_build_id(bootstrap)
    if not build_id:
        return

    companies: list[tuple[str, str, str]] = []
    for name, url, slug in iter_companies_from_listing(bootstrap):
        companies.append((name, url, slug))
        if len(companies) >= MAX_COMPANIES:
            break

    if not companies:
        return

    lock = threading.Lock()
    all_rows: list[dict] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_worker, args, build_id, lock): args
            for args in companies
        }
        for future in as_completed(futures):
            name, url, slug = futures[future]
            try:
                row = future.result()
                all_rows.append(row)
                print(f"Scraped profile [{len(all_rows)}/{len(companies)}]: {row.get('company_name', name)}")
            except Exception:
                pass

    normalise_and_save(all_rows, OUTPUT_CSV)
    print(f"Saved {len(all_rows)} companies to {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
