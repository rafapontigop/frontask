#!/usr/bin/env python3
"""
Candy.ai Scraper - Playwright + Network Interception

Phase 1: Live Action videos for 10 characters
Phase 2: Character profile assets (images + videos) for first 100 characters

Usage:
    export PATH="$HOME/Library/Python/3.9/bin:$PATH"
    python3 candy_scraper.py
"""

import asyncio
import os
import re
import time
import json
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import Optional, Dict, List

import requests
from playwright.async_api import async_playwright, Page, BrowserContext, Response

# -- Configuration -----------------------------------------------------------

BASE_URL = "https://candy.ai"
OUTPUT_DIR = Path.home() / "Desktop"

LIVE_ACTION_CHARACTERS = [
    "coco-bailey",
    "darkangel666",
    "emilia-vermont",
    "elodie-valmont",
    "mila-nowak",
    "isabella-torres",
    "olivia-carter",
    "katarina-sommerfeld",
    "luna-moreno-2",
    "irina-konstantinov",
]

CDN_PATTERNS = [
    "private-cdn.candy.ai/videos/",
    "cdn.candy.ai/",
]

VIDEO_CONTENT_TYPES = {"video/mp4", "video/webm", "application/octet-stream"}
MIN_VIDEO_SIZE = 200 * 1024  # 200 KB - skip thumbnails/previews

MAX_CHARACTERS_PHASE2 = 100


# -- Helpers -----------------------------------------------------------------

def slugify(text: str) -> str:
    """Convert action text to a safe filename."""
    text = text.strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s]+", "_", text)
    return text


def prettify_name(slug: str) -> str:
    """Convert URL slug to a readable folder name."""
    return slug.replace("-", " ").title().replace(" 2", " 2")


def is_cdn_video_url(url: str) -> bool:
    """Check if URL matches Candy.ai CDN video patterns."""
    return any(pattern in url for pattern in CDN_PATTERNS)


def download_file(url: str, dest: Path, cookies: Optional[Dict] = None):
    """Download a file using requests with cookie auth."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > MIN_VIDEO_SIZE:
        print(f"    [skip] Already exists: {dest.name}")
        return

    session = requests.Session()
    if cookies:
        for name, value in cookies.items():
            session.cookies.set(name, value)

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": BASE_URL + "/",
    }
    try:
        resp = session.get(url, headers=headers, stream=True, timeout=60)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        size_mb = dest.stat().st_size / (1024 * 1024)
        print(f"    [OK] {dest.name} ({size_mb:.1f} MB)")
    except Exception as e:
        print(f"    [FAIL] {dest.name}: {e}")
        if dest.exists():
            dest.unlink()


async def extract_cookies(context: BrowserContext) -> dict:
    """Extract cookies from browser context for requests session."""
    cookies = await context.cookies()
    return {c["name"]: c["value"] for c in cookies}


# -- Phase 1: Live Action ----------------------------------------------------

async def scrape_live_action(page: Page, slug: str, cookies: dict):
    """Scrape all free live-action video clips for one character."""
    char_name = prettify_name(slug)
    out_dir = OUTPUT_DIR / "Live Action" / char_name
    out_dir.mkdir(parents=True, exist_ok=True)

    url = f"{BASE_URL}/ai-girlfriend/{slug}/live-actions?source=home_live_section"
    print(f"\n{'='*60}")
    print(f"  LIVE ACTION -- {char_name}")
    print(f"  {url}")
    print(f"{'='*60}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        print(f"  [FAIL] Could not load page: {e}")
        return

    await page.wait_for_timeout(3000)

    # -- Step 1: Click "Tap to start" to initialize the Live Action player --
    # This overlay appears before the action buttons become interactive.
    tap_clicked = False
    for text_pattern in ["Tap to start", "TAP TO START", "tap to start",
                         "Click to start", "Start"]:
        try:
            locator = page.get_by_text(text_pattern, exact=False).first
            if await locator.is_visible(timeout=2000):
                print(f"  Found '{text_pattern}' -- clicking to initialize player...")
                await locator.click(timeout=5000)
                tap_clicked = True
                await page.wait_for_timeout(3000)
                break
        except Exception:
            continue

    if not tap_clicked:
        # Try clicking any large overlay/splash element covering the video area
        try:
            overlay = page.locator("[class*='overlay'], [class*='splash'], [class*='start']").first
            if await overlay.is_visible(timeout=2000):
                await overlay.click()
                await page.wait_for_timeout(3000)
                print("  Clicked overlay element to start player")
        except Exception:
            print("  No 'Tap to start' overlay found -- player may already be active")

    # -- Step 2: Find action buttons in the RIGHT panel ----------------------
    # The right panel has action cards, each with:
    #   - A text label ("Tease me", "Show me your butt", etc.)
    #   - A pink circular play button with a white triangle/play SVG icon
    #
    # Strategy: find elements in the right half of the viewport that have
    # a play-icon SVG and a short text label. We use position-based filtering
    # to avoid picking up navigation, sidebar, and header elements.

    action_items = await page.evaluate("""
        () => {
            const results = [];
            const vw = window.innerWidth;
            const vh = window.innerHeight;

            // Gather ALL elements that could be action buttons
            // We look for clickable things with SVG icons in the right portion of the page
            const candidates = document.querySelectorAll(
                'button, [role="button"], div[class*="cursor-pointer"], div[class*="clickable"]'
            );

            for (const el of candidates) {
                const rect = el.getBoundingClientRect();

                // Must be visible and reasonably sized
                if (rect.width < 30 || rect.height < 30) continue;
                if (rect.top < 0 || rect.bottom > vh + 100) continue;

                // Skip tiny or huge elements
                if (rect.width > 600 || rect.height > 200) continue;

                // Check for SVG play icon inside (circle + polygon, or path with play shape)
                const svg = el.querySelector('svg');
                const hasPlayClass = el.querySelector('[class*="play"], [class*="Play"]');
                if (!svg && !hasPlayClass) continue;

                // Get the text content (just direct/shallow text, not deeply nested)
                let text = '';
                const textNodes = el.querySelectorAll('span, p, div, h3, h4, h5, label');
                for (const tn of textNodes) {
                    const t = tn.textContent.trim();
                    if (t.length >= 3 && t.length <= 50) { text = t; break; }
                }
                if (!text) {
                    text = el.textContent.trim().split('\\n')[0].trim();
                }
                if (!text || text.length < 3 || text.length > 60) continue;

                // Skip known non-action UI elements
                const lower = text.toLowerCase();
                const skipWords = [
                    'discord', 'companion', 'creative', 'token', 'english',
                    'login', 'sign up', 'sign in', 'menu', 'close', 'back',
                    'home', 'settings', 'profile', 'chat', 'message', 'send',
                    'hi,', 'hello', 'hey', 'subscribe', 'upgrade', 'premium',
                    'free trial', 'cookie', 'accept', 'decline', 'cancel',
                    'tap to start', 'click to start'
                ];
                if (skipWords.some(w => lower.startsWith(w) || lower === w)) continue;

                // Check for lock/premium indicators (skip locked actions)
                const html = el.innerHTML.toLowerCase();
                const isLocked = (
                    html.includes('lock') ||
                    html.includes('locked') ||
                    html.includes('premium') ||
                    el.querySelector('[class*="lock"], [class*="Lock"]') !== null
                );

                results.push({
                    text: text,
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    w: Math.round(rect.width),
                    h: Math.round(rect.height),
                    isLocked: isLocked,
                    tag: el.tagName
                });
            }

            // Deduplicate by text
            const seen = new Set();
            return results.filter(r => {
                const key = r.text.toLowerCase();
                if (seen.has(key)) return false;
                seen.add(key);
                return true;
            });
        }
    """)

    # Filter locked vs free
    free_actions = [a for a in action_items if not a.get("isLocked")]
    locked_actions = [a for a in action_items if a.get("isLocked")]

    for a in locked_actions:
        print(f"  [LOCKED] {a['text']}")

    if not free_actions:
        # Take a debug screenshot and dump page structure
        debug_path = out_dir / "_debug_screenshot.png"
        await page.screenshot(path=str(debug_path), full_page=True)
        print(f"  [WARN] No free action buttons found.")
        print(f"         Debug screenshot: {debug_path}")
        print(f"         All candidates found: {len(action_items)}")
        for a in action_items:
            print(f"           - '{a['text']}' at ({a['x']},{a['y']}) locked={a['isLocked']}")
        return

    print(f"  Found {len(free_actions)} free actions:")
    for a in free_actions:
        print(f"    - '{a['text']}' at ({a['x']},{a['y']})")

    # -- Step 3: Click each action, intercept video, download ----------------
    for idx, action_info in enumerate(free_actions):
        action_text = action_info["text"]
        action_filename = slugify(action_text) + ".mp4"
        dest = out_dir / action_filename

        if dest.exists() and dest.stat().st_size > MIN_VIDEO_SIZE:
            print(f"  [{idx+1}/{len(free_actions)}] [skip] Already have: {action_text}")
            continue

        print(f"  [{idx+1}/{len(free_actions)}] Clicking: '{action_text}'")

        # Set up response listener BEFORE clicking
        captured_url = None
        capture_event = asyncio.Event()

        async def on_response(response: Response):
            nonlocal captured_url
            resp_url = response.url
            if captured_url:
                return
            if not is_cdn_video_url(resp_url):
                return
            try:
                ct = response.headers.get("content-type", "")
                cl = response.headers.get("content-length", "0")
                if ct in VIDEO_CONTENT_TYPES or int(cl) > MIN_VIDEO_SIZE:
                    captured_url = resp_url
                    capture_event.set()
            except Exception:
                if "/videos/" in resp_url and any(resp_url.endswith(ext) for ext in [".mp4", ".webm"]):
                    captured_url = resp_url
                    capture_event.set()

        page.on("response", on_response)

        # Reset the video player before clicking next action
        try:
            await page.evaluate("""
                () => {
                    const videos = document.querySelectorAll('video');
                    videos.forEach(v => { v.pause(); v.removeAttribute('src'); v.load(); });
                }
            """)
        except Exception:
            pass

        # Click the action button by coordinates (most reliable since we
        # already know exact position from the evaluate step)
        try:
            cx = action_info["x"] + action_info["w"] // 2
            cy = action_info["y"] + action_info["h"] // 2
            await page.mouse.click(cx, cy)
        except Exception as e:
            print(f"    [WARN] Click failed: {e}")
            page.remove_listener("response", on_response)
            continue

        # Wait for video URL to appear in network traffic (up to 8s)
        try:
            await asyncio.wait_for(capture_event.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            # Fallback: read video src from DOM
            try:
                video_src = await page.evaluate("""
                    () => {
                        const v = document.querySelector('video');
                        return v ? (v.src || v.currentSrc || '') : '';
                    }
                """)
                if video_src and is_cdn_video_url(video_src):
                    captured_url = video_src
                    print(f"    [DOM fallback] Got URL from <video> element")
            except Exception:
                pass

        page.remove_listener("response", on_response)

        if captured_url:
            short_url = captured_url.split("?")[0][-60:]
            print(f"    [URL] ...{short_url}")
            download_file(captured_url, dest, cookies)
        else:
            print(f"    [WARN] No video URL captured for: {action_text}")

        # Pause between actions to let player settle
        await page.wait_for_timeout(2000)


async def run_phase1(context: BrowserContext, cookies: dict):
    """Phase 1: Scrape live action videos for all characters."""
    print("\n" + "=" * 60)
    print("  PHASE 1 -- LIVE ACTION SCRAPING")
    print("=" * 60)

    page = await context.new_page()

    for slug in LIVE_ACTION_CHARACTERS:
        await scrape_live_action(page, slug, cookies)

    await page.close()
    print("\n  Phase 1 complete!")


# -- Phase 2: Character Profiles ---------------------------------------------

async def collect_character_urls(page: Page) -> List[str]:
    """Collect up to MAX_CHARACTERS_PHASE2 character profile URLs from homepage."""
    print("\n  Collecting character URLs from homepage...")

    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(3000)

    # Scroll down to load more characters
    for _ in range(10):
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(1000)

    # Extract all character profile links
    links = await page.evaluate("""
        () => {
            const anchors = document.querySelectorAll('a[href]');
            const urls = new Set();
            for (const a of anchors) {
                const href = a.href;
                if (href.includes('/ai-girlfriend/') && !href.includes('/live-actions')) {
                    const match = href.match(/\\/ai-girlfriend\\/([a-z0-9-]+)/);
                    if (match) {
                        urls.add('https://candy.ai/ai-girlfriend/' + match[1]);
                    }
                }
            }
            return [...urls];
        }
    """)

    # Filter in Python: valid slugs only
    valid = []
    for url in links:
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "ai-girlfriend":
            slug = parts[1]
            if re.match(r"^[a-z0-9-]+$", slug):
                valid.append(url)
        if len(valid) >= MAX_CHARACTERS_PHASE2:
            break

    print(f"  Found {len(valid)} character URLs")
    return valid[:MAX_CHARACTERS_PHASE2]


async def scrape_character_profile(page: Page, profile_url: str, cookies: dict):
    """Scrape all CDN assets (images + videos) from a character profile page."""
    slug = profile_url.rstrip("/").split("/")[-1]
    char_name = prettify_name(slug)
    base_dir = OUTPUT_DIR / "Candy AI Characters" / char_name
    img_dir = base_dir / "imagenes"
    vid_dir = base_dir / "videos"

    print(f"\n  Scraping: {char_name}")

    captured_assets = []

    async def on_response(response: Response):
        url = response.url
        if not any(cdn in url for cdn in ["private-cdn.candy.ai", "cdn.candy.ai"]):
            return
        ct = response.headers.get("content-type", "")
        captured_assets.append((url, ct))

    page.on("response", on_response)

    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3000)

        for _ in range(5):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(800)

    except Exception as e:
        print(f"    [FAIL] Could not load profile: {e}")
        page.remove_listener("response", on_response)
        return

    # Scan DOM for CDN image/video URLs we might have missed
    dom_urls = await page.evaluate("""
        () => {
            const urls = new Set();
            document.querySelectorAll('img[src]').forEach(el => {
                if (el.src.includes('cdn.candy.ai') || el.src.includes('private-cdn.candy.ai')) {
                    urls.add(el.src + '|image');
                }
            });
            document.querySelectorAll('video source[src], video[src]').forEach(el => {
                const src = el.src || el.getAttribute('src');
                if (src && (src.includes('cdn.candy.ai') || src.includes('private-cdn.candy.ai'))) {
                    urls.add(src + '|video');
                }
            });
            document.querySelectorAll('[style*="background"]').forEach(el => {
                const style = el.getAttribute('style') || '';
                const match = style.match(/url\\(['"]?(https?:\\/\\/[^'"\\)]+)['"]?\\)/);
                if (match && (match[1].includes('cdn.candy.ai') || match[1].includes('private-cdn.candy.ai'))) {
                    urls.add(match[1] + '|image');
                }
            });
            return [...urls];
        }
    """)

    page.remove_listener("response", on_response)

    for entry in dom_urls:
        url, kind = entry.rsplit("|", 1)
        ct = "image/jpeg" if kind == "image" else "video/mp4"
        captured_assets.append((url, ct))

    # Deduplicate
    seen = set()
    unique_assets = []
    for url, ct in captured_assets:
        clean = url.split("?")[0]
        if clean not in seen:
            seen.add(clean)
            unique_assets.append((url, ct))

    if not unique_assets:
        print(f"    [WARN] No assets found for {char_name}")
        return

    img_count = vid_count = 0
    for url, ct in unique_assets:
        parsed_path = urlparse(url).path
        ext = Path(parsed_path).suffix.lower()

        is_video = "video" in ct or ext in {".mp4", ".webm", ".mov", ".avi"}
        is_image = "image" in ct or ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}

        if is_video:
            filename = Path(parsed_path).name or f"video_{vid_count}.mp4"
            download_file(url, vid_dir / filename, cookies)
            vid_count += 1
        elif is_image:
            filename = Path(parsed_path).name or f"image_{img_count}.jpg"
            download_file(url, img_dir / filename, cookies)
            img_count += 1

    print(f"    {char_name}: {img_count} images, {vid_count} videos")


async def run_phase2(context: BrowserContext, cookies: dict):
    """Phase 2: Scrape character profile assets."""
    print("\n" + "=" * 60)
    print("  PHASE 2 -- CHARACTER PROFILE SCRAPING")
    print("=" * 60)

    page = await context.new_page()
    char_urls = await collect_character_urls(page)

    for idx, url in enumerate(char_urls):
        print(f"\n  [{idx+1}/{len(char_urls)}]", end="")
        await scrape_character_profile(page, url, cookies)

    await page.close()
    print("\n  Phase 2 complete!")


# -- Main --------------------------------------------------------------------

async def main():
    print("Candy.ai Scraper")
    print("=" * 60)
    print(f"Output: {OUTPUT_DIR}")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()
        await page.goto(BASE_URL, wait_until="domcontentloaded")

        print("Log in to Candy.ai then press ENTER.")
        print()
        input("Press ENTER once logged in... ")

        await page.close()

        cookies = await extract_cookies(context)
        print(f"Captured {len(cookies)} cookies")

        await run_phase1(context, cookies)
        await run_phase2(context, cookies)

        await browser.close()

    print("\n" + "=" * 60)
    print("ALL DONE!")
    print(f"Output: {OUTPUT_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
