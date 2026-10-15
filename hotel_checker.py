"""Private, on-demand Google Hotels results and SQLite price history for discord.py.

V2.5: zero-credit saved-room-offers browser with over-budget rates, excluded dorms,
provider links, and comparisons to an earlier saved detailed check.
V2.4: multilingual dormitory detection and a zero-credit correction of saved
unknown room-class labels (prices and historical snapshots preserved).
V2.3: add opt-in room/provider offer inspection (one API credit per hotel).
V2.2: combine known Italian city aliases for hotel price history. Historical
search labels, timestamps and prices are preserved; no new scheduled work.

Integration: register_hotel_commands(tree) once before tree.sync().
No timers or background hotel-price polling. /hotels uses ONE SerpApi request.
/hoteloffers uses ONE extra request for ONE property, and saves room-offer history.
/hoteldetails, /hotelhistory, /hotelsavedoffers and /hotelofferhistory
read SQLite only (ZERO SerpApi credits).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands

SERPAPI_URL = 'https://serpapi.com/search.json'
DB_PATH = os.getenv('HOTEL_DB_PATH', '/data/storage/hotels.sqlite3')
SERPAPI_KEY = os.getenv('SERPAPI_API_KEY', '')
OWNER_ID = str(os.environ['OWNER_ID'])
GUILD_ID = int(os.environ['GUILD_ID'])
UK = ZoneInfo('Europe/London')

# Added to EXISTING hotel_prices; existing snapshots and data are preserved.
EXTRA_COLUMNS = {
    'lat': 'REAL',
    'lng': 'REAL',
    'amenities_json': 'TEXT',
    'description': 'TEXT',
    'review_count': 'INTEGER',
    'checkin_time': 'TEXT',
    'checkout_time': 'TEXT',
    'essential_info_json': 'TEXT',
}


def _db_connect():
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript('''
        CREATE TABLE IF NOT EXISTS hotel_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            searched_at TEXT NOT NULL,
            destination TEXT NOT NULL,
            checkin TEXT NOT NULL,
            checkout TEXT NOT NULL,
            adults INTEGER NOT NULL,
            currency TEXT NOT NULL,
            budget REAL NOT NULL,
            property_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hotel_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_id INTEGER NOT NULL REFERENCES hotel_checks(id),
            property_key TEXT NOT NULL,
            hotel_name TEXT NOT NULL,
            property_type TEXT,
            nightly_price REAL,
            total_price REAL,
            rating REAL,
            address TEXT,
            property_token TEXT,
            hotel_url TEXT,
            booking_sources TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_checks_trip ON hotel_checks
            (destination, checkin, checkout, adults, currency, id);
        CREATE INDEX IF NOT EXISTS idx_prices_key ON hotel_prices(check_id, property_key);
    ''')
    current = {r['name'] for r in con.execute('PRAGMA table_info(hotel_prices)')}
    for name, dtype in EXTRA_COLUMNS.items():
        if name not in current:
            con.execute(f'ALTER TABLE hotel_prices ADD COLUMN {name} {dtype}')
    check_columns = {r['name'] for r in con.execute('PRAGMA table_info(hotel_checks)')}
    if 'destination_key' not in check_columns:
        con.execute('ALTER TABLE hotel_checks ADD COLUMN destination_key TEXT')
    # Non-destructive migration: keep original destination, IDs, prices and dates.
    missing = con.execute("SELECT id, destination FROM hotel_checks WHERE destination_key IS NULL OR destination_key = ''").fetchall()
    if missing:
        con.executemany('UPDATE hotel_checks SET destination_key=? WHERE id=?',
                        [(_destination_key(r['destination']), r['id']) for r in missing])
    con.execute('''CREATE INDEX IF NOT EXISTS idx_checks_trip_key ON hotel_checks
        (destination_key, checkin, checkout, adults, currency, searched_at, id)''')
    # Additive only. Never rewrite hotel_checks or hotel_prices.
    con.executescript('''
        CREATE TABLE IF NOT EXISTS hotel_offer_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            searched_at TEXT NOT NULL,
            property_key TEXT NOT NULL,
            hotel_name TEXT NOT NULL,
            destination_key TEXT NOT NULL,
            checkin TEXT NOT NULL,
            checkout TEXT NOT NULL,
            adults INTEGER NOT NULL,
            currency TEXT NOT NULL,
            room_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hotel_room_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            offer_check_id INTEGER NOT NULL REFERENCES hotel_offer_checks(id),
            offer_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            room_name TEXT NOT NULL,
            total_price REAL NOT NULL,
            room_class TEXT NOT NULL,
            bathroom TEXT NOT NULL,
            free_cancellation INTEGER,
            breakfast_included INTEGER,
            booking_url TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_hotel_offer_trip
            ON hotel_offer_checks (destination_key, checkin, checkout, adults, currency, hotel_name, searched_at);
        CREATE INDEX IF NOT EXISTS idx_hotel_room_offer_check
            ON hotel_room_offers (offer_check_id, offer_key);
    ''')
    # V2.4 data-only migration: reclassify previously unknown multilingual
    # dorm beds WITHOUT modifying price, provider, timestamps, or search IDs.
    # Marker keeps subsequent database connections inexpensive and idempotent.
    con.execute("""CREATE TABLE IF NOT EXISTS hotel_migrations (
        migration_key TEXT PRIMARY KEY, applied_at TEXT NOT NULL)""")
    if not con.execute("SELECT 1 FROM hotel_migrations WHERE migration_key=?",
                       ('room_multilingual_v24',)).fetchone():
        old_unknown = con.execute("""SELECT id, room_name FROM hotel_room_offers
            WHERE room_class='unknown'""").fetchall()
        corrections = [(cls, row['id']) for row in old_unknown
                       if (cls := _room_class(row['room_name'])) != 'unknown']
        if corrections:
            con.executemany('UPDATE hotel_room_offers SET room_class=? WHERE id=?',
                            corrections)
        con.execute('INSERT INTO hotel_migrations VALUES (?, ?)',
                    ('room_multilingual_v24', dt.datetime.now(dt.timezone.utc).isoformat()))
    con.commit()
    return con


def _normalized(s: str):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


# Explicit, conservative aliases. Unknown geographic qualifiers are NEVER stripped.
# For example, 'Milan, Michigan, USA' is distinct from 'Milan, Italy'.
_ITALIAN_CITY_ALIASES = {
    'turin': ('turin', 'torino'),
    'milan': ('milan', 'milano'),
    'rome': ('rome', 'roma'),
    'florence': ('florence', 'firenze'),
    'naples': ('naples', 'napoli'),
    'venice': ('venice', 'venezia'),
    'bologna': ('bologna',),
}
_ITALY_TOKENS = ('italy', 'italia', 'it')
_ITALIAN_CITY_REGIONS = {
    'turin': ('piedmont', 'piemonte'),
    'milan': ('lombardy', 'lombardia'),
    'rome': ('lazio',),
    'florence': ('tuscany', 'toscana'),
    'naples': ('campania',),
    'venice': ('veneto',),
    'bologna': ('emilia romagna',),
}


def _destination_key(city: str) -> str:
    """Group only known city aliases, never guess unknown country/region names."""
    place = _normalized(city)
    if not place:
        return ''
    for canonical, aliases in _ITALIAN_CITY_ALIASES.items():
        valid = set(aliases)
        for name in aliases:
            for country in _ITALY_TOKENS:
                valid.add(f'{name} {country}')
                valid.add(f'{country} {name}')
            for region in _ITALIAN_CITY_REGIONS[canonical]:
                valid.add(f'{name} {region}')
                for country in _ITALY_TOKENS:
                    valid.add(f'{name} {region} {country}')
                    valid.add(f'{name} {country} {region}')
        if place in valid:
            return f'{canonical} italy'
    return place


def _price(obj):
    if not isinstance(obj, dict):
        return None
    for key in ('extracted_lowest', 'extracted_price'):
        v = obj.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 1_000_000:
            return round(float(v), 2)
    return None


def _number(v, minimum=None, maximum=None):
    try:
        f = float(v)
        if f != f or f in (float('inf'), float('-inf')):
            return None
        if minimum is not None and f < minimum:
            return None
        if maximum is not None and f > maximum:
            return None
        return f
    except (ValueError, TypeError):
        return None


def _url_or_empty(value):
    if not isinstance(value, str):
        return ''
    u = urlparse(value)
    return value if u.scheme in ('https', 'http') and u.netloc else ''


def _clean_strings(values, maximum=20):
    if not isinstance(values, list):
        return []
    return [str(v).strip()[:90] for v in values[:maximum]
            if isinstance(v, str) and v.strip()]


def _row(p):
    nightly = _price(p.get('rate_per_night'))
    total = _price(p.get('total_rate'))
    name = str(p.get('name') or 'Unnamed property')[:200]
    address = str(p.get('address') or '')[:300]
    token = str(p.get('property_token') or '')[:300]
    gps = p.get('gps_coordinates') or {}
    if not isinstance(gps, dict):
        gps = {}
    lat = _number(gps.get('latitude'), -90, 90)
    lng = _number(gps.get('longitude'), -180, 180)
    rating = _number(p.get('overall_rating') if p.get('overall_rating') is not None
                     else p.get('rating'), 0, 5)
    reviews = _number(p.get('reviews'), 0, 100_000_000)
    amenities = _clean_strings(p.get('amenities'))
    essentials = _clean_strings(p.get('essential_info'), 10)
    sources = ', '.join(str(x.get('source')) for x in (p.get('prices') or [])
                        if isinstance(x, dict) and x.get('source'))[:200]
    identity = 'token:' + token if token else 'name:' + _normalized(name) + ':' + _normalized(address)
    return dict(property_key=identity, hotel_name=name, property_type=str(p.get('type') or '')[:70],
                nightly_price=nightly, total_price=total, rating=rating, address=address,
                property_token=token, hotel_url=_url_or_empty(p.get('link')),
                booking_sources=sources, lat=lat, lng=lng,
                amenities_json=json.dumps(amenities, ensure_ascii=False),
                essential_info_json=json.dumps(essentials, ensure_ascii=False),
                description=str(p.get('description') or '')[:450],
                review_count=int(reviews) if reviews is not None else None,
                checkin_time=str(p.get('check_in_time') or '')[:40],
                checkout_time=str(p.get('check_out_time') or '')[:40])


def _snapshot(destination, checkin, checkout, adults, currency, budget, rows):
    with _db_connect() as c:
        prev = c.execute('''SELECT id, searched_at FROM hotel_checks
            WHERE destination_key=? AND checkin=? AND checkout=? AND adults=? AND currency=?
            ORDER BY searched_at DESC, id DESC LIMIT 1''',
            (_destination_key(destination), checkin, checkout, adults, currency)).fetchone()
        prev_rows = {}
        if prev:
            for r in c.execute('SELECT * FROM hotel_prices WHERE check_id=?', (prev['id'],)):
                prev_rows[r['property_key']] = dict(r)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        cur = c.execute('''INSERT INTO hotel_checks
            (searched_at,destination,destination_key,checkin,checkout,adults,currency,budget,property_count)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (now, _normalized(destination), _destination_key(destination), checkin,
             checkout, adults, currency, budget, len(rows)))
        columns = ['check_id', 'property_key', 'hotel_name', 'property_type',
                   'nightly_price', 'total_price', 'rating', 'address',
                   'property_token', 'hotel_url', 'booking_sources', *EXTRA_COLUMNS]
        sql = 'INSERT INTO hotel_prices(' + ','.join(columns) + ') VALUES (' + ','.join(':' + k for k in columns) + ')'
        for r in rows:
            c.execute(sql, {'check_id': cur.lastrowid, **r})
    return dict(prev) if prev else None, prev_rows, now


def _night_count(a, b):
    start, end = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    nights = (end-start).days
    if not 1 <= nights <= 30:
        raise ValueError('Check-out must be 1–30 days after check-in.')
    if start < dt.datetime.now(UK).date():
        raise ValueError('Check-in cannot be in the past.')
    return nights


def _google_link(city, checkin, checkout, adults):
    q = f'hotels in {city} {checkin} to {checkout} {adults} adult'
    return 'https://www.google.com/travel/hotels?' + urlencode({'q': q})


def _property_link(row, city):
    """Real Google Hotels entity link when Google returned a property_token.

    The browser may still need dates reselected; never promise a bookable offer.
    """
    token = row.get('property_token') or ''
    if token and len(token) < 300:
        return 'https://www.google.com/travel/hotels/entity/' + quote(token, safe='')
    return 'https://www.google.com/travel/hotels?' + urlencode({'q': str(row.get('hotel_name') or '') + ' ' + city})


def _map_link(row):
    if row.get('lat') is not None and row.get('lng') is not None:
        return 'https://www.google.com/maps/search/?' + urlencode({'api': '1', 'query': f'{row["lat"]},{row["lng"]}'})
    return 'https://www.google.com/maps/search/?' + urlencode({'api': '1', 'query': str(row.get('hotel_name') or '') + ' ' + str(row.get('address') or '')})


def _money(v):
    return '—' if v is None else f'£{v:,.2f}'


def _comparison(current, older):
    if not older:
        return '🆕 new'
    if older.get('total_price') is None or current.get('total_price') is None:
        return 'not comparable'
    delta = round(current['total_price'] - older['total_price'], 2)
    if abs(delta) < .01:
        return '＝ same'
    return f'📉 ↓ {_money(-delta)}' if delta < 0 else f'📈 ↑ {_money(delta)}'


def _stamp(iso):
    if not iso:
        return 'unknown time'
    try:
        date = dt.datetime.fromisoformat(iso.replace('Z', '+00:00'))
        if date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
        return date.astimezone(UK).strftime('%d %b %Y, %H:%M UK')
    except ValueError:
        return 'unknown time'


def _json_strings(text):
    try:
        data = json.loads(text or '[]')
        return data if isinstance(data, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _safe(s, length=120):
    return discord.utils.escape_markdown(str(s or '')[:length]).replace('`', '')


def _previous_price(row, older):
    """Match by stable token, falling back to unambiguous hotel name.

    Property tokens can change between source responses; never compare across
    hotels with the same common name unless the stored name is unique.
    """
    direct = older.get(row['property_key'])
    if direct is not None:
        return direct
    name = _normalized(row.get('hotel_name'))
    hits = [r for r in older.values() if _normalized(r.get('hotel_name')) == name]
    return hits[0] if name and len(hits) == 1 else None


def _search_embeds(city, checkin, checkout, nights, adults, budget, rows, previous, older, searched_at):
    """Clean hotel list. Discord field NAMES do not render masked links.

    Put clickable Markdown links only in embed descriptions / field VALUES.
    This also keeps long Google entity URLs out of the visible hotel titles.
    """
    within = [r for r in rows if r['total_price'] is not None and r['total_price'] <= budget]
    within.sort(key=lambda r: r['total_price'])
    heading = discord.Embed(
        title='🏨 Google Hotels · ' + city[:80],
        description=(
            f'**{checkin} → {checkout}** · {nights} night(s) · {adults} adult(s)\n'
            f'**{len(rows)} priced properties** · **{len(within)} within {_money(budget)}**\n'
            f'**Checked:** {_stamp(searched_at)}\n'
            f'**Compared with:** {_stamp(previous["searched_at"]) if previous else "No previous snapshot found"}\n\n'
            '⚠️ **Headline prices do not prove a private room.** Confirm sleeping arrangements, taxes and cancellation before booking.'
        )
    )
    heading.add_field(
        name='Search all hotels',
        value=f'[Open Google Hotels for these dates]({_google_link(city, checkin, checkout, adults)})',
        inline=False,
    )
    if not within:
        heading.add_field(name='No matches under budget', value='Try a larger budget or another area.', inline=False)
    pages = [heading]
    # Six short, readable hotel entries per embed — much easier on phones.
    for off in range(0, len(within), 6):
        embed = discord.Embed(title=f'🏨 Properties {off + 1}–{min(off + 6, len(within))} of {len(within)}')
        for idx, row in enumerate(within[off:off + 6], off + 1):
            name = _safe(row['hotel_name'], 90)
            current_price = _money(row['total_price'])
            change = _comparison(row, _previous_price(row, older))
            hotel_url = _property_link(row, city)
            map_url = _map_link(row)
            rating = f'⭐ {row["rating"]:.1f}/5' if row.get('rating') is not None else ''
            reviews = f' ({row["review_count"]:,} reviews)' if row.get('review_count') is not None and rating else ''
            amenities = _json_strings(row.get('amenities_json'))
            amenity_line = ' · '.join(_safe(x, 32) for x in amenities[:3]) if amenities else ''
            info = [f'🔗 [Google Hotels]({hotel_url}) · [Map]({map_url})']
            if rating:
                info.append(rating + reviews)
            if row.get('address'):
                info.append('📍 ' + _safe(row['address'], 100))
            if amenity_line:
                info.append('🧳 ' + amenity_line)
            embed.add_field(
                name=f'{idx}. {name} — {current_price} · {change}',
                value='\n'.join(info)[:1024],
                inline=False,
            )
        embed.set_footer(text='Hotel links open Google Hotels. Room type not verified. /hoteldetails shows saved details.')
        pages.append(embed)
    return pages

def _group_embeds_for_discord(embeds, max_chars=5000):
    """Discord caps the COMBINED text of all embeds on one message at 6000.

    Send separate ephemeral followups for larger result sets, never silently crop.
    """
    groups, batch, count = [], [], 0
    for e in embeds:
        size = len(e.title or '') + len(e.description or '') + len(e.footer.text or '')
        size += sum(len(f.name) + len(f.value) for f in e.fields)
        if batch and (len(batch) >= 10 or count + size > max_chars):
            groups.append(batch)
            batch, count = [], 0
        batch.append(e)
        count += size
    if batch:
        groups.append(batch)
    return groups


def _look_up_hotel(query):
    needle = _normalized(query)
    if not needle:
        return None, 'Please enter a hotel name.'
    with _db_connect() as c:
        rows = c.execute('''SELECT p.*, c.searched_at, c.destination, c.checkin,
                         c.checkout, c.adults, c.currency, c.destination_key,
                         c.id AS history_check_id
                         FROM hotel_prices p
                         JOIN hotel_checks c ON p.check_id=c.id
                         ORDER BY c.searched_at DESC, c.id DESC LIMIT 2000''').fetchall()
        matches = [dict(r) for r in rows if needle in _normalized(r['hotel_name'])]
        if not matches:
            return None, 'No matching hotel in your saved searches. Try /hotels first.'
        exact = [r for r in matches if _normalized(r['hotel_name']) == needle]
        if exact:
            selected = exact[0]
        else:
            distinct = list(dict.fromkeys(r['hotel_name'] for r in matches))
            if len(distinct) > 1:
                return None, 'Several hotels match. Please use a more specific name: ' + ', '.join(distinct[:6])
            selected = matches[0]
        older_check = c.execute('''SELECT id, searched_at FROM hotel_checks WHERE destination_key=?
             AND checkin=? AND checkout=? AND adults=? AND currency=?
             AND (searched_at < ? OR (searched_at=? AND id<?))
             ORDER BY searched_at DESC, id DESC LIMIT 1''',
             (selected['destination_key'], selected['checkin'], selected['checkout'],
              selected['adults'], selected['currency'], selected['searched_at'],
              selected['searched_at'], selected['history_check_id'])).fetchone()
        old = None
        if older_check:
            prior_rows = [dict(r) for r in c.execute(
                'SELECT * FROM hotel_prices WHERE check_id=?', (older_check['id'],))]
            prior = {r['property_key']: r for r in prior_rows}
            old = _previous_price(selected, prior)
    return (selected, dict(old) if old else None, dict(older_check) if older_check else None), None


def _detail_embed(row, older, previous):
    title = str(row['hotel_name'])[:180]
    when = f'{row["checkin"]} → {row["checkout"]} · {row["adults"]} adult(s) · {row["destination"].title()}'
    embed = discord.Embed(title='🏨 ' + title, description=when)
    embed.add_field(name='💷 Google Hotels property quote', value=_money(row['total_price']), inline=True)
    embed.add_field(name='📊 Since previous check', value=_comparison(row, older), inline=True)
    embed.add_field(name='🕒 Last checked', value=_stamp(row['searched_at']), inline=False)
    if previous:
        prev_price = _money(older['total_price']) if older else 'Not listed in previous results'
        embed.add_field(name='Previous snapshot', value=f'{prev_price} · {_stamp(previous["searched_at"])}', inline=False)
    rating = f'{row["rating"]:.1f}/5' if row.get('rating') is not None else 'Not supplied'
    if row.get('review_count') is not None:
        rating += f' · {row["review_count"]:,} reviews'
    embed.add_field(name='⭐ Google guest rating', value=rating, inline=True)
    embed.add_field(name='🏠 Property category', value=_safe(row.get('property_type') or 'Not supplied', 80), inline=True)
    embed.add_field(name='📍 Location', value=f'{_safe(row["address"], 210) + chr(10) if row.get("address") else ""}[Open in Maps]({_map_link(row)})', inline=False)
    if row.get('description'):
        embed.add_field(name='About', value=_safe(row['description'], 360), inline=False)
    amenities = _json_strings(row.get('amenities_json'))
    embed.add_field(name='🧳 Listed amenities', value=', '.join(_safe(x, 55) for x in amenities[:10])[:950] if amenities else 'Not supplied in the search response.', inline=False)
    times = ' · '.join(x for x in [f'Check-in: {_safe(row["checkin_time"], 30)}' if row.get('checkin_time') else '',
                                 f'Check-out: {_safe(row["checkout_time"], 30)}' if row.get('checkout_time') else ''] if x)
    if times:
        embed.add_field(name='🕓 Property check-in/out times', value=times, inline=False)
    essentials = _json_strings(row.get('essential_info_json'))
    if essentials:
        embed.add_field(name='Listing summary', value=', '.join(_safe(x, 60) for x in essentials[:6])[:450], inline=False)
    links = f'[Google Hotels property]({_property_link(row, row["destination"])}) · [Map]({_map_link(row)})'
    if row.get('hotel_url'):
        links += f' · [Source listing]({row["hotel_url"]})'
    embed.add_field(name='🔗 Links', value=links, inline=False)
    embed.add_field(name='🛏️ Private room?',
        value='**Not verified for this advertised rate.** Check that the selected booking option is a private room or entire unit for one guest. Also check taxes and cancellation. Google Hotels may ask you to select the dates again.', inline=False)
    if row.get('amenities_json') is None:
        embed.set_footer(text='Older snapshot: run /hotels again to collect rating, amenities and location details.')
    else:
        embed.set_footer(text='Stored search data. /hoteldetails itself makes NO API request.')
    return embed



def _history_rows(city: str, checkin: str, checkout: str, adults: int = 1, currency: str = 'GBP'):
    """Read aliases together in chronological order, without API calls."""
    with _db_connect() as c:
        rows = c.execute('''SELECT id, searched_at, property_count, destination
            FROM hotel_checks WHERE destination_key=? AND checkin=? AND checkout=?
            AND adults=? AND currency=? ORDER BY searched_at DESC, id DESC LIMIT 100''',
            (_destination_key(city), checkin, checkout, adults, currency)).fetchall()
        return [dict(r) for r in rows]


def _database_status():
    """Read-only diagnostics. Never calls SerpApi; helpful after deployment."""
    c = _db_connect()
    try:
        total_checks = c.execute('SELECT COUNT(*) FROM hotel_checks').fetchone()[0]
        total_prices = c.execute('SELECT COUNT(*) FROM hotel_prices').fetchone()[0]
        recent = c.execute("""SELECT id,searched_at,destination,destination_key,
                   checkin,checkout,adults,property_count
            FROM hotel_checks ORDER BY searched_at DESC, id DESC LIMIT 6""").fetchall()
        resolved_path = str(Path(DB_PATH).resolve())
        filesize = Path(DB_PATH).stat().st_size if Path(DB_PATH).exists() else 0
        return {
            'path': resolved_path,
            'size': filesize,
            'checks': total_checks,
            'quotes': total_prices,
            'recent': [dict(r) for r in recent],
        }
    finally:
        c.close()


def _status_embed(status):
    e = discord.Embed(
        title='🗄️ Hotel database status',
        description=(f'**Saved searches:** {status["checks"]}\n'
                     f'**Saved hotel quotes:** {status["quotes"]}\n'
                     f'**Database size:** {status["size"]:,} bytes\n'
                     f'**Database location:** `{status["path"][:250]}`\n\n'
                     'ℹ️ Uses **zero** SerpApi credits. City aliases share comparisons; original search labels are retained.')
    )
    recent = status['recent']
    if recent:
        value = '\n'.join(
            f'• {_stamp(x["searched_at"])} — {_safe(x["destination"], 40)} '
            f'({x["checkin"]} → {x["checkout"]}, {x["adults"]} guest(s))'
            for x in recent
        )
        e.add_field(name='Most recent saved checks', value=value[:1024], inline=False)
    else:
        e.add_field(name='Most recent saved checks', value='No saved hotel price snapshots.', inline=False)
    e.set_footer(text='If searches from before a redeploy are missing, check FadeHost persistent storage / HOTEL_DB_PATH.')
    return e


# --- Optional provider / room inspection. One API call per /hoteloffers invocation. ---


# Never translate entire room descriptions. Normalize diacritics and match
# precise language-specific *shared-sleeping* phrases before private-room clues.
# E.g. French "Lit dans Dortoir Mixte de 5 lits" is definitely a shared bed.
_DORM_PATTERNS = (
    # English
    r"\b(?:dorm|dorms|dormitory|dormitories|hostel bed|bed in|bunk in|"
    r"shared (?:room|bedroom|dorm|sleeping)|capsule|sleeping pod)\b",
    # French: dortoir = sleeping dormitory; chambre partagee = shared room
    r"\b(?:dortoirs?|chambre (?:partagee|commune|mixte)|lit en chambre commune)\b",
    # Italian: dormitorio is a dormitory; posto letto is sold as a bed space
    r"\b(?:letto in dormitorio|dormitorio (?:misto|mixto|condiviso|femminile|maschile)|camerat[ae]|posto letto|letto in (?:camera|stanza) "
    r"condivisa|(?:camera|stanza) condivisa)\b",
    # Spanish: 'dormitorio' alone can mean a PRIVATE bedroom, so require
    # dorm/shared context. 'cama en dormitorio' denotes a bed offer.
    r"\b(?:cama en (?:un )?dormitorio|cama en habitacion compartida|"
    r"dormitorio (?:mixto|compartido|femenino|masculino)|"
    r"habitacion (?:compartida|mixta))\b",
    # Portuguese
    r"\b(?:cama em dormitorio|dormitorio compartilhado|"
    r"quarto (?:compartilhado|partilhado|coletivo))\b",
    # German
    r"\b(?:schlafsaal|mehrbettzimmer|gemeinschaftsschlafraum|"
    r"gemeinschaftszimmer|bett im (?:gemischten )?schlafsaal)\b",
)
_PRIVATE_PATTERNS = (
    # English room names
    r"\b(?:private (?:room|bedroom)|single room|double room|twin room|"
    r"triple room|quadruple room|family room|studio|apartment|"
    r"entire (?:place|unit|home|property)|suite|king room|queen room|"
    r"standard room|deluxe room|superior room|classic room|economy room|guest room)\b",
    # French (salle = bathroom is not a private sleeping-room signal)
    r"\b(?:chambre (?:privee|privative|individuelle|simple|double|matrimoniale)|"
    r"appartement(?: entier)?)\b",
    # Italian
    r"\b(?:camera (?:privata|singola|doppia|matrimoniale)|stanza privata|"
    r"appartamento(?: intero)?)\b",
    # Spanish
    r"\b(?:habitacion (?:privada|individual|doble|matrimonial)|"
    r"apartamento(?: entero)?)\b",
    # German
    r"\b(?:einzelzimmer|doppelzimmer|privatzimmer|ferienwohnung)\b",
    # Portuguese
    r"\b(?:quarto (?:privativo|individual|duplo)|apartamento)\b",
)


def _room_class(room_name):
    """Conservative multilingual hints, never guaranteed exclusive occupancy."""
    name = _normalized(room_name)
    if not name:
        return 'unknown'
    if any(re.search(pattern, name) for pattern in _DORM_PATTERNS):
        return 'dormitory_or_shared'
    if any(re.search(pattern, name) for pattern in _PRIVATE_PATTERNS):
        return 'likely_private'
    return 'unknown'


def _bathroom_class(room_name, inclusions):
    content = _normalized(str(room_name) + ' ' + ' '.join(str(v) for v in inclusions))
    shared = (
        r'\b(?:shared|communal) (?:bathroom|toilet|bath|shower)\b',
        r'\b(?:bagno (?:in comune|condiviso)|servizi igienici in comune)\b',
        r'\b(?:salle de bains? (?:commune|partagee)|douche commune)\b',
        r'\b(?:bano compartido|bano comun)\b',
        r'\b(?:gemeinschaftsbad|gemeinschaftsbadzimmer|gemeinschafts wc)\b',
        r'\b(?:banheiro (?:compartilhado|partilhado))\b',
    )
    private = (
        r'\b(?:private|ensuite|en suite|own) (?:bathroom|toilet|bath|shower)\b',
        r'\b(?:bagno privato|bagno in camera)\b',
        r'\b(?:salle de bains? (?:privee|privative))\b',
        r'\b(?:bano privado)\b',
        r'\b(?:eigenes badezimmer|privates badezimmer)\b',
        r'\b(?:banheiro privativo)\b',
    )
    if any(re.search(p, content) for p in shared):
        return 'shared'
    if any(re.search(p, content) for p in private):
        return 'private_described'
    return 'not_specified'


def _flag(v):
    if v is True:
        return 1
    if v is False:
        return 0
    return None


def _offer_rows(response):
    """Only provider-specific, NAMED room offers. No property-level assumed price."""
    found = {}
    for section in ('featured_prices', 'prices'):
        providers = response.get(section) or []
        if not isinstance(providers, list):
            continue
        for p in providers[:60]:
            if not isinstance(p, dict):
                continue
            source = str(p.get('source') or 'Unknown provider')[:100]
            rooms = p.get('rooms') or []
            if not isinstance(rooms, list):
                continue
            for room in rooms[:80]:
                if not isinstance(room, dict) or not isinstance(room.get('name'), str):
                    continue
                name = room['name'].strip()[:160]
                if not name:
                    continue
                sub_rates = room.get('rates')
                if not isinstance(sub_rates, list) or not sub_rates:
                    sub_rates = [room]
                for rate in sub_rates[:25]:
                    if not isinstance(rate, dict):
                        continue
                    total = _price(rate.get('total_rate'))
                    # Only one-night fallback; provided by caller below.
                    if total is None:
                        total = _price(room.get('total_rate'))
                    if total is None:
                        continue
                    inc = rate.get('inclusions') or []
                    if not isinstance(inc, list):
                        inc = []
                    cls = _room_class(name)
                    bathroom = _bathroom_class(name, inc)
                    cancel = _flag(rate.get('free_cancellation'))
                    breakfast = _flag(rate.get('breakfast_included'))
                    key = '|'.join([_normalized(source), _normalized(name),
                                    str(cancel), str(breakfast), bathroom])
                    item = dict(offer_key=key, provider=source, room_name=name,
                                total_price=total, room_class=cls, bathroom=bathroom,
                                free_cancellation=cancel, breakfast_included=breakfast,
                                booking_url=_url_or_empty(rate.get('link') or room.get('link') or p.get('link')))
                    if key not in found or total < found[key]['total_price']:
                        found[key] = item
    return sorted(found.values(), key=lambda v: v['total_price'])


def _save_room_offers(hotel, rows):
    """Track dated offers separately; preserves all old property quotes."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with _db_connect() as c:
        where = (hotel['destination_key'], hotel['checkin'], hotel['checkout'],
                 hotel['adults'], hotel['currency'], _normalized(hotel['hotel_name']))
        # Match hotel by normalised name in Python, because SQLite LOWER is ASCII only.
        potential = c.execute("""SELECT * FROM hotel_offer_checks WHERE destination_key=?
            AND checkin=? AND checkout=? AND adults=? AND currency=?
            ORDER BY searched_at DESC, id DESC LIMIT 200""", where[:5]).fetchall()
        previous = next((dict(p) for p in potential
                         if _normalized(p['hotel_name']) == where[5]), None)
        prevrows = {}
        if previous:
            for x in c.execute('SELECT * FROM hotel_room_offers WHERE offer_check_id=?', (previous['id'],)):
                prevrows[x['offer_key']] = dict(x)
        cur = c.execute("""INSERT INTO hotel_offer_checks
             (searched_at,property_key,hotel_name,destination_key,checkin,checkout,adults,currency,room_count)
             VALUES (?,?,?,?,?,?,?,?,?)""",
             (now, hotel['property_key'], hotel['hotel_name'], *where[:5], len(rows)))
        for r in rows:
            c.execute("""INSERT INTO hotel_room_offers
             (offer_check_id,offer_key,provider,room_name,total_price,room_class,bathroom,
              free_cancellation,breakfast_included,booking_url)
             VALUES (?,?,?,?,?,?,?,?,?,?)""",
             (cur.lastrowid, *(r[k] for k in ('offer_key','provider','room_name','total_price',
                 'room_class','bathroom','free_cancellation','breakfast_included','booking_url'))))
    return now, previous, prevrows


def _offer_history(hotel):
    with _db_connect() as c:
        rows = c.execute("""SELECT * FROM hotel_offer_checks
            WHERE destination_key=? AND checkin=? AND checkout=? AND adults=? AND currency=?
            ORDER BY searched_at DESC,id DESC LIMIT 150""",
            (hotel['destination_key'],hotel['checkin'],hotel['checkout'],hotel['adults'],hotel['currency'])).fetchall()
        rows = [dict(r) for r in rows if _normalized(r['hotel_name']) == _normalized(hotel['hotel_name'])][:12]
        # Use saved room types only; NO additional API requests.
        for item in rows:
            counts = dict(c.execute('''SELECT room_class, COUNT(*)
                FROM hotel_room_offers WHERE offer_check_id=?
                GROUP BY room_class''', (item['id'],)).fetchall())
            item['shared_count'] = counts.get('dormitory_or_shared', 0)
            item['private_count'] = counts.get('likely_private', 0)
            item['unknown_count'] = counts.get('unknown', 0)
        return rows



def _saved_offer_snapshot(hotel, snapshot=1):
    # Read latest/specific saved detailed check and preceding comparison, no network.
    if not isinstance(snapshot, int) or not 1 <= snapshot <= 30:
        raise ValueError('Snapshot must be between 1 (newest) and 30.')
    with _db_connect() as c:
        rows = c.execute('''SELECT * FROM hotel_offer_checks
            WHERE destination_key=? AND checkin=? AND checkout=? AND adults=? AND currency=?
            ORDER BY searched_at DESC, id DESC LIMIT 1000''',
            (hotel['destination_key'], hotel['checkin'], hotel['checkout'],
             hotel['adults'], hotel['currency'])).fetchall()
        matching = [dict(r) for r in rows if _normalized(r['hotel_name']) == _normalized(hotel['hotel_name'])]
        if len(matching) < snapshot:
            return None
        check = matching[snapshot - 1]
        prev = matching[snapshot] if len(matching) > snapshot else None
        offers = [dict(r) for r in c.execute('''SELECT * FROM hotel_room_offers
            WHERE offer_check_id=? ORDER BY id''', (check['id'],)).fetchall()]
        older = {}
        if prev:
            older = {r['offer_key']: dict(r) for r in c.execute('''SELECT * FROM hotel_room_offers
                WHERE offer_check_id=?''', (prev['id'],)).fetchall()}
    return {'check': check, 'previous': prev, 'offers': offers, 'older': older,
            'available_snapshots': len(matching)}


def _saved_offer_embeds(hotel, saved, budget=120.0, page=1, per_page=18, snapshot=1):
    # Render stored rates across categories, with bounded pages for mobile Discord.
    categories = {'likely_private': (0, '🛏️ Likely private'),
                  'unknown': (1, '❔ Unclear room type'),
                  'dormitory_or_shared': (2, '🚫 Dormitory/shared sleeping')}
    offers = sorted(saved['offers'], key=lambda r: (
        categories.get(r['room_class'], (1, '❔ Unclear room type'))[0],
        r['total_price'], _normalized(r['provider']), _normalized(r['room_name'])))
    total = len(offers)
    last_page = max(1, (total + per_page - 1) // per_page)
    if not isinstance(page, int) or not 1 <= page <= last_page:
        raise ValueError(f'Page must be between 1 and {last_page}.')
    pos = (page - 1) * per_page
    selected = offers[pos:pos + per_page]
    count_private = sum(r['room_class'] == 'likely_private' for r in offers)
    count_unknown = sum(r['room_class'] == 'unknown' for r in offers)
    count_shared = sum(r['room_class'] == 'dormitory_or_shared' for r in offers)
    within_private = sum(r['room_class'] == 'likely_private' and r['total_price'] <= budget for r in offers)
    header = discord.Embed(
        title='🗂️ Saved room offers · ' + str(hotel['hotel_name'])[:145],
        description=(
            f'**{saved["check"]["checkin"]} → {saved["check"]["checkout"]}** · '
            f'**{saved["check"]["adults"]} adult(s)** · {saved["check"]["currency"]}\n'
            f'**Saved:** {_stamp(saved["check"]["searched_at"])} '
            f'· **Snapshot:** #{snapshot} of {saved["available_snapshots"]}\n'
            f'**Compared with:** {_stamp(saved["previous"]["searched_at"]) if saved["previous"] else "No earlier detailed check"}\n'
            f'**Budget:** {_money(budget)} · **{within_private} likely-private offers under budget**\n'
            f'**All {total} saved offers:** 🛏️ {count_private} likely private · '
            f'❔ {count_unknown} unclear · 🚫 {count_shared} shared/dorm\n'
            f'**Showing {pos + 1 if total else 0}–{min(pos + per_page, total)} of {total} · page {page}/{last_page}**\n\n'
            '⚠️ Google quotes may be outdated or unavailable at checkout. '
            'Likely private is **not guaranteed**; inspect the sleeping arrangements and final total.'
        ),
    )
    if not offers:
        header.add_field(name='No named room rates saved', value='Run `/hoteloffers` to request a detailed room-price check (1 credit).', inline=False)
    result = [header]
    for category in ('likely_private', 'unknown', 'dormitory_or_shared'):
        group = [r for r in selected if r['room_class'] == category]
        if not group:
            continue
        label = categories[category][1]
        for start in range(0, len(group), 5):
            chunk = group[start:start + 5]
            embed = discord.Embed(title=f'{label} · {len(group)} on this page')
            for r in chunk:
                previous = saved['older'].get(r['offer_key'])
                if previous is None:
                    change = '🆕 Not in previous snapshot' if saved['previous'] else 'First recorded offer'
                else:
                    delta = round(r['total_price'] - previous['total_price'], 2)
                    marker = ('＝ unchanged' if abs(delta) < 0.01 else
                              f'📉 ↓ {_money(-delta)}' if delta < 0 else
                              f'📈 ↑ {_money(delta)}')
                    change = f'{marker} · was {_money(previous["total_price"])}'
                price = r['total_price']
                budget_line = (f'✅ Within {_money(budget)} budget' if price <= budget else
                               f'⚠️ {_money(price - budget)} above {_money(budget)} budget')
                bath = {'private_described': 'private bathroom described',
                        'shared': 'shared bathroom',
                        'not_specified': 'bathroom unspecified'}.get(r['bathroom'], 'bathroom unspecified')
                options = [f'**Room:** {_safe(r["room_name"], 140)}',
                           f'{budget_line} · 🚿 {bath}',
                           f'📊 {change}']
                if r['free_cancellation'] is not None:
                    options.append('✅ Free cancellation stated' if r['free_cancellation'] else '⚠️ Free cancellation not stated')
                if r['breakfast_included'] is not None:
                    options.append('🍳 Breakfast included' if r['breakfast_included'] else '🍳 Breakfast not included')
                offer_url = _url_or_empty(r.get('booking_url'))
                if offer_url and len(offer_url) <= 450:
                    options.append(f'🔗 [Open provider offer]({offer_url})')
                elif offer_url:
                    options.append('🔗 Provider link too long to display safely')
                else:
                    options.append('🔗 No provider link saved')
                embed.add_field(name=f'{_safe(r["provider"], 70)} — {_money(price)}',
                                value='\n'.join(options)[:1024], inline=False)
            embed.set_footer(text='Stored provider quote, not verified checkout availability. No SerpApi request.')
            result.append(embed)
    header.set_footer(text='Zero SerpApi credits. Use /hotelsavedoffers page:2 for more saved offers if available.')
    return result


def _offer_embeds(hotel, offers, checked, previous, old_offers, budget):
    likely = [r for r in offers if r['room_class'] == 'likely_private']
    unclear = [r for r in offers if r['room_class'] == 'unknown']
    dorms = [r for r in offers if r['room_class'] == 'dormitory_or_shared']
    likely_in_budget = sorted((r for r in likely if r['total_price'] <= budget),key=lambda r:r['total_price'])
    unclear_in_budget = sorted((r for r in unclear if r['total_price'] <= budget),key=lambda r:r['total_price'])
    shown = likely_in_budget[:12] + unclear_in_budget[:5]
    header = discord.Embed(title='🛏️ Room offers · ' + hotel['hotel_name'][:140],
        description=(f'**{hotel["checkin"]} → {hotel["checkout"]} · {hotel["adults"]} adult(s)**\n'
                     f'**Budget:** {_money(budget)} for the full stay · GBP\n'
                     f'**Checked:** {_stamp(checked)}\n'
                     f'**Compared with:** {_stamp(previous["searched_at"]) if previous else "No previous room-offer check"}\n\n'
                     f'{"✅" if likely_in_budget else "🔎"} **{len(likely_in_budget)} likely-private offers ≤ budget** · '
                     f'❔ {len(unclear_in_budget)} unclear offers · '
                     f'🚫 {len(dorms)} dorm/shared offers excluded\n\n'
                     '**Likely private ≠ guaranteed.** Room names are clues, not proof of exclusivity; '
                     'confirm your own bedroom, bathroom, final total and guest count on the booking page.'))
    if not offers:
        header.add_field(name='No room-level rates supplied', value='Google returned no priced, named room offers for this hotel. Its headline price is still unverified.', inline=False)
    elif not shown:
        header.add_field(name='No suitable private-room offer identified', value='These returned offers are shared beds, priced above budget, or otherwise not suitable. This does NOT prove private rooms are sold out on other sites.', inline=False)
    if dorms:
        examples = '; '.join(f'{_safe(r["provider"],25)} {_money(r["total_price"])}: '
                             f'{_safe(r["room_name"],65)}' for r in dorms[:3])
        header.add_field(name='Excluded shared-sleeping offers (examples)',
                         value=examples[:1000], inline=False)
    embeds=[header]
    for pos in range(0,len(shown),5):
        chunk=shown[pos:pos+5]
        embed=discord.Embed(title=f'🏨 Room offers {pos+1}–{pos+len(chunk)} of {len(shown)} shown')
        for r in chunk:
            old=old_offers.get(r['offer_key'])
            change = ('🆕 new offer' if old is None else
                      '＝ same' if abs(r['total_price']-old['total_price']) < .01 else
                      f'📉 ↓ {_money(old["total_price"]-r["total_price"])}' if r['total_price']<old['total_price'] else
                      f'📈 ↑ {_money(r["total_price"]-old["total_price"])}')
            cat = '🛏️ likely private' if r['room_class']=='likely_private' else '❔ room type unclear'
            bath = {'private_described':'private bathroom described','shared':'shared bathroom',
                    'not_specified':'bathroom not specified'}[r['bathroom']]
            bits=[f'{cat} · {bath}',f'💷 {change}']
            if r['breakfast_included'] is not None:
                bits.append('🍳 breakfast included' if r['breakfast_included'] else '🍳 breakfast not included')
            if r['free_cancellation'] is not None:
                bits.append('✅ free cancellation' if r['free_cancellation'] else '⚠️ no free cancellation listed')
            if r['booking_url']:
                bits.append(f'🔗 [Open provider offer]({r["booking_url"]})')
            embed.add_field(name=f'{_safe(r["provider"],45)} · {_money(r["total_price"])} · {_safe(r["room_name"],90)}',
                value='\n'.join(bits)[:1024],inline=False)
        embed.set_footer(text='One API request for the whole hotel, not per room. Only on command.')
        embeds.append(embed)
    return embeds


def register_hotel_commands(tree: app_commands.CommandTree):
    """Register once, before the existing bot calls tree.sync()."""
    guild = discord.Object(id=GUILD_ID)

    @tree.command(name='hotels', description='Search Google Hotels on demand; show details and price changes', guild=guild)
    @app_commands.describe(city='City, e.g. Turin Italy', checkin='YYYY-MM-DD', checkout='YYYY-MM-DD',
                           budget='Maximum total trip cost in GBP', adults='Number of adults (default 1)')
    async def hotels(interaction: discord.Interaction, city: str, checkin: str, checkout: str,
                     budget: float = 120.0, adults: int = 1):
        if str(interaction.user.id) != OWNER_ID:
            return await interaction.response.send_message('This is a private command.', ephemeral=True)
        try:
            nights = _night_count(checkin, checkout)
            if not city.strip() or len(city) > 100 or not 1 <= adults <= 8 or not 0 < budget <= 10_000:
                raise ValueError('Invalid city, guest count (1–8), or budget.')
        except ValueError as e:
            return await interaction.response.send_message(f'⚠️ {e} Use YYYY-MM-DD.', ephemeral=True)
        if not SERPAPI_KEY:
            return await interaction.response.send_message('SERPAPI_API_KEY is missing from FadeHost.', ephemeral=True)
        await interaction.response.defer(thinking=True, ephemeral=True)
        params = dict(engine='google_hotels', q=f'hotels in {city.strip()}', check_in_date=checkin,
                      check_out_date=checkout, adults=adults, children=0, currency='GBP', gl='uk',
                      hl='en', sort_by='3', api_key=SERPAPI_KEY, output='json')
        # Do not impose max_price at the API: crossing the budget threshold should be recorded.
        try:
            timeout = aiohttp.ClientTimeout(total=70)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(SERPAPI_URL, params=params) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status != 200 or not isinstance(data, dict) or data.get('error'):
                        raise RuntimeError(f'SerpApi HTTP {resp.status}; check key, quota or query.')
            props = data.get('properties') or []
            if not isinstance(props, list):
                raise RuntimeError('Unexpected API result: properties list missing.')
            parsed = [_row(p) for p in props if isinstance(p, dict)]
            if nights == 1:
                for item in parsed:
                    if item['total_price'] is None:
                        item['total_price'] = item['nightly_price']
            parsed = [r for r in parsed if r['total_price'] is not None]
            if not parsed:
                return await interaction.followup.send('No priced results returned; nothing saved. Try another location.', ephemeral=True)
            previous, older, searched = await asyncio.to_thread(
                _snapshot, city.strip(), checkin, checkout, adults, 'GBP', budget, parsed)
            embeds = _search_embeds(city.strip(), checkin, checkout, nights, adults,
                                   budget, parsed, previous, older, searched)
            for batch in _group_embeds_for_discord(embeds):
                await interaction.followup.send(embeds=batch, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, OSError, discord.HTTPException) as e:
            await interaction.followup.send(f'⚠️ Hotel search failed: {str(e)[:300]}', ephemeral=True)

    @tree.command(name='hoteldetails', description='Read the latest saved hotel details; uses zero API credits', guild=guild)
    @app_commands.describe(hotel='Hotel name (from your previous /hotels results)')
    async def hoteldetails(interaction: discord.Interaction, hotel: str):
        if str(interaction.user.id) != OWNER_ID:
            return await interaction.response.send_message('This is a private command.', ephemeral=True)
        result, error = await asyncio.to_thread(_look_up_hotel, hotel)
        if error:
            return await interaction.response.send_message('⚠️ ' + _safe(error, 650), ephemeral=True)
        row, older, previous = result
        await interaction.response.send_message(embed=_detail_embed(row, older, previous), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @hoteldetails.autocomplete('hotel')
    async def hotel_autocomplete(interaction: discord.Interaction, current: str):
        if str(interaction.user.id) != OWNER_ID:
            return []
        with _db_connect() as c:
            rows = c.execute('''SELECT hotel_name FROM hotel_prices ORDER BY id DESC LIMIT 800''').fetchall()
        hotels = list(dict.fromkeys(r['hotel_name'] for r in rows))
        matches = [h for h in hotels if _normalized(current) in _normalized(h)]
        return [app_commands.Choice(name=h[:100], value=h[:100]) for h in matches[:25]]

    @tree.command(name='hotelstatus', description='Check saved hotel data and storage path; uses zero API credits', guild=guild)
    async def hotelstatus(interaction: discord.Interaction):
        if str(interaction.user.id) != OWNER_ID:
            return await interaction.response.send_message('This is a private command.', ephemeral=True)
        try:
            status = await asyncio.to_thread(_database_status)
            await interaction.response.send_message(
                embed=_status_embed(status), ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())
        except (sqlite3.Error, OSError) as exc:
            await interaction.response.send_message(
                f'⚠️ Could not inspect the hotel database: {type(exc).__name__}', ephemeral=True)

    @tree.command(name='hotelhistory', description='List saved checks for a trip without API usage', guild=guild)
    @app_commands.describe(city='Same city as the search', checkin='YYYY-MM-DD',
                           checkout='YYYY-MM-DD', adults='Guest count used for the search')
    async def hotelhistory(interaction: discord.Interaction, city: str, checkin: str,
                           checkout: str, adults: int = 1):
        if str(interaction.user.id) != OWNER_ID:
            return await interaction.response.send_message('This is a private command.', ephemeral=True)
        try:
            _night_count(checkin, checkout)
        except ValueError as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        checks = (await asyncio.to_thread(_history_rows, city, checkin, checkout, adults))[:10]
        if not checks:
            return await interaction.response.send_message('No saved checks for this trip yet.', ephemeral=True)
        lines = [f'📈 **Hotel price-check history: {_safe(city)} · {checkin} → {checkout}**']
        lines.extend(f'• {_stamp(r["searched_at"])} — {r["property_count"]} priced properties'
                     f' (searched: {_safe(r["destination"], 40)})' for r in checks)
        lines.append('Use `/hotels` to compare prices; `/hoteldetails` to inspect saved hotel information.')
        await interaction.response.send_message('\n'.join(lines), ephemeral=True)


    @tree.command(name='hoteloffers', description='Check actual room types and provider quotes for ONE saved hotel (1 API credit)', guild=guild)
    @app_commands.describe(hotel='Hotel from your saved searches', budget='Total stay budget in GBP')
    async def hoteloffers(interaction: discord.Interaction, hotel: str, budget: float = 120.0):
        if str(interaction.user.id) != OWNER_ID:
            return await interaction.response.send_message('This is a private command.', ephemeral=True)
        if not 0 < budget <= 10000:
            return await interaction.response.send_message('Enter a budget between £1 and £10,000.', ephemeral=True)
        result, error = await asyncio.to_thread(_look_up_hotel, hotel)
        if error:
            return await interaction.response.send_message('⚠️ ' + _safe(error, 500), ephemeral=True)
        selected, _, _ = result
        token = selected.get('property_token')
        if not token:
            return await interaction.response.send_message('No property token saved for this hotel. Search /hotels for this destination again first.', ephemeral=True)
        if not SERPAPI_KEY:
            return await interaction.response.send_message('SERPAPI_API_KEY is missing.', ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        params = dict(engine='google_hotels', q=f'hotels in {selected["destination"]}',
                      check_in_date=selected['checkin'], check_out_date=selected['checkout'],
                      adults=selected['adults'], children=0, currency=selected['currency'],
                      gl='uk', hl='en', property_token=token, api_key=SERPAPI_KEY, output='json')
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75)) as session:
                async with session.get(SERPAPI_URL, params=params) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status != 200 or not isinstance(data, dict) or data.get('error'):
                        raise RuntimeError(f'SerpApi HTTP {resp.status}; check key, quota, or request.')
            if data.get('property_token') and data['property_token'] != token:
                raise RuntimeError('Google returned a different property; no room prices were saved.')
            if data.get('name') and _normalized(data['name']) != _normalized(selected['hotel_name']):
                raise RuntimeError('Returned hotel name differs from your saved selection. Nothing saved.')
            offers = _offer_rows(data)
            # No inferred per-night prices: only total_rate offers are compared and saved.
            checked, previous, prevrows = await asyncio.to_thread(_save_room_offers, selected, offers)
            embeds = _offer_embeds(selected, offers, checked, previous, prevrows, budget)
            for group in _group_embeds_for_discord(embeds):
                await interaction.followup.send(embeds=group,ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, OSError, discord.HTTPException, sqlite3.Error) as exc:
            await interaction.followup.send('⚠️ Room-offer search failed: ' + _safe(str(exc), 300), ephemeral=True)

    @hoteloffers.autocomplete('hotel')
    async def offer_autocomplete(interaction: discord.Interaction, current: str):
        if str(interaction.user.id) != OWNER_ID:
            return []
        with _db_connect() as c:
            rows = c.execute('SELECT hotel_name FROM hotel_prices ORDER BY id DESC LIMIT 800').fetchall()
        hotels = list(dict.fromkeys(r['hotel_name'] for r in rows))
        matches = [h for h in hotels if _normalized(current) in _normalized(h)]
        return [app_commands.Choice(name=h[:100], value=h[:100]) for h in matches[:25]]

    @tree.command(name='hotelofferhistory', description='Show saved detailed room-rate checks (zero API credits)', guild=guild)
    @app_commands.describe(hotel='Hotel from your saved searches')
    async def hotelofferhistory(interaction: discord.Interaction, hotel: str):
        if str(interaction.user.id) != OWNER_ID:
            return await interaction.response.send_message('This is a private command.', ephemeral=True)
        result, error = await asyncio.to_thread(_look_up_hotel, hotel)
        if error:
            return await interaction.response.send_message('⚠️ ' + _safe(error, 500), ephemeral=True)
        selected, _, _ = result
        records=await asyncio.to_thread(_offer_history, selected)
        if not records:
            message='No saved detailed room checks for this hotel yet. Run `/hoteloffers` once.'
        else:
            lines=[f'🛏️ **Saved room-offer checks · {_safe(selected["hotel_name"],90)}**']
            for row in records:
                lines.append(f'• {_stamp(row["searched_at"])} — {row["room_count"]} named rates '
                             f'· 🛏️ {row["private_count"]} likely private '
                             f'· 🚫 {row["shared_count"]} shared/dorm '
                             f'· ❔ {row["unknown_count"]} unclear')
            lines.append('This is saved data and uses zero SerpApi credits.')
            message='\n'.join(lines)
        await interaction.response.send_message(message[:1900], ephemeral=True)

    @hotelofferhistory.autocomplete('hotel')
    async def offerhist_autocomplete(interaction: discord.Interaction, current: str):
        return await offer_autocomplete(interaction, current)


    @tree.command(name='hotelsavedoffers', description='View ALL saved hotel room quotes, including over-budget and dorms (0 API credits)', guild=guild)
    @app_commands.describe(hotel='Hotel from your saved searches', budget='Budget for highlighting offers (GBP)',
                           snapshot='1 = newest detailed check, 2 = previous, etc.',
                           page='Page of saved offers (up to 18 on each page)')
    async def hotelsavedoffers(interaction: discord.Interaction, hotel: str, budget: float = 120.0,
                              snapshot: int = 1, page: int = 1):
        if str(interaction.user.id) != OWNER_ID:
            return await interaction.response.send_message('This is a private command.', ephemeral=True)
        if not 0 < budget <= 10000:
            return await interaction.response.send_message('Enter a budget between £1 and £10,000.', ephemeral=True)
        if not 1 <= snapshot <= 30 or page < 1:
            return await interaction.response.send_message('Snapshot must be 1–30; page must be 1 or higher.', ephemeral=True)
        try:
            result, error = await asyncio.to_thread(_look_up_hotel, hotel)
            if error:
                return await interaction.response.send_message('⚠️ ' + _safe(error, 500), ephemeral=True)
            selected, _, _ = result
            saved = await asyncio.to_thread(_saved_offer_snapshot, selected, snapshot)
            if saved is None:
                return await interaction.response.send_message(
                    f'No saved detailed room offers for snapshot #{snapshot}. Run `/hoteloffers` to save a detailed search first.', ephemeral=True)
            embeds = _saved_offer_embeds(selected, saved, budget, page, snapshot=snapshot)
            # Multiple ephemeral messages, avoiding Discord's combined 6000-character limit.
            batches = _group_embeds_for_discord(embeds)
            await interaction.response.send_message(embeds=batches[0], ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
            for batch in batches[1:]:
                await interaction.followup.send(embeds=batch, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
        except (sqlite3.Error, OSError, ValueError, discord.HTTPException) as exc:
            message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            if interaction.response.is_done():
                await interaction.followup.send('⚠️ Saved-offer lookup failed: ' + _safe(message, 250), ephemeral=True)
            else:
                await interaction.response.send_message('⚠️ Saved-offer lookup failed: ' + _safe(message, 250), ephemeral=True)

    @hotelsavedoffers.autocomplete('hotel')
    async def savedoffer_autocomplete(interaction: discord.Interaction, current: str):
        return await offer_autocomplete(interaction, current)
