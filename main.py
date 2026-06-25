#!/usr/bin/env python3
"""
Paver Leads Bot v2
Sources: Google Maps Places API | Realtor.com Recently Sold | BuildZoom Permit Records
Counties: Oakland, Macomb, Lapeer, Livingston, St. Clair (Michigan)
"""

import os
import json
import time
import re
import logging
from datetime import datetime
from typing import List, Dict

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# ── Config ───────────────────────────────────────────────────────────────────

SHEET_ID = "11mvFKbAd7650uxv8yXheB-yAwJn8WTxXmJbxi5dnwHk"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

COUNTY_CITIES = {
    "Oakland":    ["Troy", "Birmingham", "Bloomfield Hills", "Auburn Hills",
                   "Rochester Hills", "Clarkston", "Waterford", "Novi", "Southfield", "Pontiac"],
    "Macomb":     ["Warren", "Sterling Heights", "St. Clair Shores", "Roseville",
                   "Clinton Township", "Chesterfield", "Shelby Township", "Utica"],
    "Lapeer":     ["Lapeer", "Imlay City", "Almont", "Metamora"],
    "Livingston": ["Howell", "Brighton", "Hartland", "Pinckney"],
    "St. Clair":  ["Port Huron", "Marysville", "St. Clair", "Marine City"],
}

MAPS_TARGETS = [
    "property management company",
    "HOA homeowners association",
    "commercial real estate",
    "apartment complex",
    "shopping plaza",
    "office park",
    "condominium association",
]

PERMIT_KEYWORDS = [
    "patio", "paver", "driveway", "hardscape", "retaining wall", "brick paver",
]

SHEET_HEADERS = [
    "Date Found", "Source", "Title / Name", "Description",
    "County", "City", "Service Type", "Lead Type",
    "Motivation Score", "Motivation Reason", "Contact Info",
    "URL", "Status", "Notes",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
log = logging.getLogger(__name__)

# ── Google Sheets ────────────────────────────────────────────────────────────

def get_sheet():
    creds_dict = json.loads(os.environ["SERVICE_ACCOUNT_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).sheet1


def ensure_headers(ws):
    if ws.row_values(1) != SHEET_HEADERS:
        ws.update("A1", [SHEET_HEADERS])
        ws.format("A1:N1", {"textFormat": {"bold": True}})


def get_existing_urls(ws) -> set:
    try:
        return set(ws.col_values(12)[1:])
    except Exception:
        return set()


def append_leads(ws, leads: List[Dict]):
    if not leads:
        log.info("No new leads to append.")
        return
    rows = [[
        l["date_found"], l["source"], l["name"], l["description"],
        l["county"], l["city"], l["service_type"], l["lead_type"],
        l["motivation_score"], l["motivation_reason"], l["contact"],
        l["url"], "New", "",
    ] for l in leads]
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    log.info(f"Appended {len(rows)} lead(s).")

# ── Source 1: Google Maps Places ─────────────────────────────────────────────

def scrape_google_maps(api_key: str) -> List[Dict]:
    if not api_key:
        log.info("GOOGLE_MAPS_API_KEY not set — skipping.")
        return []

    leads = []
    for county in COUNTY_CITIES:
        for term in MAPS_TARGETS:
            query = f"{term} {county} County Michigan"
            try:
                r = requests.get(
                    "https://maps.googleapis.com/maps/api/place/textsearch/json",
                    params={"query": query, "key": api_key},
                    timeout=12,
                )
                results = r.json().get("results", [])[:6]
                log.info(f"Maps '{term}' / {county}: {len(results)} result(s)")

                for place in results:
                    name     = place.get("name", "")
                    address  = place.get("formatted_address", "")
                    rating   = place.get("rating", None)
                    reviews  = place.get("user_ratings_total", 0)
                    city     = address.split(",")[1].strip() if "," in address else county
                    place_id = place.get("place_id", "")
                    url      = (f"https://maps.google.com/?cid={place_id}"
                                if place_id else
                                f"https://maps.google.com/?q={requests.utils.quote(name)}")

                    score  = 3
                    reason = f"Commercial target — {term}"
                    if rating and rating < 3.5 and reviews > 10:
                        score  = 4
                        reason += f"; Low-rated ({rating}★, {reviews} reviews) — dissatisfied client pool"

                    leads.append({
                        "date_found":        datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "source":            "Google Maps",
                        "name":              name,
                        "description":       f"{term} | {rating or 'N/A'}★ ({reviews} reviews) | {address}",
                        "county":            county,
                        "city":              city,
                        "service_type":      "General",
                        "lead_type":         "Commercial",
                        "motivation_score":  score,
                        "motivation_reason": reason,
                        "contact":           address,
                        "url":               url,
                    })
                time.sleep(0.5)
            except Exception as e:
                log.error(f"Maps error '{query}': {e}")
    return leads

# ── Source 2: Realtor.com Recently Sold ──────────────────────────────────────

def scrape_realtor_recently_sold() -> List[Dict]:
    leads = []
    for county, cities in COUNTY_CITIES.items():
        for city in cities:
            slug = city.replace(" ", "-")
            url  = f"https://www.realtor.com/realestateandhomes-search/{slug}_MI/show-recently-sold"
            try:
                r    = requests.get(url, headers=HEADERS, timeout=15)
                soup = BeautifulSoup(r.text, "html.parser")
                script = soup.find("script", {"id": "__NEXT_DATA__"})
                if not script:
                    log.warning(f"Realtor.com: no data found for {city}")
                    time.sleep(2)
                    continue

                data  = json.loads(script.string)
                props = (
                    data.get("props", {})
                        .get("pageProps", {})
                        .get("searchResults", {})
                        .get("home_search", {})
                        .get("results", [])
                )
                log.info(f"Realtor.com {city}: {len(props)} listing(s)")

                for prop in props[:8]:
                    addr     = prop.get("location", {}).get("address", {})
                    street   = addr.get("line", "")
                    city_val = addr.get("city", city)
                    price    = prop.get("list_price") or prop.get("last_sold_price") or 0
                    sold_dt  = prop.get("last_sold_date", "N/A")
                    beds     = prop.get("description", {}).get("beds", "N/A")
                    sqft     = prop.get("description", {}).get("sqft", "N/A")
                    prop_url = prop.get("permalink", "") or url
                    if prop_url and not prop_url.startswith("http"):
                        prop_url = "https://www.realtor.com" + prop_url

                    score  = 4
                    reason = "Recently sold — new homeowner, peak renovation window (first 90 days)"
                    if price and price > 400_000:
                        score  = 5
                        reason += f"; Premium home (${price:,}) — strong hardscape investment likelihood"

                    leads.append({
                        "date_found":        datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "source":            "Realtor.com Recently Sold",
                        "name":              f"{street}, {city_val}, MI",
                        "description":       f"Sold: {sold_dt} | Price: ${price:,} | Beds: {beds} | SqFt: {sqft}",
                        "county":            county,
                        "city":              city_val,
                        "service_type":      "Installation / General",
                        "lead_type":         "Residential",
                        "motivation_score":  score,
                        "motivation_reason": reason,
                        "contact":           "",
                        "url":               prop_url,
                    })
                time.sleep(2)
            except Exception as e:
                log.error(f"Realtor.com error '{city}': {e}")
    return leads

# ── Source 3: BuildZoom Permit Records ───────────────────────────────────────

def _permit_service_type(keyword: str) -> str:
    if keyword in ("paver", "brick paver", "hardscape"):
        return "Installation"
    if keyword == "driveway":
        return "Installation / Repair"
    if keyword == "retaining wall":
        return "Installation"
    return "General"


def scrape_permit_records() -> List[Dict]:
    leads = []
    for county in COUNTY_CITIES:
        for keyword in PERMIT_KEYWORDS:
            query = f"{keyword} {county} County Michigan"
            url   = f"https://www.buildzoom.com/permits?q={requests.utils.quote(query)}"
            try:
                r    = requests.get(url, headers=HEADERS, timeout=15)
                soup = BeautifulSoup(r.text, "html.parser")

                items = (
                    soup.select(".permit-item")
                    or soup.select("[data-permit]")
                    or soup.select("article.permit")
                    or soup.select("li.result")
                    or soup.select("div.result-card")
                )
                log.info(f"Permits '{keyword}' / {county}: {len(items)} result(s)")

                for item in items[:5]:
                    text     = item.get_text(separator=" ", strip=True)
                    addr_el  = item.select_one(".address, [data-address], .location")
                    address  = addr_el.get_text(strip=True) if addr_el else text[:80]

                    city_match = next(
                        (c for c in COUNTY_CITIES[county] if c.lower() in text.lower()),
                        county
                    )
                    item_url = url
                    link = item.select_one("a[href]")
                    if link:
                        href = link.get("href", "")
                        item_url = href if href.startswith("http") else "https://www.buildzoom.com" + href

                    leads.append({
                        "date_found":        datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "source":            "Permit Records",
                        "name":              address,
                        "description":       f"Permit keyword: {keyword} | {text[:250]}",
                        "county":            county,
                        "city":              city_match,
                        "service_type":      _permit_service_type(keyword),
                        "lead_type":         "Residential",
                        "motivation_score":  5,
                        "motivation_reason": "Active building permit — outdoor project in progress or imminent",
                        "contact":           "",
                        "url":               item_url,
                    })
                time.sleep(1.5)
            except Exception as e:
                log.error(f"Permit error '{keyword}' / {county}: {e}")
    return leads

# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    log.info("═" * 55)
    log.info("Paver Leads Bot v2 — starting run")
    log.info("═" * 55)

    ws = get_sheet()
    ensure_headers(ws)
    existing_urls = get_existing_urls(ws)

    all_leads: List[Dict] = []
    all_leads.extend(scrape_google_maps(os.environ.get("GOOGLE_MAPS_API_KEY", "")))
    all_leads.extend(scrape_realtor_recently_sold())
    all_leads.extend(scrape_permit_records())

    new_leads = [l for l in all_leads if l["url"] not in existing_urls]
    log.info(f"Total: {len(all_leads)} found | {len(new_leads)} new after dedup")

    append_leads(ws, new_leads)
    log.info("Run complete.")


if __name__ == "__main__":
    main()
