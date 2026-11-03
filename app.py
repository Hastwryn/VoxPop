import os
import base64
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from flask import (
    Flask, redirect, request, session, url_for, render_template,
    jsonify, abort,
)

import rooms
import uuid

def get_visitor_id():
    """Stable per-browser ID so we can enforce one vote per person per song."""
    vid = session.get("visitor_id")
    if not vid:
        vid = uuid.uuid4().hex
        session["visitor_id"] = vid
    return vid

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY missing in .env")

# Cookie policy — required for OAuth redirects over plain HTTP on 127.0.0.1
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,   # True only when serving over HTTPS
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24,  # 1 day
)
CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:5000/callback")
SCOPE = "user-read-private user-read-email user-read-playback-state user-modify-playback-state streaming"

if not CLIENT_ID or not CLIENT_SECRET:
    raise RuntimeError("Missing SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET in .env")

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"


# ---------- Auth ----------

@app.route('/')
def home():
    return render_template('home.html')


@app.route('/login')
def login():
    role = request.args.get("role", "")
    room = request.args.get("room", "")
    state = f"{role}|{room}" if role and room else ""
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "show_dialog": "true",
        "state": state,
    }
    return redirect(f"{AUTH_URL}?{urlencode(params)}")


@app.route('/callback')
def callback():
    if request.args.get("error"):
        return f"Authorization failed: {request.args['error']}"

    code = request.args.get('code')
    auth_header = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
    )
    info = resp.json()
    access_token = info.get("access_token")
    refresh_token = info.get("refresh_token")
    expires_in = info.get("expires_in", 3600)

    # Also keep it in the admin's session (belt-and-braces)
    session['access_token'] = access_token
    session['refresh_token'] = refresh_token

    state = request.args.get("state", "")
    if "|" in state:
        role, room_code = state.split("|", 1)
        if role == "admin" and rooms.room_exists(room_code):
            # Attach the token to the ROOM so guests can use it too
            rooms.set_room_token(room_code, access_token, refresh_token, expires_in)
            return redirect(url_for('admin_view', code=room_code))
    return redirect(url_for('home'))

@app.route('/api/<code>/token')
def api_room_token(code):
    code = code.upper()
    if not rooms.room_exists(code):
        return jsonify({"error": "not found"}), 404
    token = rooms.get_room_token(code)
    if not token:
        # Try refresh
        refresh = rooms.get_room_refresh_token(code)
        if refresh:
            new = _refresh_room_token(code, refresh)
            if new:
                return jsonify({"access_token": new})
        return jsonify({"error": "host has not logged in"}), 401
    return jsonify({"access_token": token})


def _refresh_room_token(code, refresh_token):
    auth_header = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    if r.status_code != 200:
        return None
    info = r.json()
    rooms.set_room_token(code, info["access_token"],
                         info.get("refresh_token", refresh_token),
                         info.get("expires_in", 3600))
    return info["access_token"]

@app.route('/api/<code>/status')
def api_room_status(code):
    code = code.upper()
    if not rooms.room_exists(code):
        return jsonify({"error": "not found"}), 404
    room = rooms.get_room(code)
    logged_in = bool(rooms.get_room_token(code))
    return jsonify({
        "logged_in": logged_in,
        "queue_size": len(room["queue"]),
    })

@app.route('/get_token')
def get_token():
    token = session.get('access_token')
    if not token:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"access_token": token})


# ---------- Room creation / entry ----------

@app.route('/create', methods=['POST'])
def create_room():
    code, admin_token = rooms.create_room()
    # Admin token is stored in the admin's session — nobody else can use it
    session[f'admin_{code}'] = admin_token
    return redirect(url_for('admin_view', code=code))


@app.route('/join', methods=['POST'])
def join_room():
    code = (request.form.get('code') or "").strip().upper()
    if not rooms.room_exists(code):
        return render_template('home.html', error="That room code doesn't exist."), 404
    return redirect(url_for('room_view', code=code))


# ---------- Room views ----------

@app.route('/<code>')
def room_view(code):
    code = code.upper()
    if not rooms.room_exists(code):
        abort(404)
    return render_template('room.html', code=code)


@app.route('/<code>/admin')
def admin_view(code):
    code = code.upper()
    room = rooms.get_room(code)
    if not room:
        abort(404)
    # Only the session that created the room can access admin
    if session.get(f'admin_{code}') != room["admin_token"]:
        abort(403)
    return render_template('admin.html', code=code)


# ---------- Room API ----------

@app.route('/api/<code>/queue')
def api_queue(code):
    code = code.upper()
    if not rooms.room_exists(code):
        return jsonify({"error": "not found"}), 404
    return jsonify({"queue": rooms.sorted_queue(code, get_visitor_id())})


@app.route('/api/<code>/add', methods=['POST'])
def api_add(code):
    code = code.upper()
    if not rooms.room_exists(code):
        return jsonify({"error": "not found"}), 404
    data = request.get_json() or {}
    uri = data.get("uri")
    name = data.get("name")
    artist = data.get("artist")
    requester = data.get("requester") or "Anonymous"
    if not uri or not name:
        return jsonify({"error": "uri and name required"}), 400
    song = rooms.add_song(code, uri, name, artist, requester)
    return jsonify({"song": song})



@app.route('/api/<code>/vote', methods=['POST'])
def api_vote(code):
    code = code.upper()
    if not rooms.room_exists(code):
        return jsonify({"error": "not found"}), 404
    data = request.get_json() or {}
    song_id = data.get("id")
    direction = data.get("direction")
    if direction not in ("up", "down"):
        return jsonify({"error": "direction must be 'up' or 'down'"}), 400
    delta = 1 if direction == "up" else -1
    song = rooms.vote(code, song_id, delta, get_visitor_id())
    if not song:
        return jsonify({"error": "song not found"}), 404
    return jsonify({"song": song})

# ---------- Admin API ----------

def _require_admin(code):
    room = rooms.get_room(code)
    if not room:
        abort(404)
    if session.get(f'admin_{code}') != room["admin_token"]:
        abort(403)
    return room


@app.route('/api/<code>/admin/remove', methods=['POST'])
def api_admin_remove(code):
    code = code.upper()
    _require_admin(code)
    song_id = (request.get_json() or {}).get("id")
    ok = rooms.remove_song(code, song_id)
    return jsonify({"ok": ok})


@app.route('/api/<code>/admin/reorder', methods=['POST'])
def api_admin_reorder(code):
    """Accepts {'ids': [...]} and rewrites scores so the given order is the new ranking.

    Higher score = higher priority. We assign descending scores so the list
    of ids becomes the sorted result for the whole queue.
    """
    code = code.upper()
    _require_admin(code)
    ids = (request.get_json() or {}).get("ids", [])
    room = rooms.get_room(code)
    if not room:
        abort(404)
    total = len(room["queue"])
    # New score: first item gets highest
    id_to_score = {sid: (total - i) for i, sid in enumerate(ids)}
    for s in room["queue"]:
        if s["id"] in id_to_score:
            s["score"] = id_to_score[s["id"]]
    return jsonify({"ok": True})


@app.route('/api/<code>/admin/shutdown', methods=['POST'])
def api_admin_shutdown(code):
    code = code.upper()
    _require_admin(code)
    rooms.shut_down_room(code)
    return jsonify({"ok": True})


if __name__ == '__main__':
    app.run(port=5000, debug=True)