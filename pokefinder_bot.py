"""
PokeFinder Discord Bot
Reads Trackalacker alerts from the #pokemon channel
and saves them to Supabase restocks table.

Requirements:
    pip install discord.py-self supabase python-dotenv httpx

Usage:
    python pokefinder_bot.py
"""

import discord
import asyncio
import re
import os
import httpx
from datetime import datetime
from supabase import create_client

# ── CONFIG ──────────────────────────────────────────
DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN", "YOUR_DISCORD_TOKEN_HERE")
SUPABASE_URL    = "https://efkeafzzcvupjsrinoti.supabase.co"
SUPABASE_KEY    = os.getenv("SUPABASE_KEY", "YOUR_SUPABASE_SERVICE_ROLE_KEY_HERE")

WATCH_CHANNELS  = ["pokemon"]
TRACKALACKER_BOT_NAME = "trackalacker bot"
# ────────────────────────────────────────────────────

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
client   = discord.Client()


async def extract_direct_url(trackalacker_url: str, retailer: str) -> str:
    """
    Fetch the TrackaLacker showcase page and extract the direct
    retailer URL (Amazon, Walmart, etc.) from the HTML.
    Falls back to the TrackaLacker URL if anything fails.
    """
    if not trackalacker_url:
        return trackalacker_url

    # Retailer domains to look for
    retailer_domains = {
        "AMAZON":     "amazon.com",
        "WALMART":    "walmart.com",
        "TARGET":     "target.com",
        "COSTCO":     "costco.com",
        "SAM'S CLUB": "samsclub.com",
        "GAMESTOP":   "gamestop.com",
        "BEST BUY":   "bestbuy.com",
        "BESTBUY":    "bestbuy.com",
    }

    domain = retailer_domains.get(retailer.upper())
    if not domain:
        return trackalacker_url

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(follow_redirects=True, timeout=8) as client_http:
            resp = await client_http.get(trackalacker_url, headers=headers)
            html = resp.text

        # Find all hrefs containing the retailer domain
        matches = re.findall(rf'href=["\']([^"\']*{re.escape(domain)}[^"\']*)["\']', html)
        if matches:
            # Prefer links that look like product pages (contain /dp/, /ip/, /p/)
            for m in matches:
                if any(x in m for x in ['/dp/', '/ip/', '/p/', '/product']):
                    return m
            return matches[0]  # fallback to first match

    except Exception as e:
        print(f"[BOT] Could not extract direct URL: {e}")

    return trackalacker_url


def parse_trackalacker(message: discord.Message) -> dict | None:
    """
    Parse a Trackalacker embed message into a clean restock dict.
    Returns None if the message doesn't look like a restock alert.
    """
    if not message.embeds:
        return None

    embed = message.embeds[0]

    title_text = (embed.title or "") + (message.content or "")
    if "IN STOCK" not in title_text.upper():
        return None

    # Product name — strip "is in stock at Retailer"
    product_name = embed.title or ""
    product_name = re.sub(r'\s+is in stock.*$', '', product_name, flags=re.IGNORECASE).strip()
    if not product_name:
        return None

    retailer     = None
    price        = None
    trackalacker_url = embed.url or None
    availability = "ONLINE"

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
        "product_name":      product_name,
        "retailer":          retailer,
        "price":             price,
        "url":               trackalacker_url,  # will be replaced with direct URL async
        "trackalacker_url":  trackalacker_url,
        "availability":      availability,
        "status":            "LIVE",
        "source":            "trackalacker",
        "discord_msg_id":    str(message.id),
    }


def save_restock(data: dict) -> bool:
    """Save a restock to Supabase. Returns True if saved, False if duplicate."""
    try:
        supabase.table("restocks").insert(data).execute()
        return True
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            return False
        print(f"[ERROR] Supabase insert failed: {e}")
        return False


def update_url(discord_msg_id: str, direct_url: str):
    """Update the URL for a saved restock with the direct retailer link."""
    try:
        supabase.table("restocks") \
            .update({"url": direct_url}) \
            .eq("discord_msg_id", discord_msg_id) \
            .execute()
    except Exception as e:
        print(f"[ERROR] Failed to update URL: {e}")


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

    restock = parse_trackalacker(message)
    if not restock:
        return

    saved = save_restock(restock)
    if saved:
        print(f"[PokeFinder] Saved: {restock['product_name']} @ {restock['retailer']}")
        # Now try to get the direct retailer URL in the background
        if restock.get("trackalacker_url"):
            asyncio.create_task(
                fetch_and_update_url(restock["discord_msg_id"], restock["trackalacker_url"], restock["retailer"])
            )
    else:
        print(f"[PokeFinder] Duplicate skipped: {restock['discord_msg_id']}")


async def fetch_and_update_url(discord_msg_id: str, trackalacker_url: str, retailer: str):
    """Background task: fetch direct URL and update the DB row."""
    direct_url = await extract_direct_url(trackalacker_url, retailer)
    if direct_url and direct_url != trackalacker_url:
        update_url(discord_msg_id, direct_url)
        print(f"[PokeFinder] Updated URL → {direct_url[:60]}...")
    else:
        print(f"[PokeFinder] Kept TrackaLacker URL (no direct found)")


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
            print(f"[ERROR] Failed to update status: {e}")


if __name__ == "__main__":
    print("[PokeFinder] Starting...")
    client.run(DISCORD_TOKEN)


# ── CONFIG ──────────────────────────────────────────
DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN", "YOUR_DISCORD_TOKEN_HERE")
SUPABASE_URL    = "https://efkeafzzcvupjsrinoti.supabase.co"
SUPABASE_KEY    = os.getenv("SUPABASE_KEY", "YOUR_SUPABASE_SERVICE_ROLE_KEY_HERE")

# The channel(s) to watch — add more if needed
WATCH_CHANNELS  = ["pokemon", "pokemon-cards"]

# Only process messages from this bot
TRACKALACKER_BOT_NAME = "trackalacker bot"
# ────────────────────────────────────────────────────

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
client   = discord.Client()


def parse_trackalacker(message: discord.Message) -> dict | None:
    """
    Parse a Trackalacker embed message into a clean restock dict.
    Returns None if the message doesn't look like a restock alert.
    """

    # Must have embeds
    if not message.embeds:
        return None

    embed = message.embeds[0]

    # Check it's an IN STOCK alert (not ended/sold out)
    title_text = (embed.title or "") + (message.content or "")
    if "IN STOCK" not in title_text.upper():
        return None

    # Product name — from embed title, strip "is in stock at Retailer"
    product_name = embed.title or ""
    product_name = re.sub(r'\s+is in stock.*$', '', product_name, flags=re.IGNORECASE).strip()
    if not product_name:
        return None

    # Parse fields from embed
    retailer     = None
    price        = None
    url          = embed.url or None
    availability = "ONLINE"  # Trackalacker only tracks online

    for field in embed.fields:
        name  = (field.name  or "").strip().upper()
        value = (field.value or "").strip()

        if name == "RETAILER":
            retailer = value.upper()

        elif name == "PRICE":
            # Extract numeric price e.g. "$49.99"
            match = re.search(r'\$?([\d,]+\.?\d*)', value)
            if match:
                price = float(match.group(1).replace(',', ''))

    if not retailer:
        # Try to get retailer from title/description
        for known in ["WALMART","TARGET","COSTCO","SAM'S CLUB","GAMESTOP","AMAZON","BESTBUY","BEST BUY"]:
            if known in title_text.upper():
                retailer = known
                break

    if not retailer or not product_name:
        return None

    return {
        "product_name":   product_name,
        "retailer":       retailer,
        "price":          price,
        "url":            url,
        "availability":   availability,
        "status":         "LIVE",
        "source":         "trackalacker",
        "discord_msg_id": str(message.id),
    }


def save_restock(data: dict) -> bool:
    """Save a restock to Supabase. Returns True if saved, False if duplicate."""
    try:
        result = supabase.table("restocks").insert(data).execute()
        return True
    except Exception as e:
        # Unique constraint on discord_msg_id — already saved
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            return False
        print(f"[ERROR] Supabase insert failed: {e}")
        return False


@client.event
async def on_ready():
    print(f"[PokeFinder Bot] Logged in as {client.user}")
    print(f"[PokeFinder Bot] Watching channels: {WATCH_CHANNELS}")
    print(f"[PokeFinder Bot] Scanning for Trackalacker alerts...")


@client.event
async def on_message(message: discord.Message):
    # Only care about the channels we're watching
    if not hasattr(message.channel, 'name'):
        return
    if message.channel.name not in WATCH_CHANNELS:
        return

    # Only care about TrackaLacker bot messages
    if TRACKALACKER_BOT_NAME not in message.author.name.lower():
        return

    print(f"[BOT] Trackalacker message in #{message.channel.name}: {message.content[:80]}")

    restock = parse_trackalacker(message)
    if not restock:
        print(f"[BOT] Skipped — not a restock alert")
        return

    saved = save_restock(restock)
    if saved:
        print(f"[BOT] ✅ Saved: {restock['product_name']} @ {restock['retailer']} — ${restock['price']}")
    else:
        print(f"[BOT] ⚠️  Duplicate skipped: {restock['discord_msg_id']}")


@client.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """
    Trackalacker sometimes edits messages when stock ends.
    If the edit says OUT OF STOCK / SOLD OUT, update status to ENDED.
    """
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
            print(f"[BOT] 🔴 Marked ENDED: {after.id}")
        except Exception as e:
            print(f"[ERROR] Failed to update status: {e}")


if __name__ == "__main__":
    print("[PokeFinder Bot] Starting...")
    client.run(DISCORD_TOKEN)
