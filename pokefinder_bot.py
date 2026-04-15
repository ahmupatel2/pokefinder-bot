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
from supabase import create_client

# ── CONFIG ───────────────────────────────────────────
DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN", "YOUR_DISCORD_TOKEN_HERE")
SUPABASE_URL    = "https://efkeafzzcvupjsrinoti.supabase.co"
SUPABASE_KEY    = os.getenv("SUPABASE_KEY", "YOUR_SUPABASE_SERVICE_ROLE_KEY_HERE")

WATCH_CHANNELS  = ["pokemon"]
TRACKALACKER_BOT_NAME = "trackalacker bot"
# ───────────────────────────────────────────────────

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

        matches = re.findall(rf'href=["\']([^"\']*.{re.escape(domain)}[^"\"]*)["\']', html)
        if matches:
            for m in matches:
                if any(x in m for x in ['/dp/', '/ip/', '/p/', '/product']):
                    return m
            return matches[0]

    except Exception as e:
        print(f"[PokeFinder] Could not extract direct URL: {e}")

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

    product_name = embed.title or ""
    product_name = re.sub(r'\s+is in stock.*$', '', product_name, flags=re.IGNORECASE).strip()
    if not product_name:
        return None

    retailer          = None
    price             = None
    trackalacker_url  = embed.url or None
    availability      = "ONLINE"

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
    """Save a restock to Supabase. Always saves — no duplicate blocking."""
    try:
        supabase.table("restocks").insert(data).execute()
        return True
    except Exception as e:
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
        if restock.get("trackalacker_url"):
            asyncio.create_task(
                fetch_and_update_url(restock["discord_msg_id"], restock["trackalacker_url"], restock["retailer"])
            )
    else:
        print(f"[PokeFinder] Save failed: {restock['discord_msg_id']}")


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
