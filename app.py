import sqlite3
from flask import Flask, render_template, request, g, abort

DB_PATH = "db.sqlite3"

app = Flask(__name__)

# ---------- DB helpers ----------

def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row  # lets us access columns by name
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
