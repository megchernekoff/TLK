import sqlite3
import os
from flask import Flask, render_template, request, g, abort
from sync_recipes import sync_recipes
DB_PATH = "db.sqlite3"

app = Flask(__name__)




# ---------- DB helpers ----------

def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # Ensure the recipes table exists
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id TEXT,
                title TEXT,
                url TEXT,
                created_at TEXT
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
    q = request.args.get("q", "").strip()
    db = get_db()

    if q:
        like = f"%{q}%"
        rows = db.execute(
            """
            SELECT id, title, url, created_at
            FROM recipes
            WHERE title LIKE ? OR url LIKE ?
            ORDER BY datetime(created_at) DESC
            """,
            (like, like),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT id, title, url, created_at
            FROM recipes
            ORDER BY datetime(created_at) DESC
            """
        ).fetchall()

    return render_template("index.html", recipes=rows, q=q)


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


@app.route("/recipe/<int:recipe_id>")
def recipe_detail(recipe_id):
    db = get_db()
    row = db.execute(
        "SELECT id, title, url, created_at FROM recipes WHERE id = ?",
        (recipe_id,),
    ).fetchone()

    if row is None:
        abort(404)

    return render_template("recipe.html", recipe=row)



if __name__ == "__main__":
    app.run(debug=True)
