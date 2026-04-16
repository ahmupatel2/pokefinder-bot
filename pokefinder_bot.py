"""
PokeFinder Discord Bot
- Saves Trackalacker alerts to Supabase instantly
- Polls price every 60s to detect sell-out
- Sends email-to-SMS text notifications
- Logs full embed/component data to find direct URLs
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


def debug_message(message: discord.Message):
    """Print everything in a message to find where the direct URL hides."""
    print(f"\n=== DEBUG MESSAGE {message.id} ===")
    print(f"Content: {message.content[:200]}")
    print(f"Components: {message.components}")
    
    for i, embed in enumerate(message.embeds):
        print(f"--- Embed {i} ---")
        print(f"  title: {embed.title}")
        print(f"  url: {embed.url}")
        print(f"  description: {embed.description}")
        print(f"  color: {embed.color}")
        for field in embed.fields:
            print(f"  field: {field.name} = {field.value}")
        if embed.author:
            print(f"  author: {embed.author.name} | url: {embed.author.url}")
        if embed.footer:
            print(f"  footer: {embed.footer.text}")
        if embed.image:
            print(f"  image: {embed.image.url}")
        if embed.thumbnail:
            print(f"  thumbnail: {embed.thumbnail.url}")
    
    # Check for buttons in components
    for comp in message.components:
        print(f"  component type: {type(comp).__name__}")
        if hasattr(comp, 'children'):
            for child in comp.children:
                print(f"    child: {type(child).__name__} label={getattr(child,'label','')} url={getattr(child,'url','')}")
    print("=== END DEBUG ===")


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
    direct_url = None

    # Check embed description for direct URLs
    if embed.description:
        url_match = re.search(r'https?://(?:www\.)?(?:amazon|walmart|target|costco|samsclub|gamestop|bestbuy|dickssporting)[^\s)>"]+', embed.description)
        if url_match:
            direct_url = url_match.group(0)
            print(f"[PokeFinder] Direct URL from description: {direct_url[:80]}")

    for field in embed.fields:
        name  = (field.name  or "").strip().upper()
        value = (field.value or "").strip()
        if name == "RETAILER":
            retailer = value.upper()
        elif name == "PRICE":
            match = re.search(r'\$?([\d,]+\.?\d*)', value)
            if match:
                price = float(match.group(1).replace(',', ''))
        # Check field values for direct URLs
        url_match = re.search(r'https?://(?:www\.)?(?:amazon|walmart|target|costco|samsclub|gamestop|bestbuy|dickssporting)[^\s)>"]+', value)
        if url_match:
            direct_url = url_match.group(0)
            print(f"[PokeFinder] Direct URL from field '{name}': {direct_url[:80]}")

    # Check message components (buttons) for direct URLs
    for comp in message.components:
        if hasattr(comp, 'children'):
            for child in comp.children:
                url = getattr(child, 'url', None)
                label = getattr(child, 'label', '')
                if url and any(d in url for d in ['amazon', 'walmart', 'target', 'costco', 'samsclub', 'gamestop', 'bestbuy', 'dickssporting']):
                    direct_url = url
                    print(f"[PokeFinder] Direct URL from button '{label}': {direct_url[:80]}")

    if not retailer:
        for known in ["WALMART","TARGET","COSTCO","SAM'S CLUB","GAMESTOP","AMAZON","BESTBUY","BEST BUY","DICK'S"]:
            if known in title_text.upper():
                retailer = known
                break
    if not retailer or not product_name:
        return None

    return {
        "product_name":     product_name,
        "retailer":         retailer,
        "price":            price,
        "url":              direct_url or trackalacker_url,
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


@client.event
async def on_ready():
    print(f"[PokeFinder] Logged in as {client.user}")
    print(f"[PokeFinder] Watching: {WATCH_CHANNELS}")
    for guild in client.guilds:
        print(f"[PokeFinder] Server: {guild.name}")


@client.event
async def on_message(message: discord.Message):
    if not hasattr(message.channel, 'name'):
        return
    if message.channel.name not in WATCH_CHANNELS:
        return
    if TRACKALACKER_BOT_NAME not in message.author.name.lower():
        return

    # Debug log the full message structure
    debug_message(message)

    restock = parse_trackalacker(message)
    if not restock:
        return
    saved = save_restock(restock)
    if not saved:
        print(f"[PokeFinder] Save failed: {restock['discord_msg_id']}")
        return
    print(f"[PokeFinder] Saved: {restock['product_name']} @ {restock['retailer']} | URL: {restock['url'][:60] if restock['url'] else 'none'}")

    if restock.get("trackalacker_url") and restock.get("price") is not None:
        asyncio.create_task(poll_price(restock["discord_msg_id"], restock["trackalacker_url"], restock["price"]))
    asyncio.create_task(asyncio.to_thread(send_sms_notifications, restock["product_name"], restock["retailer"], restock.get("price"), restock.get("url", "")))


@client.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not hasattr(after.channel, 'name'):
        return
    if after.channel.name not in WATCH_CHANNELS:
        return
    if TRACKALACKER_BOT_NAME not in after.author.name.lower():
        return
    title = (after.embeds[0].title if after.embeds else "") or ""
    content = (title + (after.content or "")).upper()
    if any(x in content for x in ["OUT OF STOCK", "SOLD OUT", "ENDED", "NO LONGER"]):
        try:
            supabase.table("restocks").update({"status": "ENDED"}).eq("discord_msg_id", str(after.id)).execute()
            print(f"[PokeFinder] Marked ENDED: {after.id}")
        except Exception as e:
            print(f"[ERROR] Mark ENDED failed: {e}")


if __name__ == "__main__":
    print("[PokeFinder] Starting...")
    client.run(DISCORD_TOKEN)
