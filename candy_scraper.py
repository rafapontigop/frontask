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
import re
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, Dict, List

import requests
from playwright.async_api import async_playwright, Page, BrowserContext, Response

# -- Configuration -----------------------------------------------------------

BASE_URL = "https://candy.ai"
OUTPUT_DIR = Path.home() / "Desktop"

LIVE_ACTION_CHARACTERS = [
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
MIN_VIDEO_SIZE = 200 * 1024  # 200 KB

MAX_CHARACTERS_PHASE2 = 100


# -- Helpers -----------------------------------------------------------------

def slugify(text: str) -> str:
    text = text.strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s]+", "_", text)
    return text


def is_cdn_video_url(url: str) -> bool:
    return "private-cdn.candy.ai/videos/" in url or "cdn.candy.ai/" in url


def download_file(url: str, dest: Path, cookies: Optional[Dict] = None):
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
    cookies = await context.cookies()
    return {c["name"]: c["value"] for c in cookies}


# -- Phase 1: Live Action ----------------------------------------------------

async def find_action_rows(page: Page) -> List[dict]:
    """
    Find the action rows in the right panel.

    Each row is structured as:
      [ text label ]                    [ pink circle play button ]
      e.g. "Tease me"                         (pink circle)

    Locked rows instead show:
      [ text label ]              [ lock icon ]  "Level N"

    We detect FREE rows by finding rows that contain an SVG inside a
    pink/rose-colored circular element (the play button), and do NOT
    contain "Level" text on the right side.
    """
    return await page.evaluate("""
        () => {
            const results = [];

            // The action rows are in the right panel.
            // Each row has: a text label + either a pink play button OR a lock + "Level N"
            //
            // Strategy: find all SVG elements that are inside a pink/rose circle.
            // The pink play button is a circle (border-radius: 50%) with a pink/rose
            // background, containing an SVG with a play triangle (polygon).
            // Then walk up to the parent row to get the action text.

            // Approach: find every element that looks like a row containing
            // both text and a circular pink button.
            // The rows are siblings in a scrollable list in the right panel.

            // First, let's find all elements with pink/rose background that contain SVG
            const allElements = document.querySelectorAll('*');
            const playButtons = [];

            for (const el of allElements) {
                const style = window.getComputedStyle(el);
                const bg = style.backgroundColor;
                const br = style.borderRadius;

                // Check if this is a circle (border-radius ~50% or high px)
                const isCircular = br.includes('50%') || br.includes('9999') ||
                    parseInt(br) >= 20;
                if (!isCircular) continue;

                // Check if background is pink/rose (high R, medium-low G and B)
                const rgbMatch = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                if (!rgbMatch) continue;
                const r = parseInt(rgbMatch[1]);
                const g = parseInt(rgbMatch[2]);
                const b = parseInt(rgbMatch[3]);

                // Pink/rose: R > 180, G < 130, B < 160 (covers various pink shades)
                const isPink = r > 180 && g < 130 && b < 160;
                if (!isPink) continue;

                // Must contain an SVG (the play triangle icon)
                const svg = el.querySelector('svg');
                if (!svg) continue;

                playButtons.push(el);
            }

            // For each pink play button, walk up to find the parent row and extract text
            for (const btn of playButtons) {
                // Walk up max 5 levels to find the row container
                let row = btn.parentElement;
                for (let i = 0; i < 5 && row; i++) {
                    const rect = row.getBoundingClientRect();
                    // A row should be wider than tall, and reasonably sized
                    if (rect.width > 200 && rect.height > 30 && rect.height < 120) {
                        break;
                    }
                    row = row.parentElement;
                }

                if (!row) continue;

                // Extract the text label (should be the action name)
                // Get text NOT inside the button itself
                let actionText = '';
                const textElements = row.querySelectorAll('span, p, div, h3, h4, h5');
                for (const te of textElements) {
                    // Skip if this is inside the pink button
                    if (btn.contains(te)) continue;
                    const t = te.textContent.trim();
                    if (t.length >= 3 && t.length <= 60 && !t.includes('Level')) {
                        actionText = t;
                        break;
                    }
                }

                if (!actionText) {
                    // Fallback: get all text from row, remove button text
                    const rowText = row.textContent.trim();
                    const btnText = btn.textContent.trim();
                    actionText = rowText.replace(btnText, '').trim().split('\\n')[0].trim();
                }

                if (!actionText || actionText.length < 3 || actionText.length > 60) continue;

                // Skip UI noise
                const lower = actionText.toLowerCase();
                if (['level', 'skip to', 'xp', 'beta'].some(w => lower.startsWith(w))) continue;

                const btnRect = btn.getBoundingClientRect();
                results.push({
                    text: actionText,
                    btnX: Math.round(btnRect.x + btnRect.width / 2),
                    btnY: Math.round(btnRect.y + btnRect.height / 2),
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


async def scrape_live_action(page: Page, slug: str, char_name: str, cookies: dict):
    """Scrape all free live-action video clips for one character."""
    out_dir = OUTPUT_DIR / "Live Action" / char_name
    out_dir.mkdir(parents=True, exist_ok=True)

    url = f"{BASE_URL}/ai-girlfriend/{slug}/live-actions?source=home_live_section"
    print(f"\n{'='*60}")
    print(f"  {char_name}")
    print(f"  {url}")
    print(f"{'='*60}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        print(f"  [FAIL] Could not load page: {e}")
        return

    await page.wait_for_timeout(3000)

    # -- Step 1: Click "Tap to start" if present -----------------------------
    for text_pattern in ["Tap to start", "TAP TO START", "tap to start"]:
        try:
            loc = page.get_by_text(text_pattern, exact=False).first
            if await loc.is_visible(timeout=2000):
                print(f"  Clicking '{text_pattern}'...")
                await loc.click(timeout=5000)
                await page.wait_for_timeout(3000)
                break
        except Exception:
            continue

    # -- Step 2: Find the pink play buttons ----------------------------------
    actions = await find_action_rows(page)

    if not actions:
        debug_path = out_dir / "_debug.png"
        await page.screenshot(path=str(debug_path), full_page=True)
        print(f"  [WARN] No action buttons found. Screenshot: {debug_path}")
        return

    print(f"  Found {len(actions)} free actions:")
    for a in actions:
        print(f"    - {a['text']}")

    # -- Step 3: Click each pink play button, intercept video, download ------
    for idx, action in enumerate(actions):
        action_text = action["text"]
        filename = slugify(action_text) + ".mp4"
        dest = out_dir / filename

        if dest.exists() and dest.stat().st_size > MIN_VIDEO_SIZE:
            print(f"  [{idx+1}/{len(actions)}] [skip] {action_text}")
            continue

        print(f"  [{idx+1}/{len(actions)}] {action_text}")

        # Set up response interceptor BEFORE clicking
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
                if "/videos/" in resp_url:
                    captured_url = resp_url
                    capture_event.set()

        page.on("response", on_response)

        # Reset video player before clicking
        try:
            await page.evaluate("""
                () => {
                    document.querySelectorAll('video').forEach(v => {
                        v.pause();
                        v.removeAttribute('src');
                        v.load();
                    });
                }
            """)
        except Exception:
            pass

        # Click the pink play button at its exact coordinates
        try:
            await page.mouse.click(action["btnX"], action["btnY"])
        except Exception as e:
            print(f"    [WARN] Click failed: {e}")
            page.remove_listener("response", on_response)
            continue

        # Wait for CDN video response (up to 8s)
        try:
            await asyncio.wait_for(capture_event.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            # Fallback: check <video> src in DOM
            try:
                video_src = await page.evaluate("""
                    () => {
                        const v = document.querySelector('video');
                        return v ? (v.src || v.currentSrc || '') : '';
                    }
                """)
                if video_src and is_cdn_video_url(video_src):
                    captured_url = video_src
                    print(f"    [DOM fallback]")
            except Exception:
                pass

        page.remove_listener("response", on_response)

        if captured_url:
            short = captured_url.split("?")[0].split("/")[-1]
            print(f"    -> {short}")
            download_file(captured_url, dest, cookies)
        else:
            print(f"    [WARN] No video captured")

        # Pause before next click
        await page.wait_for_timeout(2000)


async def run_phase1(context: BrowserContext, cookies: dict):
    print("\n" + "=" * 60)
    print("  PHASE 1 -- LIVE ACTION")
    print("=" * 60)

    page = await context.new_page()

    for slug, char_name in LIVE_ACTION_CHARACTERS:
        await scrape_live_action(page, slug, char_name, cookies)

    await page.close()
    print("\n  Phase 1 complete!")


# -- Phase 2: Character Profiles ---------------------------------------------

async def collect_character_urls(page: Page) -> List[str]:
    print("\n  Collecting character URLs from homepage...")

    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(3000)

    for _ in range(10):
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(1000)

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
    slug = profile_url.rstrip("/").split("/")[-1]
    char_name = slug.replace("-", " ").title()
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
        print(f"    [FAIL] Could not load: {e}")
        page.remove_listener("response", on_response)
        return

    dom_urls = await page.evaluate("""
        () => {
            const urls = new Set();
            document.querySelectorAll('img[src]').forEach(el => {
                if (el.src.includes('cdn.candy.ai') || el.src.includes('private-cdn.candy.ai'))
                    urls.add(el.src + '|image');
            });
            document.querySelectorAll('video source[src], video[src]').forEach(el => {
                const src = el.src || el.getAttribute('src');
                if (src && (src.includes('cdn.candy.ai') || src.includes('private-cdn.candy.ai')))
                    urls.add(src + '|video');
            });
            document.querySelectorAll('[style*="background"]').forEach(el => {
                const style = el.getAttribute('style') || '';
                const m = style.match(/url\\(['"]?(https?:\\/\\/[^'"\\)]+)['"]?\\)/);
                if (m && (m[1].includes('cdn.candy.ai') || m[1].includes('private-cdn.candy.ai')))
                    urls.add(m[1] + '|image');
            });
            return [...urls];
        }
    """)

    page.remove_listener("response", on_response)

    for entry in dom_urls:
        url, kind = entry.rsplit("|", 1)
        ct = "image/jpeg" if kind == "image" else "video/mp4"
        captured_assets.append((url, ct))

    seen = set()
    unique_assets = []
    for url, ct in captured_assets:
        clean = url.split("?")[0]
        if clean not in seen:
            seen.add(clean)
            unique_assets.append((url, ct))

    if not unique_assets:
        print(f"    [WARN] No assets found")
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
    print("\n" + "=" * 60)
    print("  PHASE 2 -- CHARACTER PROFILES")
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
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()
        await page.goto(BASE_URL, wait_until="domcontentloaded")

        print("Log in to Candy.ai in the browser window.")
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
