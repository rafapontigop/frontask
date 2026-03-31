#!/usr/bin/env python3
"""
Candy.ai Scraper — Playwright + Network Interception

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

import requests
from playwright.async_api import async_playwright, Page, BrowserContext, Response

# ── Configuration ──────────────────────────────────────────────────────────────

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
MIN_VIDEO_SIZE = 200 * 1024  # 200 KB — skip thumbnails/previews

MAX_CHARACTERS_PHASE2 = 100


# ── Helpers ────────────────────────────────────────────────────────────────────

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


def download_file(url: str, dest: Path, cookies: dict | None = None):
    """Download a file using requests with cookie auth."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > MIN_VIDEO_SIZE:
        print(f"    ⏭  Already exists: {dest.name}")
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
        size_kb = dest.stat().st_size / 1024
        print(f"    ✅ Downloaded: {dest.name} ({size_kb:.0f} KB)")
    except Exception as e:
        print(f"    ❌ Failed to download {dest.name}: {e}")
        if dest.exists():
            dest.unlink()


async def extract_cookies(context: BrowserContext) -> dict:
    """Extract cookies from browser context for requests session."""
    cookies = await context.cookies()
    return {c["name"]: c["value"] for c in cookies}


# ── Phase 1: Live Action ──────────────────────────────────────────────────────

async def scrape_live_action(page: Page, slug: str, cookies: dict):
    """Scrape all free live-action video clips for one character."""
    char_name = prettify_name(slug)
    out_dir = OUTPUT_DIR / "Live Action" / char_name
    out_dir.mkdir(parents=True, exist_ok=True)

    url = f"{BASE_URL}/ai-girlfriend/{slug}/live-actions?source=home_live_section"
    print(f"\n{'='*60}")
    print(f"📹 Phase 1 — {char_name}")
    print(f"   {url}")
    print(f"{'='*60}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        print(f"  ❌ Failed to load page: {e}")
        return

    # Wait for action buttons to render
    await page.wait_for_timeout(3000)

    # Locate the action buttons in the right panel.
    # Each action card typically contains: a text label + a play button.
    # Locked cards contain a lock icon or premium/subscribe/token text.
    buttons = await page.query_selector_all(
        "[class*='action'] button, "
        "[class*='Action'] button, "
        "[data-testid*='action'] button, "
        "button[class*='play'], "
        ".actions-panel button, "
        ".action-card button, "
        ".action-item button"
    )

    if not buttons:
        # Broader fallback: find all clickable elements near play icons
        buttons = await page.query_selector_all("button")
        print(f"  ℹ️  Found {len(buttons)} generic buttons — will filter")

    # If we still don't have action-specific selectors, try to find the right panel
    # and enumerate its interactive children.
    action_items = []

    # Strategy: look for elements that have both text and a play-like icon
    # The right panel typically contains cards/rows with action labels
    all_candidates = await page.query_selector_all(
        "[class*='action'], [class*='Action'], "
        "[class*='card'], [class*='Card'], "
        "[class*='item'], [class*='Item']"
    )

    for elem in all_candidates:
        text = (await elem.inner_text()).strip()
        if not text or len(text) > 80:
            continue

        # Skip locked/premium actions
        html = await elem.inner_html()
        lock_keywords = ["lock", "premium", "subscribe", "token", "🔒"]
        if any(kw.lower() in html.lower() for kw in lock_keywords):
            print(f"  🔒 Skipping locked: {text[:40]}")
            continue

        # Check if there's a clickable play button inside
        play_btn = await elem.query_selector(
            "button, [role='button'], [class*='play'], [class*='Play'], svg"
        )
        if play_btn:
            action_items.append((text.split("\n")[0].strip(), play_btn, elem))

    if not action_items:
        # Last resort: screenshot for debugging
        debug_path = out_dir / "_debug_screenshot.png"
        await page.screenshot(path=str(debug_path), full_page=True)
        print(f"  ⚠️  No action buttons found. Debug screenshot saved to {debug_path}")
        return

    print(f"  🎬 Found {len(action_items)} free actions")

    for idx, (action_text, play_btn, card_elem) in enumerate(action_items):
        action_filename = slugify(action_text) + ".mp4"
        dest = out_dir / action_filename
        if dest.exists() and dest.stat().st_size > MIN_VIDEO_SIZE:
            print(f"  ⏭  [{idx+1}/{len(action_items)}] Already have: {action_text}")
            continue

        print(f"  🔄 [{idx+1}/{len(action_items)}] Clicking: {action_text}")

        # Set up network interception BEFORE clicking
        captured_url = None
        capture_event = asyncio.Event()

        async def on_response(response: Response):
            nonlocal captured_url
            url = response.url
            if captured_url:
                return
            if not is_cdn_video_url(url):
                return
            # Check content type from headers
            try:
                ct = response.headers.get("content-type", "")
                cl = response.headers.get("content-length", "0")
                if ct in VIDEO_CONTENT_TYPES or int(cl) > MIN_VIDEO_SIZE:
                    captured_url = url
                    capture_event.set()
            except Exception:
                # If we can't read headers, still capture CDN video URLs
                if "/videos/" in url and any(url.endswith(ext) for ext in [".mp4", ".webm"]):
                    captured_url = url
                    capture_event.set()

        page.on("response", on_response)

        # Reset the video player before clicking next action
        try:
            await page.evaluate("""
                () => {
                    const videos = document.querySelectorAll('video');
                    videos.forEach(v => {
                        v.pause();
                        v.removeAttribute('src');
                        v.load();
                    });
                }
            """)
        except Exception:
            pass

        # Click the play button
        try:
            await play_btn.click(timeout=5000)
        except Exception as e:
            print(f"    ⚠️  Click failed: {e}")
            page.remove_listener("response", on_response)
            continue

        # Wait for the video URL to be captured (up to 8 seconds)
        try:
            await asyncio.wait_for(capture_event.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            # Fallback: try to read the video src directly from the DOM
            try:
                video_src = await page.evaluate("""
                    () => {
                        const video = document.querySelector('video');
                        return video ? (video.src || video.currentSrc) : null;
                    }
                """)
                if video_src and is_cdn_video_url(video_src):
                    captured_url = video_src
            except Exception:
                pass

        page.remove_listener("response", on_response)

        if captured_url:
            print(f"    🎯 Captured URL: {captured_url[:80]}...")
            download_file(captured_url, dest, cookies)
        else:
            print(f"    ⚠️  No video URL captured for: {action_text}")

        # Brief pause between actions
        await page.wait_for_timeout(1500)


async def run_phase1(context: BrowserContext, cookies: dict):
    """Phase 1: Scrape live action videos for all characters."""
    print("\n" + "=" * 60)
    print("🎬 PHASE 1 — LIVE ACTION SCRAPING")
    print("=" * 60)

    page = await context.new_page()

    for slug in LIVE_ACTION_CHARACTERS:
        await scrape_live_action(page, slug, cookies)

    await page.close()
    print("\n✅ Phase 1 complete!")


# ── Phase 2: Character Profiles ───────────────────────────────────────────────

async def collect_character_urls(page: Page) -> list[str]:
    """Collect up to MAX_CHARACTERS_PHASE2 character profile URLs from homepage."""
    print("\n📋 Collecting character URLs from homepage...")

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
                    // Normalize to just the profile URL
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

    print(f"\n  👤 Scraping: {char_name}")

    # Collect CDN URLs via response interception
    captured_assets = []  # list of (url, content_type)

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

        # Scroll to trigger lazy-loaded assets
        for _ in range(5):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(800)

    except Exception as e:
        print(f"    ❌ Failed to load profile: {e}")
        page.remove_listener("response", on_response)
        return

    # Also scan the DOM for any CDN image/video URLs we might have missed
    dom_urls = await page.evaluate("""
        () => {
            const urls = new Set();
            // Images
            document.querySelectorAll('img[src]').forEach(el => {
                if (el.src.includes('cdn.candy.ai') || el.src.includes('private-cdn.candy.ai')) {
                    urls.add(el.src + '|image');
                }
            });
            // Videos
            document.querySelectorAll('video source[src], video[src]').forEach(el => {
                const src = el.src || el.getAttribute('src');
                if (src && (src.includes('cdn.candy.ai') || src.includes('private-cdn.candy.ai'))) {
                    urls.add(src + '|video');
                }
            });
            // Background images
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

    # Merge DOM-discovered URLs
    for entry in dom_urls:
        url, kind = entry.rsplit("|", 1)
        ct = "image/jpeg" if kind == "image" else "video/mp4"
        captured_assets.append((url, ct))

    # Deduplicate by URL
    seen = set()
    unique_assets = []
    for url, ct in captured_assets:
        # Strip query params for dedup
        clean = url.split("?")[0]
        if clean not in seen:
            seen.add(clean)
            unique_assets.append((url, ct))

    if not unique_assets:
        print(f"    ⚠️  No assets found for {char_name}")
        return

    # Classify and download
    img_count = vid_count = 0
    for url, ct in unique_assets:
        # Determine type from content-type or file extension
        parsed_path = urlparse(url).path
        ext = Path(parsed_path).suffix.lower()

        is_video = (
            "video" in ct
            or ext in {".mp4", ".webm", ".mov", ".avi"}
        )
        is_image = (
            "image" in ct
            or ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
        )

        if is_video:
            filename = Path(parsed_path).name or f"video_{vid_count}.mp4"
            download_file(url, vid_dir / filename, cookies)
            vid_count += 1
        elif is_image:
            filename = Path(parsed_path).name or f"image_{img_count}.jpg"
            download_file(url, img_dir / filename, cookies)
            img_count += 1

    print(f"    📊 {char_name}: {img_count} images, {vid_count} videos")


async def run_phase2(context: BrowserContext, cookies: dict):
    """Phase 2: Scrape character profile assets."""
    print("\n" + "=" * 60)
    print("👤 PHASE 2 — CHARACTER PROFILE SCRAPING")
    print("=" * 60)

    page = await context.new_page()

    # Collect character URLs
    char_urls = await collect_character_urls(page)

    for idx, url in enumerate(char_urls):
        print(f"\n  [{idx+1}/{len(char_urls)}]", end="")
        await scrape_character_profile(page, url, cookies)

    await page.close()
    print("\n✅ Phase 2 complete!")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    print("🍬 Candy.ai Scraper")
    print("=" * 60)
    print(f"Output directory: {OUTPUT_DIR}")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # Need visible browser for manual login
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        # Navigate to Candy.ai for manual login
        page = await context.new_page()
        await page.goto(BASE_URL, wait_until="domcontentloaded")

        print("🔐 MANUAL LOGIN REQUIRED")
        print("   1. Log in to Candy.ai in the browser window")
        print("   2. Make sure you're on the homepage and fully logged in")
        print("   3. Come back here and press ENTER to continue")
        print()
        input("   Press ENTER when logged in... ")

        await page.close()

        # Capture cookies for download requests
        cookies = await extract_cookies(context)
        print(f"   🍪 Captured {len(cookies)} cookies")

        # Run both phases
        await run_phase1(context, cookies)
        await run_phase2(context, cookies)

        await browser.close()

    print("\n" + "=" * 60)
    print("🎉 ALL DONE!")
    print(f"   Output: {OUTPUT_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
