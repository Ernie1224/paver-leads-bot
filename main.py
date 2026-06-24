#!/usr/bin/env python3
"""
Paver Leads Bot
Automated lead generation for hardscape/paver services.
Covers: Oakland, Macomb, Lapeer, Livingston, St. Clair counties (Michigan)
Outputs to Google Sheets.
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
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

KEYWORDS = [
    "paver", "pavers", "brick patio", "brick driveway",
    "patio pavers", "driveway pavers", "hardscape", "hardscaping",
    "paver sealing", "paver repair", "power wash", "pressure wash",
    "paver cleaning", "paver restoration", "retaining wall",
]

CLEANING_KW  = ["power wash", "pressure wash", "sealing", "cleaning", "restoration", "seal"]
REPAIR_KW    = ["repair", "fix", "broken", "cracked", "damaged", "replace", "sinking", "settling"]
INSTALL_KW   = ["install", "new", "build", "patio", "driveway", "walkway", "hardscape"]
URGENCY_KW   = ["asap", "urgent", "soon", "immediately", "this week", "right away", "quickly", "today"]
BUDGET_KW    = ["budget", "price", "quote", "estimate", "cost", "affordable", "reasonable", "how much"]
VAGUE_KW     = ["maybe", "thinking about", "considering", "someday", "eventually", "not sure"]
COMMERCIAL_KW = ["business", "commercial", "property management", "hoa", "association", "complex", "plaza", "center"]

COUNTY_CITIES = {
    "Oakland":    ["Pontiac", "Troy", "Birmingham", "Bloomfield", "Farmington Hills",
                   "Auburn Hills", "Rochester", "Clarkston", "Waterford", "Novi", "Southfield"],
    "Macomb":     ["Warren", "Sterling Heights", "St. Clair Shores", "Roseville",
                   "Clinton Township", "Chesterfield", "Shelby Township", "Utica", "Fraser"],
    "Lapeer":     ["Lapeer", "Imlay City", "Almont", "Metamora", "Attica"],
    "Livingston": ["Howell", "Brighton", "Hartland", "Pinckney", "Fowlerville", "Fenton"],
    "St. Clair":  ["Port Huron", "Marysville", "St. Clair", "Marine City", "Algonac", "Kimball"],
}

CL_BASE = "https://detroit.craigslist.org"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
log = logging.getLogger(__name__)

# ── Google Sheets helpers ────────────────────────────────────────────────────

SHEET_HEADERS = [
    "Date Found", "Source", "Title / Name", "Description",
    "County", "City", "Service Type", "Lead Type",
    "Motivation Score", "Motivation Reason", "Contact Info",
    "URL", "Status", "Notes",
]

def get_sheet():
    creds_json = os.environ["SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_json)
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
        return set(ws.col_values(12)[1:])   # column L = URL
    except Exception:
        return set()


def append_leads(ws, leads: List[Dict]):
    if not leads:
        log.info("No new leads to append.")
        return
    rows = [
        [
            l["date_found"], l["source"], l["name"], l["description"],
            l["county"], l["city"], l["service_type"], l["lead_type"],
            l["motivation_score"], l["motivation_reason"], l["contact"],
            l["url"], "New", "",
        ]
        for l in leads
    ]
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    log.info(f"Appended {len(rows)} new lead(s) to sheet.")

# ── Scoring & classification ─────────────────────────────────────────────────

def detect_service_type(text: str) -> str:
    t = text.lower()
    types = []
    if any(k in t for k in CLEANING_KW):
        types.append("Cleaning/Sealing")
    if any(k in t for k in REPAIR_KW):
        types.append("Repair")
    if any(k in t for k in INSTALL_KW):
        types.append("Installation")
    return " + ".join(types) or "General"


def detect_county_city(text: str):
    t = text.lower()
    for county, cities in COUNTY_CITIES.items():
        for city in cities:
            if city.lower() in t:
                return county, city
    return "Unknown", "Unknown"


def score_lead(text: str, source: str = "Craigslist"):
    t = text.lower()
    score = 3
    reasons = [f"Active {source} post (high intent)"]

    if any(k in t for k in URGENCY_KW):
        score = min(5, score + 1)
        reasons.append("Urgency language")
    if any(k in t for k in BUDGET_KW):
        score = min(5, score + 1)
        reasons.append("Price/quote inquiry — ready to buy")
    if any(k in t for k in VAGUE_KW):
        score = max(1, score - 1)
        reasons.append("Vague/exploratory language")

    return score, "; ".join(reasons)

# ── Craigslist scraper ───────────────────────────────────────────────────────

def fetch_cl_detail(url: str) -> Dict:
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        body = soup.select_one("#postingbody")
        desc = body.get_text(strip=True) if body else ""
        desc = re.sub(r"QR Code Link to This Post\s*", "", desc).strip()
        phone = re.search(r"\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}", desc)
        contact = phone.group() if phone else ""
        return {"description": desc, "contact": contact}
    except Exception as e:
        log.warning(f"  Detail fetch failed ({url}): {e}")
        return {"description": "", "contact": ""}


def scrape_craigslist() -> List[Dict]:
    leads = []
    seen_urls = set()

    for keyword in KEYWORDS:
        url = f"{CL_BASE}/search/swa?query={keyword.replace(' ', '+')}&sort=date"
        try:
            r = requests.get(url, headers=HEADERS, timeout=12)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            results = soup.select("li.cl-static-search-result")
            log.info(f"CL '{keyword}': {len(results)} result(s)")

            for item in results:
                try:
                    title_el = item.select_one(".title")
                    link_el  = item.select_one("a")
                    if not title_el or not link_el:
                        continue

                    title    = title_el.get_text(strip=True)
                    post_url = link_el.get("href", "")
                    if not post_url.startswith("http"):
                        post_url = CL_BASE + post_url

                    if post_url in seen_urls:
                        continue
                    seen_urls.add(post_url)

                    time.sleep(1.5)
                    detail   = fetch_cl_detail(post_url)
                    full     = title + " " + detail["description"]
                    county, city = detect_county_city(full)
                    svc_type = detect_service_type(full)
                    score, reason = score_lead(full)

                    if score < 3:
                        continue

                    leads.append({
                        "date_found":       datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "source":           "Craigslist",
                        "name":             title,
                        "description":      detail["description"][:350],
                        "county":           county,
                        "city":             city,
                        "service_type":     svc_type,
                        "lead_type":        "Commercial" if any(k in full.lower() for k in COMMERCIAL_KW) else "Residential",
                        "motivation_score": score,
                        "motivation_reason": reason,
                        "contact":          detail["contact"],
                        "url":              post_url,
                    })

                except Exception as e:
                    log.warning(f"  Item parse error: {e}")

            time.sleep(2)

        except Exception as e:
            log.error(f"CL search '{keyword}' failed: {e}")

    return leads

# ── Google Maps Places scraper ───────────────────────────────────────────────

def scrape_google_maps(api_key: str) -> List[Dict]:
    if not api_key:
        log.info("GOOGLE_MAPS_API_KEY not set — skipping Places search.")
        return []

    leads = []
    search_terms = [
        "property management company",
        "HOA management Michigan",
        "commercial property owner",
        "real estate investment company",
    ]

    for county in COUNTY_CITIES:
        for term in search_terms:
            query = f"{term} {county} County Michigan"
            try:
                r = requests.get(
                    "https://maps.googleapis.com/maps/api/place/textsearch/json",
                    params={"query": query, "key": api_key},
                    timeout=12,
                )
                data = r.json()
                for place in data.get("results", [])[:5]:
                    name    = place.get("name", "")
                    address = place.get("formatted_address", "")
                    rating  = place.get("rating", 0)
                    reviews = place.get("user_ratings_total", 0)

                    score  = 3
                    reason = "Commercial property/management target"
                    if rating and rating < 3.5 and reviews > 10:
                        score  = 4
                        reason = f"Low-rated ({rating}★, {reviews} reviews) — dissatisfied customer pool"

                    city = address.split(",")[1].strip() if address.count(",") >= 1 else ""
                    leads.append({
                        "date_found":       datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "source":           "Google Maps",
                        "name":             name,
                        "description":      f"{term} | Rating: {rating}/5 ({reviews} reviews)",
                        "county":           county,
                        "city":             city,
                        "service_type":     "General",
                        "lead_type":        "Commercial",
                        "motivation_score": score,
                        "motivation_reason": reason,
                        "contact":          address,
                        "url":              f"https://maps.google.com/?q={requests.utils.quote(name + ' ' + county + ' Michigan')}",
                    })
                time.sleep(1)

            except Exception as e:
                log.error(f"Maps '{query}' failed: {e}")

    return leads

# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    log.info("═" * 55)
    log.info("Paver Leads Bot — starting run")
    log.info("═" * 55)

    ws = get_sheet()
    ensure_headers(ws)
    existing_urls = get_existing_urls(ws)

    all_leads = []
    all_leads.extend(scrape_craigslist())
    all_leads.extend(scrape_google_maps(os.environ.get("GOOGLE_MAPS_API_KEY", "")))

    new_leads = [l for l in all_leads if l["url"] not in existing_urls]
    log.info(f"Total leads found: {len(all_leads)} | New (deduped): {len(new_leads)}")

    append_leads(ws, new_leads)
    log.info("Run complete.")


if __name__ == "__main__":
    main()
