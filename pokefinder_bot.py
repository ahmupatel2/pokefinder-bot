"""
PokeFinder Discord Bot
- Saves Trackalacker alerts to Supabase instantly
- Logs into TrackaLacker via httpx (bypasses Cloudflare), passes cookies to Playwright
- Uses Playwright to render the page and grab the direct retailer ADD TO CART link
- Polls price every 60s to detect sell-out
- Sends email-to-SMS text notifications
"""

import discord
import asyncio
import re
import os
import smtplib
import httpx
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
from supabase import create_client

DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN", "")
SUPABASE_URL          = "https://efkeafzzcvupjsrinoti.supabase.co"
SUPABASE_KEY          = os.getenv("SUPABASE_KEY", "")
TRACKALACKER_EMAIL    = os.getenv("TRACKALACKER_EMAIL", "")
TRACKALACKER_PASSWORD = os.getenv("TRACKALACKER_PASSWORD", "")
GMAIL_USER            = os.getenv("GMAIL_USER", "")
GMAIL_PASS            = os.getenv("GMAIL_PASS", "")
WATCH_CHANNELS        = ["pokemon"]
TRACKALACKER_BOT_NAME = "trackalacker bot"
POLL_INTERVAL         = 60
POLL_MAX_TIME         = 3600

CARRIER_GATEWAYS = {
    "att":        "{number}@txt.att.net",
    "verizon":    "{number}@vtext.com",
    "tmobile":    "{number}@tmomail.net",
    "sprint":     "{number}@messaging.sprintpcs.com",
    "boost":      "{number}@sms.myboostmobile.com",
    "cricket":    "{number}@sms.cricketwireless.net",
    "metro":      "{number}@mymetropcs.com",
    "uscellular": "{number}@email.uscc.net",
}

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
client   = discord.Client()

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_pw_browser  = None
_pw_context  = None
_tl_cookies  = None  # Cookies from httpx login


async def httpx_login() -> dict | None:
    """Log into TrackaLacker via httpx. Returns cookies dict or None."""
    if not TRACKALACKER_EMAIL or not TRACKALACKER_PASSWORD:
        return None
    try:
        session = httpx.Client(follow_redirects=True, timeout=15, headers=HTTP_HEADERS)
        # Get CSRF token
        login_page = session.get("https://www.trackalacker.com/users/sign_in")
        soup = BeautifulSoup(login_page.text, "html.parser")
        csrf_meta = soup.find("meta", attrs={"name": "csrf-token"})
        if not csrf_meta:
            print("[PokeFinder] No CSRF token found")
            return None
        csrf = csrf_meta["content"]

        # Submit login
        resp = session.post(
            "https://www.trackalacker.com/users/sign_in",
            data={
                "authenticity_token": csrf,
                "user[email]": TRACKALACKER_EMAIL,
                "user[password]": TRACKALACKER_PASSWORD,
                "user[remember_me]": "1",
                "commit": "Log in"
            },
            headers={
                **HTTP_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://www.trackalacker.com/users/sign_in",
                "Origin": "https://www.trackalacker.com"
            }
        )

        # Verify login success
        profile = session.get("https://www.trackalacker.com/users/edit")
        if "sign_in" in str(profile.url):
            print("[PokeFinder] TrackaLacker httpx login failed")
            return None

        # Extract all cookies
        cookies = dict(session.cookies)
        print(f"[PokeFinder] TrackaLacker httpx login successful ({len(cookies)} cookies)")
        return cookies

    except Exception as e:
        print(f"[PokeFinder] httpx login error: {e}")
        return None


async def get_pw_context():
    """Get or create a Playwright browser context with TrackaLacker session cookies."""
    global _pw_browser, _pw_context, _tl_cookies

    try:
        from playwright.async_api import async_playwright

        if _pw_context is not None:
            return _pw_context

        # Log in via httpx first
        _tl_cookies = await asyncio.to_thread(httpx_login) if not _tl_cookies else _tl_cookies
        # httpx_login is sync so run in thread
        if _tl_cookies is None:
            _tl_cookies = await asyncio.get_event_loop().run_in_executor(None, lambda: __import__('asyncio').run(httpx_login()))

        pw = await async_playwright().start()
        _pw_browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-setuid-sandbox"]
        )
        context = await _pw_browser.new_context(
            user_agent=HTTP_HEADERS["User-Agent"]
        )

        # Inject cookies from httpx session into Playwright
        if _tl_cookies:
            pw_cookies = [
                {"name": k, "value": v, "domain": ".trackalacker.com", "path": "/"}
                for k, v in _tl_cookies.items()
            ]
            await context.add_cookies(pw_cookies)
            print(f"[PokeFinder] Injected {len(pw_cookies)} cookies into Playwright")

        _pw_context = context
        return context

    except Exception as e:
        print(f"[PokeFinder] Playwright init error: {e}")
        return None


async def get_direct_url(notification_url: str) -> str | None:
    """Use Playwright (with injected session) to grab the ADD TO CART href."""
    if not notification_url:
        return None
    try:
        context = await get_pw_context()
        if not context:
            return None

        page = await context.new_page()
        try:
            await page.goto(notification_url, wait_until="networkidle", timeout=20000)
            try:
                await page.wait_for_selector("a.btn-primary", timeout=8000)
            except:
                pass

            links = await page.query_selector_all("a.btn-primary, a.gtm-click-trigger")
            for link in links:
                href = await link.get_attribute("href")
                if href and href.startswith("http") and "trackalacker" not in href:
                    print(f"[PokeFinder] Direct URL found: {href[:80]}")
                    return href

            # Debug: log what buttons ARE on the page
            all_links = await page.query_selector_all("a")
            btns = []
            for l in all_links[:10]:
                h = await l.get_attribute("href") or ""
                t = await l.inner_text() or ""
                if h or t:
                    btns.append(f"{t.strip()[:30]}:{h[:60]}")
            print(f"[PokeFinder] Page links: {btns}")

        finally:
            await page.close()

    except Exception as e:
        print(f"[PokeFinder] get_direct_url error: {e}")
    return None


async def fetch_price_from_page(url: str) -> float | None:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10, headers=HTTP_HEADERS) as c:
            resp = await c.get(url)
            html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        for meta in soup.find_all("meta"):
            prop = meta.get("property", "") or meta.get("name", "")
            if "price" in prop.lower():
                val = meta.get("content", "")
                match = re.search(r'[\d,]+\.?\d*', val)
                if match:
                    return float(match.group().replace(",", ""))
        price_matches = re.findall(r'\$([\d,]+\.\d{2})', html)
        if price_matches:
            return float(price_matches[0].replace(",", ""))
    except Exception as e:
        print(f"[PokeFinder] Price fetch error: {e}")
    return None


async def poll_price(discord_msg_id: str, url: str, original_price: float):
    elapsed = 0
    print(f"[PokeFinder] Polling started: {discord_msg_id} @ ${original_price}")
    while elapsed < POLL_MAX_TIME:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        current_price = await fetch_price_from_page(url)
        if current_price is None:
            continue
        if current_price != original_price:
            print(f"[PokeFinder] Price changed {discord_msg_id}: ${original_price} -> ${current_price} - ENDED")
            try:
                supabase.table("restocks").update({"status": "ENDED"}).eq("discord_msg_id", discord_msg_id).execute()
            except Exception as e:
                print(f"[ERROR] Mark ENDED failed: {e}")
            return
        print(f"[PokeFinder] Poll tick: {discord_msg_id} still ${current_price}")
    print(f"[PokeFinder] Poll timeout: {discord_msg_id}")


def send_sms_notifications(product: str, retailer: str, price: float, url: str):
    if not GMAIL_USER or not GMAIL_PASS:
        return
    try:
        result = supabase.table("alert_subscriptions").select("phone_number, carrier, retailers").eq("active", True).execute()
        if not result.data:
            return
        price_str = f"${price:.2f}" if price else "N/A"
        msg_text = f"POKEFINDER DROP\n{product}\n{retailer} - {price_str}\n{url}"
        sent = 0
        for sub in result.data:
            sub_retailers = sub.get("retailers") or []
            if sub_retailers and retailer.upper() not in [r.upper() for r in sub_retailers]:
                continue
            phone = re.sub(r'\D', '', sub.get("phone_number", ""))
            carrier = sub.get("carrier", "").lower().replace(" ", "")
            gateway = CARRIER_GATEWAYS.get(carrier)
            if not phone or not gateway:
                continue
            sms_email = gateway.format(number=phone)
            try:
                msg = MIMEText(msg_text)
                msg["From"] = GMAIL_USER
                msg["To"] = sms_email
                msg["Subject"] = ""
                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                    smtp.login(GMAIL_USER, GMAIL_PASS)
                    smtp.sendmail(GMAIL_USER, sms_email, msg.as_string())
                sent += 1
            except Exception as e:
                print(f"[PokeFinder] SMS failed to {sms_email}: {e}")
        if sent:
            print(f"[PokeFinder] Sent {sent} SMS notifications")
    except Exception as e:
        print(f"[PokeFinder] SMS error: {e}")


def parse_trackalacker(message: discord.Message) -> dict | None:
    if not message.embeds:
        return None
    embed = message.embeds[0]
    title_text = (embed.title or "") + (message.content or "")
    if "IN STOCK" not in title_text.upper():
        return None
    product_name = embed.title or ""
    product_name = re.sub(r'\s+is in stock.*$', '', product_name, flags=re.IGNORECASE).strip()
    if not product_name:
        return None
    retailer = None
    price = None
    trackalacker_url = embed.url or None
    for field in embed.fields:
        name  = (field.name  or "").strip().upper()
        value = (field.value or "").strip()
        if name == "RETAILER":
            retailer = value.upper()
        elif name == "PRICE":
            match = re.search(r'\$?([\d,]+\.?\d*)', value)
            if match:
                price = float(match.group(1).replace(',', ''))
    if not retailer:
        for known in ["WALMART","TARGET","COSTCO","SAM'S CLUB","GAMESTOP","AMAZON","BESTBUY","BEST BUY"]:
            if known in title_text.upper():
                retailer = known
                break
    if not retailer or not product_name:
        return None
    return {
        "product_name":     product_name,
        "retailer":         retailer,
        "price":            price,
        "url":              trackalacker_url,
        "trackalacker_url": trackalacker_url,
        "availability":     "ONLINE",
        "status":           "LIVE",
        "source":           "trackalacker",
        "discord_msg_id":   str(message.id),
    }


def save_restock(data: dict) -> bool:
    try:
        supabase.table("restocks").insert(data).execute()
        return True
    except Exception as e:
        print(f"[ERROR] Supabase insert failed: {e}")
        return False


def update_url(discord_msg_id: str, direct_url: str):
    try:
        supabase.table("restocks").update({"url": direct_url}).eq("discord_msg_id", discord_msg_id).execute()
    except Exception as e:
        print(f"[ERROR] URL update failed: {e}")


@client.event
async def on_ready():
    print(f"[PokeFinder] Logged in as {client.user}")
    print(f"[PokeFinder] Watching: {WATCH_CHANNELS}")
    for guild in client.guilds:
        print(f"[PokeFinder] Server: {guild.name}")
    asyncio.create_task(get_pw_context())


@client.event
async def on_message(message: discord.Message):
    if not hasattr(message.channel, 'name'):
        return
    if message.channel.name not in WATCH_CHANNELS:
        return
    if TRACKALACKER_BOT_NAME not in message.author.name.lower():
        return
    restock = parse_trackalacker(message)
    if not restock:
        return
    saved = save_restock(restock)
    if not saved:
        print(f"[PokeFinder] Save failed: {restock['discord_msg_id']}")
        return
    print(f"[PokeFinder] Saved: {restock['product_name']} @ {restock['retailer']}")
    if restock.get("trackalacker_url"):
        asyncio.create_task(fetch_and_update_url(restock["discord_msg_id"], restock["trackalacker_url"]))
    if restock.get("trackalacker_url") and restock.get("price") is not None:
        asyncio.create_task(poll_price(restock["discord_msg_id"], restock["trackalacker_url"], restock["price"]))
    asyncio.create_task(asyncio.to_thread(send_sms_notifications, restock["product_name"], restock["retailer"], restock.get("price"), restock.get("trackalacker_url", "")))


async def fetch_and_update_url(discord_msg_id: str, notification_url: str):
    direct_url = await get_direct_url(notification_url)
    if direct_url:
        update_url(discord_msg_id, direct_url)
        print(f"[PokeFinder] Direct URL saved: {direct_url[:80]}")
    else:
        print(f"[PokeFinder] No direct URL found, keeping TrackaLacker link")


@client.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not hasattr(after.channel, 'name'):
        return
    if after.channel.name not in WATCH_CHANNELS:
        return
    if TRACKALACKER_BOT_NAME not in after.author.name.lower():
        return
    content = ((after.embeds[0].title if after.embeds else "") + after.content).upper()
    if any(x in content for x in ["OUT OF STOCK", "SOLD OUT", "ENDED", "NO LONGER"]):
        try:
            supabase.table("restocks").update({"status": "ENDED"}).eq("discord_msg_id", str(after.id)).execute()
            print(f"[PokeFinder] Marked ENDED: {after.id}")
        except Exception as e:
            print(f"[ERROR] Mark ENDED failed: {e}")


async def _init_tl_cookies():
    global _tl_cookies
    _tl_cookies = await asyncio.to_thread(lambda: __import__('asyncio').new_event_loop().run_until_complete(httpx_login()) if False else _sync_httpx_login())


def _sync_httpx_login() -> dict | None:
    """Synchronous version of httpx login."""
    if not TRACKALACKER_EMAIL or not TRACKALACKER_PASSWORD:
        return None
    try:
        session = httpx.Client(follow_redirects=True, timeout=15, headers=HTTP_HEADERS)
        login_page = session.get("https://www.trackalacker.com/users/sign_in")
        soup = BeautifulSoup(login_page.text, "html.parser")
        csrf_meta = soup.find("meta", attrs={"name": "csrf-token"})
        if not csrf_meta:
            return None
        csrf = csrf_meta["content"]
        session.post(
            "https://www.trackalacker.com/users/sign_in",
            data={
                "authenticity_token": csrf,
                "user[email]": TRACKALACKER_EMAIL,
                "user[password]": TRACKALACKER_PASSWORD,
                "user[remember_me]": "1",
                "commit": "Log in"
            },
            headers={
                **HTTP_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://www.trackalacker.com/users/sign_in",
                "Origin": "https://www.trackalacker.com"
            }
        )
        profile = session.get("https://www.trackalacker.com/users/edit")
        if "sign_in" in str(profile.url):
            print("[PokeFinder] TrackaLacker login failed")
            return None
        cookies = dict(session.cookies)
        print(f"[PokeFinder] TrackaLacker login successful ({len(cookies)} cookies)")
        return cookies
    except Exception as e:
        print(f"[PokeFinder] Login error: {e}")
        return None


async def get_pw_context():
    """Get or create Playwright context with injected TrackaLacker session cookies."""
    global _pw_browser, _pw_context, _tl_cookies
    try:
        from playwright.async_api import async_playwright
        if _pw_context is not None:
            return _pw_context

        # Login via httpx (sync, runs in thread to not block event loop)
        if _tl_cookies is None:
            _tl_cookies = await asyncio.to_thread(_sync_httpx_login)

        pw = await async_playwright().start()
        _pw_browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-setuid-sandbox"]
        )
        context = await _pw_browser.new_context(user_agent=HTTP_HEADERS["User-Agent"])

        # Inject cookies so Playwright is logged in
        if _tl_cookies:
            pw_cookies = [
                {"name": k, "value": v, "domain": ".trackalacker.com", "path": "/"}
                for k, v in _tl_cookies.items()
            ]
            await context.add_cookies(pw_cookies)
            print(f"[PokeFinder] Playwright context ready with {len(pw_cookies)} session cookies")

        _pw_context = context
        return context
    except Exception as e:
        print(f"[PokeFinder] Playwright init error: {e}")
        return None


if __name__ == "__main__":
    print("[PokeFinder] Starting...")
    client.run(DISCORD_TOKEN)
