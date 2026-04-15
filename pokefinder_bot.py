"""
PokeFinder Discord Bot
- Saves Trackalacker alerts to Supabase
- Polls price every 60s to detect sell-out
- Fetches direct retailer link via TrackaLacker login
- Sends email-to-SMS text notifications

Requirements:
    pip install discord.py-self supabase python-dotenv httpx beautifulsoup4
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

# ── CONFIG ──────────────────────────────────────────────────────────
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
# ────────────────────────────────────────────────────────────────────

# Carrier email-to-SMS gateways
CARRIER_GATEWAYS = {
    "att":       "{number}@txt.att.net",
    "verizon":   "{number}@vtext.com",
    "tmobile":   "{number}@tmomail.net",
    "sprint":    "{number}@messaging.sprintpcs.com",
    "boost":     "{number}@sms.myboostmobile.com",
    "cricket":   "{number}@sms.cricketwireless.net",
    "metro":     "{number}@mymetropcs.com",
    "uscellular": "{number}@email.uscc.net",
}

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
client   = discord.Client()

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Shared authenticated TrackaLacker session
_tl_session = None


async def get_tl_session() -> httpx.AsyncClient | None:
    """Get or create an authenticated TrackaLacker session."""
    global _tl_session
    if _tl_session is not None:
        return _tl_session

    if not TRACKALACKER_EMAIL or not TRACKALACKER_PASSWORD:
        return None

    try:
        session = httpx.AsyncClient(follow_redirects=True, timeout=15, headers=HTTP_HEADERS)

        # Get login page + CSRF token
        login_page = await session.get("https://www.trackalacker.com/users/sign_in")
        soup = BeautifulSoup(login_page.text, "html.parser")
        csrf_meta = soup.find("meta", attrs={"name": "csrf-token"})
        if not csrf_meta:
            print("[PokeFinder] TrackaLacker: no CSRF token found")
            return None
        csrf = csrf_meta["content"]

        # Submit login form
        login_resp = await session.post(
            "https://www.trackalacker.com/users/sign_in",
            data={
                "authenticity_token": csrf,
                "user[email]": TRACKALACKER_EMAIL,
                "user[password]": TRACKALACKER_PASSWORD,
                "user[remember_me]": "1",
                "commit": "Log in"
            },
            headers={**HTTP_HEADERS, "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": "https://www.trackalacker.com/users/sign_in"}
        )

        # Check if logged in
        if "sign_in" in str(login_resp.url) or "Invalid" in login_resp.text:
            print("[PokeFinder] TrackaLacker login failed")
            return None

        print("[PokeFinder] TrackaLacker login successful")
        _tl_session = session
        return session

    except Exception as e:
        print(f"[PokeFinder] TrackaLacker login error: {e}")
        return None


async def get_direct_url(notification_url: str) -> str | None:
    """
    Log into TrackaLacker, fetch the notification page,
    and grab the direct retailer link from the ADD TO CART button.
    """
    if not notification_url:
        return None

    try:
        session = await get_tl_session()
        if not session:
            return None

        resp = await session.get(notification_url)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Find the ADD TO CART / btn-primary link with external retailer URL
        for tag in soup.find_all("a", class_=lambda c: c and "btn-primary" in c):
            href = tag.get("href", "")
            # Must be an external retailer link
            if href and href.startswith("http") and "trackalacker" not in href:
                print(f"[PokeFinder] Direct URL found: {href[:80]}")
                return href

        # Fallback: any gtm-click-trigger link pointing to a retailer
        for tag in soup.find_all("a", class_=lambda c: c and "gtm-click-trigger" in c):
            href = tag.get("href", "")
            if href and href.startswith("http") and "trackalacker" not in href:
                return href

    except Exception as e:
        print(f"[PokeFinder] get_direct_url error: {e}")

    return None


async def fetch_price_from_page(url: str, session=None) -> float | None:
    """Fetch current price from TrackaLacker page."""
    try:
        s = session or httpx.AsyncClient(follow_redirects=True, timeout=10, headers=HTTP_HEADERS)
        resp = await s.get(url)
        html = resp.text

        # Price in meta tags
        soup = BeautifulSoup(html, "html.parser")
        for meta in soup.find_all("meta"):
            prop = meta.get("property", "") or meta.get("name", "")
            if "price" in prop.lower():
                val = meta.get("content", "")
                match = re.search(r'[\d,]+\.?\d*', val)
                if match:
                    return float(match.group().replace(",", ""))

        # Fallback: first dollar amount in page
        price_matches = re.findall(r'\$([\d,]+\.\d{2})', html)
        if price_matches:
            return float(price_matches[0].replace(",", ""))

    except Exception as e:
        print(f"[PokeFinder] Price fetch error: {e}")

    return None


async def poll_price(discord_msg_id: str, url: str, original_price: float):
    """Poll price every 60s. Mark ENDED when price changes."""
    elapsed = 0
    print(f"[PokeFinder] Polling started: {discord_msg_id} @ ${original_price}")
    session = await get_tl_session()

    while elapsed < POLL_MAX_TIME:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        current_price = await fetch_price_from_page(url, session)
        if current_price is None:
            continue

        if current_price != original_price:
            print(f"[PokeFinder] Price changed {discord_msg_id}: ${original_price} -> ${current_price} — ENDED")
            try:
                supabase.table("restocks") \
                    .update({"status": "ENDED"}) \
                    .eq("discord_msg_id", discord_msg_id) \
                    .execute()
            except Exception as e:
                print(f"[ERROR] Mark ENDED failed: {e}")
            return

        print(f"[PokeFinder] Poll tick: {discord_msg_id} still ${current_price}")

    print(f"[PokeFinder] Poll timeout: {discord_msg_id}")


def send_sms_notifications(product: str, retailer: str, price: float, url: str):
    """Send email-to-SMS texts to all subscribed users for this retailer."""
    if not GMAIL_USER or not GMAIL_PASS:
        return

    try:
        # Get subscribers who want alerts for this retailer
        result = supabase.table("alert_subscriptions") \
            .select("phone_number, carrier, retailers") \
            .eq("active", True) \
            .execute()

        if not result.data:
            return

        # Build message
        price_str = f"${price:.2f}" if price else "N/A"
        msg_text = f"POKEFINDER DROP\n{product}\n{retailer} - {price_str}\n{url}"

        sent = 0
        for sub in result.data:
            # Check if this subscriber wants this retailer
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
                print(f"[PokeFinder] SMS send failed to {sms_email}: {e}")

        if sent:
            print(f"[PokeFinder] Sent {sent} SMS notifications")

    except Exception as e:
        print(f"[PokeFinder] SMS notifications error: {e}")


def parse_trackalacker(message: discord.Message) -> dict | None:
    """Parse a Trackalacker embed into a restock dict."""
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

    retailer         = None
    price            = None
    trackalacker_url = embed.url or None
    availability     = "ONLINE"

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
        "availability":     availability,
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
        supabase.table("restocks") \
            .update({"url": direct_url}) \
            .eq("discord_msg_id", discord_msg_id) \
            .execute()
    except Exception as e:
        print(f"[ERROR] URL update failed: {e}")


@client.event
async def on_ready():
    print(f"[PokeFinder] Logged in as {client.user}")
    print(f"[PokeFinder] Watching: {WATCH_CHANNELS}")
    for guild in client.guilds:
        print(f"[PokeFinder] Server: {guild.name}")
    # Pre-login to TrackaLacker on startup
    asyncio.create_task(get_tl_session())


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

    # Fire all background tasks simultaneously
    tasks = []

    # 1. Get direct retailer URL
    if restock.get("trackalacker_url"):
        tasks.append(asyncio.create_task(
            fetch_and_update_url(restock["discord_msg_id"], restock["trackalacker_url"])
        ))

    # 2. Start price polling
    if restock.get("trackalacker_url") and restock.get("price") is not None:
        tasks.append(asyncio.create_task(
            poll_price(restock["discord_msg_id"], restock["trackalacker_url"], restock["price"])
        ))

    # 3. Send SMS notifications
    tasks.append(asyncio.create_task(
        asyncio.to_thread(
            send_sms_notifications,
            restock["product_name"],
            restock["retailer"],
            restock.get("price"),
            restock.get("trackalacker_url", "")
        )
    ))


async def fetch_and_update_url(discord_msg_id: str, notification_url: str):
    """Background: get direct retailer link and update DB."""
    direct_url = await get_direct_url(notification_url)
    if direct_url:
        update_url(discord_msg_id, direct_url)
        print(f"[PokeFinder] Direct URL saved: {direct_url[:60]}")
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
            supabase.table("restocks") \
                .update({"status": "ENDED"}) \
                .eq("discord_msg_id", str(after.id)) \
                .execute()
            print(f"[PokeFinder] Marked ENDED: {after.id}")
        except Exception as e:
            print(f"[ERROR] Mark ENDED failed: {e}")


if __name__ == "__main__":
    print("[PokeFinder] Starting...")
    client.run(DISCORD_TOKEN)
