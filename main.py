"""Private video finder and Sanremo concert watcher for one Discord guild.

Secrets: DISCORD_TOKEN, OWNER_ID, GUILD_ID, TICKETMASTER_API_KEY in FadeHost.
Runtime data: /data/storage/videos.sqlite3 (persistent on FadeHost).
"""

import asyncio
import datetime as dt
import difflib
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks


OWNER_ID = int(os.environ["OWNER_ID"])
GUILD_ID = int(os.environ["GUILD_ID"])
TOKEN = os.environ["DISCORD_TOKEN"]
TM_KEY = os.environ.get("TICKETMASTER_API_KEY", "")
ROME = ZoneInfo("Europe/Rome")
LONDON = ZoneInfo("Europe/London")
TM_ROOT = "https://app.ticketmaster.com/discovery/v2"

DB_PATH = Path("/data/storage/videos.sqlite3")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".wmv",
    ".flv", ".mpeg", ".mpg", ".3gp",
}

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
db = sqlite3.connect(DB_PATH)
db.execute("PRAGMA foreign_keys=ON")
db.execute("""CREATE TABLE IF NOT EXISTS videos (
    message_id INTEGER NOT NULL, attachment_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL, caption TEXT NOT NULL,
    filename TEXT NOT NULL, message_url TEXT NOT NULL,
    uploaded_at TEXT NOT NULL, PRIMARY KEY (message_id, attachment_id)
)""")

# The original artist pool is deliberately static and auditable. A pair or group
# is included together when it tours under that name; members are also listed
# separately where they have their own concerts.
SANREMO_BY_YEAR = {
    2021: """Aiello|Annalisa|Arisa|Malika Ayane|Orietta Berti|Bugo|Colapesce Dimartino|Coma_Cose|Extraliscio|Fasma|Fulminacci|Gaia|Max Gazzè|Ghemon|Gio Evan|Irama|La Rappresentante di Lista|Lo Stato Sociale|Madame|Måneskin|Ermal Meta|Francesca Michielin|Fedez|Noemi|Random|Francesco Renga|Willie Peyote""",
    2022: """Iva Zanicchi|Achille Lauro|Aka 7even|Michele Bravi|Emma|Massimo Ranieri|Sangiovanni|Gianni Morandi|Ana Mena|Elisa|Rkomi|Ditonellapiaga|Rettore|Fabrizio Moro|Giusy Ferreri|Giovanni Truppi|Mahmood|Blanco|Highsnob|Hu|Le Vibrazioni|Dargen D'Amico|Tananai|Yuman|Matteo Romano""",
    2023: """Paola & Chiara|Mara Sattei|Rosa Chemical|Gianluca Grignani|Levante|Lazza|LDA|Ultimo|Elodie|Mr.Rain|Giorgia|Colla Zio|Marco Mengoni|I Cugini di Campagna|Olly|Anna Oxa|Articolo 31|Ariete|Sethu|Shari|gIANMARIA|Modà|Will|Leo Gassmann""",
    2024: """Alessandra Amoroso|Alfa|Angelina Mango|BigMama|Bnkr44|Clara|Diodato|Fiorella Mannoia|Fred De Palma|Gazzelle|Geolier|Ghali|Il Tre|Il Volo|La Sad|Loredana Bertè|Maninni|Negramaro|Nek|Ricchi e Poveri|Rose Villain|Santi Francesi|The Kolors""",
    2025: """Brunori Sas|Sarah Toscano|Simone Cristicchi|Joan Thiele|Bresh|Marcella Bella|Tony Effe|Lucio Corsi|Shablo|Guè|Joshua|Tormento|Serena Brancale|Rocco Hunt|Francesco Gabbani|Rkomi""",
    2026: """Tommaso Paradiso|Chiello|Tredici Pietro|Sal Da Vinci|Samurai Jay|Luchè|Raf|Bambole di Pezza|Nayt|Elettra Lamborghini|J-Ax|Enrico Nigiotti|Maria Antonietta & Colombre|Maria Antonietta|Colombre|Marco Masini|Fedez & Masini|LDA & Aka 7even|Patty Pravo|Eddie Brock""",
}

# These names are official-Rai artist names, with common event-listing variants.
ARTIST_ALIASES = {
    "colapesce dimartino": "Colapesce Dimartino",
    "colapesce e dimartino": "Colapesce Dimartino",
    "paola e chiara": "Paola & Chiara",
    "the kolors": "The Kolors",
    "cugini di campagna": "I Cugini di Campagna",
    "la rappresentante di lista": "La Rappresentante di Lista",
    "aka7even": "Aka 7even",
    "mr rain": "Mr.Rain",
    "renga": "Francesco Renga",
}

# A plain name match is too weak for these short or common stage names.
# They remain in the roster/research log until an attraction ID is verified.
AMBIGUOUS_NAMES = {"Random", "Will", "Hu", "Joshua", "Gaia", "Emma", "Raf", "Nek"}

VENUES = (
    ("Santeria", "Milano"), ("Alcatraz", "Milano"),
    ("Fabrique", "Milano"), ("Hiroshima Mon Amour", "Torino"),
    ("Teatro Colosseo", "Torino"), ("Estragon", "Bologna"),
    ("Locomotiv", "Bologna"), ("Europauditorium", "Bologna"),
    ("Largo Venue", "Roma"), ("Atlantico", "Roma"),
    ("Casa della Musica", "Napoli"), ("Hall", "Padova"),
    ("Gran Teatro Geox", "Padova"),
    ("Teatro Concordia", "Venaria Reale"),
)

db.executescript("""
CREATE TABLE IF NOT EXISTS sanremo_artists (
    name TEXT PRIMARY KEY, years TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS concerts (
    event_id TEXT PRIMARY KEY, source TEXT NOT NULL,
    event_name TEXT NOT NULL, concert_date TEXT, city TEXT,
    region TEXT, venue TEXT, url TEXT NOT NULL,
    status TEXT NOT NULL, ticket_status TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS concert_artists (
    event_id TEXT NOT NULL REFERENCES concerts(event_id),
    artist TEXT NOT NULL REFERENCES sanremo_artists(name),
    PRIMARY KEY (event_id, artist)
);
CREATE TABLE IF NOT EXISTS concert_history (
    id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, changed_at TEXT NOT NULL,
    old_date TEXT, new_date TEXT, old_status TEXT, new_status TEXT,
    note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS concert_research_log (
    id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, kind TEXT NOT NULL,
    target TEXT NOT NULL, source TEXT NOT NULL, result TEXT NOT NULL,
    matches INTEGER NOT NULL, note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS concert_notifications (
    id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, kind TEXT NOT NULL,
    detail TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS concert_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS concerts_by_date_city
    ON concerts(concert_date, city);
CREATE INDEX IF NOT EXISTS concert_artists_by_name
    ON concert_artists(artist, event_id);
""")


def normal(text):
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(c)
    )
    return " ".join(re.findall(r"[^\W_]+", folded, re.UNICODE))


def words(text):
    return normal(text).split()


def is_video(attachment):
    return (Path(attachment.filename.lower()).suffix in VIDEO_EXTENSIONS
            or (attachment.content_type or "").lower().startswith("video/"))


def index_message(message):
    if message.guild is None or message.guild.id != GUILD_ID:
        return 0
    if message.author.id != OWNER_ID:
        return 0
    count = 0
    for attachment in message.attachments:
        if not is_video(attachment):
            continue
        db.execute("""INSERT OR REPLACE INTO videos
            (message_id, attachment_id, channel_id, caption, filename,
             message_url, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (message.id, attachment.id, message.channel.id, message.content,
             attachment.filename, message.jump_url,
             message.created_at.isoformat()))
        count += 1
    if count:
        db.commit()
    return count


def matches(query_words, caption, filename):
    available = words(caption + " " + filename)
    return bool(available) and all(
        any(q == word or (len(q) >= 4 and
            difflib.SequenceMatcher(None, q, word).ratio() >= 0.78)
            for word in available)
        for q in query_words
    )


@bot.event
async def on_ready():
    if not getattr(bot, "commands_synced", False):
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        bot.commands_synced = True
    if not weekly_concerts.is_running():
        weekly_concerts.start()
    print(f"Ready as {bot.user}; owner {OWNER_ID}; guild {GUILD_ID}")


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
    rows = db.execute("""SELECT caption, filename, message_url, uploaded_at
        FROM videos ORDER BY uploaded_at DESC""").fetchall()
    results = [row for row in rows if matches(query, row[0], row[1])][:10]
    if not results:
        reply = f"No videos found for **{discord.utils.escape_markdown(search)}**."
    else:
        parts = [f"🔎 Videos matching **{discord.utils.escape_markdown(search)}**:"]
        for number, (caption, filename, url, uploaded_at) in enumerate(results, 1):
            parts.append(
                f"\n**{number}. {discord.utils.escape_markdown(filename)}**"
                f" · {uploaded_at[:10]}\n"
                f"{discord.utils.escape_markdown(caption[:180]) or '(No caption)'}"
                f"\n[Jump to video]({url})"
            )
        reply = "\n".join(parts)
        if len(reply) > 1900:
            reply = reply[:1850] + "\n… Try a more specific search."
    await interaction.response.send_message(
        reply, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="reindex", description="Index your older video uploads",
              guild=discord.Object(id=GUILD_ID))
async def reindex(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("This command is private.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    total = scanned = 0
    skipped = []
    for channel in interaction.guild.text_channels:
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
        f"{total} video attachments.{note}", ephemeral=True)


# --- Concert tracking: Ticketmaster Discovery API, with a persistent audit trail ---

def seed_artists():
    years_by_name = {}
    for year, names in SANREMO_BY_YEAR.items():
        for name in names.split("|"):
            years_by_name.setdefault(name, set()).add(year)
    for name, years in years_by_name.items():
        db.execute("""INSERT INTO sanremo_artists(name, years) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET years=excluded.years""",
            (name, ", ".join(map(str, sorted(years)))))
    db.commit()
    aliases = {normal(name): name for name in years_by_name}
    aliases.update({normal(k): v for k, v in ARTIST_ALIASES.items()})
    return aliases


ARTIST_LOOKUP = seed_artists()
concert_lock = asyncio.Lock()


def research(kind, target, result, matches_count=0, note=""):
    db.execute("""INSERT INTO concert_research_log
        (checked_at, kind, target, source, result, matches, note)
        VALUES (?, ?, ?, 'Ticketmaster Discovery API', ?, ?, ?)""",
        (dt.datetime.now(dt.timezone.utc).isoformat(), kind, target,
         result, matches_count, note[:500]))
    db.commit()


class TicketmasterUnavailable(RuntimeError):
    pass


async def tm_get(session, endpoint, params=None):
    params = {**(params or {}), "apikey": TM_KEY}
    for attempt in range(2):
        async with session.get(TM_ROOT + endpoint, params=params) as response:
            if response.status == 429 and attempt == 0:
                await asyncio.sleep(5)
                continue
            if response.status == 404:
                return {}
            if response.status in (401, 403):
                raise TicketmasterUnavailable(
                    "API key was rejected. Check TICKETMASTER_API_KEY in FadeHost.")
            if response.status == 429:
                raise TicketmasterUnavailable("API rate limit reached. Try later.")
            if response.status != 200:
                raise RuntimeError(f"Ticketmaster HTTP {response.status}")
            return await response.json()
    raise TicketmasterUnavailable("API rate limit reached. Try later.")


async def tm_pages(session, endpoint, params, noun):
    events = []
    truncated = False
    for page in range(5):  # API deep-paging limit: 5 x 200 = 1,000
        data = await tm_get(session, endpoint, {**params, "size": 200, "page": page})
        events.extend(data.get("_embedded", {}).get(noun, []))
        pages = data.get("page", {}).get("totalPages", 0)
        if page + 1 >= pages:
            break
        if page == 4:
            truncated = True
        await asyncio.sleep(0.55)
    return events, truncated


def identified_artists(event):
    # Exact attraction-name matching avoids false alerts from keyword searches.
    names = event.get("_embedded", {}).get("attractions", [])
    return {
        ARTIST_LOOKUP[normal(item.get("name", ""))]
        for item in names
        if normal(item.get("name", "")) in ARTIST_LOOKUP
        and ARTIST_LOOKUP[normal(item.get("name", ""))] not in AMBIGUOUS_NAMES
    }


def event_fields(event):
    date_info = event.get("dates", {})
    start = date_info.get("start", {})
    date = start.get("localDate")
    if start.get("dateTBA") or start.get("dateTBD"):
        date = None
    status = date_info.get("status", {}).get("code", "unknown").lower()
    if status == "canceled":
        state = "CANCELLED"
    elif status == "postponed":
        state = "POSTPONED"
    elif status == "rescheduled":
        state = "RESCHEDULED" if date else "WATCH"
    else:
        state = "CONFIRMED" if date and status in ("onsale", "offsale") else "WATCH"
    venue = next(iter(event.get("_embedded", {}).get("venues", [])), {})
    return dict(
        event_id=event.get("id", ""), event_name=event.get("name", ""),
        date=date, city=venue.get("city", {}).get("name", ""),
        region=venue.get("state", {}).get("name", ""),
        venue=venue.get("name", ""), url=event.get("url", ""),
        status=state, ticket_status=status,
    )


def save_event(event, artists):
    fields = event_fields(event)
    if not fields["event_id"] or not fields["url"] or not artists:
        return False
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    previous = db.execute(
        "SELECT concert_date, status FROM concerts WHERE event_id=?",
        (fields["event_id"],)).fetchone()
    if previous is None:
        db.execute("""INSERT INTO concerts(event_id, source, event_name,
            concert_date, city, region, venue, url, status, ticket_status,
            first_seen, last_seen) VALUES (?, 'Ticketmaster', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (fields["event_id"], fields["event_name"], fields["date"],
             fields["city"], fields["region"], fields["venue"], fields["url"],
             fields["status"], fields["ticket_status"], now, now))
        if fields["status"] in ("CONFIRMED", "RESCHEDULED"):
            db.execute("""INSERT INTO concert_notifications
                (event_id, kind, detail, created_at) VALUES (?, 'NEW', '', ?)""",
                (fields["event_id"], now))
    else:
        db.execute("""UPDATE concerts SET event_name=?, concert_date=?, city=?,
            region=?, venue=?, url=?, status=?, ticket_status=?, last_seen=?
            WHERE event_id=?""",
            (fields["event_name"], fields["date"], fields["city"],
             fields["region"], fields["venue"], fields["url"],
             fields["status"], fields["ticket_status"], now, fields["event_id"]))
        if previous != (fields["date"], fields["status"]):
            detail = f"Previous: {previous[0] or 'date unknown'} ({previous[1]})"
            db.execute("""INSERT INTO concert_history
                (event_id, changed_at, old_date, new_date, old_status,
                 new_status, note) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (fields["event_id"], now, previous[0], fields["date"],
                 previous[1], fields["status"],
                 "Ticketmaster event record changed; reason not supplied."))
            pending_new = db.execute("""SELECT id FROM concert_notifications
                WHERE event_id=? AND kind='NEW' AND sent_at IS NULL""",
                (fields["event_id"],)).fetchone()
            if pending_new and fields["status"] not in ("CONFIRMED", "RESCHEDULED"):
                db.execute("DELETE FROM concert_notifications WHERE id=?", pending_new)
            elif not pending_new:
                db.execute("""DELETE FROM concert_notifications
                    WHERE event_id=? AND kind='UPDATE' AND sent_at IS NULL""",
                    (fields["event_id"],))
                db.execute("""INSERT INTO concert_notifications
                    (event_id, kind, detail, created_at) VALUES (?, 'UPDATE', ?, ?)""",
                    (fields["event_id"], detail, now))
    for artist in artists:
        db.execute("""INSERT OR IGNORE INTO concert_artists(event_id, artist)
            VALUES (?, ?)""", (fields["event_id"], artist))
    db.commit()
    return previous is None


def venue_matches(candidate, expected_name, expected_city):
    cities = {
        "milano": {"milano", "milan"}, "torino": {"torino", "turin"},
        "roma": {"roma", "rome"}, "napoli": {"napoli", "naples"},
        "padova": {"padova", "padua"},
        "venaria reale": {"venaria reale", "venaria"},
    }
    actual_city = normal(candidate.get("city", {}).get("name", ""))
    actual_name = normal(candidate.get("name", ""))
    desired_name = normal(expected_name)
    city_ok = actual_city in cities.get(normal(expected_city), {normal(expected_city)})
    return city_ok and (actual_name == desired_name or
        actual_name.startswith(desired_name + " ") or
        desired_name.startswith(actual_name + " "))


async def discover_concerts(session):
    new_count = errors = 0
    consecutive_errors = 0
    today = dt.datetime.now(ROME).date()
    until_year = max(2027, today.year + 1)
    common = dict(countryCode="IT", startDateTime=f"{today}T00:00:00Z",
                  endDateTime=f"{until_year + 1}-01-01T00:00:00Z")
    roster = [row[0] for row in db.execute("SELECT name FROM sanremo_artists ORDER BY name")]

    for artist in roster:
        try:
            found, truncated = await tm_pages(
                session, "/events.json", {**common, "keyword": artist}, "events")
            matches = [event for event in found if artist in identified_artists(event)]
            for event in matches:
                new_count += save_event(event, identified_artists(event))
            note = ("Name requires attraction-ID verification" if artist in AMBIGUOUS_NAMES
                    else "Result set exceeded API paging limit" if truncated
                    else "No exact attraction match" if not matches else "")
            research("artist", artist, "FOUND" if matches else "WATCH",
                     len(matches), note)
            consecutive_errors = 0
        except TicketmasterUnavailable:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
            errors += 1
            consecutive_errors += 1
            research("artist", artist, "ERROR", note=type(exc).__name__)
            if consecutive_errors >= 3:
                raise TicketmasterUnavailable(
                    "Source failed three times in a row. Try again later.") from exc
        await asyncio.sleep(0.55)

    # Venue -> artist sweep, within the same ticketing source.
    for venue_name, city in VENUES:
        target = f"{venue_name}, {city}"
        try:
            candidates, _ = await tm_pages(
                session, "/venues.json",
                {"keyword": venue_name, "countryCode": "IT"}, "venues")
            venues = [v for v in candidates if venue_matches(v, venue_name, city)]
            matches_count = 0
            truncated_any = False
            for venue in venues:
                events, truncated = await tm_pages(session, "/events.json",
                    {**common, "venueId": venue["id"]}, "events")
                truncated_any |= truncated
                for event in events:
                    artists = identified_artists(event)
                    if artists:
                        matches_count += 1
                        new_count += save_event(event, artists)
                await asyncio.sleep(0.55)
            research("venue", target, "FOUND" if matches_count else "WATCH",
                     matches_count, "Result set exceeded API paging limit" if truncated_any else
                     "Venue absent from Ticketmaster or no roster match" if not matches_count else "")
            consecutive_errors = 0
        except TicketmasterUnavailable:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
            errors += 1
            consecutive_errors += 1
            research("venue", target, "ERROR", note=type(exc).__name__)
            if consecutive_errors >= 3:
                raise TicketmasterUnavailable(
                    "Source failed three times in a row. Try again later.") from exc
        await asyncio.sleep(0.55)

    # Recheck known events by ID so changed statuses are not missed by an
    # upcoming-date search. A vanished page remains unresolved, not cancelled.
    known = db.execute("SELECT event_id FROM concerts").fetchall()
    for (event_id,) in known:
        try:
            event = await tm_get(session, f"/events/{event_id}.json")
            if event.get("id"):
                artists = {row[0] for row in db.execute(
                    "SELECT artist FROM concert_artists WHERE event_id=?", (event_id,))}
                save_event(event, artists)
            else:
                research("event", event_id, "WATCH", note="Detail page absent; not proof of cancellation")
            consecutive_errors = 0
        except TicketmasterUnavailable:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
            errors += 1
            consecutive_errors += 1
            research("event", event_id, "ERROR", note=type(exc).__name__)
            if consecutive_errors >= 3:
                raise TicketmasterUnavailable(
                    "Source failed three times in a row. Try again later.") from exc
        await asyncio.sleep(0.55)
    return new_count, errors


async def send_pending_alerts():
    guild = bot.get_guild(GUILD_ID)
    channel = discord.utils.get(guild.text_channels, name="concert-alerts") if guild else None
    if channel is None:
        return 0, "Create a text channel named #concert-alerts; alerts remain queued."
    sent = 0
    queued = db.execute("""SELECT n.id, n.event_id, n.kind, n.detail,
        c.event_name, c.concert_date, c.city, c.venue, c.url, c.status,
        c.ticket_status FROM concert_notifications n
        JOIN concerts c ON c.event_id=n.event_id
        WHERE n.sent_at IS NULL ORDER BY n.id""").fetchall()
    for (notification_id, event_id, kind, detail, event_name, date,
         city, venue, url, status, ticket_status) in queued:
        artists = ", ".join(row[0] for row in db.execute(
            "SELECT artist FROM concert_artists WHERE event_id=? ORDER BY artist",
            (event_id,)))
        colour = (discord.Colour.green() if status in ("CONFIRMED", "RESCHEDULED")
                  else discord.Colour.orange() if status == "POSTPONED"
                  else discord.Colour.red() if status == "CANCELLED"
                  else discord.Colour.blue())
        embed = discord.Embed(
            title=("🇮🇹 New Sanremo artist concert" if kind == "NEW"
                   else "🇮🇹 Concert status/date update"),
            description=event_name[:300], url=url, colour=colour)
        embed.add_field(name="Artist", value=artists[:1000] or "Unknown", inline=False)
        embed.add_field(name="Date", value=date or "To be verified", inline=True)
        embed.add_field(name="Where", value=(venue + ", " + city).strip(", ") or "Unknown", inline=True)
        embed.add_field(name="Status", value=status.title(), inline=True)
        if detail:
            embed.add_field(name="Change", value=detail[:1000], inline=False)
        embed.set_footer(text=f"Source: Ticketmaster • Ticket status: {ticket_status}")
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException) as exc:
            return sent, f"Could not post to #concert-alerts: {type(exc).__name__}."
        db.execute("UPDATE concert_notifications SET sent_at=? WHERE id=?",
                   (dt.datetime.now(dt.timezone.utc).isoformat(), notification_id))
        db.commit()
        sent += 1
    return sent, ""


async def run_concert_check():
    if not TM_KEY:
        return "Set TICKETMASTER_API_KEY in FadeHost Environment first."
    if concert_lock.locked():
        return "A concert check is already running."
    async with concert_lock:
        try:
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                new_count, errors = await discover_concerts(session)
        except TicketmasterUnavailable as exc:
            return f"Concert check stopped: {exc} Saved data remains unchanged where no update was verified."
        sent, alert_note = await send_pending_alerts()
        return (f"Concert check finished. {new_count} new Ticketmaster events saved; "
                f"{sent} alerts posted; {errors} source errors. "
                f"Missing results remain WATCH/unresolved. {alert_note}")


@tree.command(name="checkconcerts", description="Check Sanremo artists' Italian concerts now",
              guild=discord.Object(id=GUILD_ID))
async def checkconcerts(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("This command is private.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        report = await run_concert_check()
        await interaction.followup.send(report[:1900], ephemeral=True)
    except Exception as exc:
        print(f"Concert check failed: {type(exc).__name__}: {exc}")
        await interaction.followup.send(
            "Concert check stopped unexpectedly. See FadeHost's live console.",
            ephemeral=True)


# This task wakes daily at 09:00 London time, but calls the research API only
# on Friday. A persisted marker prevents a second automatic Friday check.
@tasks.loop(time=dt.time(hour=9, minute=0, tzinfo=LONDON))
async def weekly_concerts():
    today = dt.datetime.now(LONDON).date()
    if today.weekday() != 4 or not TM_KEY:
        return
    last = db.execute("SELECT value FROM concert_meta WHERE key='last_friday'").fetchone()
    if last and last[0] == str(today):
        return
    db.execute("""INSERT INTO concert_meta(key, value) VALUES ('last_friday', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (str(today),))
    db.commit()
    try:
        print("Friday concert check: " + await run_concert_check())
    except Exception as exc:
        print(f"Friday concert check failed: {type(exc).__name__}: {exc}")


@weekly_concerts.before_loop
async def before_weekly_concerts():
    await bot.wait_until_ready()


bot.run(TOKEN)
