import asyncio
import difflib
import os
import re
import sqlite3
import unicodedata
from pathlib import Path

import discord
from discord import app_commands


OWNER_ID = int(os.environ["OWNER_ID"])
GUILD_ID = int(os.environ["GUILD_ID"])
TOKEN = os.environ["DISCORD_TOKEN"]

# FadeHost keeps /data/storage across restarts and deployments.
DB_PATH = Path("/data/storage/videos.sqlite3")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".webm", ".mkv",
    ".avi", ".wmv", ".flv", ".mpeg", ".mpg", ".3gp",
}

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

db = sqlite3.connect(DB_PATH)
db.execute("""
    CREATE TABLE IF NOT EXISTS videos (
        message_id INTEGER NOT NULL,
        attachment_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        caption TEXT NOT NULL,
        filename TEXT NOT NULL,
        message_url TEXT NOT NULL,
        uploaded_at TEXT NOT NULL,
        PRIMARY KEY (message_id, attachment_id)
    )
""")
db.commit()


def words(text):
    # Case and Lithuanian accent insensitive: "patylet" finds "patylėt".
    text = "".join(
        char for char in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(char)
    )
    return re.findall(r"[^\W_]+", text, re.UNICODE)


def is_video(attachment):
    extension = Path(attachment.filename.lower()).suffix
    return (
        extension in VIDEO_EXTENSIONS
        or (attachment.content_type or "").lower().startswith("video/")
    )


def index_message(message):
    if message.guild is None or message.guild.id != GUILD_ID:
        return 0
    if message.author.id != OWNER_ID:
        return 0

    count = 0
    for attachment in message.attachments:
        if not is_video(attachment):
            continue
        db.execute(
            """INSERT OR REPLACE INTO videos
               (message_id, attachment_id, channel_id, caption,
                filename, message_url, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                message.id, attachment.id, message.channel.id,
                message.content, attachment.filename,
                message.jump_url, message.created_at.isoformat(),
            ),
        )
        count += 1
    if count:
        db.commit()
    return count


def matches(query_words, caption, filename):
    available = words(caption + " " + filename)
    if not available:
        return False

    # Every query word must match. Allow a close spelling for words
    # of at least four characters, while keeping short words exact.
    return all(
        any(
            q == word or (
                len(q) >= 4
                and difflib.SequenceMatcher(None, q, word).ratio() >= 0.78
            )
            for word in available
        )
        for q in query_words
    )


@bot.event
async def on_ready():
    if not getattr(bot, "commands_synced", False):
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        bot.commands_synced = True
    print(f"Ready as {bot.user}; indexing owner {OWNER_ID} in guild {GUILD_ID}")


@bot.event
async def on_message(message):
    index_message(message)


@bot.event
async def on_message_edit(before, after):
    if before.guild and before.guild.id == GUILD_ID and before.author.id == OWNER_ID:
        db.execute("DELETE FROM videos WHERE message_id = ?", (before.id,))
        db.commit()
        index_message(after)


@bot.event
async def on_raw_message_delete(payload):
    if payload.guild_id == GUILD_ID:
        db.execute("DELETE FROM videos WHERE message_id = ?", (payload.message_id,))
        db.commit()


@tree.command(name="find", description="Find your videos by caption or filename",
              guild=discord.Object(id=GUILD_ID))
@app_commands.describe(search="Words to search for")
async def find(interaction: discord.Interaction, search: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("This command is private.", ephemeral=True)
        return

    query = words(search)
    if not query:
        await interaction.response.send_message("Enter at least one word.", ephemeral=True)
        return

    rows = db.execute(
        """SELECT caption, filename, message_url, uploaded_at
           FROM videos ORDER BY uploaded_at DESC"""
    ).fetchall()
    results = [
        row for row in rows if matches(query, row[0], row[1])
    ][:10]

    if not results:
        reply = f"No videos found for **{discord.utils.escape_markdown(search)}**."
    else:
        parts = [f"🔎 Videos matching **{discord.utils.escape_markdown(search)}**:"]
        for number, (caption, filename, url, uploaded_at) in enumerate(results, 1):
            safe_name = discord.utils.escape_markdown(filename)
            safe_caption = discord.utils.escape_markdown(caption[:180])
            parts.append(
                f"\n**{number}. {safe_name}** · {uploaded_at[:10]}\n"
                f"{safe_caption or '(No caption)'}\n[Jump to video]({url})"
            )
        reply = "\n".join(parts)
        if len(reply) > 1900:
            reply = reply[:1850] + "\n… Try a more specific search."

    await interaction.response.send_message(
        reply, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


@tree.command(name="reindex", description="Index your older video uploads",
              guild=discord.Object(id=GUILD_ID))
async def reindex(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("This command is private.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    total = 0
    scanned = 0
    skipped = []

    for channel in guild.text_channels:
        try:
            async for message in channel.history(limit=None, oldest_first=True):
                scanned += 1
                total += index_message(message)
                if scanned % 100 == 0:
                    await asyncio.sleep(0)
        except (discord.Forbidden, discord.HTTPException):
            skipped.append(channel.name)

    note = f" Skipped: {', '.join(skipped[:5])}." if skipped else ""
    await interaction.followup.send(
        f"Reindex complete: scanned {scanned} messages and indexed "
        f"{total} video attachments.{note}",
        ephemeral=True,
    )


bot.run(TOKEN)
