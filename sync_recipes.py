import base64
import sqlite3
import requests
import os
import json
import re
import html
from urllib.parse import urlparse, parse_qs, unquote, urljoin
from bs4 import BeautifulSoup
from typing import Optional, List, Tuple
from abc import ABC, abstractmethod
from datetime import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ============================================================================
# CONFIGURATION
# ============================================================================

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DB_PATH = "db.sqlite3"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36"
}

# ============================================================================
# DATABASE
# ============================================================================

def get_db():
    """Initialize database connection and create tables if needed."""
    conn = sqlite3.connect(DB_PATH)
    
    # Create table if it doesn't exist
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id TEXT,
            source TEXT,
            title TEXT,
            url TEXT,
            created_at TEXT,
            parent_url TEXT,
            homepage TEXT,
            UNIQUE(email_id, url)
        )
    """)
    
    # Add homepage column if it doesn't exist (for existing databases)
    try:
        conn.execute("ALTER TABLE recipes ADD COLUMN homepage TEXT")
        conn.commit()
        print("Added homepage column to existing database")
    except sqlite3.OperationalError:
        # Column already exists
        pass
    
    conn.commit()
    return conn


def save_recipe(conn, email_id, source, title, url, parent_url=None, homepage=None):
    """Save a recipe to the database."""
    title = html.unescape(title)
    
    # Debug: Show what we're about to save
    print(f"      DEBUG: Saving - Title: '{title}' | URL: {url[:60]}")
    
    try:
        cursor = conn.execute("""
            INSERT OR IGNORE INTO recipes
              (email_id, source, title, url, parent_url, homepage, created_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """, (email_id, source, title, url, parent_url, homepage))
        
        if cursor.rowcount == 0:
            print(f"      DEBUG: Recipe already exists in DB (duplicate)")
        
        conn.commit()
    except Exception as e:
        print(f"      ERROR saving recipe: {e}")


# ============================================================================
# GMAIL API
# ============================================================================

def get_gmail_creds():
    """Get Gmail credentials from token.json or environment variable."""
    creds = None
    
    # Local dev: use token.json file if present
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    else:
        # Render: read JSON from env var
        token_json = os.getenv("GMAIL_TOKEN_JSON")
        if token_json:
            info = json.loads(token_json)
            creds = Credentials.from_authorized_user_info(info, SCOPES)
    
    if not creds:
        raise RuntimeError("No Gmail credentials found. Provide token.json or GMAIL_TOKEN_JSON.")
    
    # Refresh token if expired
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        
        # Save refreshed credentials back to token.json if using local file
        if os.path.exists("token.json"):
            with open("token.json", "w") as token:
                token.write(creds.to_json())
    
    return creds


def get_gmail_service():
    """Build and return Gmail API service."""
    creds = get_gmail_creds()
    return build("gmail", "v1", credentials=creds)


def find_recipe_messages(service, max_results=50):
    """Find recipe emails using Gmail search."""
    query = '(skinnytaste OR "The Lost Kitchen")'
    results = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results
    ).execute()
    return results.get("messages", [])


def get_message_metadata(service, msg_id):
    """Extract subject and date from email."""
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
    """Extract HTML content from email."""
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


# ============================================================================
# UTILITIES
# ============================================================================

def unwrap_redirect(href: str) -> str:
    """Unwrap Gmail redirect URLs."""
    try:
        p = urlparse(href)
        if "google.com" in (p.netloc or "") and p.path == "/url":
            q = parse_qs(p.query).get("q", [None])[0]
            if q:
                return unquote(q)
    except Exception:
        pass
    return href


def extract_homepage_from_url(url: str) -> str:
    """Extract the homepage URL from any recipe URL."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def fetch_page_html(url: str) -> Optional[str]:
    """Fetch HTML content from a URL."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"⚠️  Could not fetch {url}: {e}")
        return None


def fetch_recipe_title(url: str) -> Optional[str]:
    """Extract title from a recipe page."""
    html = fetch_page_html(url)
    if not html:
        return None
    
    soup = BeautifulSoup(html, "html.parser")
    
    # Try og:title (often the cleanest)
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    
    # Try the first <h1>
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    
    # Fallback: <title> tag
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)
        for sep in ["|", "—", "-"]:
            if sep in title:
                title = title.split(sep)[0].strip()
                break
        return html.unescape(title)
    
    return None


def extract_title_from_url(url: str) -> str:
    """
    Extract a readable title from the URL path as a fallback.
    Example: /chicken-tacos-recipe/ -> Chicken Tacos Recipe
    """
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    
    # Get the last segment of the path
    if '/' in path:
        path = path.split('/')[-1]
    
    # Remove common file extensions
    path = path.replace('.html', '').replace('.htm', '')
    
    # Replace hyphens/underscores with spaces and title case
    title = path.replace('-', ' ').replace('_', ' ')
    title = ' '.join(word.capitalize() for word in title.split())
    
    return title


# ============================================================================
# PROVIDER SYSTEM
# ============================================================================

class RecipeProvider(ABC):
    """Base class for recipe providers."""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (e.g., 'skinnytaste')."""
        pass
    
    @property
    @abstractmethod
    def domains(self) -> List[str]:
        """List of domains this provider uses."""
        pass
    
    def matches_domain(self, url: str) -> bool:
        """Check if URL matches this provider's domains."""
        url_lower = url.lower()
        return any(domain in url_lower for domain in self.domains)
    
    @abstractmethod
    def extract_links_from_email(self, email_html: str) -> List[str]:
        """
        Extract recipe-related URLs from email HTML.
        Returns list of URLs that might be landing pages or direct recipe links.
        """
        pass
    
    @abstractmethod
    def resolve_to_recipe_pages(self, url: str) -> List[Tuple[str, str]]:
        """
        Given a URL from the email, resolve it to actual recipe page(s).
        Returns list of (parent_url, recipe_url) tuples.
        
        - If URL is a landing page with multiple recipes, return multiple tuples
        - If URL is already a recipe page, return [(url, url)]
        """
        pass


class SkinnytasteProvider(RecipeProvider):
    """Provider for Skinnytaste recipes."""
    
    @property
    def name(self) -> str:
        return "skinnytaste"
    
    @property
    def domains(self) -> List[str]:
        return ["skinnytaste.us", "skinnytaste.com"]
    
    def extract_links_from_email(self, email_html: str) -> List[str]:
        """
        Skinnytaste emails contain 'GET THE RECIPE' buttons that link through MailChimp tracking.
        We need to follow the redirect to get the clean recipe URL.
        """
        soup = BeautifulSoup(email_html, "html.parser")
        links = []
        
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True).lower()
            href = unwrap_redirect(a["href"])
            
            # Verify it's a Skinnytaste URL (including MailChimp tracking domain)
            if not self.matches_domain(href):
                continue
            
            # Skip preferences/account links
            parsed = urlparse(href)
            path = (parsed.path or "").lower()
            if "preferences" in path or "unsubscribe" in path or "account" in path:
                continue
            
            # Look for "GET THE RECIPE" or similar text
            if any(pattern in text for pattern in [
                "get the recipe",
                "get recipe",
                "view recipe",
                "read more"
            ]):
                # Follow the redirect to get the clean URL
                clean_url = self._follow_redirect(href)
                if clean_url:
                    links.append(clean_url)
        
        # Deduplicate while preserving order
        seen = set()
        unique_links = []
        for link in links:
            if link not in seen:
                seen.add(link)
                unique_links.append(link)
        
        return unique_links
    
    def _follow_redirect(self, url: str) -> Optional[str]:
        """
        Follow MailChimp tracking URL to get the clean final URL.
        Also removes tracking query parameters.
        """
        try:
            # Follow redirects but don't actually fetch the page content
            resp = requests.head(url, headers=HEADERS, allow_redirects=True, timeout=10)
            final_url = resp.url
            
            # Remove tracking parameters
            parsed = urlparse(final_url)
            # Keep only the base URL without query params or fragments
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            
            # Remove trailing slash for consistency
            clean_url = clean_url.rstrip('/')
            
            return clean_url
        except Exception as e:
            print(f"   ⚠️  Could not follow redirect for {url[:80]}: {e}")
            return None
    
    def resolve_to_recipe_pages(self, url: str) -> List[Tuple[str, str]]:
        """
        Skinnytaste emails typically link directly to recipe pages.
        Just return the URL as-is.
        """
        return [(url, url)]


class TheLostKitchenProvider(RecipeProvider):
    """Provider for The Lost Kitchen recipes."""
    
    @property
    def name(self) -> str:
        return "the_lost_kitchen"
    
    @property
    def domains(self) -> List[str]:
        return ["thelostkitchen", "findthelostkitchen"]
    
    def extract_links_from_email(self, email_html: str) -> List[str]:
        """
        The Lost Kitchen emails contain links to landing pages or recipe pages.
        """
        soup = BeautifulSoup(email_html, "html.parser")
        links = []
        
        for a in soup.find_all("a", href=True):
            href = unwrap_redirect(a["href"])
            
            if not self.matches_domain(href):
                continue
            
            parsed = urlparse(href)
            path = (parsed.path or "").lower()
            text = a.get_text(" ", strip=True).lower()
            
            # Skip unsubscribe/account links
            if "unsubscribe" in path or "account" in path:
                continue
            
            # Include recipe-related links
            if "recipe" in path or "recipes" in path or "recipe" in text or "very+good" in path:
                links.append(href)
        
        # Deduplicate
        seen = set()
        unique_links = []
        for link in links:
            if link not in seen:
                seen.add(link)
                unique_links.append(link)
        
        return unique_links
    
    def resolve_to_recipe_pages(self, url: str) -> List[Tuple[str, str]]:
        """
        The Lost Kitchen URLs might be landing pages with multiple recipes.
        Check if page contains multiple 'Get the Recipe' links.
        """
        html = fetch_page_html(url)
        if not html:
            # If we can't fetch it, assume it's a direct recipe link
            return [(url, url)]
        
        soup = BeautifulSoup(html, "html.parser")
        recipe_links = []
        
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True).lower()
            
            # Skip footer links
            if "everyday shop" in text:
                continue
            
            # Look for "Get the Recipe" buttons
            if "get" in text and "recipe" in text:
                href = urljoin(url, a["href"])
                recipe_links.append(href)
        
        # Deduplicate
        seen = set()
        unique_recipe_links = []
        for link in recipe_links:
            if link not in seen:
                seen.add(link)
                unique_recipe_links.append(link)
        
        # If we found multiple recipe links, it's a landing page
        if len(unique_recipe_links) >= 2:
            return [(url, recipe_url) for recipe_url in unique_recipe_links]
        else:
            # Single recipe or couldn't find any - treat URL as recipe page
            return [(url, url)]


# ============================================================================
# PROVIDER REGISTRY
# ============================================================================

class ProviderRegistry:
    """Manages all recipe providers."""
    
    def __init__(self):
        self.providers: List[RecipeProvider] = []
    
    def register(self, provider: RecipeProvider):
        """Register a new provider."""
        self.providers.append(provider)
    
    def get_provider_for_url(self, url: str) -> Optional[RecipeProvider]:
        """Find the provider that matches a given URL."""
        for provider in self.providers:
            if provider.matches_domain(url):
                return provider
        return None
    
    def extract_all_recipes_from_email(self, email_html: str) -> List[Tuple[str, str, str]]:
        """
        Extract all recipe links from an email using all registered providers.
        Returns list of (provider_name, parent_url, recipe_url) tuples.
        """
        results = []
        
        for provider in self.providers:
            # Get candidate URLs from email
            email_links = provider.extract_links_from_email(email_html)
            
            # Resolve each URL to actual recipe page(s)
            for email_link in email_links:
                resolved = provider.resolve_to_recipe_pages(email_link)
                for parent_url, recipe_url in resolved:
                    results.append((provider.name, parent_url, recipe_url))
        
        # Deduplicate by recipe_url
        seen = set()
        unique_results = []
        for provider_name, parent_url, recipe_url in results:
            if recipe_url not in seen:
                seen.add(recipe_url)
                unique_results.append((provider_name, parent_url, recipe_url))
        
        return unique_results


# Initialize global registry
registry = ProviderRegistry()
registry.register(SkinnytasteProvider())
registry.register(TheLostKitchenProvider())


# ============================================================================
# MAIN SYNC
# ============================================================================

def sync_recipes():
    """Main sync function - fetch emails and extract recipes."""
    service = get_gmail_service()
    conn = get_db()
    
    messages = find_recipe_messages(service, max_results=50)
    
    if not messages:
        print("No messages found.")
        return
    
    print(f"Found {len(messages)} messages. Syncing...")
    
    for m in messages:
        email_id = m["id"]
        subject, date_header = get_message_metadata(service, email_id)
        print(f"\n📧 Processing: {subject}")
        
        email_html = get_message_html(service, email_id)
        
        # Extract all recipes from this email
        recipes = registry.extract_all_recipes_from_email(email_html)
        
        if not recipes:
            print(f"   ⚠️  No recipe URLs found")
            continue
        
        print(f"   Found {len(recipes)} recipe(s)")
        
        for provider_name, parent_url, recipe_url in recipes:
            # For multi-recipe emails, prefer URL-based titles to avoid duplicates
            if len(recipes) > 1:
                # Try to fetch from page first
                title = fetch_recipe_title(recipe_url)
                # If blocked, extract from URL (better than duplicate email subject)
                if not title:
                    title = extract_title_from_url(recipe_url)
            else:
                # For single recipe emails, try these in order
                title = fetch_recipe_title(recipe_url)
                if not title:
                    title = extract_title_from_url(recipe_url)
                if not title:
                    title = subject
            
            # Final fallback
            if not title:
                title = subject
            
            # Extract homepage from recipe URL
            homepage = extract_homepage_from_url(recipe_url)
            
            print(f"   ✓ {title}")
            
            save_recipe(
                conn=conn,
                email_id=email_id,
                source=provider_name,
                title=title,
                url=recipe_url,
                parent_url=parent_url if parent_url != recipe_url else None,
                homepage=homepage
            )
    
    conn.close()
    print("\n✅ Sync complete.")


if __name__ == "__main__":
    sync_recipes()