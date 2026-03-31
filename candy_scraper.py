#!/usr/bin/env python3
"""
Candy.ai Live Action Scraper

For each character: navigate to /live-actions, click every request row,
if a video plays -> download it. If not (paywall popup) -> dismiss and move on.
"""

import asyncio
import re
from pathlib import Path
import requests
from playwright.async_api import async_playwright, Page, Response

BASE_URL = "https://candy.ai"
OUTPUT_DIR = Path.home() / "Desktop" / "Live Action"

CHARACTERS = [
    ("coco-bailey", "Coco"),
    ("darkangel666", "Darkangel666"),
    ("emilia-vermont", "Emilia"),
    ("elodie-valmont", "Elodie"),
    ("mila-nowak", "Mila"),
    ("isabella-torres", "Isabella"),
    ("olivia-carter", "Olivia"),
    ("katarina-sommerfeld", "Katarina"),
    ("luna-moreno-2", "Luna"),
    ("irina-konstantinov", "Irina"),
]

VIDEO_CONTENT_TYPES = {"video/mp4", "video/webm", "application/octet-stream"}
MIN_VIDEO_SIZE = 200 * 1024


def slugify(text):
    text = re.sub(r"[^\w\s-]", "", text.strip())
    return re.sub(r"\s+", "_", text)


def is_cdn_video(url):
    return "private-cdn.candy.ai/videos/" in url or "cdn.candy.ai/" in url


def download(url, dest, cookies):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > MIN_VIDEO_SIZE:
        print(f"      [skip] already have {dest.name}")
        return
    s = requests.Session()
    for k, v in cookies.items():
        s.cookies.set(k, v)
    try:
        r = s.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": BASE_URL + "/",
        }, stream=True, timeout=60)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        mb = dest.stat().st_size / (1024 * 1024)
        print(f"      [OK] {dest.name} ({mb:.1f} MB)")
    except Exception as e:
        print(f"      [FAIL] {dest.name}: {e}")
        if dest.exists():
            dest.unlink()


async def dismiss_popups(page):
    """Close any paywall/token popup that might appear after clicking a paid request."""
    try:
        # Try clicking any X/close button that appeared
        for selector in [
            "button:has-text('Close')",
            "button:has-text('close')",
            "[aria-label='Close']",
            "[aria-label='close']",
            "button:has-text('Cancel')",
            "button:has-text('No thanks')",
            "button:has-text('Maybe later')",
        ]:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=500):
                await loc.click(timeout=2000)
                await page.wait_for_timeout(500)
                return True
    except Exception:
        pass

    # Try pressing Escape
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
    except Exception:
        pass

    # Click outside any modal (top-left corner of the page)
    try:
        await page.mouse.click(100, 100)
        await page.wait_for_timeout(500)
    except Exception:
        pass

    return False


async def get_request_rows(page):
    """
    Find ALL request rows in the right panel.
    Each row has an action name like "Tease me", "Dance for me", etc.
    No filtering -- we click them all and see what happens.
    """
    return await page.evaluate("""
        () => {
            const rows = [];
            const seen = new Set();
            const allDivs = document.querySelectorAll('div, li, a, button');

            for (const el of allDivs) {
                const rect = el.getBoundingClientRect();

                // Right panel rows: x > 700, reasonable row size
                if (rect.left < 700) continue;
                if (rect.width < 250 || rect.width > 500) continue;
                if (rect.height < 35 || rect.height > 80) continue;

                const text = el.textContent.trim();
                if (text.length < 3 || text.length > 100) continue;

                // Extract action name: first line, strip trailing noise
                let name = text.split('\\n')[0].trim();
                // Remove trailing "Free", "Request", or numbers
                name = name.replace(/\\s*(Free|Request|\\d+)\\s*$/i, '').trim();
                if (name.length < 3) continue;

                // Skip non-action rows
                const lower = name.toLowerCase();
                if (lower.includes('live action')) continue;
                if (lower.includes('tokens balance')) continue;
                if (lower.includes('get tokens')) continue;
                if (lower.includes('skip to level')) continue;
                if (lower.includes('beta')) continue;
                if (lower.match(/^\\d+\\s*\\/\\s*\\d+/)) continue;  // "5 / 200 XP"
                if (lower.match(/^level\\s*\\d+$/)) continue;

                // Deduplicate
                const key = name.toLowerCase();
                if (seen.has(key)) continue;
                seen.add(key);

                rows.push({
                    name: name,
                    x: Math.round(rect.x + rect.width / 2),
                    y: Math.round(rect.y + rect.height / 2),
                });
            }
            return rows;
        }
    """)


async def scrape_character(page, slug, char_name, cookies):
    url = f"{BASE_URL}/ai-girlfriend/{slug}/live-actions?source=home_live_section"
    out_dir = OUTPUT_DIR / char_name

    print(f"\n{'='*60}")
    print(f"  {char_name}")
    print(f"{'='*60}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"  [FAIL] {e}")
        return

    await page.wait_for_timeout(4000)

    # Click "Tap to start" if present
    for pat in ["Tap to start", "TAP TO START"]:
        try:
            loc = page.get_by_text(pat, exact=False).first
            if await loc.is_visible(timeout=2000):
                await loc.click(timeout=5000)
                print(f"  Clicked '{pat}'")
                await page.wait_for_timeout(3000)
                break
        except Exception:
            continue

    rows = await get_request_rows(page)

    if not rows:
        print(f"  [WARN] No request rows found on page")
        return

    print(f"  Found {len(rows)} requests:")
    for r in rows:
        print(f"    - {r['name']}")

    out_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0

    for idx, row in enumerate(rows):
        name = row["name"]
        fname = slugify(name) + ".mp4"
        dest = out_dir / fname

        if dest.exists() and dest.stat().st_size > MIN_VIDEO_SIZE:
            print(f"    [{idx+1}/{len(rows)}] [skip] {name}")
            downloaded += 1
            continue

        print(f"    [{idx+1}/{len(rows)}] {name} ...", end=" ", flush=True)

        # Set up network interceptor BEFORE clicking
        captured_url = None
        event = asyncio.Event()

        async def on_resp(resp):
            nonlocal captured_url
            if captured_url:
                return
            u = resp.url
            if not is_cdn_video(u):
                return
            try:
                ct = resp.headers.get("content-type", "")
                cl = resp.headers.get("content-length", "0")
                if ct in VIDEO_CONTENT_TYPES or int(cl) > MIN_VIDEO_SIZE:
                    captured_url = u
                    event.set()
            except Exception:
                if "/videos/" in u:
                    captured_url = u
                    event.set()

        page.on("response", on_resp)

        # Reset video player
        try:
            await page.evaluate("""
                () => { document.querySelectorAll('video').forEach(v => {
                    v.pause(); v.removeAttribute('src'); v.load();
                }); }
            """)
        except Exception:
            pass

        # Click the request row
        try:
            await page.mouse.click(row["x"], row["y"])
        except Exception as e:
            print(f"click failed: {e}")
            page.remove_listener("response", on_resp)
            continue

        # Wait for video from CDN (up to 6 seconds)
        try:
            await asyncio.wait_for(event.wait(), timeout=6.0)
        except asyncio.TimeoutError:
            # Fallback: check DOM video element
            try:
                src = await page.evaluate("""
                    () => {
                        const v = document.querySelector('video');
                        return v ? (v.src || v.currentSrc || '') : '';
                    }
                """)
                if src and is_cdn_video(src):
                    captured_url = src
            except Exception:
                pass

        page.remove_listener("response", on_resp)

        if captured_url:
            print("video found!")
            download(captured_url, dest, cookies)
            downloaded += 1
        else:
            print("no video (paid?) -- dismissing popup")
            await dismiss_popups(page)

        await page.wait_for_timeout(2000)

    print(f"\n  {char_name}: {downloaded}/{len(rows)} videos downloaded")


async def main():
    print("Candy.ai Live Action Scraper")
    print("=" * 60)
    print(f"Output: {OUTPUT_DIR}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = await ctx.new_page()
        await page.goto(BASE_URL, wait_until="domcontentloaded")

        print("Log in to Candy.ai in the browser window.")
        input("\nPress ENTER once logged in... ")
        await page.close()

        cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
        print(f"Captured {len(cookies)} cookies\n")

        print("=" * 60)
        print("  PHASE 1 -- LIVE ACTION")
        print("=" * 60)

        page = await ctx.new_page()
        for slug, name in CHARACTERS:
            await scrape_character(page, slug, name, cookies)
        await page.close()

        await browser.close()

    print("\n" + "=" * 60)
    print("DONE!")
    print(f"Output: {OUTPUT_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
