#!/usr/bin/env python3
"""
Candy.ai Live Action Scraper

Flow: click -> video plays fully -> download the LONGEST video -> next
Ignores the short idle/loop video that plays after each action ends.
"""

import asyncio
import re
from pathlib import Path
import requests
from playwright.async_api import async_playwright, Page, Response

BASE_URL = "https://candy.ai"
OUTPUT_DIR = Path.home() / "Desktop" / "Live Action 4"

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
MIN_VIDEO_SIZE = 2 * 1024 * 1024  # 2 MB — real action videos are 2-4 MB


def slugify(text):
    text = re.sub(r"[^\w\s-]", "", text.strip())
    return re.sub(r"\s+", "_", text)


def is_cdn_video(url):
    return "private-cdn.candy.ai/videos/" in url or "cdn.candy.ai/" in url


def get_content_length(url, cookies):
    """HEAD request to get file size without downloading."""
    s = requests.Session()
    for k, v in cookies.items():
        s.cookies.set(k, v)
    try:
        r = s.head(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": BASE_URL + "/",
        }, timeout=15, allow_redirects=True)
        return int(r.headers.get("content-length", 0))
    except Exception:
        return 0


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
        if mb < 1.5:
            print(f"      [TOO SMALL] {dest.name} ({mb:.1f} MB) - likely idle loop, removing")
            dest.unlink()
            return False
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


async def wait_for_video_end(page, timeout_s=120):
    """
    Wait for the action video to finish playing.
    The video has ended when:
    - v.ended is true, OR
    - v.paused and currentTime > 0, OR
    - currentTime >= duration - 0.5
    Does NOT touch the video element. Read-only polling.
    """
    await page.wait_for_timeout(3000)  # let video start loading/playing

    last_current = -1
    stuck_count = 0

    for i in range(timeout_s):
        try:
            state = await page.evaluate("""() => {
                const v = document.querySelector('video');
                if (!v) return { status: 'no_video' };
                return {
                    ended: v.ended,
                    paused: v.paused,
                    current: v.currentTime,
                    duration: v.duration || 0,
                    readyState: v.readyState,
                    src: (v.src || v.currentSrc || '').substring(0, 80),
                };
            }""")

            if state.get("status") == "no_video":
                if i > 15:
                    return
                await page.wait_for_timeout(1000)
                continue

            current = state.get("current", 0)
            duration = state.get("duration", 0)
            ended = state.get("ended", False)
            paused = state.get("paused", False)

            # Video ended naturally
            if ended:
                print(f"ended ({duration:.0f}s)", end=" ", flush=True)
                return

            # Video finished (currentTime reached duration)
            if duration > 5 and current >= duration - 0.5:
                print(f"complete ({duration:.0f}s)", end=" ", flush=True)
                return

            # Video paused after playing (some players pause at end instead of 'ended')
            if paused and current > 5:
                print(f"paused at {current:.0f}s", end=" ", flush=True)
                return

            # Show progress every 5s
            if i > 0 and i % 5 == 0 and duration > 0:
                print(f"{current:.0f}/{duration:.0f}s", end=" ", flush=True)

            # Detect if playback is stuck
            if abs(current - last_current) < 0.1:
                stuck_count += 1
                if stuck_count > 15 and current > 3:
                    # Stuck for 15s after at least 3s of play = probably done
                    print(f"stalled at {current:.0f}s", end=" ", flush=True)
                    return
            else:
                stuck_count = 0
            last_current = current

        except Exception:
            pass

        await page.wait_for_timeout(1000)

    print(f"timeout", end=" ", flush=True)


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

    # Click "Tap to start"
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

        # --- Collect ALL CDN video URLs after clicking (not just the first) ---
        collected_urls = []

        def make_handler():
            urls = []

            async def handler(resp):
                u = resp.url
                if not is_cdn_video(u):
                    return
                try:
                    ct = resp.headers.get("content-type", "")
                    cl = int(resp.headers.get("content-length", "0"))
                    if ct in VIDEO_CONTENT_TYPES or cl > 100000:
                        urls.append({"url": u, "size": cl})
                except Exception:
                    if "/videos/" in u:
                        urls.append({"url": u, "size": 0})

            return handler, urls

        handler, collected_urls = make_handler()
        page.on("response", handler)

        # --- Click ---
        clicked = await scroll_and_click(page, action_name)
        if not clicked:
            print(f"      NOT FOUND on page")
            page.remove_listener("response", handler)
            continue

        # --- Wait for the action video to fully play ---
        print(f"      playing: ", end="", flush=True)
        await wait_for_video_end(page, timeout_s=120)
        print()  # newline after progress

        # --- Wait a moment for the idle loop to start (its URL also gets captured) ---
        await page.wait_for_timeout(3000)

        page.remove_listener("response", handler)

        # --- Pick the BIGGEST video URL (action video >> idle loop) ---
        if not collected_urls:
            # Last resort: check DOM
            try:
                src = await page.evaluate("""() => {
                    const v = document.querySelector('video');
                    return v ? (v.src || v.currentSrc || '') : '';
                }""")
                if src and is_cdn_video(src):
                    collected_urls.append({"url": src, "size": 0})
            except Exception:
                pass

        if not collected_urls:
            print(f"      no video URLs captured")
            await dismiss_popups(page)
            await page.wait_for_timeout(2000)
            continue

        # Check actual sizes via HEAD for URLs without content-length
        print(f"      captured {len(collected_urls)} CDN URLs, picking largest...")
        for entry in collected_urls:
            if entry["size"] == 0:
                entry["size"] = get_content_length(entry["url"], cookies)

        # Sort by size descending, pick the biggest
        collected_urls.sort(key=lambda x: x["size"], reverse=True)
        best = collected_urls[0]
        best_mb = best["size"] / (1024 * 1024)
        short = best["url"].split("?")[0].split("/")[-1]
        print(f"      best: {short} ({best_mb:.1f} MB)")

        # --- Download ---
        if download(best["url"], dest, cookies):
            ok_count += 1

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
