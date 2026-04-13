import discord
import re
import os
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL  = "https://efkeafzzcvupjsrinoti.supabase.co"
SUPABASE_KEY  = os.getenv("SUPABASE_KEY")
WATCH_CHANNELS = ["pokemon"]
TRACKALACKER_BOT_NAME = "trackalacker bot"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
client = discord.Client()

def parse_trackalacker(message):
    if not message.embeds:
        return None
    embed = message.embeds[0]
    title_text = (embed.title or "") + (message.content or "")
    if "IN STOCK" not in title_text.upper():
        return None
    product_name = re.sub(r'\s+is in stock.*$', '', embed.title or "", flags=re.IGNORECASE).strip()
    if not product_name:
        return None
    retailer = None
    price = None
    url = embed.url or None
    for field in embed.fields:
        name = (field.name or "").strip().upper()
        value = (field.value or "").strip()
        if name == "RETAILER":
            retailer = value.upper()
        elif name == "PRICE":
            match = re.search(r'\$?([\d,]+\.?\d*)', value)
            if match:
                price = float(match.group(1).replace(',', ''))
    if not retailer:
        for known in ["WALMART","TARGET","COSTCO","SAM'S CLUB","GAMESTOP","AMAZON","BEST BUY"]:
            if known in title_text.upper():
                retailer = known
                break
    if not retailer or not product_name:
        return None
    return {
        "product_name": product_name,
        "retailer": retailer,
        "price": price,
        "url": url,
        "availability": "ONLINE",
        "status": "LIVE",
        "source": "trackalacker",
        "discord_msg_id": str(message.id),
    }

def save_restock(data):
    try:
        supabase.table("restocks").insert(data).execute()
        return True
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            return False
        print(f"[ERROR] {e}")
        return False

@client.event
async def on_ready():
    print(f"[PokeFinder] Logged in as {client.user}")
    print(f"[PokeFinder] Watching: {WATCH_CHANNELS}")
    for guild in client.guilds:
        print(f"[PokeFinder] Server: {guild.name}")

@client.event
async def on_message(message):
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
    else:
        print(f"[PokeFinder] Duplicate skipped")

@client.event
async def on_message_edit(before, after):
    if not hasattr(after.channel, 'name'):
        return
    if after.channel.name not in WATCH_CHANNELS:
        return
    if TRACKALACKER_BOT_NAME not in after.author.name.lower():
        return
    content = ((after.embeds[0].title if after.embeds else "") + after.content).upper()
    if any(x in content for x in ["OUT OF STOCK","SOLD OUT","ENDED"]):
        try:
            supabase.table("restocks").update({"status":"ENDED"}).eq("discord_msg_id", str(after.id)).execute()
            print(f"[PokeFinder] Marked ENDED: {after.id}")
        except Exception as e:
            print(f"[ERROR] {e}")

client.run(DISCORD_TOKEN)
