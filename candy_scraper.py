#!/usr/bin/env python3
"""
Candy.ai Live Action Scraper

Navigates to each character's /live-actions page, clicks each free
request row, intercepts the video from the CDN, and downloads it.

Handles both UI versions:
  - Beta:   rows show "Free" or token cost (number)
  - Beta v2: rows show pink play button or lock icon + "Level N"
"""

import asyncio
import re
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, Dict, List

import requests
from playwright.async_api import async_playwright, Page, BrowserContext, Response

# ---------------------------------------------------------------------------

BASE_URL = "https://candy.ai"
OUTPUT_DIR = Path.home() / "Desktop"

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

MAX_PHASE2 = 100

# ---------------------------------------------------------------------------


def slugify(text):
    text = text.strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s]+", "_", text)
    return text


def is_cdn_video(url):
    return "private-cdn.candy.ai/videos/" in url or "cdn.candy.ai/" in url


def download(url, dest, cookies=None):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > MIN_VIDEO_SIZE:
        print(f"      [skip] {dest.name}")
        return
    s = requests.Session()
    if cookies:
        for k, v in cookies.items():
            s.cookies.set(k, v)
    h = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": BASE_URL + "/",
    }
    try:
        r = s.get(url, headers=h, stream=True, timeout=60)
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


# ---------------------------------------------------------------------------
# Phase 1
# ---------------------------------------------------------------------------

async def get_free_requests(page):
    """
    Find all free request rows in the right panel.

    The right panel is the area with "Live Action" header.
    Inside it, each request is a row/card with:
      - A text label (the action name)
      - Either "Free" / pink play button (= free, click it)
      - Or a token cost / lock icon + "Level N" (= paid, skip)

    Returns list of {text, index} for free rows only.
    """
    return await page.evaluate("""
        () => {
            // Find the Live Action panel - it contains "Live Action" text
            // The request rows are inside this panel, below the header/level info
            const all = [...document.querySelectorAll('*')];

            // Step 1: Find all text nodes that look like action names.
            // Action names are things like "Tease me", "Smile", "Dance for me", etc.
            // They sit inside rows that are stacked vertically in the right panel.
            //
            // The simplest approach: find the scrollable container in the right
            // panel and get its direct-ish row children.

            // Find elements containing "Live Action" to locate the panel
            let panel = null;
            for (const el of all) {
                if (el.childNodes.length < 20 &&
                    el.textContent.includes('Live Action') &&
                    el.getBoundingClientRect().width > 300) {
                    const rect = el.getBoundingClientRect();
                    // The panel is on the right side of the page
                    if (rect.left > 600) {
                        panel = el;
                    }
                }
            }

            // If we can't find the panel precisely, just look at the right
            // half of the page for row-like elements
            const rows = [];
            const candidates = document.querySelectorAll('div, li, a, button');

            for (const el of candidates) {
                const rect = el.getBoundingClientRect();

                // Must be in the right portion of the page (right panel)
                if (rect.left < 700) continue;

                // Row-like: wide and short
                if (rect.width < 250 || rect.width > 500) continue;
                if (rect.height < 35 || rect.height > 80) continue;

                // Must have text content that looks like an action name
                const text = el.textContent.trim();
                if (text.length < 3 || text.length > 80) continue;

                // Skip headers, level info, buttons that aren't action rows
                const lower = text.toLowerCase();
                if (lower.includes('live action')) continue;
                if (lower.includes('tokens balance')) continue;
                if (lower.includes('get tokens')) continue;
                if (lower.includes('skip to level')) continue;
                if (lower.match(/^\\d+\\s*\\/\\s*\\d+\\s*xp/)) continue;
                if (lower.match(/^level\\s*\\d+$/)) continue;
                if (lower.match(/^\\d+$/)) continue;

                // Extract just the action name (first line of text, without
                // "Free" or number suffixes)
                let actionName = text.split('\\n')[0].trim();
                // Remove trailing "Free" or number
                actionName = actionName.replace(/\\s*(Free|\\d+)\\s*$/, '').trim();

                if (actionName.length < 3) continue;

                // Determine if free or paid
                const isFree = (
                    text.includes('Free') ||
                    // Beta v2: has pink play button (SVG) but NOT "Level" text
                    (el.querySelector('svg') !== null && !text.includes('Level'))
                );

                // Paid: has "Level N" text or a number (token cost) without "Free"
                const isPaid = (
                    text.includes('Level') ||
                    (!text.includes('Free') && !el.querySelector('svg'))
                );

                rows.push({
                    actionName: actionName,
                    isFree: isFree,
                    x: Math.round(rect.x + rect.width / 2),
                    y: Math.round(rect.y + rect.height / 2),
                    rawText: text.substring(0, 60),
                });
            }

            // Deduplicate by action name (keep first occurrence)
            const seen = new Set();
            const unique = [];
            for (const r of rows) {
                const key = r.actionName.toLowerCase();
                if (seen.has(key)) continue;
                seen.add(key);
                unique.push(r);
            }

            return unique;
        }
    """)


async def scrape_character(page, slug, name, cookies):
    url = f"{BASE_URL}/ai-girlfriend/{slug}/live-actions?source=home_live_section"
    out_dir = OUTPUT_DIR / "Live Action" / name

    print(f"\n{'='*60}")
    print(f"  {name} -- {url}")
    print(f"{'='*60}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"  [FAIL] Page load: {e}")
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

    # Scroll the right panel to ensure all rows are loaded
    try:
        await page.evaluate("""
            () => {
                const panels = document.querySelectorAll('div');
                for (const p of panels) {
                    const r = p.getBoundingClientRect();
                    if (r.left > 700 && r.height > 400 && p.scrollHeight > p.clientHeight) {
                        p.scrollTop = p.scrollHeight;
                    }
                }
            }
        """)
        await page.wait_for_timeout(1000)
        await page.evaluate("""
            () => {
                const panels = document.querySelectorAll('div');
                for (const p of panels) {
                    const r = p.getBoundingClientRect();
                    if (r.left > 700 && r.height > 400 && p.scrollHeight > p.clientHeight) {
                        p.scrollTop = 0;
                    }
                }
            }
        """)
        await page.wait_for_timeout(500)
    except Exception:
        pass

    rows = await get_free_requests(page)

    free = [r for r in rows if r["isFree"]]
    paid = [r for r in rows if not r["isFree"]]

    if paid:
        print(f"  Paid (skipping): {', '.join(r['actionName'] for r in paid)}")

    if not free:
        print(f"  [WARN] No free requests found")
        print(f"  All rows detected: {rows}")
        return

    print(f"  Free requests ({len(free)}):")
    for r in free:
        print(f"    - {r['actionName']}")

    out_dir.mkdir(parents=True, exist_ok=True)

    for idx, row in enumerate(free):
        action = row["actionName"]
        fname = slugify(action) + ".mp4"
        dest = out_dir / fname

        if dest.exists() and dest.stat().st_size > MIN_VIDEO_SIZE:
            print(f"    [{idx+1}/{len(free)}] [skip] {action}")
            continue

        print(f"    [{idx+1}/{len(free)}] {action}")

        # Interceptor
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

        # Reset video
        try:
            await page.evaluate("""
                () => { document.querySelectorAll('video').forEach(v => {
                    v.pause(); v.removeAttribute('src'); v.load();
                }); }
            """)
        except Exception:
            pass

        # Click the row
        try:
            await page.mouse.click(row["x"], row["y"])
        except Exception as e:
            print(f"      [WARN] Click failed: {e}")
            page.remove_listener("response", on_resp)
            continue

        # Wait for video
        try:
            await asyncio.wait_for(event.wait(), timeout=8.0)
        except asyncio.TimeoutError:
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
            short = captured_url.split("?")[0].split("/")[-1]
            print(f"      -> {short}")
            download(captured_url, dest, cookies)
        else:
            print(f"      [WARN] No video captured")

        await page.wait_for_timeout(2000)


async def run_phase1(ctx, cookies):
    print("\n" + "=" * 60)
    print("  PHASE 1 -- LIVE ACTION")
    print("=" * 60)
    page = await ctx.new_page()
    for slug, name in CHARACTERS:
        await scrape_character(page, slug, name, cookies)
    await page.close()
    print("\n  Phase 1 complete!")


# ---------------------------------------------------------------------------
# Phase 2
# ---------------------------------------------------------------------------

async def collect_char_urls(page):
    print("\n  Collecting character URLs...")
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)
    for _ in range(10):
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(1000)

    links = await page.evaluate("""
        () => {
            const s = new Set();
            document.querySelectorAll('a[href]').forEach(a => {
                const m = a.href.match(/\\/ai-girlfriend\\/([a-z0-9-]+)/);
                if (m && !a.href.includes('/live-actions'))
                    s.add('https://candy.ai/ai-girlfriend/' + m[1]);
            });
            return [...s];
        }
    """)
    valid = []
    for u in links:
        parts = urlparse(u).path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "ai-girlfriend":
            if re.match(r"^[a-z0-9-]+$", parts[1]):
                valid.append(u)
        if len(valid) >= MAX_PHASE2:
            break
    print(f"  Found {len(valid)} characters")
    return valid[:MAX_PHASE2]


async def scrape_profile(page, url, cookies):
    slug = url.rstrip("/").split("/")[-1]
    name = slug.replace("-", " ").title()
    base = OUTPUT_DIR / "Candy AI Characters" / name
    print(f"\n  {name}")

    assets = []

    async def on_resp(r):
        u = r.url
        if "cdn.candy.ai" in u or "private-cdn.candy.ai" in u:
            assets.append((u, r.headers.get("content-type", "")))

    page.on("response", on_resp)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        for _ in range(5):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(800)
    except Exception as e:
        print(f"    [FAIL] {e}")
        page.remove_listener("response", on_resp)
        return

    dom = await page.evaluate("""
        () => {
            const u = new Set();
            document.querySelectorAll('img[src]').forEach(e => {
                if (e.src.includes('cdn.candy.ai')) u.add(e.src+'|image');
            });
            document.querySelectorAll('video source[src],video[src]').forEach(e => {
                const s = e.src||e.getAttribute('src');
                if (s && s.includes('cdn.candy.ai')) u.add(s+'|video');
            });
            return [...u];
        }
    """)
    page.remove_listener("response", on_resp)

    for entry in dom:
        u, k = entry.rsplit("|", 1)
        assets.append((u, "image/jpeg" if k == "image" else "video/mp4"))

    seen = set()
    uniq = []
    for u, ct in assets:
        c = u.split("?")[0]
        if c not in seen:
            seen.add(c)
            uniq.append((u, ct))

    ic = vc = 0
    for u, ct in uniq:
        p = urlparse(u).path
        ext = Path(p).suffix.lower()
        if "video" in ct or ext in {".mp4", ".webm"}:
            fn = Path(p).name or f"video_{vc}.mp4"
            download(u, base / "videos" / fn, cookies)
            vc += 1
        elif "image" in ct or ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            fn = Path(p).name or f"image_{ic}.jpg"
            download(u, base / "imagenes" / fn, cookies)
            ic += 1
    print(f"    {ic} images, {vc} videos")


async def run_phase2(ctx, cookies):
    print("\n" + "=" * 60)
    print("  PHASE 2 -- CHARACTER PROFILES")
    print("=" * 60)
    page = await ctx.new_page()
    urls = await collect_char_urls(page)
    for i, u in enumerate(urls):
        print(f"\n  [{i+1}/{len(urls)}]", end="")
        await scrape_profile(page, u, cookies)
    await page.close()
    print("\n  Phase 2 complete!")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print("Candy.ai Scraper")
    print("=" * 60)

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

        print("\nLog in to Candy.ai in the browser window.")
        input("\nPress ENTER once logged in... ")
        await page.close()

        cookies = await ctx.cookies()
        jar = {c["name"]: c["value"] for c in cookies}
        print(f"Captured {len(jar)} cookies\n")

        await run_phase1(ctx, jar)
        await run_phase2(ctx, jar)

        await browser.close()

    print("\n" + "=" * 60)
    print("DONE!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
