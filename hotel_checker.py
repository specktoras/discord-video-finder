"""Add on-demand Google Hotels searching + SQLite price history to an existing discord.py CommandTree.
Call register_hotel_commands(tree) before tree.sync().
"""
from __future__ import annotations
import asyncio
import datetime as dt
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import discord
from discord import app_commands

SERPAPI_URL = 'https://serpapi.com/search.json'
DB_PATH = os.getenv('HOTEL_DB_PATH', '/data/storage/hotels.sqlite3')
SERPAPI_KEY = os.getenv('SERPAPI_API_KEY', '')
OWNER_ID = os.environ['OWNER_ID']
GUILD_ID = int(os.environ['GUILD_ID'])
PAGE_LIMIT = 1  # one API request per command; never background polling


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
        CREATE INDEX IF NOT EXISTS idx_prices_key ON hotel_prices (check_id, property_key);
    ''')
    return con


def _normalized(s: str):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


def _price(obj):
    if not isinstance(obj, dict):
        return None
    for key in ('extracted_lowest', 'extracted_price'):
        v = obj.get(key)
        if isinstance(v, (float, int)) and v > 0:
            return round(float(v), 2)
    return None


def _row(p):
    price = _price(p.get('rate_per_night'))
    total = _price(p.get('total_rate'))
    # Do not guess trip totals for multi-night stays.
    if total is None:
        total = None
    name = str(p.get('name') or 'Unnamed property')
    gps = p.get('gps_coordinates') or {}
    address = str(p.get('address') or '')
    token = p.get('property_token') or ''
    # Hotel identity: token where present, fallback to normalized name + place.
    identity = ('token:' + str(token)) if token else 'name:' + _normalized(name) + ':' + _normalized(address)
    sources = ', '.join(str(x.get('source')) for x in (p.get('prices') or []) if isinstance(x, dict) and x.get('source'))[:200]
    rating = p.get('overall_rating') or p.get('rating')
    try: rating = float(rating)
    except (ValueError, TypeError): rating = None
    return dict(property_key=identity, hotel_name=name, property_type=p.get('type') or '',
                nightly_price=price, total_price=total, rating=rating, address=address,
                property_token=str(token), hotel_url=str(p.get('link') or ''), booking_sources=sources)


def _snapshot(destination, checkin, checkout, adults, currency, budget, rows):
    with _db_connect() as c:
        prev = c.execute('''SELECT id FROM hotel_checks WHERE destination=? AND checkin=? AND checkout=?
            AND adults=? AND currency=? ORDER BY id DESC LIMIT 1''',
            (_normalized(destination), checkin, checkout, adults, currency)).fetchone()
        prev_rows = {}
        if prev:
            for r in c.execute('SELECT * FROM hotel_prices WHERE check_id=?', (prev['id'],)):
                prev_rows[r['property_key']] = dict(r)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        cur = c.execute('''INSERT INTO hotel_checks(searched_at,destination,checkin,checkout,adults,currency,budget,property_count)
            VALUES (?,?,?,?,?,?,?,?)''', (now, _normalized(destination), checkin, checkout, adults, currency, budget, len(rows)))
        check_id = cur.lastrowid
        for r in rows:
            c.execute('''INSERT INTO hotel_prices(check_id,property_key,hotel_name,property_type,nightly_price,total_price,rating,address,property_token,hotel_url,booking_sources)
                VALUES(:check_id,:property_key,:hotel_name,:property_type,:nightly_price,:total_price,:rating,:address,:property_token,:hotel_url,:booking_sources)''', {'check_id':check_id, **r})
    return check_id, prev_rows


def _night_count(a,b):
    start,end=dt.date.fromisoformat(a),dt.date.fromisoformat(b)
    n=(end-start).days
    if n < 1 or n > 30: raise ValueError('Check-out must be 1–30 days after check-in.')
    if start < dt.date.today(): raise ValueError('Check-in cannot be in the past.')
    return n


def _google_link(city,checkin,checkout,adults):
    # Google Hotels landing page: users can verify the actual private-room offer.
    q=f'hotels in {city} {checkin} to {checkout} {adults} adult'
    return 'https://www.google.com/travel/hotels?' + urlencode({'q':q})


def _money(v):
    return '—' if v is None else f'£{v:,.2f}'


def _comparison(current, older):
    if not older or older.get('total_price') is None or current.get('total_price') is None:
        return 'new' if not older else 'n/a'
    d=round(current['total_price']-older['total_price'], 2)
    if abs(d)<0.01: return 'same'
    return (f'↓ £{-d:.2f}' if d<0 else f'↑ £{d:.2f}')


def register_hotel_commands(tree: app_commands.CommandTree):
    """Run once, before the existing bot calls tree.sync()."""
    @tree.command(name='hotels', description='Search Google Hotels on demand and compare with the previous check', guild=discord.Object(id=GUILD_ID))
    @app_commands.describe(city='City, e.g. Turin Italy', checkin='YYYY-MM-DD', checkout='YYYY-MM-DD', budget='Maximum total trip cost in GBP', adults='Number of adults (default 1)')
    async def hotels(interaction: discord.Interaction, city: str, checkin: str, checkout: str, budget: float = 120.0, adults: int = 1):
        if OWNER_ID and str(interaction.user.id) != OWNER_ID:
            return await interaction.response.send_message('This is a private command.', ephemeral=True)
        try:
            nights = _night_count(checkin, checkout)
            if not city.strip() or len(city)>100 or adults<1 or adults>8 or budget<=0 or budget>10000:
                raise ValueError('Invalid city, guest count (1–8), or budget.')
        except ValueError as e:
            return await interaction.response.send_message(f'⚠️ {e} Use YYYY-MM-DD.',ephemeral=True)
        if not SERPAPI_KEY:
            return await interaction.response.send_message('SERPAPI_API_KEY is missing from the server environment.', ephemeral=True)
        await interaction.response.defer(thinking=True, ephemeral=True)
        params = dict(engine='google_hotels', q=f'hotels in {city.strip()}', check_in_date=checkin,
                      check_out_date=checkout, adults=adults, children=0, currency='GBP', gl='uk',
                      hl='en', sort_by='3', api_key=SERPAPI_KEY, output='json')
        # Intentionally omit max_price to catch changes across the budget threshold.
        try:
            timeout = aiohttp.ClientTimeout(total=70)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(SERPAPI_URL, params=params) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status != 200 or data.get('error'):
                        raise RuntimeError(f'SerpApi returned HTTP {resp.status}; check your key, quota, or query in the SerpApi dashboard.')
            props = data.get('properties') or []
            if not isinstance(props,list): raise RuntimeError('Unexpected API response: missing properties list.')
            parsed = [_row(p) for p in props if isinstance(p,dict)]
            if nights == 1:
                for row in parsed:
                    if row['total_price'] is None:
                        row['total_price'] = row['nightly_price']
            parsed = [r for r in parsed if r['total_price'] is not None]
            if not parsed:
                return await interaction.followup.send('No priced results returned; nothing was saved. Try another location.', ephemeral=True)
            _, previous = await asyncio.to_thread(_snapshot, city.strip(),checkin,checkout,adults,'GBP',budget,parsed)
            in_budget = [x for x in parsed if x['total_price'] <= budget]
            in_budget.sort(key=lambda r:r['total_price'])
            link=_google_link(city,checkin,checkout,adults)
            lines=[f'🏨 **{city}** · {checkin} → {checkout} ({nights} night(s), {adults} adult(s))',
                   f'**Google Hotels quotes:** {len(parsed)} priced properties checked · **{len(in_budget)} ≤ {_money(budget)}**',
                   f'Compared against: {"previous check" if previous else "none — first check"}',
                   '⚠️ Lowest property quotes **do not prove a private room**. Confirm room type, final tax and cancellation before paying.', '']
            for item in in_budget[:12]:
                name=discord.utils.escape_markdown(item['hotel_name'])[:55]
                lines.append(f'• **{name}** — {_money(item["total_price"])} · {_comparison(item,previous.get(item["property_key"]))}')
            if not in_budget: lines.append('No quoted properties within budget in this page of results.')
            lines.extend(['', f'🔗 [Open Google Hotels to check room offers]({link})',
                          'ℹ️ On-demand only. Prices stored for this exact city, dates, guests and currency. The search covers one results page; not all hotels.'])
            msg='\n'.join(lines)
            await interaction.followup.send(msg[:1950], ephemeral=True, suppress_embeds=True)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, OSError) as e:
            await interaction.followup.send(f'⚠️ Hotel search failed: {str(e)[:300]}',ephemeral=True)

    @tree.command(name='hotelhistory', description='View the last checks saved for a hotel trip', guild=discord.Object(id=GUILD_ID))
    @app_commands.describe(city='Same city as the search',checkin='YYYY-MM-DD',checkout='YYYY-MM-DD',adults='Guest count used for search')
    async def hotelhistory(interaction: discord.Interaction, city: str, checkin: str, checkout: str, adults: int=1):
        if OWNER_ID and str(interaction.user.id) != OWNER_ID:
            return await interaction.response.send_message('This is a private command.', ephemeral=True)
        try: _night_count(checkin,checkout)
        except ValueError as e: return await interaction.response.send_message(str(e),ephemeral=True)
        with _db_connect() as c:
            checks=c.execute('''SELECT id,searched_at,property_count FROM hotel_checks WHERE destination=? AND checkin=? AND checkout=? AND adults=? AND currency='GBP' ORDER BY id DESC LIMIT 8''',(_normalized(city),checkin,checkout,adults)).fetchall()
        if not checks: return await interaction.response.send_message('No saved checks for this exact trip yet.',ephemeral=True)
        lines=[f'📈 **Hotel search history: {city}, {checkin} to {checkout}**']
        for chk in checks:
            stamp=chk['searched_at'][:16].replace('T',' ')+' UTC'
            lines.append(f'• {stamp} — {chk["property_count"]} quoted properties')
        lines.append('Run `/hotels` again to compare current quotes with the previous snapshot.')
        await interaction.response.send_message('\n'.join(lines),ephemeral=True)
