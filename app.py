import sqlite3
import os
import random
from flask import Flask, render_template, request, g, abort, redirect, url_for, flash
from sync_recipes import (
    registry, 
    fetch_recipe_title, 
    extract_title_from_url,
    extract_homepage_from_url,
    sync_recipes,
    DB_PATH
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-in-production")

# ---------- Helper Functions ----------
def prettify_source(source):
    """Convert snake_case source names to Title Case for display."""
    if not source:
        return source
    # Replace underscores with spaces and title case each word
    return source.replace('_', ' ').title()

# Make it available in templates
@app.context_processor
def utility_processor():
    return dict(prettify_source=prettify_source)

# ---------- DB helpers ----------
def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id TEXT,
                source TEXT,
                title TEXT,
                url TEXT,
                created_at TEXT,
                parent_url TEXT,
                section_anchor TEXT,
                provider_page_title TEXT,
                homepage TEXT,
                UNIQUE(email_id, url)
            )
            """
        )
        g.db = conn
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop("db", None)
    if db is not None:
        db.close()

# ---------- Routes ----------
@app.route("/")
def index():
    db = get_db()
    # Distinct sources for dropdown
    sources = [r[0] for r in db.execute(
        "SELECT DISTINCT source FROM recipes WHERE source IS NOT NULL AND source <> '' ORDER BY source"
    ).fetchall()]
    
    # Optional: featured recipe
    featured = db.execute(
        """
        SELECT id, title, url, source
        FROM recipes
        ORDER BY RANDOM()
        LIMIT 1
        """
    ).fetchone()
    
    return render_template("index.html", sources=sources, featured=featured)

@app.route("/source/<source>")
def source_page(source):
    q = request.args.get("q", "").strip()
    db = get_db()
    
    # Get the homepage for this source (from any recipe)
    homepage_row = db.execute(
        "SELECT homepage FROM recipes WHERE source = ? AND homepage IS NOT NULL LIMIT 1",
        (source,)
    ).fetchone()
    homepage = homepage_row["homepage"] if homepage_row else None
    
    if q:
        rows = db.execute(
            """
            SELECT id, title, url, source
            FROM recipes
            WHERE source = ?
              AND (title LIKE ? OR url LIKE ?)
            ORDER BY id DESC
            """,
            (source, f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT id, title, url, source
            FROM recipes
            WHERE source = ?
            ORDER BY id DESC
            """,
            (source,),
        ).fetchall()
    
    return render_template("source.html", source=source, recipes=rows, q=q, homepage=homepage)

@app.route("/recipe/<int:recipe_id>")
def recipe_detail(recipe_id):
    db = get_db()
    row = db.execute(
        "SELECT id, title, url, created_at, source FROM recipes WHERE id = ?",
        (recipe_id,),
    ).fetchone()
    
    if row is None:
        abort(404)
    
    return render_template("recipe.html", recipe=row)

# ---------- Import Feature ----------
@app.route("/import", methods=["GET", "POST"])
def import_recipe():
    # Get existing sources for the dropdown
    db = get_db()
    existing_sources = [r[0] for r in db.execute(
        "SELECT DISTINCT source FROM recipes WHERE source IS NOT NULL AND source <> '' ORDER BY source"
    ).fetchall()]
    
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        custom_source = request.form.get("custom_source", "").strip()
        
        if not url:
            flash("Please enter a URL", "error")
            return render_template("import.html", existing_sources=existing_sources, url=url, custom_source=custom_source)
        
        # Validate URL
        if not url.startswith(("http://", "https://")):
            flash("Invalid URL - must start with http:// or https://", "error")
            return render_template("import.html", existing_sources=existing_sources, url=url, custom_source=custom_source)
        
        # Try to detect provider
        provider = registry.get_provider_for_url(url)
        
        if provider:
            source = provider.name
        elif custom_source:
            # Use custom source if provided
            source = custom_source.lower().replace(" ", "_")
        else:
            flash("Could not detect recipe source. Please provide a custom source name.", "error")
            return render_template("import.html", existing_sources=existing_sources, url=url, custom_source=custom_source)
        
        # Try to fetch title from the page
        title = fetch_recipe_title(url)
        
        # If that fails, extract from URL
        if not title:
            title = extract_title_from_url(url)
        
        # If still no title, use a generic one
        if not title:
            title = "Imported Recipe"
        
        # Extract homepage from URL
        homepage = extract_homepage_from_url(url)
        
        # Save to database
        try:
            db.execute(
                """
                INSERT INTO recipes (email_id, source, title, url, homepage, created_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                """,
                ("manual_import", source, title, url, homepage)
            )
            db.commit()
            flash(f"Successfully imported: {title}", "success")
            return redirect(url_for("source_page", source=source))
        except sqlite3.IntegrityError:
            flash("This recipe URL already exists in the database", "error")
            return render_template("import.html", existing_sources=existing_sources, url=url, custom_source=custom_source)
        except Exception as e:
            flash(f"Error saving recipe: {str(e)}", "error")
            return render_template("import.html", existing_sources=existing_sources, url=url, custom_source=custom_source)
    
    # GET request - show the form
    return render_template("import.html", existing_sources=existing_sources, url="", custom_source="")

# ---------- Internal Routes ----------
@app.route("/internal/test")
def internal_test():
    return "test OK\n"

@app.route("/internal/sync")
def internal_sync():
    # Simple shared-secret check so randos can't trigger it
    expected = os.getenv("SYNC_SECRET")
    provided = request.args.get("secret")
    
    if not expected or provided != expected:
        abort(403)
    
    # Run the sync against the same DB the web app uses
    sync_recipes()
    return "OK\n"

if __name__ == "__main__":
    app.run(debug=True)