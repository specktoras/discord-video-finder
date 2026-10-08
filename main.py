# Main Festival artist roster update — 2026-10-07
"""Private video finder and Sanremo concert watcher for one Discord guild.

Secrets: DISCORD_TOKEN, OWNER_ID, GUILD_ID, TICKETMASTER_API_KEY in FadeHost.
Runtime data: /data/storage/videos.sqlite3 (persistent on FadeHost).
"""

import asyncio
import csv
import datetime as dt
import difflib
import hashlib
import io
import os
import re
import sqlite3
import unicodedata
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
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
SANTERIA_CALENDAR = "https://www.santeria.milano.it/eventi/"

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

# The artist pool covers people who competed at the main Festival, including
# Nuove Proposte, but not every contestant in the preliminary Giovani shows.
# Some pairs or groups and their members are indexed separately for their
# individual concerts.
SANREMO_BY_YEAR = {
    2021: """Aiello|Annalisa|Arisa|Malika Ayane|Orietta Berti|Bugo|Colapesce Dimartino|Coma_Cose|Extraliscio|Fasma|Fulminacci|Gaia|Max Gazzè|Ghemon|Gio Evan|Irama|La Rappresentante di Lista|Lo Stato Sociale|Madame|Måneskin|Ermal Meta|Francesca Michielin|Fedez|Noemi|Random|Francesco Renga|Willie Peyote""",
    2022: """Iva Zanicchi|Achille Lauro|Aka 7even|Michele Bravi|Emma|Massimo Ranieri|Sangiovanni|Gianni Morandi|Ana Mena|Elisa|Rkomi|Ditonellapiaga|Rettore|Fabrizio Moro|Giusy Ferreri|Giovanni Truppi|Mahmood|Blanco|Highsnob|Hu|Le Vibrazioni|Dargen D'Amico|Tananai|Yuman|Matteo Romano""",
    2023: """Paola & Chiara|Mara Sattei|Rosa Chemical|Gianluca Grignani|Levante|Lazza|LDA|Ultimo|Elodie|Mr.Rain|Giorgia|Colla Zio|Marco Mengoni|I Cugini di Campagna|Olly|Anna Oxa|Articolo 31|Ariete|Sethu|Shari|gIANMARIA|Modà|Will|Leo Gassmann""",
    2024: """Alessandra Amoroso|Alfa|Angelina Mango|BigMama|Bnkr44|Clara|Diodato|Fiorella Mannoia|Fred De Palma|Gazzelle|Geolier|Ghali|Il Tre|Il Volo|La Sad|Loredana Bertè|Maninni|Negramaro|Nek|Ricchi e Poveri|Rose Villain|Santi Francesi|The Kolors""",
    2025: """Brunori Sas|Sarah Toscano|Simone Cristicchi|Joan Thiele|Bresh|Marcella Bella|Tony Effe|Lucio Corsi|Shablo|Guè|Joshua|Tormento|Serena Brancale|Rocco Hunt|Francesco Gabbani|Rkomi""",
    2026: """Tommaso Paradiso|Chiello|Tredici Pietro|Sal Da Vinci|Samurai Jay|Luchè|Raf|Bambole di Pezza|Nayt|Elettra Lamborghini|J-Ax|Enrico Nigiotti|Maria Antonietta & Colombre|Maria Antonietta|Colombre|Marco Masini|Fedez & Masini|LDA & Aka 7even|Patty Pravo|Eddie Brock|Sayf""",
}

# Rai: 2021 had a separate Nuove Proposte competition; 2025 and 2026
# restored it. In 2022–2024 the Giovani qualifiers joined the Big lineup
# and are already included above. Members of the Nuove Proposte groups below
# are also searchable on their own.
SANREMO_NUOVE_PROPOSTE_BY_YEAR = {
    2021: """Gaudiano|Folcast|Greta Zuccoli|Davide Shorty|WrongOnYou|Avincola|Dellai|Elena Faggi""",
    2025: """Alex Wyse|Maria Tomba|Settembre|Vale LP e Lil Jolie|Vale LP|Lil Jolie""",
    2026: """Angelica Bove|Nicolò Filippucci|Blind, El Ma & Soniko|Blind|El Ma|Soniko|Mazzariello""",
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
    "vale lp & lil jolie": "Vale LP e Lil Jolie",
    "blind el ma e soniko": "Blind, El Ma & Soniko",
}

# A plain name match is too weak for these short or common stage names.
# They remain in the roster/research log until an attraction ID is verified.
AMBIGUOUS_NAMES = {"Random", "Will", "Hu", "Joshua", "Gaia", "Emma",
                   "Raf", "Nek", "Blind", "El Ma"}

VENUES = (
    ("Santeria", "Milano"), ("Alcatraz", "Milano"),
    ("Fabrique", "Milano"), ("Allo Sbagliato - Teatro Principe", "Milano"),
    ("Hiroshima Mon Amour", "Torino"),
    ("Teatro Colosseo", "Torino"), ("Estragon", "Bologna"),
    ("Locomotiv", "Bologna"), ("Europauditorium", "Bologna"),
    ("Largo Venue", "Roma"), ("Atlantico", "Roma"),
    ("Casa della Musica", "Napoli"), ("Hall", "Padova"),
    ("Gran Teatro Geox", "Padova"),
    ("Teatro Concordia", "Venaria Reale"),
)

# Official listings reviewed on 2026-10-07. A one-time source snapshot:
# future changes on these sites require a fresh review or an owner edit.
# Format: (artist, source, official tour URL, status, (date, city, venue) rows).
REVIEWED_TOURS = (
    ("Francesco Renga", "Friends & Partners", "https://www.friendsandpartners.it/in-tour/live-teatri-2027", "RESCHEDULED", (
        ("2027-04-17", "Spoleto", "TEATRO NUOVO MENOTTI"),
        ("2027-04-19", "Bologna", "TEATRO EUROPAUDITORIUM"),
        ("2027-04-24", "Legnano", "TEATRO GALLERIA"),
        ("2027-04-30", "Brescia", "TEATRO DIS_PLAY"),
        ("2027-05-03", "Torino", "TEATRO COLOSSEO"),
        ("2027-05-06", "Firenze", "TEATRO VERDI"),
        ("2027-05-11", "Roma", "TEATRO BRANCACCIO"),
        ("2027-05-13", "Bari", "TEATRO TEAM"),
        ("2027-05-17", "Napoli", "TEATRO AUGUSTEO"),
        ("2027-05-21", "Padova", "GRAN TEATRO GEOX"),
        ("2027-05-24", "Milano", "TEATRO ARCIMBOLDI"),
    )),
    ("Raf", "Friends & Partners", "https://www.friendsandpartners.it/index.php/in-tour/raf-infinito-palasport-2027", "RESCHEDULED", (
        ("2027-10-02", "Milano", "UNIPOL FORUM"),
        ("2027-10-09", "Roma", "PALAZZO DELLO SPORT"),
        ("2027-10-14", "Napoli", "TEATRO PALAPARTENOPE"),
    )),
    ("Mr.Rain", "Friends & Partners", "https://www.friendsandpartners.it/in-tour/mr-rain-nei-teatri", "CONFIRMED", (
        ("2026-10-11", "Roma", "TEATRO BRANCACCIO"),
        ("2026-10-19", "Torino", "TEATRO COLOSSEO"),
        ("2026-10-23", "Brescia", "TEATRO CLERICI"),
        ("2026-10-26", "Firenze", "TEATRO VERDI"),
        ("2026-10-29", "Padova", "GRAN TEATRO GEOX"),
        ("2026-11-05", "Bologna", "EUROPAUDITORIUM"),
    )),
    ("Fiorella Mannoia", "Friends & Partners", "https://www.friendsandpartners.it/in-tour/fiorella-mannoia-fiorella-canta-fabrizio-e-ivano-anime-salve", "CONFIRMED", (
        ("2026-10-08", "Padova", "GRAN TEATRO GEOX"),
        ("2026-10-10", "Ancona", "TEATRO DELLE MUSE"),
        ("2026-10-11", "Assisi", "TEATRO LYRICK"),
        ("2026-10-19", "Cremona", "TEATRO PONCHIELLI"),
        ("2026-10-21", "Bologna", "EUROPAUDITORIUM"),
        ("2026-10-22", "Bologna", "TEATRO EUROPAUDITORIUM"),
        ("2026-10-28", "Torino", "TEATRO COLOSSEO"),
        ("2026-10-29", "Torino", "TEATRO COLOSSEO"),
        ("2026-10-30", "Mantova", "TEATRO PALAUNICAL"),
        ("2026-11-09", "Palermo", "TEATRO MASSIMO"),
        ("2026-11-11", "Ragusa", "TEATRO DUEMILA"),
        ("2026-11-17", "Firenze", "TEATRO VERDI"),
        ("2026-11-18", "Firenze", "TEATRO VERDI"),
        ("2026-11-21", "Bergamo", "CHORUSLIFE ARENA"),
        ("2026-11-23", "Legnano", "TEATRO GALLERIA"),
        ("2026-11-24", "Genova", "TEATRO CARLO FELICE"),
        ("2026-11-26", "Ravenna", "PALA DE' ANDRE'"),
        ("2026-11-28", "Salerno", "TEATRO VERDI"),
        ("2026-11-30", "Napoli", "TEATRO AUGUSTEO"),
        ("2026-12-02", "Bari", "TEATRO TEAM"),
        ("2026-12-03", "Lecce", "PALA EVENTI"),
    )),
    ("Arisa", "Friends & Partners", "https://www.friendsandpartners.it/in-tour/live-tour", "CONFIRMED", (
        ("2026-11-14", "Parma", "TEATRO REGIO"),
        ("2026-11-17", "Genova", "POLITEAMA GENOVESE"),
        ("2026-11-19", "Montecatini", "TEATRO VERDI"),
        ("2026-11-25", "Torino", "TEATRO COLOSSEO"),
        ("2026-11-28", "Milano", "TEATRO LIRICO"),
        ("2026-11-30", "Firenze", "TEATRO VERDI"),
        ("2026-12-02", "Bitritto", "PALATOUR"),
        ("2026-12-04", "Napoli", "TEATRO AUGUSTEO"),
        ("2026-12-07", "Roma", "TEATRO BRANCACCIO"),
        ("2026-12-10", "Bologna", "TEATRO EUROPAUDITORIUM"),
        ("2026-12-12", "Legnano", "TEATRO GALLERIA"),
        ("2026-12-14", "Padova", "GRAN TEATRO GEOX"),
        ("2026-12-16", "Brescia", "TEATRO CLERICI"),
        ("2026-12-18", "Mantova", "TEATRO PALAUNICAL"),
        ("2026-12-21", "Cremona", "TEATRO PONCHIELLI"),
    )),
    ("Brunori Sas", "Vivo Concerti", "https://www.vivoconcerti.com/roster/brunori-sas/tuttobrunori-canzoni-e-monologhi", "CONFIRMED", (
        ("2026-10-08", "Milano", "Teatro Arcimboldi"),
        ("2026-10-09", "Milano", "Teatro Arcimboldi"),
        ("2026-10-10", "Milano", "Teatro Arcimboldi"),
        ("2026-10-12", "Genova", "Teatro Carlo Felice"),
        ("2026-10-14", "Torino", "Auditorium Lingotto"),
        ("2026-10-15", "Torino", "Auditorium Lingotto"),
        ("2026-10-17", "Firenze", "Teatro Verdi"),
        ("2026-10-18", "Firenze", "Teatro Verdi"),
        ("2026-10-19", "Bologna", "Teatro Europauditorium"),
        ("2026-10-22", "Trieste", "Teatro Rossetti"),
        ("2026-10-23", "Padova", "Gran Teatro Geox"),
        ("2026-10-31", "Avellino", "Teatro Gesualdo"),
        ("2026-11-03", "Napoli", "Teatro Augusteo"),
        ("2026-11-05", "Roma", "Teatro Conciliazione"),
        ("2026-11-06", "Roma", "Teatro Conciliazione"),
        ("2026-11-07", "Roma", "Teatro Conciliazione"),
        ("2026-11-10", "Assisi", "Teatro Lyrick"),
        ("2026-11-11", "Ancona", "Teatro delle Muse"),
        ("2026-11-14", "Catania", "Teatro Metropolitan"),
        ("2026-11-15", "Palermo", "Teatro Politeama"),
        ("2026-11-17", "Bari", "Teatro Petruzzelli"),
        ("2026-11-18", "Bari", "Teatro Petruzzelli"),
        ("2026-11-21", "Catanzaro", "Teatro Politeama"),
        ("2026-11-22", "Reggio Calabria", "Teatro Cilea"),
        ("2026-11-24", "Cosenza", "Teatro Alfonso Rendano"),
        ("2026-11-25", "Cosenza", "Teatro Alfonso Rendano"),
    )),
    ("Ultimo", "Vivo Concerti", "https://www.vivoconcerti.com/roster/ultimo/stadi-2027-la-favola-continua-1", "CONFIRMED", (
        ("2027-06-10", "Lignano Sabbiadoro", "Stadio Teghil"),
        ("2027-06-13", "Bologna", "Stadio Dall'Ara"),
        ("2027-06-14", "Bologna", "Stadio Dall'Ara"),
        ("2027-06-17", "Padova", "Stadio Euganeo"),
        ("2027-06-20", "Milano", "Stadio San Siro"),
        ("2027-06-21", "Milano", "Stadio San Siro"),
        ("2027-06-24", "Napoli", "Stadio Diego Armando Maradona"),
        ("2027-06-25", "Napoli", "Stadio Diego Armando Maradona"),
        ("2027-06-28", "Messina", "Stadio Franco Scoglio"),
        ("2027-06-29", "Messina", "Stadio Franco Scoglio"),
        ("2027-07-03", "Reggio Calabria", "Stadio Granillo"),
        ("2027-07-06", "Bari", "Stadio San Nicola"),
        ("2027-07-07", "Bari", "Stadio San Nicola"),
        ("2027-07-10", "Firenze", "Visarno Arena (Parco delle Cascine)"),
        ("2027-07-16", "Torino", "Allianz Stadium"),
        ("2027-07-17", "Torino", "Allianz Stadium"),
        ("2027-07-24", "Olbia", "Olbia Arena"),
    )),
    ("Elodie", "Vivo Concerti", "https://www.vivoconcerti.com/roster/elodie/elodie-show-2027", "CONFIRMED", (
        ("2027-04-24", "Ancona", "Palaprometeo"),
        ("2027-04-29", "Roma", "Palazzo dello Sport"),
        ("2027-04-30", "Roma", "Palazzo dello Sport"),
        ("2027-05-04", "Napoli", "Teatro PalaPartenope"),
        ("2027-05-05", "Napoli", "Teatro Palapartenope"),
        ("2027-05-08", "Bari", "PalaFlorio"),
        ("2027-05-09", "Bari", "PalaFlorio"),
        ("2027-05-13", "Milano", "Unipol Forum"),
        ("2027-05-14", "Milano", "Unipol Forum"),
        ("2027-05-18", "Firenze", "Mandela Forum"),
        ("2027-05-22", "Bologna", "Unipol Arena"),
    )),
    ("Francesca Michielin", "TicketOne", "https://www.ticketone.it/artist/francesca-michielin/", "CONFIRMED", (
        ("2026-11-08", "Trento", "Teatro Auditorium Santa Chiara"),
        ("2026-11-11", "Venezia", "Teatro Malibran"),
        ("2026-11-13", "Torino", "Teatro Colosseo"),
        ("2026-11-15", "Ancona", "Teatro delle Muse"),
        ("2026-11-16", "Firenze", "Teatro Verdi"),
        ("2026-11-20", "Bari", "Teatro Petruzzelli"),
        ("2026-11-22", "Bologna", "Teatro Europauditorium"),
        ("2026-11-23", "Trieste", "Politeama Rossetti - Sala Assicurazioni Generali"),
        ("2026-11-25", "Padova", "Gran Teatro Geox"),
        ("2026-11-28", "Roma", "Auditorium Conciliazione"),
        ("2026-12-01", "Milano", "Teatro Arcimboldi"),
        ("2026-12-03", "Napoli", "Teatro Augusteo"),
    )),
    ("Sal Da Vinci", "TicketOne", "https://www.ticketone.it/en/artist/sal-da-vinci/", "CONFIRMED", (
        ("2026-10-08", "Ancona", "Teatro delle Muse"),
        ("2026-10-09", "Roma", "Auditorium Conciliazione"),
        ("2026-10-12", "Brescia", "Teatro Clerici"),
        ("2026-10-13", "Torino", "Teatro Colosseo"),
        ("2026-10-14", "Torino", "Teatro Colosseo"),
        ("2026-10-16", "Padova", "Gran Teatro Geox"),
        ("2026-10-18", "Bologna", "Teatro Europauditorium"),
        ("2026-10-20", "Milano", "Teatro Arcimboldi"),
        ("2026-10-23", "Bitritto", "Palatour"),
        ("2026-10-27", "Catania", "Teatro Metropolitan"),
        ("2026-10-29", "Avellino", "Teatro Carlo Gesualdo"),
        ("2026-10-30", "Avellino", "Teatro Carlo Gesualdo"),
        ("2026-11-03", "Firenze", "Teatro Verdi"),
    )),
    ("Mara Sattei", "TicketOne", "https://www.ticketone.it/artist/mara-sattei/", "CONFIRMED", (
        ("2026-11-13", "Roma", "Alcazar Live"),
        ("2026-11-18", "Torino", "Hiroshima Mon Amour"),
        ("2026-11-20", "Pordenone", "Capitol"),
        ("2026-11-23", "Milano", "Santeria Toscana 31"),
        ("2026-11-27", "Conversano", "Casa delle Arti"),
    )),
    ("Annalisa", "TicketOne", "https://www.ticketone.it/en/artist/annalisa/?pnum=2", "CONFIRMED", (
        ("2027-06-12", "Milano", "Stadio San Siro"),
    )),
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
    for lineup in (SANREMO_BY_YEAR, SANREMO_NUOVE_PROPOSTE_BY_YEAR):
        for year, names in lineup.items():
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


def research(kind, target, result, matches_count=0, note="",
             source="Ticketmaster Discovery API"):
    db.execute("""INSERT INTO concert_research_log
        (checked_at, kind, target, source, result, matches, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (dt.datetime.now(dt.timezone.utc).isoformat(), kind, target, source,
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


def same_concert_place(city_a, venue_a, city_b, venue_b):
    """Treat documented spellings of the same city/venue as one location."""
    def city_key(value):
        name = normal(re.sub(r"\s*\([A-Za-z]{2,}\)$", "", value or ""))
        return {"milan": "milano", "turin": "torino", "rome": "roma",
                "naples": "napoli", "florence": "firenze", "padua": "padova",
                "bitritto bari": "bitritto"}.get(name, name)

    def venue_key(value):
        name = normal(value or "")
        aliases = {
            "santeria": "santeria toscana 31",
            "santeria social club": "santeria toscana 31",
            "teatro europauditorium": "europauditorium",
            "teatro degli arcimboldi": "arcimboldi",
            "teatro arcimboldi": "arcimboldi",
            "teatro arcimboldi teatro degli arcimboldi": "arcimboldi",
            "tam teatro arcimboldi milano": "arcimboldi",
            "teatro pala partenope": "palapartenope",
            "teatro palapartenope": "palapartenope",
            "palaunical teatro": "palaunical",
            "teatro palaunical": "palaunical",
            "stadio dell ara": "stadio dall ara",
            "stadio diego armando maradona": "stadio maradona",
            "stadio armando maradona": "stadio maradona",
            "teatro carlo gesualdo": "teatro gesualdo",
            "auditorium conciliazione": "conciliazione",
            "teatro conciliazione": "conciliazione",
        }
        return aliases.get(name, name)

    venue = venue_key(venue_a)
    if not venue or venue != venue_key(venue_b):
        return False
    first, second = city_key(city_a), city_key(city_b)
    return bool(first and second) and (first == second or
        (venue == "unipol forum" and {first, second} == {"milano", "assago"}))


def other_source_has_event(date, city, venue, artists, source):
    """Suppress two sources alerting the same artist/date/place as new shows."""
    if not date or not city or not venue:
        return False
    rows = db.execute("""SELECT c.source, c.city, c.venue, a.artist
        FROM concerts c JOIN concert_artists a ON a.event_id=c.event_id
        WHERE c.concert_date=? AND c.source!=?
          AND c.status IN ('CONFIRMED', 'RESCHEDULED')""", (date, source))
    return any(artist in artists and
               same_concert_place(existing_city, existing_venue, city, venue)
               for _, existing_city, existing_venue, artist in rows)


def save_event(event, artists):
    fields = event_fields(event)
    if not fields["event_id"] or not fields["url"] or not artists:
        return False
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    previous = db.execute(
        "SELECT concert_date, status FROM concerts WHERE event_id=?",
        (fields["event_id"],)).fetchone()
    if previous is None:
        if fields["status"] in ("CONFIRMED", "RESCHEDULED") and \
                other_source_has_event(fields["date"], fields["city"],
                                       fields["venue"], artists, "Ticketmaster"):
            return False
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
    known = db.execute("""SELECT event_id FROM concerts
        WHERE source='Ticketmaster'""").fetchall()
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


# Santeria's own public calendar covers events sold via TicketOne, DICE and
# other sellers. Its markup is venue-specific; a changed page is an error,
# never evidence that a missing concert was cancelled.
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}
CALENDAR_DATE = re.compile(
    r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(20\d{2})\b",
    re.IGNORECASE,
)


class SanteriaCards(HTMLParser):
    """Read only date, title, venue tag and source link from event cards."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.card_depth = None
        self.card = None
        self.cards = []
        self.title_h2 = False
        self.title_link = False
        self.date_link = False

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        classes = (attrs.get("class") or "").split()
        if tag == "div":
            self.depth += 1
            if self.card is None and "fusion-column-wrapper" in classes:
                self.card_depth = self.depth
                self.card = {"title": [], "date_label": [], "locations": set(),
                             "url": "", "sold_out": False}
        if self.card is None:
            return
        if tag == "h2" and "titolo-evento" in classes:
            self.title_h2 = True
        if tag == "a":
            href = attrs.get("href") or ""
            if self.title_h2 and href and not self.card["url"]:
                self.card["url"] = href
                self.title_link = True
            elif "fusion-button" in classes and "button-small" in classes:
                self.date_link = True
            if "location=" in href:
                self.card["locations"].add(href.split("location=")[-1].split("&")[0])
        if tag == "img" and normal(attrs.get("alt") or "") == "sold out":
            self.card["sold_out"] = True

    def handle_data(self, data):
        if self.card is None:
            return
        if self.title_link:
            self.card["title"].append(data)
        if self.date_link:
            self.card["date_label"].append(data)

    def handle_endtag(self, tag):
        if tag == "a":
            self.title_link = False
            self.date_link = False
        elif tag == "h2":
            self.title_h2 = False
        elif tag == "div":
            if self.card is not None and self.depth == self.card_depth:
                self.cards.append(self.card)
                self.card = None
                self.card_depth = None
            self.depth -= 1


def parse_santeria(html, today):
    parser = SanteriaCards()
    parser.feed(html)
    cards = [card for card in parser.cards if card["url"] and card["date_label"]]
    if not cards:
        raise ValueError("Santeria calendar event cards were not found")
    events = []
    for card in cards:
        title = " ".join(" ".join(card["title"]).split())
        artist_name = re.split(r"\s+[|–—-]\s+", title, maxsplit=1)[0].strip()
        artist = ARTIST_LOOKUP.get(normal(artist_name))
        if not artist or artist in AMBIGUOUS_NAMES:
            continue
        date_label = " ".join(card["date_label"])
        date_match = CALENDAR_DATE.search(date_label)
        if not date_match:
            continue  # Missing year is not enough evidence for a new alert.
        try:
            date = dt.date(int(date_match[3]), MONTHS[date_match[1].lower()],
                           int(date_match[2]))
        except ValueError:
            continue
        if date < today or date.year > max(2027, today.year + 1):
            continue
        locations = card["locations"]
        if "toscana-31" in locations:
            venue = "Santeria Toscana 31"
        elif "paladini-8" in locations:
            venue = "Santeria Paladini 8"
        else:
            continue  # External events need their own verified city/venue.
        url = card["url"]
        parsed_url = urlparse(url)
        if (parsed_url.scheme != "https" or
                parsed_url.hostname != "www.santeria.milano.it"):
            continue
        label = normal(title + " " + date_label)
        status = ("CANCELLED" if "annullato" in label or "cancelled" in label
                  else "POSTPONED" if "rinviato" in label or "postponed" in label
                  else "CONFIRMED")
        sold_out = card["sold_out"]
        events.append(dict(event_id="santeria:" + hashlib.sha256(
                          url.encode("utf-8")).hexdigest()[:24],
                           event_name=title, date=date.isoformat(),
                           city="Milano", region="Lombardia", venue=venue,
                           url=url, status=status,
                           ticket_status="sold out" if sold_out else "not checked",
                           artist=artist))
    return events


def save_santeria_event(fields):
    event_id = fields["event_id"]
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    previous = db.execute("SELECT concert_date, status FROM concerts WHERE event_id=?",
                          (event_id,)).fetchone()
    if previous is None:
        if fields["status"] == "CONFIRMED" and \
                other_source_has_event(fields["date"], fields["city"],
                                       fields["venue"], {fields["artist"]}, "Santeria"):
            return False
        db.execute("""INSERT INTO concerts(event_id, source, event_name,
            concert_date, city, region, venue, url, status, ticket_status,
            first_seen, last_seen) VALUES (?, 'Santeria', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, fields["event_name"], fields["date"], fields["city"],
             fields["region"], fields["venue"], fields["url"], fields["status"],
             fields["ticket_status"], now, now))
        db.execute("INSERT INTO concert_artists(event_id, artist) VALUES (?, ?)",
                   (event_id, fields["artist"]))
        if fields["status"] == "CONFIRMED":
            db.execute("""INSERT INTO concert_notifications
                (event_id, kind, detail, created_at) VALUES (?, 'NEW', '', ?)""",
                (event_id, now))
    else:
        db.execute("""UPDATE concerts SET event_name=?, concert_date=?, city=?,
            region=?, venue=?, url=?, status=?, ticket_status=?, last_seen=?
            WHERE event_id=?""",
            (fields["event_name"], fields["date"], fields["city"],
             fields["region"], fields["venue"], fields["url"], fields["status"],
             fields["ticket_status"], now, event_id))
        if previous != (fields["date"], fields["status"]):
            detail = f"Previous: {previous[0] or 'date unknown'} ({previous[1]})"
            db.execute("""INSERT INTO concert_history
                (event_id, changed_at, old_date, new_date, old_status,
                 new_status, note) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event_id, now, previous[0], fields["date"], previous[1],
                 fields["status"], "Santeria calendar listing changed; reason not supplied."))
            pending = db.execute("""SELECT id FROM concert_notifications
                WHERE event_id=? AND kind='NEW' AND sent_at IS NULL""",
                (event_id,)).fetchone()
            if pending and fields["status"] != "CONFIRMED":
                db.execute("DELETE FROM concert_notifications WHERE id=?", pending)
            elif not pending:
                db.execute("""DELETE FROM concert_notifications
                    WHERE event_id=? AND kind='UPDATE' AND sent_at IS NULL""",
                    (event_id,))
                db.execute("""INSERT INTO concert_notifications
                    (event_id, kind, detail, created_at)
                    VALUES (?, 'UPDATE', ?, ?)""", (event_id, detail, now))
    db.commit()
    return previous is None


async def discover_santeria(session):
    async with session.get(SANTERIA_CALENDAR,
                           headers={"User-Agent": "PersonalConcertWatcher/1.0"}) as response:
        if response.status != 200:
            raise RuntimeError(f"Santeria calendar HTTP {response.status}")
        if response.content_length and response.content_length > 4_000_000:
            raise RuntimeError("Santeria calendar response was unexpectedly large")
        html = await response.text()
        if len(html) > 4_000_000:
            raise RuntimeError("Santeria calendar response was unexpectedly large")
    events = parse_santeria(html, dt.datetime.now(ROME).date())
    added = sum(save_santeria_event(event) for event in events)
    research("venue", "Santeria, Milano", "FOUND" if events else "WATCH",
             len(events), "No exact roster matches" if not events else "",
             source="Santeria official calendar")
    return added


async def send_pending_alerts():
    guild = bot.get_guild(GUILD_ID)
    channel = discord.utils.get(guild.text_channels, name="concert-alerts") if guild else None
    if channel is None:
        return 0, "Create a text channel named #concert-alerts; alerts remain queued."
    sent = 0
    queued = db.execute("""SELECT n.id, n.event_id, n.kind, n.detail,
        c.event_name, c.concert_date, c.city, c.venue, c.url, c.status,
        c.ticket_status, c.source FROM concert_notifications n
        JOIN concerts c ON c.event_id=n.event_id
        WHERE n.sent_at IS NULL ORDER BY n.id""").fetchall()
    for (notification_id, event_id, kind, detail, event_name, date,
         city, venue, url, status, ticket_status, source) in queued:
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
        embed.set_footer(text=f"Source: {source} • Ticket status: {ticket_status}")
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
    if concert_lock.locked():
        return "A concert check is already running."
    async with concert_lock:
        timeout = aiohttp.ClientTimeout(total=25)
        tm_new = venue_new = errors = 0
        notes = []
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if TM_KEY:
                try:
                    tm_new, tm_errors = await discover_concerts(session)
                    errors += tm_errors
                except (TicketmasterUnavailable, aiohttp.ClientError,
                        asyncio.TimeoutError) as exc:
                    errors += 1
                    notes.append(f"Ticketmaster: {type(exc).__name__}.")
            else:
                notes.append("Ticketmaster key missing; checked venue calendar only.")
            try:
                venue_new = await discover_santeria(session)
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError,
                    ValueError, UnicodeError) as exc:
                errors += 1
                research("venue", "Santeria, Milano", "ERROR",
                         note=type(exc).__name__, source="Santeria official calendar")
                notes.append(f"Santeria calendar: {type(exc).__name__}.")
        sent, alert_note = await send_pending_alerts()
        return (f"Concert check finished. {tm_new} new Ticketmaster events; "
                f"{venue_new} new Santeria events; {sent} alerts posted; "
                f"{errors} source errors. Missing results remain unresolved. "
                + " ".join(notes) + " " + alert_note)


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


# --- Private concert catalogue: search and owner-entered listings ---

def parse_calendar_day(value):
    try:
        parsed = dt.date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except (TypeError, ValueError):
        return None


def valid_listing_url(value):
    value = value.strip()
    parsed = urlparse(value)
    return (value if parsed.scheme == "https" and parsed.hostname and
            not parsed.username and not parsed.password and
            len(value) <= 350 and not any(c in value for c in " \n\r\t<>()")
            else None)


def find_duplicate_concert(artist, date, city, venue, exclude=""):
    rows = db.execute("""SELECT c.event_id, c.source, c.city, c.venue, c.url
        FROM concerts c JOIN concert_artists a ON a.event_id=c.event_id
        WHERE c.concert_date=? AND a.artist=? AND c.event_id!=?""",
        (date, artist, exclude))
    for event_id, source, old_city, old_venue, url in rows:
        if same_concert_place(city, venue, old_city, old_venue):
            return event_id, source, url
    return None


def import_reviewed_concerts(preview=False, today=None):
    """Add the reviewed 2026-10-07 snapshot once, without generating alerts."""
    batch_key = "reviewed_official_import_2026_10_07"
    if db.execute("SELECT 1 FROM concert_meta WHERE key=?", (batch_key,)).fetchone():
        return {"already": True}
    today = today or dt.datetime.now(ROME).date()
    total = past = duplicates = 0
    missing = []
    for artist, source, url, status, events in REVIEWED_TOURS:
        if not db.execute("SELECT 1 FROM sanremo_artists WHERE name=?",
                          (artist,)).fetchone():
            raise ValueError(f"Reviewed artist missing from roster: {artist}")
        if not valid_listing_url(url) or status not in {"CONFIRMED", "RESCHEDULED"}:
            raise ValueError(f"Reviewed source invalid: {artist}")
        for date, city, venue in events:
            total += 1
            day = parse_calendar_day(date)
            if not day or not city.strip() or not venue.strip():
                raise ValueError(f"Reviewed listing incomplete: {artist} {date}")
            if day < today:
                past += 1
            elif find_duplicate_concert(artist, date, city, venue):
                duplicates += 1
            else:
                missing.append((artist, source, url, status, date, city, venue))
    if not preview:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with db:
            for artist, source, url, status, date, city, venue in missing:
                identity = "\0".join((artist, date, city, venue))
                event_id = "reviewed:" + hashlib.sha256(
                    identity.encode("utf-8")).hexdigest()[:24]
                db.execute("""INSERT INTO concerts(event_id, source, event_name,
                    concert_date, city, region, venue, url, status,
                    ticket_status, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, 'not checked', ?, ?)""",
                    (event_id, "Reviewed: " + source, artist, date, city,
                     venue, url, status, now, now))
                db.execute("""INSERT INTO concert_artists(event_id, artist)
                    VALUES (?, ?)""", (event_id, artist))
            db.execute("INSERT INTO concert_meta(key, value) VALUES (?, ?)",
                       (batch_key, now))
    return {"already": False, "total": total, "past": past,
            "duplicates": duplicates, "new": len(missing), "preview": preview}


def catalogue_rows(artist="", city="", venue="", from_date="", to_date="",
                   status="", source="", include_past=False, today=None):
    """Return catalogue rows in date order, including entries with no date."""
    today = today or dt.datetime.now(ROME).date()
    minimum = from_date or ("" if include_past else today.isoformat())
    rows = db.execute("""SELECT c.event_id, c.event_name, c.concert_date,
        c.city, c.venue, c.url, c.status, c.source,
        (SELECT GROUP_CONCAT(a.artist, ', ') FROM concert_artists a
         WHERE a.event_id=c.event_id) AS artists
        FROM concerts c""").fetchall()
    result = []
    for row in rows:
        event_id, name, date, row_city, row_venue, url, state, origin, artists = row
        if not date and (from_date or to_date):
            continue
        if date and ((minimum and date < minimum) or (to_date and date > to_date)):
            continue
        if artist and normal(artist) not in normal(artists or ""):
            continue
        if city and normal(city) not in normal(row_city or ""):
            continue
        if venue and normal(venue) not in normal(row_venue or ""):
            continue
        if status and state != status:
            continue
        if source and not origin.casefold().startswith(source.casefold()):
            continue
        result.append(row)
    return sorted(result, key=lambda row: (row[2] is None, row[2] or "9999-99-99",
                                           normal(row[8] or row[1]), row[0]))


async def artist_options(interaction: discord.Interaction, current: str):
    if interaction.user.id != OWNER_ID:
        return []
    choices = [row[0] for row in db.execute(
        "SELECT name FROM sanremo_artists ORDER BY name")
        if normal(current) in normal(row[0])]
    return [app_commands.Choice(name=name[:100], value=name)
            for name in choices[:25]]


async def manual_event_options(interaction: discord.Interaction, current: str):
    if interaction.user.id != OWNER_ID:
        return []
    rows = db.execute("""SELECT c.event_id, c.event_name, c.concert_date, c.city
        FROM concerts c WHERE c.source LIKE 'Manual:%'
           OR c.source LIKE 'Reviewed:%'
        ORDER BY c.concert_date DESC""")
    found = []
    for event_id, name, date, city in rows:
        label = f"{name} · {date or '?'} · {city}"
        if normal(current) in normal(label + " " + event_id):
            found.append(app_commands.Choice(name=label[:100], value=event_id))
        if len(found) == 25:
            break
    return found


STATUS_CHOICES = [
    app_commands.Choice(name="Confirmed", value="CONFIRMED"),
    app_commands.Choice(name="Rescheduled", value="RESCHEDULED"),
    app_commands.Choice(name="Postponed", value="POSTPONED"),
    app_commands.Choice(name="Cancelled", value="CANCELLED"),
    app_commands.Choice(name="Watch / unverified", value="WATCH"),
]


CONCERT_PAGE_SIZE = 5


def concert_page_embed(rows, page):
    embed = discord.Embed(title=f"🎵 Concerts · {len(rows)} matching",
                          colour=discord.Colour.blue())
    start = (page - 1) * CONCERT_PAGE_SIZE
    for event_id, name, date, row_city, row_venue, url, state, origin, artists in \
            rows[start:start + CONCERT_PAGE_SIZE]:
        label = discord.utils.escape_markdown((artists or name)[:80])
        location = discord.utils.escape_markdown(
            f"{row_venue or 'Venue unknown'}, {row_city or 'City unknown'}"[:110])
        embed.add_field(name=label or "Concert",
                        value=(f"{date or 'Date unknown'} · {location}\n"
                               f"{state.title()} · {discord.utils.escape_markdown(origin[:80])}\n"
                               f"[Open source]({url})"), inline=False)
    pages = (len(rows) + CONCERT_PAGE_SIZE - 1) // CONCERT_PAGE_SIZE
    embed.set_footer(text=f"Page {page}/{pages} · Dates soonest first")
    return embed


class ConcertPager(discord.ui.View):
    """Browse one filtered result set by editing its private Discord message."""

    def __init__(self, rows, page):
        super().__init__(timeout=600)
        self.rows = rows
        self.page = page
        self.last_page = (len(rows) + CONCERT_PAGE_SIZE - 1) // CONCERT_PAGE_SIZE
        self.message = None
        self.update_buttons()

    def update_buttons(self):
        self.first.disabled = self.previous.disabled = self.page == 1
        self.next.disabled = self.last.disabled = self.page == self.last_page
        self.counter.label = f"{self.page}/{self.last_page}"

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("This menu is private.",
                                                    ephemeral=True)
            return False
        return True

    async def show(self, interaction: discord.Interaction, page):
        self.page = max(1, min(page, self.last_page))
        self.update_buttons()
        await interaction.response.edit_message(
            embed=concert_page_embed(self.rows, self.page), view=self,
            allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="First", style=discord.ButtonStyle.secondary, row=0)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show(interaction, 1)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.primary, row=0)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show(interaction, self.page - 1)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary,
                       disabled=True, row=0)
    async def counter(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass  # The page indicator is deliberately not clickable.

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show(interaction, self.page + 1)

    @discord.ui.button(label="Last", style=discord.ButtonStyle.secondary, row=0)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show(interaction, self.last_page)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass  # The private response may already have been dismissed.


@tree.command(name="concerts", description="Browse your concert database with filters",
              guild=discord.Object(id=GUILD_ID))
@app_commands.describe(artist="Artist name", city="City", venue="Venue name",
                       from_date="From YYYY-MM-DD", to_date="Through YYYY-MM-DD",
                       status="Event status", source="Where the listing came from",
                       include_past="Also show older dates", page="Page number")
@app_commands.choices(status=STATUS_CHOICES, source=[
    app_commands.Choice(name="Ticketmaster", value="Ticketmaster"),
    app_commands.Choice(name="Santeria", value="Santeria"),
    app_commands.Choice(name="Reviewed official listings", value="Reviewed:"),
    app_commands.Choice(name="Added by me", value="Manual:"),
])
@app_commands.autocomplete(artist=artist_options)
async def concerts(interaction: discord.Interaction, artist: str | None = None,
                   city: str | None = None, venue: str | None = None,
                   from_date: str | None = None, to_date: str | None = None,
                   status: app_commands.Choice[str] = None,
                   source: app_commands.Choice[str] = None,
                   include_past: bool = False, page: int = 1):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("This command is private.", ephemeral=True)
        return
    if ((from_date and not parse_calendar_day(from_date)) or
            (to_date and not parse_calendar_day(to_date)) or
            (from_date and to_date and from_date > to_date)):
        await interaction.response.send_message(
            "Use dates in YYYY-MM-DD format, with the start before the end.",
            ephemeral=True)
        return
    if page < 1:
        await interaction.response.send_message("Page must be 1 or higher.", ephemeral=True)
        return
    rows = catalogue_rows(artist or "", city or "", venue or "",
                          from_date or "", to_date or "",
                          status.value if status else "",
                          source.value if source else "", include_past)
    count = len(rows)
    if (page - 1) * CONCERT_PAGE_SIZE >= count:
        await interaction.response.send_message(
            f"No concerts on page {page} for those filters ({count} total matches).",
            ephemeral=True)
        return
    view = ConcertPager(rows, page) if count > CONCERT_PAGE_SIZE else None
    await interaction.response.send_message(
        embed=concert_page_embed(rows, page), view=view, ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none())
    if view is not None:
        try:
            view.message = await interaction.original_response()
        except (discord.HTTPException, discord.ClientException):
            pass  # Paging still works; only automatic timeout disabling is lost.


@tree.command(name="exportconcerts", description="Download your concert listings as CSV",
              guild=discord.Object(id=GUILD_ID))
async def exportconcerts(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("This command is private.", ephemeral=True)
        return
    rows = db.execute("""SELECT c.event_id, GROUP_CONCAT(a.artist, ' | '),
        c.event_name, c.concert_date, c.city, c.region, c.venue, c.source,
        c.status, c.ticket_status, c.url
        FROM concerts c LEFT JOIN concert_artists a ON a.event_id=c.event_id
        GROUP BY c.event_id
        ORDER BY c.concert_date IS NULL, c.concert_date, c.event_name""").fetchall()

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("event_id", "artists", "event_name", "concert_date",
                     "city", "region", "venue", "source", "status",
                     "ticket_status", "url"))
    for row in rows:
        # Spreadsheet apps must not interpret an event title or venue as a formula.
        writer.writerow("'" + value if isinstance(value, str) and
                        value.startswith(("=", "+", "-", "@")) else
                        value if value is not None else "" for value in row)

    csv_file = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    filename = f"concerts_{dt.datetime.now(LONDON):%Y-%m-%d}.csv"
    await interaction.response.send_message(
        f"Exported {len(rows)} concert records, including past dates. "
        "Only you can see and download this file; it contains no videos or secrets.",
        file=discord.File(csv_file, filename=filename), ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="importconcerts",
              description="Add a reviewed batch from official promoter and ticket listings",
              guild=discord.Object(id=GUILD_ID))
@app_commands.describe(preview="Show the number of new listings without saving")
async def importconcerts(interaction: discord.Interaction, preview: bool = False):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("This command is private.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        async with concert_lock:
            result = import_reviewed_concerts(preview=preview)
    except Exception as exc:
        print(f"Reviewed concert import failed: {type(exc).__name__}: {exc}")
        await interaction.followup.send(
            "The import stopped without saving this batch. See FadeHost's live console.",
            ephemeral=True)
        return
    if result["already"]:
        reply = "The 7 October reviewed batch was already imported. Browse it with `/concerts`."
    else:
        action = "would add" if preview else "added"
        reply = (f"Reviewed official listings: {result['total']} dates from "
                 f"Friends & Partners, Vivo Concerti and TicketOne. "
                 f"{result['new']} {action}; {result['duplicates']} already in your "
                 f"database; {result['past']} past dates skipped. "
                 + ("Run `/importconcerts` to save them. " if preview else
                    "Find them with `/concerts`, source: Reviewed official listings. ")
                 + "This batch does not post alerts. Source links are saved; "
                   "later schedule changes need a fresh check or `/editconcert`.")
    await interaction.followup.send(reply, ephemeral=True,
                                    allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="addconcert", description="Save a concert from any official listing",
              guild=discord.Object(id=GUILD_ID))
@app_commands.describe(artist="Sanremo artist", date="YYYY-MM-DD", city="City",
                       venue="Venue", url="Official event or ticket page URL",
                       title="Optional event title")
@app_commands.autocomplete(artist=artist_options)
async def addconcert(interaction: discord.Interaction, artist: str, date: str,
                     city: str, venue: str, url: str, title: str | None = None):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("This command is private.", ephemeral=True)
        return
    canonical = ARTIST_LOOKUP.get(normal(artist.strip()))
    day, link = parse_calendar_day(date), valid_listing_url(url)
    if not canonical:
        await interaction.response.send_message(
            "Choose an artist from the Sanremo autocomplete list.", ephemeral=True)
        return
    if not day or not link or not city.strip() or not venue.strip():
        await interaction.response.send_message(
            "Enter YYYY-MM-DD, a full https:// source link, city, and venue.",
            ephemeral=True)
        return
    city, venue = city.strip()[:100], venue.strip()[:150]
    if duplicate := find_duplicate_concert(canonical, date, city, venue):
        await interaction.response.send_message(
            f"Already saved from {discord.utils.escape_markdown(duplicate[1])}: "
            f"[open listing]({duplicate[2]}).", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        return
    name = (title or canonical).strip()[:180] or canonical
    event_id = "manual:" + uuid.uuid4().hex
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    source_name = "Manual: " + urlparse(link).hostname.removeprefix("www.")
    with db:
        db.execute("""INSERT INTO concerts(event_id, source, event_name,
            concert_date, city, region, venue, url, status, ticket_status,
            first_seen, last_seen) VALUES (?, ?, ?, ?, ?, '', ?, ?, 'CONFIRMED',
            'not checked', ?, ?)""",
            (event_id, source_name, name, date, city, venue, link, now, now))
        db.execute("INSERT INTO concert_artists(event_id, artist) VALUES (?, ?)",
                   (event_id, canonical))
    await interaction.response.send_message(
        f"Saved **{discord.utils.escape_markdown(canonical)}** · {date} · "
        f"{discord.utils.escape_markdown(city)}. Find it with `/concerts`. "
        "This is your own entry; the bot does not verify its status.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="editconcert", description="Edit your own or a reviewed concert",
              guild=discord.Object(id=GUILD_ID))
@app_commands.describe(event_id="Select your saved event", new_date="YYYY-MM-DD",
                       new_city="New city", new_venue="New venue",
                       new_url="New official link", new_status="New status")
@app_commands.choices(new_status=STATUS_CHOICES)
@app_commands.autocomplete(event_id=manual_event_options)
async def editconcert(interaction: discord.Interaction, event_id: str,
                      new_date: str | None = None, new_city: str | None = None,
                      new_venue: str | None = None, new_url: str | None = None,
                      new_status: app_commands.Choice[str] = None):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("This command is private.", ephemeral=True)
        return
    existing = db.execute("""SELECT concert_date, city, venue, url, status
        FROM concerts WHERE event_id=?
          AND (source LIKE 'Manual:%' OR source LIKE 'Reviewed:%')""",
        (event_id,)).fetchone()
    if not existing:
        await interaction.response.send_message(
            "Select a manual or reviewed entry from autocomplete.", ephemeral=True)
        return
    if new_date is not None and not parse_calendar_day(new_date):
        await interaction.response.send_message("Use YYYY-MM-DD for the new date.",
                                                ephemeral=True)
        return
    link = valid_listing_url(new_url) if new_url is not None else existing[3]
    if not link or (new_city is not None and not new_city.strip()) or \
            (new_venue is not None and not new_venue.strip()):
        await interaction.response.send_message("Enter a valid https:// link, city, and venue.",
                                                ephemeral=True)
        return
    updated = (new_date or existing[0],
               new_city.strip()[:100] if new_city is not None else existing[1],
               new_venue.strip()[:150] if new_venue is not None else existing[2],
               link, new_status.value if new_status else existing[4])
    if updated == existing:
        await interaction.response.send_message("Nothing to change.", ephemeral=True)
        return
    artist = db.execute("SELECT artist FROM concert_artists WHERE event_id=?",
                        (event_id,)).fetchone()[0]
    if (updated[:3] != existing[:3] and
            find_duplicate_concert(artist, *updated[:3], exclude=event_id)):
        await interaction.response.send_message(
            "That artist/date/place is already in the database.", ephemeral=True)
        return
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with db:
        db.execute("""UPDATE concerts SET concert_date=?, city=?, venue=?, url=?,
            status=?, source=?, last_seen=? WHERE event_id=?""",
            (*updated, "Manual: " + urlparse(link).hostname.removeprefix("www."),
             now, event_id))
        db.execute("""INSERT INTO concert_history(event_id, changed_at, old_date,
            new_date, old_status, new_status, note)
            VALUES (?, ?, ?, ?, ?, ?, 'Owner edited this listing.')""",
            (event_id, now, existing[0], updated[0], existing[4], updated[4]))
    await interaction.response.send_message("Your concert entry was updated.",
                                            ephemeral=True)


@tree.command(name="deleteconcert", description="Delete your own or a reviewed concert",
              guild=discord.Object(id=GUILD_ID))
@app_commands.describe(event_id="Select your saved event")
@app_commands.autocomplete(event_id=manual_event_options)
async def deleteconcert(interaction: discord.Interaction, event_id: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("This command is private.", ephemeral=True)
        return
    row = db.execute("""SELECT event_name FROM concerts
        WHERE event_id=?
          AND (source LIKE 'Manual:%' OR source LIKE 'Reviewed:%')""",
        (event_id,)).fetchone()
    if not row:
        await interaction.response.send_message(
            "Only manual or reviewed entries can be deleted here.", ephemeral=True)
        return
    with db:
        db.execute("DELETE FROM concert_notifications WHERE event_id=?", (event_id,))
        db.execute("DELETE FROM concert_history WHERE event_id=?", (event_id,))
        db.execute("DELETE FROM concert_artists WHERE event_id=?", (event_id,))
        db.execute("DELETE FROM concerts WHERE event_id=?", (event_id,))
    await interaction.response.send_message(
        f"Deleted **{discord.utils.escape_markdown(row[0][:150])}** from your "
        "database.", ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none())


# This task wakes daily at 09:00 London time, but calls the research API only
# on Friday. A persisted marker prevents a second automatic Friday check.
@tasks.loop(time=dt.time(hour=9, minute=0, tzinfo=LONDON))
async def weekly_concerts():
    today = dt.datetime.now(LONDON).date()
    if today.weekday() != 4:
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
