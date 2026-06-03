"""Vendor-renaming and order-number rules ported from the Purchase Details app.

This is a faithful port of that app's vendor pipeline (see its
`build_all_query`) and its `clean_ext_po`, adapted to the PO Cost Changes
schema. The transformation order matters and mirrors the source exactly:

  0. Pre-map fixes: Box Office -> Default Vendor; Live Nation Flex ->
     Concert Seasons; Broadway Groups -> Broadway Seasons; and for YSA
     companies only, Live Nation -> Concert Seasons.
  1. Ticketmaster AM / Ballpark -> team name (if major-league team) else Venue.
  2. VENDOR_REPLACEMENTS substring replacements (+ the two prepended rules).
  3. Sports Extras -> Venue.
  4. Ticket Guy box-office: Default Vendor -> BROADWAY_VENUES[Venue]
     (or "Box Office - New World Stages").
  5. Concert Seasons -> CONCERT_SEASONS_MAP[Venue].
  6. Broadway Seasons -> collapse performer to "Various", then
     BROADWAY_SEASONS_MAP[Venue].
  7. Tickets.com -> team name (if MLB) else Venue.
  8. Proper-case Vendor, then fix 76ers / 49ers casing.

Major-league team membership uses the existing major_league_teams.xlsx via
teams.get_teams() (verified identical to the source app's hardcoded set).

The big lookup tables are kept inline here (as in the source app) so the
two apps can be diffed against each other.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from teams import get_teams



VENDOR_REPLACEMENTS = [
    ('AXS', 'Veritix'),
    ('AXS.com', 'Veritix'),
    ('Ticketmaster.com', 'Ticketmaster'),
    ('Toyota Center- Houston', 'Veritix'),
    ('Ak-Chin Pavilion', 'Live Nation Ak-Chin Pavilion'),
    ('BB&T Pavilion', 'Live Nation BB&T Pavilion'),
    ('Dos Equis Pavilion', 'Live Nation Dos Equis Pavilion'),
    ('Jiffy Lube Live', 'Live Nation Jiffy Lube Live'),
    ('Jacobs Pavilion', 'Live Nation Jacobs Pavilion'),
    ('MIDFLORIDA Credit Union Amp', 'Live Nation MidFlorida Credit Union Amp'),
    ('Shoreline Amphitheatre', 'Live Nation Shoreline Amphitheatre'),
    ('Ticketmaster Phones', 'Ticketmaster'),
    ('Shubert Organization Telecharge', 'Telecharge'),
    ('Veritix.com', 'Veritix'),
    ('Huntington Bank Pavilion', 'Live Nation Huntington Bank'),
    ('Darien Lake Amphitheatre', 'Live Nation Darien Lake Amphitheatre'),
    ('Leader Bank Pavillion', 'Live Nation Leader Bank Pavilion'),
    ('Coastal Credit Union Music Park at Walnut Creek', 'Live Nation MidFlorida Credit Union Amp'),
    ('TD Pavilion at the Mann', 'Live Nation TD Pavilion at the Mann'),
    ('Live Nation', 'Ticketmaster'),
    ('Xfinity Theatre', 'Live Nation Xfinity Theatre CT'),
    ('Live Nation Xfinity Boston', 'Live Nation Xfinity Center Boston'),
    ('Live Nation PNC Bank', 'Live Nation PNC Bank Charlotte'),
    ('LN Ruoff Home Mortgage Music Center', 'Live Nation Ruoff'),
    ('Alpine Valley', 'Live Nation Alpine Valley'),
    ('The Pavilion at Toyota Music Factory', 'Live Nation Pavilion at Toyota Music Factory'),
    ('Live Nation Pnc Charlotte', 'Live Nation PNC Music Pavilion'),
    ('The Pavilion at Star Lake', 'Live Nation Pavilion At Star Lake'),
    ('Bank of New Hampshire Pavilion', 'Live Nation Bank of New Hampshire'),
    ('North Island Credit Union', 'Live Nation North Island Credit Union'),
    ('Northwell Health at Jones Beach Theater', 'Live Nation Jones Beach'),
    ('PNC Music Pavilion', 'Live Nation PNC Music Pavilion'),
    ('Ruoff Music Center', 'Live Nation Ruoff Music Center'),
    ('Waterfront Music Pavilion', 'Live Nation BB&T Pavilion'),
    ('Concord Pavilion', 'Live Nation Concord Pavilion'),
    ('Bethel Woods', 'Live Nation Bethel Woods'),
    ('Ford Idaho Center Ampitheater', 'Live Nation Ford Idaho Amp'),
    ('The Met Philadelphia', 'Live Nation The Met Philadelphia'),
    ('The Masonic', 'Live Nation Masonic'),
    ('FPL Solar Ampitheater at Bayfront Park', 'Live Nation FPL Solar Amp'),
    ('Xfinity Center', 'Live Nation Xfinity Center Boston'),
    ('The Cynthia Woods Mitchell Pavilion', 'Live Nation Cynthia Woods Mitchell Pavilion'),
    ('Toyota Amphitheatre', 'Live Nation Toyota Amp'),
    ('Cellairis Amphitheatre at Lakewood', 'Live Nation Cellairis'),
    ('Oak Mountian Amphitheatre', 'Live Nation Oak Mountain'),
    ('Chicago Fire', 'Chicago Fire FC'),
    ('Dallas FC', 'FC Dallas'),
    ('Houston Dynamo', 'Houston Dynamo FC'),
    ('LAFC', 'Los Angeles FC'),
    ('LA Galaxy', 'Los Angeles Galaxy'),
    ('Minnesota United', 'Minnesota United FC'),
    ('New York FC', 'New York City FC'),
    ('Seattle Sounders', 'Seattle Sounders FC'),
    ('St. Louis City', 'St. Louis City SC'),
    ('Vancouver Whitecaps', 'Vancouver Whitecaps FC'),
    ('Portland Trailblazers', 'Portland Trail Blazers'),
    ('Concert Extras', 'Live Nation Extras'),
    ('PHILADELPHIA 76ERS', 'Philadelphia 76ers'),
    ('SAN FRANCISCO 49RS', 'San Francisco 49ers'),
    ('Concert Partials', 'Concert Seasons'),
    ('Legacy StubHub', 'StubHub'),
]

BROADWAY_VENUES = {
    'Brooks Atkinson Theatre': 'Box Office - Brooks Atkinson Theatre',
    'Hudson Broadway Theatre': 'Box Office - Hudson Broadway Theatre',
    'Minskoff Theatre': 'Box Office - Minskoff Theatre',
    'Madison Square Garden': 'Box Office - MSG Advance',
    'Neil Simon Theatre': 'Box Office - Neil Simon Theatre',
    'Richard Rodgers Theatre': 'Box Office - Richard Rodgers Theatre',
    'Winter Garden Theater New York': 'Box Office - Winter Garden Theatre',
    'Walter Kerr Theatre': 'Box Office - Walter Kerr Theatre',
    'August Wilson Theatre': 'Box Office - August Wilson Theatre',
    'Music Box Theatre New York': 'Box Office - Music Box Theatre',
    'Imperial Theatre New York': 'Box Office - Imperial Theatre',
    'Lyceum Theatre New York': 'Box Office - Lyceum Theatre',
    'Booth Theatre': 'Box Office - Booth Theatre',
    'Longacre Theatre': 'Box Office - Longacre Theatre',
    'Majestic Theatre New York': 'Box Office - Majestic Theatre',
    'Broadhurst Theatre': 'Box Office - Broadhurst Theatre',
    'Stephen Sondheim Theatre': 'Box Office - Stephen Sondheim Theatre',
    'Lena Horne Theatre': 'Box Office - Lena Horne Theatre',
    'Lyric Theatre - NY': 'Box Office - Lyric Theatre',
    'Winter Garden Theatre (Toronto)': 'Box Office - Winter Garden Theatre (Toronto)',
    'Marquis Theatre New York': 'Box Office - Marquis Theatre',
    'Lunt Fontanne Theatre': 'Box Office - Lunt Fontanne Theatre',
    'John Golden Theatre': 'Box Office - John Golden Theatre',
    'Circle In The Square': 'Box Office - Circle In The Square',
}

CONCERT_SEASONS_MAP = {
    'Ak-Chin Pavilion': 'Live Nation Ak-Chin Pavilion',
    'Alpine Valley Music Theatre': 'Live Nation Alpine Valley',
    'Bank of New Hampshire Pavilion': 'Live Nation Bank of New Hampshire',
    "Darling's Waterfront Pavilion": 'Live Nation Waterfront',
    'Darlings Waterfront Pavilion': 'Live Nation Waterfront',
    'Bethel Woods Center For The Arts': 'Live Nation Bethel Woods',
    'Blossom Music Center': 'Live Nation Blossom MC',
    'Lakewood Amphitheatre': 'Live Nation Lakewood Amphitheatre',
    'Coastal Credit Union Music Park at Walnut Creek': 'Live Nation Coastal Credit Union',
    'Concord Pavilion': 'Live Nation Concord Pavilion',
    'Cynthia Woods Mitchell Pavilion': 'Live Nation Cynthia Woods Mitchell Pavilion',
    'Darien Lake Amphitheater': 'Live Nation Darien Lake Amphitheatre',
    'Dos Equis Pavilion': 'Live Nation Dos Equis Pavilion',
    'FivePoint Amphitheatre': 'Live Nation FivePoint',
    'Ford Idaho Center': 'Live Nation Ford Idaho Amp',
    'Bayfront Park Amphitheatre': 'Live Nation FPL Solar Amp',
    'Glen Helen Amphitheater': 'Live Nation Glen Helen',
    'Gorge Amphitheatre': 'Live Nation Gorge',
    'Hershey Theatre': 'Live Nation Hershey',
    'Hollywood Casino Amphitheatre - Tinley Park': 'Live Nation Hollywood Casino - Tinley Park',
    'Huntington Bank Pavilion at Northerly Island': 'Live Nation Huntington Bank',
    'Isleta Amphitheater': 'Live Nation Isleta Amphitheatre',
    'Jacobs Pavilion at Nautica': 'Live Nation Jacobs Pavilion',
    'Jiffy Lube Live': 'Live Nation Jiffy Lube Live',
    'Northwell Health at Jones Beach Theater': 'Live Nation Jones Beach',
    'KeyBank Center': 'Live Nation KeyBank',
    'Leader Bank Pavilion': 'Live Nation Leader Bank Pavilion',
    'The Masonic': 'Live Nation Masonic',
    'MGM Music Hall at Fenway': 'Live Nation MGM Music Hall',
    'MidFlorida Credit Union Amphitheatre': 'Live Nation MidFlorida Credit Union Amp',
    'North Island Credit Union Amphitheatre': 'Live Nation North Island Credit Union',
    'Oak Mountain Amphitheatre': 'Live Nation Oak Mountain',
    'The Pavilion at Star Lake': 'Live Nation Pavilion At Star Lake',
    'Pavilion at the Toyota Music Factory': 'Live Nation Pavilion At Toyota Music Factory',
    'PNC Bank Arts Center': 'Live Nation PNC Bank Charlotte',
    'PNC Music Pavilion': 'Live Nation PNC Music Pavilion',
    'Ruoff Music Center': 'Live Nation Ruoff',
    'Shoreline Amphitheatre': 'Live Nation Shoreline Amphitheatre',
    'TD Pavilion at the Mann': 'Live Nation TD Pavilion at the Mann',
    'The Met Philadelphia': 'Live Nation The Met Philadelphia',
    'Toyota Amphitheatre': 'Live Nation Toyota Amp',
    'White River Amphitheatre': 'Live Nation White River',
    'The Wiltern': 'Live Nation Wiltern',
    'Xfinity Center': 'Live Nation Xfinity Center Boston',
    'Xfinity Theatre': 'Live Nation Xfinity Theatre CT',
    'Freedom Mortgage Pavilion': 'Live Nation Freedom Mortgage Music Pavilion',
    'Bayfront Park-Miami': 'Live Nation FPL Solar Amp',
    'Idaho Center Amphitheater': 'Live Nation Ford Idaho Amp',
    'Coca-Cola Roxy': 'Live Nation Coca-Cola Roxy Theatre',
    'Farm Bureau Insurance Lawn at White River State Park': 'Live Nation TCU Amp',
    'Charlotte Metro Credit Union Amphitheatre': 'Live Nation Charlotte Metro Credit Union',
    '713 Music Hall': 'Live Nation 713 Music Hall',
    'TCU Amphitheater at White River State Park': 'Live Nation TCU Amp',
    'USANA Amphitheatre': 'Live Nation USANA Amp',
    'Hollywood Casino Amphitheater - St. Louis': 'Live Nation Hollywood Casino - St. Louis',
    'Hollywood Casino Amphitheatre St Louis': 'Live Nation Hollywood Casino - St. Louis',
    'iTHINK Financial Amphitheatre': 'Live Nation iTHINK Financial Amp',
    'Ameris Bank Amphitheatre': 'Live Nation Ameris Bank Amp',
    'Ascend Amphitheater': 'Live Nation Ascend Amp',
    'FirstBank Amphitheater': 'Live Nation FirstBank Amp',
    'Hersheypark Stadium': 'Live Nation Hersheypark Stadium',
    'Veterans United Home Loans Amphitheater': 'Live Nation Veterans United Amp',
    'Starlight Theatre': 'Live Nation Starlight Theatre',
    'Riverbend Music Center': 'Live Nation Riverbend Music Center',
    'The Terminal - Houston': 'Live Nation 713 Music Hall',
    'Veterans United Home Loans Amphitheater at Virginia Beach': 'Live Nation Veterans United Amp',
    'Saint Louis Music Park': 'Live Nation Saint Louis Music Park',
    'Old National Centre': 'Live Nation Old National Centre',
    'Skyla Credit Union': 'Live Nation Skyla Credit Union Amp',
    'Skyla Credit Union Amphitheatre': 'Live Nation Skyla Credit Union Amp',
    'Pine Knob Music Center': 'Live Nation Pine Knob Music Center',
    "St. Joseph's Health Amphitheater at Lakeview": "Live Nation St. Joseph's Health Amp",
    'Red Hat Amphitheater': 'Live Nation Red Hat Amphitheater',
    'Talking Stick Resort Amphitheatre': 'Live Nation Talking Stick Resort Amp',
    'The Fillmore Detroit': 'Live Nation The Fillmore Detroit',
    'CFG Bank Arena': 'Live Nation CFG Bank Arena',
    'Aragon Ballroom': 'Live Nation Aragon Ballroom',
    'Saratoga Performing Arts Center': 'Live Nation Saratoga Springs PAC',
    'The Fillmore - Charlotte': 'Live Nation The Fillmore Charlotte',
    'Fillmore Auditorium-CO': 'Live Nation The Fillmore Denver',
    'The Fillmore - Philadelphia': 'Live Nation The Fillmore Philly',
    'The Fillmore-Silver Spring': 'Live Nation The Fillmore Silver Spring',
    'SAP Center at San Jose': 'Live Nation SAP Center at San Jose',
    'Arizona Federal Theatre': 'Live Nation Arizona Federal Theatre',
    'Flagstar at Westbury Music Fair': 'Live Nation Flagstar At Westbury Music Fair',
    'Uptown Minneapolis': 'Live Nation Uptown Minneapolis',
    'The Pavilion At Toyota Music Factory': 'Live Nation Pavilion At Toyota Music Factory',
    'Forest Hills Stadium': 'Forest Hills Stadium',
    'NRG Stadium': 'Houston Rodeo',
    'Hayden Homes Amphitheater': 'Live Nation Hayden Homes Amphitheater',
    'The Dome at Oakdale Theatre': 'Live Nation Toyota Oakdale Theatre',
    'The Dome at Toyota Oakdale Theatre': 'Live Nation Toyota Oakdale Theatre',
    'Broadview Stage at SPAC': 'Live Nation Broadview Stage at SPAC',
    'BECU Live Outdoor Venue': 'Live Nation BECU',
    'BankNH Pavilion': 'Live Nation Bank of New Hampshire',
    'Everwise Amphitheater at White River State Park': 'Live Nation White River',
    'The Cynthia Woods Mitchell Pavilion presented by Huntsman': 'Live Nation Cynthia Woods Mitchell Pavilion',
    'Harbor Yard Amphitheater': 'Live Nation Harbor Yard Amp',
    'Toyota Pavilion at Concord': 'Live Nation Concord Pavilion',
    'Utah First Credit Union Amphitheatre (formerly USANA Amp)': 'Live Nation Usana Amp',
    'Toyota Oakdale Theatre': 'Live Nation Toyota Oakdale Theatre',
    'Byline Bank Aragon Ballroom': 'Live Nation Aragon Ballroom',
    'Skyline Stage at the Mann': 'Live Nation Skyline Stage At The Mann',
    'Santa Barbara Bowl': 'Live Nation Santa Barbara Bowl',
    '20 Monroe Live': 'Live Nation GLC Live at 20 Monroe',
    'MIDFLORIDA Credit Union Amphitheatre at the FL State Fairgrounds': 'Live Nation MidFlorida Credit Union Amp',
    'Credit Union 1 Amphitheatre': 'Live Nation Credit Union 1 Amphitheatre',
    "Daily's Place": 'Live Nation Dailys Place',
    'Greek Theatre Los Angeles': 'Live Nation Greek Theatre Los Angeles',
    'Koka Booth Field 2': 'Live Nation Koka Booth Amphitheatre',
    'Michigan Lottery Amphitheatre at Freedom Hill': 'Live Nation Michigan Lottery Amphitheatre',
    'Mountain Winery': 'Live Nation Mountain Winery',
    'Skyla Credit Union Amphitheatre at AvidXchange Music Factory': 'Live Nation Skyla Credit Union Amp',
    'Vibrant Music Hall': 'Live Nation Vibrant Music Hall',
    'Vina Robles Amphitheatre': 'Live Nation Vina Robles Amphitheatre',
    'Whitewater Amphitheater': 'Live Nation Whitewater Amphitheater',
    'Old National Centre Complex': 'Live Nation Old National Centre',
    'Fiddlers Green Amphitheatre': 'Live Nation Fiddlers Green Amphitheatre',
    'Pine Knob Music Theatre': 'Live Nation Pine Knob Music Center',
    'Hollywood Palladium': 'Live Nation Hollywood Palladium',
    'Hartford HealthCare Amphitheater': 'Live Nation Hartford Healthcare Amphitheater (Harbor Yard)',
    'Mystic Lake Amphitheater': 'Live Nation Mystic Lake Amphitheater',
    'Fillmore Minneapolis': 'Live Nation Fillmore Minneapolis',
    'Uptown Theater - Minneapolis': 'Live Nation Uptown Minneapolis',
    'Old National Centre.': 'Live Nation Old National Centre',
    'Sandy Amphitheater': 'Live Nation Sandy Amphitheater',
}

BROADWAY_SEASONS_MAP = {
    'Boston Opera House': 'Broadway Boston',
    'Colonial Theatre Boston': 'Broadway Boston',
    'Fox Theatre - Atlanta': 'Broadway Atlanta',
    'Hippodrome Theatre': 'Broadway Baltimore',
    'BJCC Concert Hall': 'Broadway Birmingham',
    "Shea's Buffalo Theatre": 'Broadway Buffalo',
    'Procter and Gamble Hall at Aronoff Center for the Arts': 'Broadway Cincinnati',
    'AT&T Performing Arts Center - Winspear Opera House': 'Broadway Dallas',
    'Music Hall at Fair Park': 'Broadway Dallas',
    'Durham Performing Arts Center': 'Broadway Durham',
    'Broward Center Amaturo': 'Broadway Ft Lauderdale',
    'Devos Performance Hall': 'Broadway Grand Rapids',
    'Hollywood Pantages Theatre': 'Broadway Hollywood',
    'Music Hall - Kansas City': 'Broadway Kansas City',
    'Muriel Kauffman Theatre at Kauffman Center for the Performing Arts': 'Broadway Kansas City',
    'Saenger Theatre-New Orleans': 'Broadway New Orleans',
    'Sarofim Hall at The Hobby Center': 'Broadway Houston',
    'Uihlein Hall at Marcus Center for the Performing Arts': 'Broadway Milwaukee',
    'Orpheum Theatre Minneapolis': 'Broadway Minneapolis',
    'Clowes Memorial Hall': 'Broadway Indianapolis',
    'Old National Centre': 'Broadway Indianapolis',
    'Paramount Theatre': 'Broadway Seattle',
    'San Diego Civic Theatre': 'Broadway San Diego',
    'San Jose Center for the Performing Arts': 'Broadway San Jose',
}

# Companies (raw/original names) that trigger the YSA and Ticket Guy rules.
YSA_COMPANIES = {"YSA", "YSA 2", "YSA 3"}
TICKET_GUY_COMPANIES = {
    "The Ticket Guy", "The Ticket Guy-Jas", "The Ticket Guy-Legacy", "The Ticket Guy VIP",
}

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _should_blank_order(v) -> bool:
    """True if an order number should be blanked: UUID format, or an
    all-numeric string of 19+ digits."""
    if pd.isna(v):
        return False
    s = str(v).strip()
    if UUID_RE.match(s):
        return True
    if s.isdigit() and len(s) >= 19:
        return True
    return False


def _is_numeric_at_least(v, n: int) -> bool:
    """True if v is an all-numeric string of at least n digits."""
    if pd.isna(v):
        return False
    s = str(v).strip()
    return s.isdigit() and len(s) >= n


def clean_ext_po(df: pd.DataFrame) -> pd.DataFrame:
    """Blank ExtPONumber for UUID / 19+-digit values (all vendors); for any
    Concert Seasons row (always); and for Ticketmaster AM rows only when the
    order number is a 15+-digit number. Operates on 'ExtPONumber' in place.

    Runs BEFORE vendor renaming (as in the source app, where
    clean_ext_po(df_raw) precedes build_all_query), so the Concert Seasons /
    Ticketmaster AM check sees the RAW vendor names — before any rename
    resolves them to a team or venue.
    """
    if "ExtPONumber" not in df.columns:
        return df
    df["ExtPONumber"] = df["ExtPONumber"].apply(
        lambda v: "" if _should_blank_order(v) else v
    )
    vendor_norm = df["Vendor"].astype(str).str.strip()
    # Concert Seasons: always blank the order number.
    df.loc[vendor_norm == "Concert Seasons", "ExtPONumber"] = ""
    # Ticketmaster AM: blank only when the order number is 15+ digit numeric.
    tmam = vendor_norm == "Ticketmaster AM"
    long_num = df["ExtPONumber"].apply(lambda v: _is_numeric_at_least(v, 15)).astype(bool)
    df.loc[tmam & long_num, "ExtPONumber"] = ""
    return df


def _apply_vendor_replacements(df: pd.DataFrame) -> pd.DataFrame:
    """Stage 2: the two prepended rules + the VENDOR_REPLACEMENTS substring list."""
    df["Vendor"] = df["Vendor"].replace("Box Office", "Default Vendor")
    df["Vendor"] = df["Vendor"].replace("Live Nation Flex", "Concert Seasons")
    for old, new in VENDOR_REPLACEMENTS:
        df["Vendor"] = df["Vendor"].str.replace(old, new, regex=False)
    return df


def apply_vendor_pipeline(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the full vendor-rename pipeline to a normalized PO Cost Changes
    frame. Expects columns: Vendor, Team/Performer, VenueName, Original Company.
    Mutates and returns df. Venue lookups use VenueName; company-gated rules use
    Original Company (the raw TicketVault company, e.g. 'YSA', 'The Ticket Guy').
    """
    # Venue lookups use VenueName; company-gated rules use Original Company.
    if df.empty:
        return df

    # Work with clean string views; keep originals where blank.
    df["Vendor"] = df["Vendor"].astype("string").fillna("").map(lambda s: s.strip())
    venue = df["VenueName"].astype("string").fillna("").map(lambda s: s.strip())
    perf = df["Team/Performer"].astype("string").fillna("").map(lambda s: s.strip())
    orig = df["Original Company"].astype("string").fillna("").map(lambda s: s.strip())
    teams = get_teams()  # lowercased set

    def is_team(name: str) -> bool:
        return str(name).strip().lower() in teams

    # 0. Pre-map fixes.
    df["Vendor"] = df["Vendor"].replace("Broadway Groups", "Broadway Seasons")
    ysa_mask = orig.isin(YSA_COMPANIES) & (df["Vendor"] == "Live Nation")
    df.loc[ysa_mask, "Vendor"] = "Concert Seasons"

    # 1. Ticketmaster AM / Ballpark -> team (if major league) else venue.
    def resolve_tm_am(i: int) -> str:
        v = df["Vendor"].iat[i]
        if v not in ("Ticketmaster AM", "Ballpark"):
            return v
        if is_team(perf.iat[i]):
            return perf.iat[i]
        return venue.iat[i] if venue.iat[i] else v
    df["Vendor"] = [resolve_tm_am(i) for i in range(len(df))]

    # 2. Substring replacements.
    df = _apply_vendor_replacements(df)

    # 3. Sports Extras -> venue.
    df["Vendor"] = np.where(
        df["Vendor"].astype(str) == "Sports Extras", venue.values, df["Vendor"].astype(str)
    )
    df["Vendor"] = pd.Series(df["Vendor"], index=df.index).astype("string")

    # 4. Ticket Guy box office: Default Vendor -> BROADWAY_VENUES[venue].
    def ticket_guy_vendor(i: int) -> str:
        v = df["Vendor"].iat[i]
        if orig.iat[i] not in TICKET_GUY_COMPANIES:
            return v
        if v != "Default Vendor":
            return v
        ven = venue.iat[i]
        if "New World Stages" in str(ven):
            return "Box Office - New World Stages"
        return BROADWAY_VENUES.get(ven, v)
    df["Vendor"] = [ticket_guy_vendor(i) for i in range(len(df))]

    # 5. Concert Seasons -> CONCERT_SEASONS_MAP[venue].
    df["Vendor"] = [
        CONCERT_SEASONS_MAP.get(venue.iat[i], df["Vendor"].iat[i])
        if df["Vendor"].iat[i] == "Concert Seasons" else df["Vendor"].iat[i]
        for i in range(len(df))
    ]

    # 6. Broadway Seasons -> collapse performer to "Various", then map by venue.
    is_bs = df["Vendor"] == "Broadway Seasons"
    df.loc[is_bs, "Team/Performer"] = "Various"
    df["Vendor"] = [
        BROADWAY_SEASONS_MAP.get(venue.iat[i], df["Vendor"].iat[i])
        if df["Vendor"].iat[i] == "Broadway Seasons" else df["Vendor"].iat[i]
        for i in range(len(df))
    ]

    # 7. Tickets.com -> team (if MLB) else venue.
    def resolve_tickets_com(i: int) -> str:
        v = df["Vendor"].iat[i]
        if v != "Tickets.com":
            return v
        return perf.iat[i] if is_team(perf.iat[i]) else (venue.iat[i] if venue.iat[i] else v)
    df["Vendor"] = [resolve_tickets_com(i) for i in range(len(df))]

    # 8. Proper-case, then fix known casing exceptions.
    df["Vendor"] = df["Vendor"].astype(str).str.title()
    df["Vendor"] = df["Vendor"].str.replace("Philadelphia 76Ers", "Philadelphia 76ers", regex=False)
    df["Vendor"] = df["Vendor"].str.replace("San Francisco 49Ers", "San Francisco 49ers", regex=False)

    return df
