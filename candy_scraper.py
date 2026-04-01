#!/usr/bin/env python3
"""
Candy.ai Live Action Scraper

Flow per action: click -> video plays fully -> download -> next
"""

import asyncio
import re
from pathlib import Path
import requests
from playwright.async_api import async_playwright, Page, Response

BASE_URL = "https://candy.ai"
OUTPUT_DIR = Path.home() / "Desktop" / "Live Action 3"

CHARACTERS = [
    ("coco-bailey", "Coco", [
        "Tease me",
        "Show me your butt",
        "Make an ahegao face",
        "Show me your boobs",
        "Kiss another girl",
        "Get naked for me",
    ]),
    ("darkangel666", "Darkangel666", [
        "I tease you slowly",
        "Pull an ahegao Face",
        "Dance for me",
        "Show me your butt",
        "Kiss another girl",
    ]),
    ("emilia-vermont", "Emilia", [
        "Ahegao face",
        "Panty tease",
        "Get on all fours",
        "Undress",
        "Beg me",
    ]),
    ("elodie-valmont", "Elodie", [
        "Ahegao Face",
        "Sexy Tease",
    ]),
    ("mila-nowak", "Mila", [
        "Sexy Strech",
        "Kissing (Another girl)",
        "Undress",
        "Boobjob & Facial",
        "Pussy Play & Squirt",
    ]),
    ("isabella-torres", "Isabella", [
        "Airjob",
        "Ahegao Face",
        "Kissing (Another Girl)",
    ]),
    ("olivia-carter", "Olivia", [
        "Smile for me",
        "Blow a bubble",
        "Sexy Dance for me",
        "Pull an ahegao face",
        "Undress for me",
        "Kiss another girl",
        "Show me your ass",
    ]),
    ("katarina-sommerfeld", "Katarina", [
        "Sexy tease",
        "Show butt",
        "Flash boobs",
        "Undress",
    ]),
]

VIDEO_CONTENT_TYPES = {"video/mp4", "video/webm", "application/octet-stream"}
MIN_VIDEO_SIZE = 2 * 1024 * 1024  # 2 MB minimum for a valid video


def slugify(text):
    text = re.sub(r"[^\w\s-]", "", text.strip())
    return re.sub(r"\s+", "_", text)


def is_cdn_video(url):
    return "private-cdn.candy.ai/videos/" in url or "cdn.candy.ai/" in url


def download(url, dest, cookies):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > MIN_VIDEO_SIZE:
        print(f"      [skip] already have {dest.name}")
        return True
    s = requests.Session()
    for k, v in cookies.items():
        s.cookies.set(k, v)
    try:
        r = s.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": BASE_URL + "/",
        }, stream=True, timeout=180)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        mb = dest.stat().st_size / (1024 * 1024)
        print(f"      [OK] {dest.name} ({mb:.1f} MB)")
        return True
    except Exception as e:
        print(f"      [FAIL] {dest.name}: {e}")
        if dest.exists():
            dest.unlink()
        return False


async def click_action_by_text(page, action_name):
    coords = await page.evaluate("""
        (actionName) => {
            const target = actionName.toLowerCase().trim();
            const allElements = document.querySelectorAll('div, button, a, li, span');
            let best = null;
            let bestArea = Infinity;

            for (const el of allElements) {
                const rect = el.getBoundingClientRect();
                if (rect.left < 700) continue;
                if (rect.width < 200 || rect.height < 30 || rect.height > 80) continue;

                const raw = el.textContent.trim();
                const norm = raw.toLowerCase()
                    .replace(/free$/i, '').replace(/request$/i, '')
                    .replace(/\\d+$/, '').trim();

                if (norm === target || norm.startsWith(target) || target.startsWith(norm)) {
                    const area = rect.width * rect.height;
                    if (area < bestArea) {
                        bestArea = area;
                        best = {
                            x: Math.round(rect.x + rect.width / 2),
                            y: Math.round(rect.y + rect.height / 2),
                        };
                    }
                }
            }
            return best;
        }
    """, action_name)

    if not coords:
        return False
    await page.mouse.click(coords["x"], coords["y"])
    return True


async def scroll_and_click(page, action_name):
    # Reset scroll
    try:
        await page.evaluate("""() => {
            document.querySelectorAll('div').forEach(p => {
                const r = p.getBoundingClientRect();
                if (r.left > 700 && r.height > 300 && p.scrollHeight > p.clientHeight)
                    p.scrollTop = 0;
            });
        }""")
        await page.wait_for_timeout(300)
    except Exception:
        pass

    if await click_action_by_text(page, action_name):
        return True

    for _ in range(4):
        try:
            await page.evaluate("""() => {
                document.querySelectorAll('div').forEach(p => {
                    const r = p.getBoundingClientRect();
                    if (r.left > 700 && r.height > 300 && p.scrollHeight > p.clientHeight)
                        p.scrollBy(0, 300);
                });
            }""")
            await page.wait_for_timeout(400)
        except Exception:
            pass
        if await click_action_by_text(page, action_name):
            return True

    return False


async def wait_for_video_to_finish(page, timeout_s=120):
    """
    Wait for the <video> element to finish playing.
    Polls every 1s checking currentTime vs duration.
    Does NOT modify the video element in any way.
    """
    # First wait a moment for the video to start loading
    await page.wait_for_timeout(2000)

    for i in range(timeout_s):
        try:
            state = await page.evaluate("""() => {
                const v = document.querySelector('video');
                if (!v) return { status: 'no_video' };
                if (v.ended) return { status: 'ended', duration: v.duration };
                if (v.readyState < 2) return { status: 'loading' };
                if (v.paused && v.currentTime > 0) return { status: 'ended', duration: v.duration };
                return {
                    status: 'playing',
                    current: v.currentTime,
                    duration: v.duration || 0,
                };
            }""")

            status = state.get("status", "unknown")

            if status == "ended":
                dur = state.get("duration", 0)
                print(f"finished ({dur:.0f}s)!", flush=True)
                return True

            if status == "playing":
                cur = state.get("current", 0)
                dur = state.get("duration", 0)
                if dur > 0 and cur >= dur - 0.5:
                    print(f"finished ({dur:.0f}s)!", flush=True)
                    return True
                # Show progress every 5 seconds
                if i > 0 and i % 5 == 0:
                    print(f"{cur:.0f}/{dur:.0f}s", end=" ", flush=True)

            if status == "no_video":
                if i > 10:
                    print("no video element", flush=True)
                    return False

        except Exception:
            pass

        await page.wait_for_timeout(1000)

    print(f"timeout ({timeout_s}s)", flush=True)
    return False


async def dismiss_popups(page):
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
    except Exception:
        pass
    for sel in ["button:has-text('Close')", "button:has-text('Cancel')",
                "button:has-text('No thanks')", "[aria-label='Close']"]:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=300):
                await loc.click(timeout=1000)
                await page.wait_for_timeout(300)
        except Exception:
            pass
    try:
        await page.mouse.click(100, 100)
        await page.wait_for_timeout(300)
    except Exception:
        pass


async def scrape_character(page, slug, char_name, actions, cookies):
    url = f"{BASE_URL}/ai-girlfriend/{slug}/live-actions?source=home_live_section"
    out_dir = OUTPUT_DIR / char_name

    print(f"\n{'='*60}")
    print(f"  {char_name} ({len(actions)} actions)")
    print(f"{'='*60}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"  [FAIL] Page load: {e}")
        return

    await page.wait_for_timeout(4000)

    # Click "Tap to start" if present
    for pat in ["Tap to start", "TAP TO START"]:
        try:
            loc = page.get_by_text(pat, exact=False).first
            if await loc.is_visible(timeout=3000):
                await loc.click(timeout=5000)
                print(f"  Clicked '{pat}'")
                await page.wait_for_timeout(5000)
                break
        except Exception:
            continue

    out_dir.mkdir(parents=True, exist_ok=True)
    ok_count = 0

    for idx, action_name in enumerate(actions):
        fname = slugify(action_name) + ".mp4"
        dest = out_dir / fname

        if dest.exists() and dest.stat().st_size > MIN_VIDEO_SIZE:
            print(f"  [{idx+1}/{len(actions)}] [skip] {action_name}")
            ok_count += 1
            continue

        print(f"  [{idx+1}/{len(actions)}] {action_name}")

        # --- Interceptor BEFORE clicking ---
        capture_event = asyncio.Event()

        def make_handler(evt):
            captured = {"url": None}

            async def handler(resp):
                if captured["url"]:
                    return
                u = resp.url
                if not is_cdn_video(u):
                    return
                try:
                    ct = resp.headers.get("content-type", "")
                    cl = resp.headers.get("content-length", "0")
                    if ct in VIDEO_CONTENT_TYPES or int(cl) > 200000:
                        captured["url"] = u
                        evt.set()
                except Exception:
                    if "/videos/" in u:
                        captured["url"] = u
                        evt.set()

            return handler, captured

        handler, captured_ref = make_handler(capture_event)
        page.on("response", handler)

        # --- Click the action ---
        clicked = await scroll_and_click(page, action_name)
        if not clicked:
            print(f"      NOT FOUND on page")
            page.remove_listener("response", handler)
            continue

        # --- Wait for CDN URL (up to 30s) ---
        print(f"      waiting for video URL...", end=" ", flush=True)
        try:
            await asyncio.wait_for(capture_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            # DOM fallback
            try:
                src = await page.evaluate("""() => {
                    const v = document.querySelector('video');
                    return v ? (v.src || v.currentSrc || '') : '';
                }""")
                if src and is_cdn_video(src):
                    captured_ref["url"] = src
            except Exception:
                pass

        captured_url = captured_ref["url"]

        if not captured_url:
            print("no URL found")
            page.remove_listener("response", handler)
            await dismiss_popups(page)
            await page.wait_for_timeout(2000)
            continue

        short = captured_url.split("?")[0].split("/")[-1]
        print(f"got it ({short})")

        # --- Wait for video to FINISH PLAYING ---
        print(f"      playing: ", end="", flush=True)
        await wait_for_video_to_finish(page, timeout_s=120)

        page.remove_listener("response", handler)

        # --- Now download the complete video ---
        print(f"      downloading...", end=" ", flush=True)
        if download(captured_url, dest, cookies):
            ok_count += 1

        # --- Brief pause before next action ---
        await page.wait_for_timeout(2000)

    print(f"\n  {char_name}: {ok_count}/{len(actions)} downloaded")


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
        for slug, name, actions in CHARACTERS:
            await scrape_character(page, slug, name, actions, cookies)
        await page.close()

        await browser.close()

    print("\n" + "=" * 60)
    print("DONE!")
    print(f"Output: {OUTPUT_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
