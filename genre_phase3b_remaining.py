#!/usr/bin/env python3
"""
Phase 3b: Tag remaining untagged tracks that Phase 3 missed.
These are mostly romanized Japanese artists and some Western artists
that weren't in the known lists.

Approach:
1. Check folder path for Japanese release patterns (date format, FLAC tags, etc.)
2. Known Western artist -> genre mapping
3. Best-effort for everything else

Run on NAS:
    python3 /volume1/homes/homelab/genre_phase3b_remaining.py
"""

import json
import os
import sys
import logging
import time
import re
from collections import Counter

from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TCON
from mutagen.mp4 import MP4
from mutagen import File as MutagenFile

# === Config ===
STILL_UNTAGGED = "/volume1/homes/homelab/genre_still_untagged.json"
UNGENRED_JSON = "/volume1/homes/homelab/ungenred_tracks.json"
PHASE1_RESULTS = "/volume1/homes/homelab/genre_phase1_results.json"
PHASE3_RESULTS = "/volume1/homes/homelab/genre_phase3_results.json"
GENRED_JSON = "/volume1/homes/homelab/genred_by_artist.json"
LOG_FILE = "/volume1/homes/homelab/genre_phase3b.log"
RESULT_JSON = "/volume1/homes/homelab/genre_phase3b_results.json"
NAS_PATH_PREFIX = "/volume1"

# Explicit artist -> genre mapping for remaining artists
ARTIST_GENRE = {
    # Japanese artists (romanized)
    "Magokoro Brothers": "J-Rock",
    "Leyona": "J-Pop",
    "Nanase Aikawa": "J-Rock",
    "Hirose Kohmi": "J-Pop",
    "Tsuki Amano": "J-Pop",
    "KICK THE CAN CREW": "Hip Hop",
    "Toko Furuuchi": "J-Pop",
    "NATSUMI": "J-Pop",
    "Twiggy": "J-Pop",
    "Mai Hoshimura": "J-Pop",
    "Asami Kobayashi": "City Pop",
    "Ginji Ito": "City Pop",
    "Orange Pekoe": "Jazz",
    "Mai Yamane": "City Pop",
    "Picasso": "J-Pop",
    "Frasco": "J-Pop",
    "Strawberry Machine": "J-Pop",
    "Garnet Crow": "J-Pop",
    "Mika Nakashima": "J-Pop",
    "Tomoko Kawase": "J-Pop",
    "Yuna Ito": "J-Pop",
    "Chisato Moritaka": "J-Pop",
    "Harumi Hosono": "Electronic",
    "Haruomi Hosono": "Electronic",
    "Akiko Wada": "Kayōkyoku",
    "Nami Tamaki": "J-Pop",
    "Yoko Kanno": "Soundtrack",
    "Aira Mitsuki": "Electropop",
    "Koda Kumi": "J-Pop",
    "Ai Otsuka": "J-Pop",
    "Sowelu": "J-R&B",
    "TeddyLoid": "Electronic",
    "Polysics": "New Wave",
    "Puffy AmiYumi": "J-Pop",
    "Asian Kung-Fu Generation": "J-Rock",
    "Minmi": "Reggae",
    "Beni": "J-Pop",
    "Shikao Suga": "J-Pop",
    "Spitz": "J-Rock",
    "Monkey Majik": "J-Rock",
    "Rip Slyme": "Hip Hop",
    "Halcali": "J-Pop",
    "Supercar": "Shoegaze",
    "Number Girl": "J-Rock",
    "Fishmans": "Dream Pop",
    "Takako Mamiya": "City Pop",
    "Miki Imai": "J-Pop",

    # Korean
    "CHUNG HA": "K-Pop",
    "BBGIRLS": "K-Pop",

    # Western artists
    "Elvis Presley": "Rock",
    "Take That": "Pop",
    "Ryan Adams": "Alternative Rock",
    "Jackson Browne": "Folk Rock",
    "Tom Jones": "Pop",
    "alt-J": "Alternative Rock",
    "B2K": "R&B",
    "Vitamin String Quartet": "Classical",
    "Chris Rea": "Soft Rock",
    "Deep Purple": "Hard Rock",
    "Gorillaz": "Alternative",
    "Gillan": "Hard Rock",
    "Hole": "Grunge",
    "Papa Roach": "Alternative Rock",
    "Phil Collins": "Pop",
    "Scorpions": "Hard Rock",
    "Shania Twain": "Country",
    "Train": "Pop Rock",
    "Destiny's Child": "R&B",
    "Manic Street Preachers": "Alternative Rock",
    "Laura Branigan": "Pop",
    "Nat King Cole": "Jazz",
    "Ice Cube": "Hip Hop",

    # Soundtracks / Game music
    "Petros Sklias": "Soundtrack",
    "DDBY": "Soundtrack",
    "Lorien Testard": "Soundtrack",
}

# Path-based Japanese detection patterns
JP_PATH_PATTERNS = [
    r'\(\d{4}\.\d{2}\.\d{2}\)',  # (YYYY.MM.DD) date format common in JP releases
    r'\(album\)',                  # "(album)" tag common in JP rips
    r'\[FLAC\]\s*\{',            # [FLAC] {catalog} pattern
    r'KURA-|VIZL-|CTCR-|AICL-|TOCT-|WPCL-|KSCL-|GZCA-|VICL-|TKCA-',  # JP label catalog numbers
]

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


def is_japanese_path(path):
    """Check if a file path looks like a Japanese music release."""
    for pattern in JP_PATH_PATTERNS:
        if re.search(pattern, path):
            return True
    return False


def has_cjk_in_path(path):
    """Check if the file path contains any CJK/kana characters."""
    for ch in path:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x9FFF or 0xFF65 <= cp <= 0xFF9F:
            return True
    return False


def main():
    log.info("=== Phase 3b: Tag Remaining Untagged Tracks ===")

    # Load still-untagged artists from Phase 3
    with open(STILL_UNTAGGED) as f:
        untagged_artists = json.load(f)
    log.info(f"Still untagged artists: {len(untagged_artists)}")
    log.info(f"Still untagged tracks: {sum(a['count'] for a in untagged_artists)}")

    # Load full ungenred track list
    with open(UNGENRED_JSON) as f:
        all_ungenred = json.load(f)

    # Load all phase results to get tagged IDs
    tagged_ids = set()
    for rf in [PHASE1_RESULTS, PHASE3_RESULTS]:
        if os.path.exists(rf):
            try:
                with open(rf) as f:
                    data = json.load(f)
                    tagged_ids.update(data.get("tagged_track_ids", []))
            except:
                pass
    log.info(f"Already tagged by Phase 1+3: {len(tagged_ids)}")

    # Get remaining tracks grouped by artist
    remaining_by_artist = {}
    for track in all_ungenred:
        if track["id"] not in tagged_ids:
            artist = track.get("artist", "")
            if artist not in remaining_by_artist:
                remaining_by_artist[artist] = []
            remaining_by_artist[artist].append(track)

    log.info(f"Remaining artists: {len(remaining_by_artist)}")
    log.info(f"Remaining tracks: {sum(len(v) for v in remaining_by_artist.values())}")

    # Load genred data for hints
    with open(GENRED_JSON) as f:
        genred_by_artist = json.load(f)

    stats = {
        "tracks_tagged_explicit": 0,
        "tracks_tagged_path": 0,
        "tracks_tagged_cjk_path": 0,
        "tracks_still_untagged": 0,
        "tracks_failed": 0,
    }
    results = []
    newly_tagged = set()
    start_time = time.time()

    for artist, tracks in sorted(remaining_by_artist.items(), key=lambda x: -len(x[1])):
        genre = None
        method = ""

        # 1. Check explicit mapping
        if artist in ARTIST_GENRE:
            genre = ARTIST_GENRE[artist]
            method = "explicit"

        # 2. Check if path looks Japanese
        if not genre and tracks:
            sample_path = tracks[0].get("path", "")
            if is_japanese_path(sample_path):
                genre = "J-Pop"
                method = "jp_path_pattern"
            elif has_cjk_in_path(sample_path):
                genre = "J-Pop"
                method = "cjk_in_path"

        # 3. Check genred_by_artist for hints (may have been missed by Phase 1
        #    due to threshold requirements)
        if not genre and artist in genred_by_artist:
            genre_counts = genred_by_artist[artist]
            if genre_counts:
                top = max(genre_counts, key=genre_counts.get)
                top = top.strip()
                if top:
                    genre = top
                    method = "genred_hint_below_threshold"

        if genre:
            tagged = 0
            failed = 0
            for track in tracks:
                fpath = nas_path(track["path"])
                if os.path.exists(fpath):
                    if write_genre_to_file(fpath, genre):
                        tagged += 1
                        newly_tagged.add(track["id"])
                    else:
                        failed += 1
                else:
                    failed += 1

            if "explicit" in method:
                stats["tracks_tagged_explicit"] += tagged
            elif "path" in method or "cjk" in method:
                stats["tracks_tagged_path"] += tagged
            else:
                stats["tracks_tagged_cjk_path"] += tagged
            stats["tracks_failed"] += failed

            results.append({
                "artist": artist,
                "genre": genre,
                "method": method,
                "tracks": len(tracks),
                "tagged": tagged
            })
        else:
            stats["tracks_still_untagged"] += len(tracks)
            if len(tracks) >= 5:
                log.info(f"  Still untagged: {artist} ({len(tracks)} tracks) "
                         f"path: {tracks[0].get('path','')[:80]}")

    elapsed = time.time() - start_time
    total_tagged = stats["tracks_tagged_explicit"] + stats["tracks_tagged_path"] + stats["tracks_tagged_cjk_path"]

    output = {
        "stats": stats,
        "results": results,
        "tagged_track_ids": list(newly_tagged),
        "elapsed_seconds": round(elapsed, 1)
    }
    with open(RESULT_JSON, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    log.info(f"\n{'='*60}")
    log.info(f"PHASE 3b COMPLETE in {elapsed:.0f}s")
    log.info(f"{'='*60}")
    log.info(f"Tracks tagged (explicit):      {stats['tracks_tagged_explicit']}")
    log.info(f"Tracks tagged (path pattern):  {stats['tracks_tagged_path']}")
    log.info(f"Tracks tagged (CJK in path):   {stats['tracks_tagged_cjk_path']}")
    log.info(f"Total tagged:                  {total_tagged}")
    log.info(f"Tracks failed:                 {stats['tracks_failed']}")
    log.info(f"Tracks still untagged:         {stats['tracks_still_untagged']}")
    log.info(f"\nResults: {RESULT_JSON}")


if __name__ == '__main__':
    main()
