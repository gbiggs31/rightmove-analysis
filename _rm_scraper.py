import asyncio
import json
import re
import sys
from playwright.async_api import async_playwright

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

async def scrape(location_id, radius, max_pages):
    all_properties = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        base_url = (
            f"https://www.rightmove.co.uk/property-for-sale/find.html"
            f"?locationIdentifier={location_id}&sortType=6&channel=BUY&radius={radius}"
        )
        await page.goto(base_url + "&index=0", wait_until="domcontentloaded", timeout=30000)
        try:
            await page.click("#onetrust-accept-btn-handler", timeout=5000)
            await asyncio.sleep(1)
        except Exception:
            pass
        await asyncio.sleep(2)
        total_text = await page.evaluate(JS_GET_TOTAL)
        total = int(re.sub(r"[^0-9]", "", total_text)) if total_text else 0
        props = await page.evaluate(JS_SCRAPE_CARDS)
        all_properties.extend(props)
        total_pages = min(max_pages, -(-total // RESULTS_PER_PAGE))
        for page_num in range(2, total_pages + 1):
            index = (page_num - 1) * RESULTS_PER_PAGE
            await asyncio.sleep(2)
            await page.goto(base_url + f"&index={index}", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            props = await page.evaluate(JS_SCRAPE_CARDS)
            all_properties.extend(props)
            if not props:
                break
        await browser.close()
    print(json.dumps(all_properties))

if __name__ == "__main__":
    location_id = sys.argv[1]
    radius      = float(sys.argv[2])
    max_pages   = int(sys.argv[3])
    asyncio.run(scrape(location_id, radius, max_pages))