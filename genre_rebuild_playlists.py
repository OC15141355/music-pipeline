#!/usr/bin/env python3
"""
Rebuild genre playlists in Jellyfin.
For each of the 92 genre playlists, clear all items and re-add all tracks
that match the genre from Jellyfin's metadata.

Run from core-01 (needs curl access to Jellyfin):
    python3 ~/music-pipeline/genre_rebuild_playlists.py
"""

import json
import os
import subprocess
import sys
import time
import logging
from urllib.parse import quote

JF_URL = "https://192.168.0.30"
JF_KEY = os.environ.get("JELLYFIN_API_KEY", "YOUR_JELLYFIN_API_KEY")
JF_HOST = "jellyfin.homelab"

# Admin user ID (needed for playlist operations)
ADMIN_USER_ID = None  # Will be auto-detected

LOG_FILE = None  # Will use stdout

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)


def jf_request(method, path, params=None, body=None):
    """Make a Jellyfin API request via curl."""
    if params is None:
        params = {}
    params["api_key"] = JF_KEY
    param_str = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    url = f"{JF_URL}{path}?{param_str}"

    cmd = ["curl", "-sk", "-X", method, url, "-H", f"Host: {JF_HOST}"]
    if body is not None:
        cmd.extend(["-H", "Content-Type: application/json", "-d", json.dumps(body)])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.stdout.strip():
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout
    return None


def get_admin_user_id():
    """Get the admin user ID."""
    users = jf_request("GET", "/Users")
    if users:
        for user in users:
            if user.get("Policy", {}).get("IsAdministrator"):
                return user["Id"]
        # Fallback to first user
        return users[0]["Id"]
    return None


def get_all_playlists():
    """Get all playlists with their IDs."""
    data = jf_request("GET", "/Items", {
        "IncludeItemTypes": "Playlist",
        "Recursive": "true",
        "Limit": "200"
    })
    playlists = {}
    if data and "Items" in data:
        for p in data["Items"]:
            playlists[p["Name"]] = p["Id"]
    return playlists


def get_playlist_items(playlist_id):
    """Get all item IDs in a playlist."""
    items = []
    start = 0
    batch = 1000
    while True:
        data = jf_request("GET", f"/Playlists/{playlist_id}/Items", {
            "Limit": str(batch),
            "StartIndex": str(start),
            "UserId": ADMIN_USER_ID
        })
        if not data or "Items" not in data:
            break
        for item in data["Items"]:
            items.append(item["Id"])
        if len(data["Items"]) < batch:
            break
        start += batch
    return items


def clear_playlist(playlist_id):
    """Remove all items from a playlist."""
    items = get_playlist_items(playlist_id)
    if not items:
        return 0

    # Remove in batches
    batch_size = 200
    removed = 0
    for i in range(0, len(items), batch_size):
        batch = items[i:i+batch_size]
        entry_ids = ",".join(batch)
        jf_request("DELETE", f"/Playlists/{playlist_id}/Items", {
            "EntryIds": entry_ids
        })
        removed += len(batch)

    return removed


def get_tracks_by_genre(genre):
    """Get all track IDs that have a specific genre."""
    track_ids = []
    start = 0
    batch = 5000
    while True:
        data = jf_request("GET", "/Items", {
            "IncludeItemTypes": "Audio",
            "Recursive": "true",
            "Genres": genre,
            "Limit": str(batch),
            "StartIndex": str(start),
            "SortBy": "AlbumArtist,Album,IndexNumber",
            "Fields": "Genres"
        })
        if not data or "Items" not in data:
            break
        for item in data["Items"]:
            track_ids.append(item["Id"])
        if len(data["Items"]) < batch:
            break
        start += batch
    return track_ids


def add_to_playlist(playlist_id, track_ids):
    """Add tracks to a playlist in batches."""
    added = 0
    batch_size = 200
    for i in range(0, len(track_ids), batch_size):
        batch = track_ids[i:i+batch_size]
        ids_param = ",".join(batch)
        jf_request("POST", f"/Playlists/{playlist_id}/Items", {
            "Ids": ids_param,
            "UserId": ADMIN_USER_ID
        })
        added += len(batch)
    return added


def main():
    global ADMIN_USER_ID

    log.info("=== Rebuild Genre Playlists ===")

    # Get admin user
    ADMIN_USER_ID = get_admin_user_id()
    if not ADMIN_USER_ID:
        log.error("Could not find admin user")
        sys.exit(1)
    log.info(f"Admin user ID: {ADMIN_USER_ID}")

    # Get all playlists
    playlists = get_all_playlists()
    log.info(f"Found {len(playlists)} playlists")

    stats = {
        "playlists_rebuilt": 0,
        "total_tracks_added": 0,
        "empty_playlists": 0,
    }
    results = []

    start_time = time.time()

    for genre_name, playlist_id in sorted(playlists.items()):
        log.info(f"  Rebuilding: {genre_name}...")

        # Clear existing items
        removed = clear_playlist(playlist_id)

        # Get all tracks with this genre
        track_ids = get_tracks_by_genre(genre_name)

        if track_ids:
            added = add_to_playlist(playlist_id, track_ids)
            stats["playlists_rebuilt"] += 1
            stats["total_tracks_added"] += added
            results.append({
                "genre": genre_name,
                "tracks": len(track_ids),
                "removed": removed,
                "added": added
            })
            log.info(f"    {genre_name}: {removed} removed, {added} added")
        else:
            stats["empty_playlists"] += 1
            results.append({
                "genre": genre_name,
                "tracks": 0,
                "removed": removed,
                "added": 0
            })
            log.info(f"    {genre_name}: 0 tracks (empty)")

    elapsed = time.time() - start_time

    log.info(f"\n{'='*60}")
    log.info(f"PLAYLIST REBUILD COMPLETE in {elapsed:.0f}s")
    log.info(f"{'='*60}")
    log.info(f"Playlists rebuilt:     {stats['playlists_rebuilt']}")
    log.info(f"Empty playlists:       {stats['empty_playlists']}")
    log.info(f"Total tracks added:    {stats['total_tracks_added']}")

    # Print top playlists by track count
    results.sort(key=lambda x: -x['tracks'])
    log.info(f"\nTop 20 playlists by track count:")
    for r in results[:20]:
        log.info(f"  {r['genre']}: {r['tracks']} tracks")


if __name__ == '__main__':
    main()
