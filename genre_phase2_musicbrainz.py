#!/usr/bin/env python3
"""
Phase 2: MusicBrainz Genre Lookup
For remaining untagged tracks, check if their albums have MusicBrainz Release IDs.
Query the MB API for genre/tag data and apply to files.

Run on NAS:
    python3 /volume1/homes/homelab/genre_phase2_musicbrainz.py
"""

import json
import os
import sys
import logging
import time
import re
from collections import Counter

import requests
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TCON
from mutagen.mp4 import MP4
from mutagen import File as MutagenFile

# === Config ===
UNGENRED_JSON = "/volume1/homes/homelab/ungenred_tracks.json"
PHASE1_RESULTS = "/volume1/homes/homelab/genre_phase1_results.json"
LOG_FILE = "/volume1/homes/homelab/genre_phase2.log"
RESULT_JSON = "/volume1/homes/homelab/genre_phase2_results.json"
NAS_PATH_PREFIX = "/volume1"

JF_URL = "https://192.168.0.30"
JF_KEY = os.environ.get("JELLYFIN_API_KEY", "YOUR_JELLYFIN_API_KEY")
JF_HOST = "jellyfin.homelab"

MB_API = "https://musicbrainz.org/ws/2"
MB_RATE_LIMIT = 1.1  # seconds between requests (MB requires 1/sec)
MB_USER_AGENT = "HomeLabMusicTagger/1.0 (declan@homelab)"

# Map MB tags to our established genres (lowercase MB tag -> canonical genre)
MB_TAG_MAP = {
    "pop": "Pop", "rock": "Rock", "jazz": "Jazz", "blues": "Blues",
    "electronic": "Electronic", "hip hop": "Hip Hop", "hip-hop": "Hip Hop",
    "r&b": "R&B", "rhythm and blues": "R&B", "rnb": "R&B",
    "soul": "Soul", "funk": "Funk", "country": "Country",
    "classical": "Classical", "metal": "Heavy Metal", "heavy metal": "Heavy Metal",
    "punk": "Punk", "punk rock": "Punk Rock", "reggae": "Reggae",
    "folk": "Folk", "indie": "Indie", "indie rock": "Indie Rock",
    "indie pop": "Indie Pop", "alternative rock": "Alternative Rock",
    "alternative": "Alternative", "ambient": "Ambient",
    "dance": "Dance", "disco": "Disco", "house": "House", "techno": "Techno",
    "trance": "Trance", "drum and bass": "Drum & Bass", "dubstep": "EDM",
    "edm": "EDM", "electro": "Electro", "electronica": "Electronica",
    "experimental": "Experimental", "industrial": "Industrial",
    "new wave": "New Wave", "post-punk": "Post-Punk",
    "progressive rock": "Progressive Rock", "prog rock": "Progressive Rock",
    "psychedelic rock": "Psychedelic Rock", "grunge": "Grunge",
    "hard rock": "Hard Rock", "classic rock": "Classic Rock",
    "soft rock": "Soft Rock", "art rock": "Art Rock",
    "shoegaze": "Shoegaze", "dream pop": "Dream Pop",
    "post-rock": "Post-Rock", "math rock": "Math Rock",
    "emo": "Emo", "metalcore": "Metalcore",
    "death metal": "Death Metal", "progressive metal": "Progressive Metal",
    "gothic metal": "Gothic Metal", "gothic rock": "Gothic Rock",
    "power pop": "Power Pop", "britpop": "BritPop",
    "garage rock": "Garage Rock", "ska": "Ska",
    "latin": "Latin", "bossa nova": "Latin Jazz", "latin jazz": "Latin Jazz",
    "swing": "Swing", "big band": "Swing",
    "smooth jazz": "Smooth Jazz", "jazz fusion": "Jazz Fusion",
    "acid jazz": "Acid Jazz",
    "singer-songwriter": "Singer-Songwriter", "singer/songwriter": "Singer-Songwriter",
    "folk rock": "Folk Rock", "bluegrass": "Bluegrass",
    "world": "World", "world music": "World",
    "soundtrack": "Soundtrack", "film score": "Soundtrack",
    "anime": "Anime", "game": "Soundtrack",
    "new age": "New Age", "easy listening": "Easy Listening",
    "chillout": "ChillOut", "downtempo": "Downtempo", "trip hop": "Trip Hop",
    "lo-fi": "Lo-Fi", "lofi": "Lo-Fi",
    "synth-pop": "Synth-Pop", "synthpop": "Synth-Pop", "synthwave": "Synthwave",
    "vaporwave": "Vaporwave", "future funk": "Future Funk",
    "city pop": "City Pop", "shibuya-kei": "Shibuya-Kei", "shibuya kei": "Shibuya-Kei",
    "j-pop": "J-Pop", "jpop": "J-Pop", "japanese pop": "J-Pop",
    "j-rock": "J-Rock", "jrock": "J-Rock", "japanese rock": "J-Rock",
    "enka": "Enka", "kayōkyoku": "Kayōkyoku", "kayokyoku": "Kayōkyoku",
    "k-pop": "K-Pop", "kpop": "K-Pop", "korean pop": "K-Pop",
    "mandopop": "Mandopop", "cantopop": "Cantopop",
    "chanson": "Chanson", "flamenco": "Flamenco", "celtic": "Celtic",
    "gospel": "Gospel", "a cappella": "A Cappella",
    "opera": "Opera", "musical": "Musical",
    "comedy": "Comedy", "spoken word": "Vocal",
    "instrumental": "Instrumental", "acoustic": "Acoustic",
    "dub": "Dub", "dancehall": "Dancehall",
    "neo soul": "Neo Soul", "neo-soul": "Neo Soul",
    "yacht rock": "Yacht Rock",
    "adult contemporary": "Adult Contemporary",
    "contemporary r&b": "Contemporary R&B",
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)


def nas_path(jellyfin_path):
    if jellyfin_path.startswith("/media/"):
        return NAS_PATH_PREFIX + jellyfin_path
    return jellyfin_path


def write_genre_to_file(filepath, genre):
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == '.flac':
            audio = FLAC(filepath)
            audio['genre'] = genre
            audio.save()
            return True
        elif ext == '.mp3':
            audio = MP3(filepath)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(TCON(encoding=3, text=[genre]))
            audio.save()
            return True
        elif ext in ('.m4a', '.mp4', '.aac'):
            audio = MP4(filepath)
            if audio.tags is None:
                audio.tags = {}
            audio.tags['\xa9gen'] = [genre]
            audio.save()
            return True
        else:
            audio = MutagenFile(filepath)
            if audio is not None and hasattr(audio, 'tags') and audio.tags is not None:
                audio.tags['genre'] = genre
                audio.save()
                return True
    except Exception as e:
        log.warning(f"Failed to write genre to {filepath}: {e}")
    return False


def jf_get(path, params={}):
    """Query Jellyfin API."""
    param_str = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{JF_URL}{path}?api_key={JF_KEY}&{param_str}"
    try:
        resp = requests.get(url, headers={"Host": JF_HOST}, verify=False, timeout=30)
        return resp.json()
    except Exception as e:
        log.warning(f"Jellyfin API error: {e}")
        return {}


def mb_get_release_tags(mbid):
    """Get tags for a MusicBrainz release. Returns list of (tag_name, count)."""
    url = f"{MB_API}/release/{mbid}?inc=tags+genres&fmt=json"
    headers = {"User-Agent": MB_USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            tags = data.get("tags", [])
            genres = data.get("genres", [])
            # Combine tags and genres
            all_tags = [(t["name"].lower(), t.get("count", 0)) for t in tags]
            all_tags += [(g["name"].lower(), g.get("count", 0)) for g in genres]
            return all_tags
        elif resp.status_code == 404:
            return []
        else:
            log.warning(f"MB API returned {resp.status_code} for {mbid}")
            return []
    except Exception as e:
        log.warning(f"MB API error for {mbid}: {e}")
        return []


def mb_get_release_group_tags(mbid):
    """Get tags for a release via its release-group (often has more tags)."""
    url = f"{MB_API}/release/{mbid}?inc=release-groups&fmt=json"
    headers = {"User-Agent": MB_USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            rg = data.get("release-group", {})
            rg_id = rg.get("id")
            if rg_id:
                time.sleep(MB_RATE_LIMIT)
                url2 = f"{MB_API}/release-group/{rg_id}?inc=tags+genres&fmt=json"
                resp2 = requests.get(url2, headers=headers, timeout=15)
                if resp2.status_code == 200:
                    data2 = resp2.json()
                    tags = data2.get("tags", [])
                    genres = data2.get("genres", [])
                    all_tags = [(t["name"].lower(), t.get("count", 0)) for t in tags]
                    all_tags += [(g["name"].lower(), g.get("count", 0)) for g in genres]
                    return all_tags
        return []
    except Exception as e:
        log.warning(f"MB RG API error for {mbid}: {e}")
        return []


def map_mb_tags_to_genre(tags):
    """Map MusicBrainz tags to our established genre. Returns best genre or None."""
    if not tags:
        return None

    # Score each of our genres based on MB tags
    genre_scores = Counter()
    for tag_name, count in tags:
        tag_lower = tag_name.lower().strip()
        if tag_lower in MB_TAG_MAP:
            mapped = MB_TAG_MAP[tag_lower]
            genre_scores[mapped] += max(count, 1)

    if genre_scores:
        return genre_scores.most_common(1)[0][0]
    return None


def main():
    log.info("=== Phase 2: MusicBrainz Genre Lookup ===")

    # Suppress InsecureRequestWarning
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # Load ungenred tracks
    with open(UNGENRED_JSON) as f:
        ungenred = json.load(f)

    # Load Phase 1 results to exclude already-tagged tracks
    phase1_tagged = set()
    if os.path.exists(PHASE1_RESULTS):
        with open(PHASE1_RESULTS) as f:
            p1 = json.load(f)
            phase1_tagged = set(p1.get("tagged_track_ids", []))
        log.info(f"Phase 1 tagged {len(phase1_tagged)} tracks, excluding them")

    # Filter to remaining ungenred tracks
    remaining = [t for t in ungenred if t["id"] not in phase1_tagged]
    log.info(f"Remaining ungenred tracks after Phase 1: {len(remaining)}")

    # Group by album to look up MBIDs per album
    albums = {}  # album_id -> {"tracks": [...], "album": "...", "artist": "..."}
    no_album = []
    for track in remaining:
        aid = track.get("album_id", "")
        if aid:
            if aid not in albums:
                albums[aid] = {
                    "tracks": [],
                    "album": track.get("album", ""),
                    "artist": track.get("artist", "")
                }
            albums[aid]["tracks"].append(track)
        else:
            no_album.append(track)

    log.info(f"Unique albums to check: {len(albums)}")
    log.info(f"Tracks with no album ID: {len(no_album)}")

    # Get MBIDs from Jellyfin for these albums (batch fetch)
    album_mbids = {}  # album_id -> mbid
    album_ids_list = list(albums.keys())
    batch_size = 200

    log.info("Fetching album MBIDs from Jellyfin...")
    for i in range(0, len(album_ids_list), batch_size):
        batch = album_ids_list[i:i+batch_size]
        ids_param = ",".join(batch)
        data = jf_get("/Items", {
            "Ids": ids_param,
            "Fields": "ProviderIds",
            "Limit": str(len(batch))
        })
        for item in data.get("Items", []):
            providers = item.get("ProviderIds", {})
            mbid = providers.get("MusicBrainzAlbum", "")
            if mbid:
                album_mbids[item["Id"]] = mbid

    log.info(f"Albums with MusicBrainz IDs: {len(album_mbids)} / {len(albums)}")

    # Query MB API for each album with an MBID
    stats = {
        "albums_checked": 0,
        "albums_with_genre": 0,
        "albums_no_genre": 0,
        "tracks_tagged": 0,
        "tracks_failed": 0,
        "mb_api_calls": 0,
    }
    results = []
    tagged_track_ids = set()

    start_time = time.time()
    albums_to_check = [(aid, mbid) for aid, mbid in album_mbids.items()]

    for idx, (album_id, mbid) in enumerate(albums_to_check):
        stats["albums_checked"] += 1

        # Rate limit
        time.sleep(MB_RATE_LIMIT)
        stats["mb_api_calls"] += 1

        # Try release tags first
        tags = mb_get_release_tags(mbid)
        genre = map_mb_tags_to_genre(tags)

        # If no genre from release, try release-group
        if not genre:
            time.sleep(MB_RATE_LIMIT)
            stats["mb_api_calls"] += 1
            tags = mb_get_release_group_tags(mbid)
            genre = map_mb_tags_to_genre(tags)

        if genre:
            stats["albums_with_genre"] += 1
            album_info = albums[album_id]
            tagged = 0
            failed = 0

            for track in album_info["tracks"]:
                fpath = nas_path(track["path"])
                if os.path.exists(fpath):
                    if write_genre_to_file(fpath, genre):
                        tagged += 1
                        tagged_track_ids.add(track["id"])
                    else:
                        failed += 1
                else:
                    failed += 1

            stats["tracks_tagged"] += tagged
            stats["tracks_failed"] += failed

            results.append({
                "album_id": album_id,
                "mbid": mbid,
                "album": album_info["album"],
                "artist": album_info["artist"],
                "genre": genre,
                "tracks_tagged": tagged,
                "mb_tags": [t[0] for t in tags[:5]]
            })
        else:
            stats["albums_no_genre"] += 1

        if (idx + 1) % 50 == 0:
            elapsed = time.time() - start_time
            rate = (idx + 1) / elapsed if elapsed > 0 else 0
            eta = (len(albums_to_check) - idx - 1) / rate if rate > 0 else 0
            log.info(f"  Progress: {idx+1}/{len(albums_to_check)} albums "
                     f"({stats['albums_with_genre']} with genre, "
                     f"{stats['tracks_tagged']} tracks tagged, "
                     f"ETA {eta/60:.0f}min)")

    elapsed = time.time() - start_time

    output = {
        "stats": stats,
        "results": results,
        "tagged_track_ids": list(tagged_track_ids),
        "elapsed_seconds": round(elapsed, 1)
    }
    with open(RESULT_JSON, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    log.info(f"\n{'='*60}")
    log.info(f"PHASE 2 COMPLETE in {elapsed:.0f}s ({elapsed/60:.1f}min)")
    log.info(f"{'='*60}")
    log.info(f"Albums checked:       {stats['albums_checked']}")
    log.info(f"  With genre found:   {stats['albums_with_genre']}")
    log.info(f"  No genre:           {stats['albums_no_genre']}")
    log.info(f"Tracks tagged:        {stats['tracks_tagged']}")
    log.info(f"Tracks failed:        {stats['tracks_failed']}")
    log.info(f"MB API calls:         {stats['mb_api_calls']}")
    log.info(f"\nResults saved to {RESULT_JSON}")


if __name__ == '__main__':
    main()
