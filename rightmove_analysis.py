"""
Rightmove Property Analysis
============================
Usage:
    python rightmove_analysis.py
    python rightmove_analysis.py --location "Oxford" --radius 3 --max-pages 5
    python rightmove_analysis.py --location "NN15" --radius 2 --output results.csv
"""

import argparse
import asyncio
import json
import re
import sys
import subprocess
import os

import pandas as pd
import requests
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.rightmove.co.uk/",
}

RESULTS_PER_PAGE = 24

JS_SCRAPE_CARDS = """
() => {
    const cards = document.querySelectorAll('[data-testid^="propertyCard-"]');
    return Array.from(cards).map(card => {
        const getText = (sel) => { const el = card.querySelector(sel); return el ? el.textContent.trim() : null; };
        const getAttr = (sel, attr) => { const el = card.querySelector(sel); return el ? el.getAttribute(attr) : null; };
        return {
            id:            card.querySelector('a[id^="prop"]') ? card.querySelector('a[id^="prop"]').id.replace('prop','') : null,
            price:         getText('.PropertyPrice_price__VL65t'),
            address:       getAttr('address', 'aria-label'),
            property_type: getText('.PropertyInformation_propertyType__u8e76'),
            bedrooms:      getText('.PropertyInformation_bedroomsCount___2b5R'),
            bathrooms:     getText('.PropertyInformation_bathContainer__ut8VY span[aria-label]'),
            description:   getText('[data-testid="property-description"]'),
            added:         getText('.MarketedBy_addedOrReduced__Vtc9o'),
            agent:         getText('.MarketedBy_joinedText__HTONp'),
            url:           getAttr('a[data-testid="property-details-lozenge"]', 'href'),
        };
    });
}
"""

JS_GET_TOTAL = """
() => {
    const el = document.querySelector('[data-testid="total-results"]');
    return el ? el.textContent.trim() : null;
}
"""

# ---------------------------------------------------------------------------
# Step 1: Resolve location name -> Rightmove locationIdentifier
# ---------------------------------------------------------------------------

def _tokenise(query: str) -> str:
    query = query.upper().replace(" ", "")
    tokens = [query[i:i+2] for i in range(0, len(query), 2)]
    return "/".join(tokens)


def resolve_location(query: str) -> tuple[str, str]:
    """
    Returns (locationIdentifier, display_name) for the best match.
    locationIdentifier is URL-encoded (^ -> %5E) ready for use in URLs.
    """
    tokenised = _tokenise(query)
    url = f"https://www.rightmove.co.uk/typeAhead/uknostreet/{tokenised}/"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    locations = data.get("typeAheadLocations", [])
    if not locations:
        raise ValueError(
            f"No Rightmove location found for '{query}'. "
            "Try a broader term e.g. town name rather than full postcode."
        )

    best = locations[0]
    raw_id = best["locationIdentifier"]   # e.g. REGION^732
    url_id = raw_id.replace("^", "%5E")   # e.g. REGION%5E732
    return url_id, best["displayName"]


# ---------------------------------------------------------------------------
# Step 2: Scrape listings via Playwright
# ---------------------------------------------------------------------------

async def _scrape(location_id: str, location_name: str, radius: float, max_pages: int) -> list[dict]:
    all_properties = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        base_url = (
            f"https://www.rightmove.co.uk/property-for-sale/find.html"
            f"?locationIdentifier={location_id}&sortType=6&channel=BUY&radius={radius}"
        )

        # Page 1
        print(f"  Fetching page 1 for {location_name}...", flush=True)
        await page.goto(f"{base_url}&index=0", wait_until="domcontentloaded", timeout=30000)

        # Accept cookies once
        try:
            await page.click("#onetrust-accept-btn-handler", timeout=5000)
            await asyncio.sleep(1)
        except Exception:
            pass

        await asyncio.sleep(2)

        # Get total listing count
        total_text = await page.evaluate(JS_GET_TOTAL)
        total = int(re.sub(r"[^0-9]", "", total_text)) if total_text else 0
        print(f"  Total listings available: {total}", flush=True)

        props = await page.evaluate(JS_SCRAPE_CARDS)
        all_properties.extend(props)
        print(f"  Page 1: {len(props)} listings", flush=True)

        # Remaining pages
        total_pages = min(max_pages, -(-total // RESULTS_PER_PAGE))
        for page_num in range(2, total_pages + 1):
            index = (page_num - 1) * RESULTS_PER_PAGE
            print(f"  Fetching page {page_num}/{total_pages}...", flush=True)
            await asyncio.sleep(2)
            await page.goto(f"{base_url}&index={index}", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            props = await page.evaluate(JS_SCRAPE_CARDS)
            all_properties.extend(props)
            print(f"  Page {page_num}: {len(props)} listings", flush=True)
            if not props:
                break

        await browser.close()

    return all_properties


def fetch_listings(location_id: str, location_name: str, radius: float, max_pages: int) -> list[dict]:
    scraper = os.path.join(os.path.dirname(__file__), "_rm_scraper.py")
    result = subprocess.run(
        [sys.executable, scraper, location_id, str(radius), str(max_pages)],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        raise RuntimeError(f"Scraper failed:\n{result.stderr[-2000:]}")
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Step 3: Parse into DataFrame
# ---------------------------------------------------------------------------

def parse_sqft(description: str) -> float | None:
    if not description:
        return None
    sqft = re.search(r"([\d,]+\.?\d*)\s*sq\.?\s*ft", description, re.IGNORECASE)
    sqm  = re.search(r"([\d,]+\.?\d*)\s*sq\.?\s*m(?!ile)", description, re.IGNORECASE)
    if sqft:
        return float(sqft.group(1).replace(",", ""))
    if sqm:
        return round(float(sqm.group(1).replace(",", "")) * 10.764, 1)
    return None


def clean(props: list[dict]) -> pd.DataFrame:
    records = []
    for p in props:
        price_str = p.get("price", "") or ""
        price = int(re.sub(r"[^0-9]", "", price_str)) if price_str else None

        address = (p.get("address") or "").replace("Property address: ", "")

        bath_str = p.get("bathrooms") or ""
        bath_match = re.search(r"\d+", bath_str)

        description = p.get("description")
        size_sqft = parse_sqft(description)

        records.append({
            "id":            p.get("id"),
            "price_gbp":     price,
            "address":       address,
            "property_type": p.get("property_type"),
            "bedrooms":      int(p["bedrooms"]) if p.get("bedrooms") else None,
            "bathrooms":     int(bath_match.group()) if bath_match else None,
            "description":   description,
            "size_sqft":     size_sqft,
            "added":         (p.get("added") or "").replace("Added on ", "").replace("Added ", ""),
            "agent":         p.get("agent"),
            "url":           "https://www.rightmove.co.uk" + p["url"] if p.get("url") else None,
        })

    df = pd.DataFrame(records)
    df["price_per_sqft"] = (df["price_gbp"] / df["size_sqft"]).round(2)
    return df


# ---------------------------------------------------------------------------
# Step 4: Analysis
# ---------------------------------------------------------------------------

def analyse(df: pd.DataFrame, location_name: str) -> None:
    sep = "-" * 60
    print(f"\n{sep}")
    print(f"  RIGHTMOVE ANALYSIS -- {location_name.upper()}")
    print(f"{sep}")
    print(f"  Total listings fetched : {len(df)}")

    priced = df[df["price_gbp"].notna()]
    print(f"  Listings with price    : {len(priced)}")

    sized = df[df["size_sqft"].notna()]
    print(f"  Listings with sq ft    : {len(sized)} ({100*len(sized)//max(len(df),1)}%)")

    if len(priced) == 0:
        print("  No priced listings to analyse.")
        return

    prices = priced["price_gbp"]
    print(f"\n  PRICES (all types)")
    print(f"    Median : {prices.median():>10,.0f}")
    print(f"    Mean   : {prices.mean():>10,.0f}")
    print(f"    Min    : {prices.min():>10,.0f}")
    print(f"    Max    : {prices.max():>10,.0f}")

    print(f"\n  MEDIAN PRICE BY BEDROOMS")
    by_bed = (
        priced[priced["bedrooms"].notna()]
        .groupby("bedrooms")["price_gbp"]
        .agg(["median", "count"])
        .sort_index()
    )
    for beds, row in by_bed.iterrows():
        if row["count"] >= 2:
            print(f"    {int(beds)}-bed : {row['median']:>10,.0f}  (n={int(row['count'])})")

    print(f"\n  MEDIAN PRICE BY TYPE")
    by_type = (
        priced[priced["property_type"].notna()]
        .groupby("property_type")["price_gbp"]
        .agg(["median", "count"])
        .sort_values("median", ascending=False)
    )
    for ptype, row in by_type.iterrows():
        if row["count"] >= 2:
            print(f"    {ptype:<22}: {row['median']:>10,.0f}  (n={int(row['count'])})")

    print(f"\n{sep}\n")


# ---------------------------------------------------------------------------
# CLI + notebook entry point
# ---------------------------------------------------------------------------

def run(location: str = None, radius: float = 1.0, max_pages: int = 10, output: str = None) -> pd.DataFrame:
    """
    Call this from a notebook:
        df = run(location="Kettering", radius=1.0, max_pages=5)
    """
    location_query = location or input("Enter location (town, city or postcode): ").strip()

    print(f"\nResolving location: '{location_query}'...")
    loc_id, loc_name = resolve_location(location_query)
    print(f"  Found: {loc_name} ({loc_id})")

    print(f"\nFetching listings (radius={radius} mi, up to {max_pages} pages)...")
    raw = fetch_listings(loc_id, loc_name, radius=radius, max_pages=max_pages)

    if not raw:
        print("No listings returned.")
        return pd.DataFrame()

    df = clean(raw)
    analyse(df, loc_name)

    if output:
        df.to_csv(output, index=False)
        print(f"Saved to: {output}")

    return df


def main():
    parser = argparse.ArgumentParser(description="Rightmove property analysis")
    parser.add_argument("--location", default=None)
    parser.add_argument("--radius", type=float, default=1.0)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    run(location=args.location, radius=args.radius, max_pages=args.max_pages, output=args.output)


if __name__ == "__main__":
    main()
