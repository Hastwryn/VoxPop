import random
import string
import time

_rooms = {}


def generate_code(length=6):
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(alphabet, k=length))
        if code not in _rooms:
            return code


def create_room():
    code = generate_code()
    _rooms[code] = {
        "created_at": time.time(),
        "admin_token": generate_code(16),
        "queue": [],
        "next_id": 1,
        "shut_down": False,
        # Room-level Spotify auth, shared by everyone in the room
        "access_token": None,
        "refresh_token": None,
        "token_expires_at": 0,   # unix timestamp
        # song_id -> { visitor_id: +1 | -1 }
        "votes": {},
    }
    return code, _rooms[code]["admin_token"]


def get_room(code):
    return _rooms.get(code)


def room_exists(code):
    return code in _rooms and not _rooms[code]["shut_down"]


def shut_down_room(code):
    if code in _rooms:
        _rooms[code]["shut_down"] = True


def set_room_token(code, access_token, refresh_token, expires_in):
    room = _rooms.get(code)
    if not room:
        return False
    room["access_token"] = access_token
    room["refresh_token"] = refresh_token
    room["token_expires_at"] = time.time() + int(expires_in) - 30
    return True


def get_room_token(code):
    """Return a valid access token for the room, or None."""
    room = _rooms.get(code)
    if not room:
        return None
    if not room["access_token"]:
        return None
    if time.time() >= room["token_expires_at"]:
        return None  # caller should trigger a refresh
    return room["access_token"]


def get_room_refresh_token(code):
    room = _rooms.get(code)
    return room.get("refresh_token") if room else None


def add_song(code, uri, name, artist, requester):
    room = _rooms.get(code)
    if not room or room["shut_down"]:
        return None
    song = {
        "id": room["next_id"],
        "uri": uri,
        "name": name,
        "artist": artist,
        "requester": requester or "Anonymous",
        "score": 0,
        "added_at": time.time(),
    }
    room["next_id"] += 1
    room["queue"].append(song)
    room["votes"][song["id"]] = {}
    return song


def sorted_queue(code, visitor_id=None):
    room = _rooms.get(code)
    if not room:
        return []
    out = []
    for s in sorted(room["queue"], key=lambda s: (-s["score"], s["added_at"])):
        s2 = dict(s)  # copy so we don't leak internal state
        s2["my_vote"] = (
            room["votes"].get(s["id"], {}).get(visitor_id, 0)
            if visitor_id else 0
        )
        out.append(s2)
    return out


def vote(code, song_id, delta, visitor_id):
    """Set/replace/remove a visitor's vote on a song.

    Rules:
      - Same direction as current -> remove (toggle off)
      - Different direction        -> replace
      - No existing vote           -> add
    """
    room = _rooms.get(code)
    if not room or not visitor_id:
        return None
    song = next((s for s in room["queue"] if s["id"] == song_id), None)
    if not song:
        return None

    votes = room["votes"].setdefault(song_id, {})
    prev = votes.get(visitor_id, 0)

    if prev == delta:
        votes.pop(visitor_id, None)
        new_vote = 0
    else:
        votes[visitor_id] = delta
        new_vote = delta

    # Recompute score from scratch so it can never drift
    song["score"] = sum(votes.values())

    result = dict(song)
    result["my_vote"] = new_vote
    return result


def remove_song(code, song_id):
    room = _rooms.get(code)
    if not room:
        return False
    before = len(room["queue"])
    room["queue"] = [s for s in room["queue"] if s["id"] != song_id]
    room["votes"].pop(song_id, None)
    return len(room["queue"]) < before


def pop_next(code):
    """Called by the admin player when a track finishes."""
    room = _rooms.get(code)
    if not room or not room["queue"]:
        return None
    ordered = sorted(room["queue"], key=lambda s: (-s["score"], s["added_at"]))
    return ordered[0]


def reorder_manual(code, ordered_ids):
    """Admin manual override: store a manual_order list of ids."""
    room = _rooms.get(code)
    if not room:
        return False
    room["manual_order"] = ordered_ids
    return True