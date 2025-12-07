import base64
import sqlite3
import requests
import os 
import json

from datetime import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from bs4 import BeautifulSoup

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DB_PATH = "db.sqlite3"


# --------- DB helpers ---------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id TEXT,
            title TEXT,       -- recipe name from the web page
            url TEXT,
            created_at TEXT
        )
        """
    )
    return conn


# --------- Gmail helpers ---------

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def get_gmail_creds():
    """
    First try local token.json (for your laptop),
    otherwise use GMAIL_TOKEN_JSON env var (for Render).
    """
    # Local dev: use token.json file if present
 #   if os.path.exists("token.json"):
  #      return Credentials.from_authorized_user_file("token.json", SCOPES)

    # Render: read JSON from env var
    token_json = os.getenv("GMAIL_TOKEN_JSON")
    if token_json:
        info = json.loads(token_json)
        return Credentials.from_authorized_user_info(info, SCOPES)

    raise RuntimeError("No Gmail credentials found. Provide token.json or GMAIL_TOKEN_JSON.")


def get_gmail_service():
    creds = get_gmail_creds()
    service = build("gmail", "v1", credentials=creds)
    return service


def find_lost_kitchen_messages(service, max_results=20):
    # Use the exact email address that worked for you before:
    query = 'from:frontdesk@findthelostkitchen.com'  # <- replace this with your working from: value
    results = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results
    ).execute()
    return results.get("messages", [])

def get_message_metadata(service, msg_id):
    msg = service.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["Subject", "Date"]
    ).execute()

    headers = msg.get("payload", {}).get("headers", [])
    subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(no subject)")
    date = next((h["value"] for h in headers if h["name"] == "Date"), None)

    return subject, date

def get_message_html(service, msg_id):
    msg = service.users().messages().get(
        userId="me",
        id=msg_id,
        format="full"
    ).execute()

    payload = msg.get("payload", {})
    parts = payload.get("parts", [])

    # Try multipart first
    if parts:
        for part in parts:
            if part.get("mimeType") == "text/html":
                data = part["body"].get("data")
                if data:
                    html_bytes = base64.urlsafe_b64decode(data.encode("utf-8"))
                    return html_bytes.decode("utf-8")

    # Fallback: single-part
    body = payload.get("body", {})
    data = body.get("data")
    if data:
        html_bytes = base64.urlsafe_b64decode(data.encode("utf-8"))
        return html_bytes.decode("utf-8")

    return ""

# --------- Email HTML parsing ---------

from urllib.parse import urlparse
from bs4 import BeautifulSoup

def extract_recipe_links_from_email(html):
    soup = BeautifulSoup(html, "html.parser")
    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        parsed = urlparse(href)
        domain = parsed.netloc.lower()
        path = parsed.path.lower()
        text = a.get_text(" ", strip=True).lower()

        # Only TLK domains
        if not ("thelostkitchen" in domain or "findthelostkitchen" in domain):
            continue

        # Skip unsubscribe / account links
        if "unsubscribe" in path or "account" in path:
            continue

        # Heuristics:
        #   - path looks like a recipe URL
        #   - OR anchor text mentions recipe
        if ("recipe" in path or "recipes" in path) or ("recipe" in text) or ("very+good" in path):
            links.append(href)

    # de-dupe, preserving order
    seen = set()
    unique_links = []
    for href in links:
        if href not in seen:
            seen.add(href)
            unique_links.append(href)

    return unique_links


# --------- Recipe Parsing ---------

def fetch_recipe_title(url):
    """Fetch the recipe page and extract a nice title."""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"⚠️  Could not fetch {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # 1) Try og:title (often the cleanest)
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()

    # 2) Try the first <h1>
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)

    # 3) Fallback: <title> tag, clean it up a bit
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)
        # often something like "Recipe Name – The Lost Kitchen"
        for sep in ["|", "–", "-"]:
            if sep in title:
                title = title.split(sep)[0].strip()
                break
        return title

    return None



# --------- Main sync ---------

def sync_recipes():
    service = get_gmail_service()
    conn = get_db()
    cur = conn.cursor()

    messages = find_lost_kitchen_messages(service, max_results=50)

    if not messages:
        print("No Lost Kitchen messages found.")
        return

    print(f"Found {len(messages)} messages. Syncing...")

    for m in messages:
        email_id = m["id"]

        subject, date_header = get_message_metadata(service, email_id)
        html = get_message_html(service, email_id)
        urls = extract_recipe_links_from_email(html)

        if not urls:
            print(f"⚠️  No recipe URLs found for email: {subject}")
            continue

        for url in urls:
            # dedupe on email_id + url
            cur.execute(
                "SELECT 1 FROM recipes WHERE email_id = ? AND url = ?",
                (email_id, url),
            )
            if cur.fetchone():
                continue

            # NEW: fetch the recipe page to get the real title
            title = fetch_recipe_title(url)
            if not title:
                # fallback to email subject if page parsing fails
                title = subject

            created_at = datetime.utcnow().isoformat()

            cur.execute(
                """
                INSERT INTO recipes (email_id, title, url, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (email_id, title, url, created_at),
            )

            print(f"✅ Saved: {title} -> {url}")


    conn.commit()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    sync_recipes()
