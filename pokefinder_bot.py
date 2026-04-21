"""
PokeFinder Discord Bot
- Saves TrackaLacker alerts to Supabase instantly
- Fetches direct retailer URL via Supabase Edge Function proxy
- Polls price every 60s to detect sell-out
- Sends SMS notifications through Resend HTTPS
"""

import discord
import asyncio
import re
import os
import httpx
from supabase import create_client

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN", "")
SUPABASE_URL       = "https://efkeafzzcvupjsrinoti.supabase.co"
SUPABASE_KEY       = os.getenv("SUPABASE_KEY", "")
RESEND_API_KEY     = os.getenv("RESEND_API_KEY", "")
WATCH_CHANNELS     = ["pokemon"]
TRACKALACKER_BOT   = "trackalacker bot"
POLL_INTERVAL      = 60
POLL_MAX_TIME      = 3600

EDGE_PROXY_URL = "https://efkeafzzcvupjsrinoti.supabase.co/functions/v1/trackalacker-proxy"

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


def send_via_resend(to_email: str, message_text: str) -> bool:
    if not RESEND_API_KEY:
        print("[PokeFinder] Missing RESEND_API_KEY")
        return False

    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "from": "PokeFinder <onboarding@resend.dev>",
        "to": [to_email],
        "subject": " ",
        "text": message_text,
    }

    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers=headers,
            json=payload,
            timeout=20,
        )
        if resp.status_code in (200, 201):
            return True
        print(f"[PokeFinder] Resend failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[PokeFinder] Resend error: {e}")
    return False


def send_sms_confirmation(phone: str, carrier: str):
    gateway = CARRIER_GATEWAYS.get(carrier.lower().replace(" ", ""))
    if not gateway or not phone:
        return

    sms_email = gateway.format(number=phone)
    msg_text = (
        "Welcome to PokeFinder Alerts!\n\n"
        "You're now subscribed to live restock notifications.\n"
        "We'll text you the second a Pokemon TCG product drops at "
        "Target, Walmart, Costco, and more.\n\n"
        "Reply STOP to unsubscribe."
    )

    ok = send_via_resend(sms_email, msg_text)
    if ok:
        print(f"[PokeFinder] Confirmation SMS sent to {sms_email}")


supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def extract_slug(trackalacker_url: str) -> str | None:
    match = re.search(r'/products/showcase/([^?&/]+)', trackalacker_url)
    return match.group(1) if match else None


async def fetch_json(slug: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(EDGE_PROXY_URL, params={"slug": slug})
            if resp.status_code == 200:
                data = resp.json()
                if "error" not in data:
                    print(f"[PokeFinder] JSON fetched via proxy for {slug}")
                    return data
                print(f"[PokeFinder] Proxy error: {data['error']}")
            else:
                print(f"[PokeFinder] Proxy returned {resp.status_code}")
    except Exception as e:
        print(f"[PokeFinder] Proxy fetch error: {e}")
    return None


async def get_direct_url(trackalacker_url: str, retailer: str, alert_price: float) -> str | None:
    slug = extract_slug(trackalacker_url)
    if not slug:
        return None

    data = await fetch_json(slug)
    if not data:
        return None

    listings = data.get("product", {}).get("listings", [])
    retailer_upper = retailer.upper()
    best_match = None

    for listing in listings:
        provider = listing.get("provider", {}).get("display_name", "").upper()
        current = listing.get("current_status", {})
        price = listing.get("price") or 0
        in_stock = current.get("online_availability", False)
        direct_url = listing.get("url", "")

        if not direct_url or not in_stock:
            continue

        if retailer_upper in provider or provider in retailer_upper:
            if abs(price - alert_price) < 0.01:
                print(f"[PokeFinder] Direct URL found (exact): {direct_url[:80]}")
                return direct_url
            best_match = direct_url

    if best_match:
        print(f"[PokeFinder] Direct URL found (best): {best_match[:80]}")
        return best_match

    return None


async def fetch_price_from_json(trackalacker_url: str, retailer: str) -> float | None:
    slug = extract_slug(trackalacker_url)
    if not slug:
        return None

    data = await fetch_json(slug)
    if not data:
        return None

    retailer_upper = retailer.upper()
    for listing in data.get("product", {}).get("listings", []):
        provider = listing.get("provider", {}).get("display_name", "").upper()
        if retailer_upper in provider or provider in retailer_upper:
            price = listing.get("current_status", {}).get("price")
            if price is not None:
                return float(price)

    return None


async def poll_price(discord_msg_id: str, trackalacker_url: str, retailer: str, original_price: float):
    elapsed = 0
    print(f"[PokeFinder] Polling started: {discord_msg_id} @ ${original_price}")

    while elapsed < POLL_MAX_TIME:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        current_price = await fetch_price_from_json(trackalacker_url, retailer)
        if current_price is None:
            continue

        if abs(current_price - original_price) > 0.01:
            print(f"[PokeFinder] Price changed {discord_msg_id}: ${original_price} -> ${current_price} - ENDED")
            try:
                supabase.table("restocks").update({"status": "ENDED"}).eq("discord_msg_id", discord_msg_id).execute()
            except Exception as e:
                print(f"[ERROR] Mark ENDED failed: {e}")
            return

        print(f"[PokeFinder] Poll tick: {discord_msg_id} still ${current_price}")

    print(f"[PokeFinder] Poll timeout: {discord_msg_id}")


def send_sms_notifications(product: str, retailer: str, price: float | None, url: str):
    try:
        result = (
            supabase.table("alert_subscriptions")
            .select("phone_number, carrier, retailers")
            .eq("active", True)
            .execute()
        )

        if not result.data:
            return

        price_str = f"${price:.2f}" if price is not None else "N/A"
        msg_text = f"POKEFINDER DROP\n{product}\n{retailer} - {price_str}\n{url}"

        sent = 0
        for sub in result.data:
            sub_retailers = sub.get("retailers") or []
            if sub_retailers and retailer.upper() not in [r.upper() for r in sub_retailers]:
                continue

            phone = re.sub(r"\D", "", sub.get("phone_number", ""))
            carrier = sub.get("carrier", "").lower().replace(" ", "")
            gateway = CARRIER_GATEWAYS.get(carrier)

            if not phone or not gateway:
                continue

            sms_email = gateway.format(number=phone)
            ok = send_via_resend(sms_email, msg_text)

            if ok:
                sent += 1
            else:
                print(f"[PokeFinder] SMS failed to {sms_email}")

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
    product_name = re.sub(r"\s+is in stock.*$", "", product_name, flags=re.IGNORECASE).strip()
    if not product_name:
        return None

    retailer = None
    price = None
    trackalacker_url = embed.url or None

    for field in embed.fields:
        name = (field.name or "").strip().upper()
        value = (field.value or "").strip()

        if name == "RETAILER":
            retailer = value.upper()
        elif name == "PRICE":
            match = re.search(r"\$?([\d,]+\.?\d*)", value)
            if match:
                price = float(match.group(1).replace(",", ""))

    if not retailer:
        for known in ["WALMART", "TARGET", "COSTCO", "SAM'S CLUB", "GAMESTOP", "AMAZON", "BESTBUY", "BEST BUY", "DICK'S"]:
            if known in title_text.upper():
                retailer = known
                break

    if not retailer or not product_name:
        return None

    return {
        "product_name": product_name,
        "retailer": retailer,
        "price": price,
        "url": trackalacker_url,
        "trackalacker_url": trackalacker_url,
        "availability": "ONLINE",
        "status": "LIVE",
        "source": "trackalacker",
        "discord_msg_id": str(message.id),
    }


def save_restock(data: dict) -> bool:
    try:
        supabase.table("restocks").insert(data).execute()

        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            supabase.table("drop_patterns").insert({
                "retailer": data.get("retailer", "UNKNOWN"),
                "dropped_at": now.isoformat(),
                "day_of_week": now.weekday(),
                "hour_of_day": now.hour,
                "product_name": data.get("product_name"),
                "price": float(data.get("price", 0)) if data.get("price") else None
            }).execute()
        except Exception as pe:
            print(f"[WARN] drop_patterns insert failed: {pe}")

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
    print(f"[PokeFinder] Proxy: {EDGE_PROXY_URL}")


@client.event
async def on_message(message: discord.Message):
    if not hasattr(message.channel, "name"):
        return
    if message.channel.name not in WATCH_CHANNELS:
        return
    if TRACKALACKER_BOT not in message.author.name.lower():
        return

    restock = parse_trackalacker(message)
    if not restock:
        return

    saved = save_restock(restock)
    if not saved:
        print(f"[PokeFinder] Save failed: {restock['discord_msg_id']}")
        return

    print(f"[PokeFinder] Saved: {restock['product_name']} @ {restock['retailer']}")

    final_url = restock.get("trackalacker_url") or ""

    if restock.get("trackalacker_url") and restock.get("retailer") and restock.get("price") is not None:
        direct_url = await get_direct_url(
            restock["trackalacker_url"],
            restock["retailer"],
            restock["price"]
        )
        if direct_url:
            update_url(restock["discord_msg_id"], direct_url)
            final_url = direct_url
            print(f"[PokeFinder] Direct URL saved: {direct_url[:80]}")
        else:
            print(f"[PokeFinder] No direct URL found, keeping TrackaLacker link")

    if restock.get("trackalacker_url") and restock.get("price") is not None:
        asyncio.create_task(
            poll_price(
                restock["discord_msg_id"],
                restock["trackalacker_url"],
                restock["retailer"],
                restock["price"]
            )
        )

    asyncio.create_task(
        asyncio.to_thread(
            send_sms_notifications,
            restock["product_name"],
            restock["retailer"],
            restock.get("price"),
            final_url
        )
    )


@client.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not hasattr(after.channel, "name"):
        return
    if after.channel.name not in WATCH_CHANNELS:
        return
    if TRACKALACKER_BOT not in after.author.name.lower():
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
