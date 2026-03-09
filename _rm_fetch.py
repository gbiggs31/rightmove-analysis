import asyncio
    import json
    import re
    from playwright.async_api import async_playwright

    JS_SCRAPE_CARDS = "\n() => {\n    const cards = document.querySelectorAll('[data-testid^=\"propertyCard-\"]');\n    return Array.from(cards).map(card => {\n        const getText = (sel) => { const el = card.querySelector(sel); return el ? el.textContent.trim() : null; };\n        const getAttr = (sel, attr) => { const el = card.querySelector(sel); return el ? el.getAttribute(attr) : null; };\n        return {\n            id:            card.querySelector('a[id^=\"prop\"]') ? card.querySelector('a[id^=\"prop\"]').id.replace('prop','') : null,\n            price:         getText('.PropertyPrice_price__VL65t'),\n            address:       getAttr('address', 'aria-label'),\n            property_type: getText('.PropertyInformation_propertyType__u8e76'),\n            bedrooms:      getText('.PropertyInformation_bedroomsCount___2b5R'),\n            bathrooms:     getText('.PropertyInformation_bathContainer__ut8VY span[aria-label]'),\n            description:   getText('[data-testid=\"property-description\"]'),\n            added:         getText('.MarketedBy_addedOrReduced__Vtc9o'),\n            agent:         getText('.MarketedBy_joinedText__HTONp'),\n            url:           getAttr('a[data-testid=\"property-details-lozenge\"]', 'href'),\n        };\n    });\n}\n"
    JS_GET_TOTAL = "\n() => {\n    const el = document.querySelector('[data-testid=\"total-results\"]');\n    return el ? el.textContent.trim() : null;\n}\n"
    RESULTS_PER_PAGE = 24

    async def scrape():
        all_properties = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            base_url = (
                "https://www.rightmove.co.uk/property-for-sale/find.html"
                "?locationIdentifier=REGION%5E732&sortType=6&channel=BUY&radius=1.0"
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
            total_pages = min(5, -(-total // RESULTS_PER_PAGE))
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

    asyncio.run(scrape())