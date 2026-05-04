#!/usr/bin/env python3
"""
Phase 1: Artist-Based Genre Inference
For each artist with untagged tracks, check their already-tagged tracks.
If >=80% share one genre (60% for artists with >50 tagged tracks), apply that
genre to all untagged tracks by writing to files via mutagen.

Run on NAS:
    python3 /volume1/homes/homelab/genre_phase1_artist_inference.py
"""

import json
import os
import sys
import logging
import time
from collections import Counter

# mutagen imports
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TCON
from mutagen.mp4 import MP4
from mutagen import File as MutagenFile

# === Config ===
UNGENRED_JSON = "/volume1/homes/homelab/ungenred_tracks.json"
GENRED_JSON = "/volume1/homes/homelab/genred_by_artist.json"
LOG_FILE = "/volume1/homes/homelab/genre_phase1.log"
RESULT_JSON = "/volume1/homes/homelab/genre_phase1_results.json"
NAS_PATH_PREFIX = "/volume1"  # Jellyfin sees /media, NAS has /volume1/media

SKIP_ARTISTS = {"?", "", "Various Artists", "V.A.", "Various", "VA",
                "various artists", "v.a.", "Unknown Artist", "Unknown"}

# Normalise genre names to canonical forms
GENRE_NORMALISE = {
    "J-POP": "J-Pop", "J-pop": "J-Pop", "JPop": "J-Pop", "Jpop": "J-Pop",
    "j-pop": "J-Pop",
    "Pop ": "Pop", "Rock ": "Rock", "Funk ": "Funk",
    "Hip hop": "Hip Hop", "hip hop": "Hip Hop",
    "R&b": "R&B", "r&b": "R&B",
    "Folk rock": "Folk Rock", "Folk/Rock": "Folk Rock",
    "Pop/Rock": "Pop Rock", "Pop/rock": "Pop Rock", "Pop-Rock": "Pop Rock",
    "Synth-pop": "Synth-Pop",
    "Shibuya-kei": "Shibuya-Kei",
    "Contemporary jazz": "Contemporary Jazz",
    "Smooth jazz": "Smooth Jazz",
    "Aor": "AOR",
    "   Blues Rock": "Blues Rock",
    "   Classical Crossover": "Classical Crossover",
    "   Other": "Other",
    "   Vocal Jazz": "Vocal Jazz",
    " ": None,  # empty/whitespace genre
    "anime": "Anime",
    "soul": "Soul",
    "Kayokyoku": "Kayōkyoku",
    "Chillout": "ChillOut",
    "\ufeffVocal": "Vocal",
    "댄스/팝": "K-Pop",
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


def normalise_genre(genre):
    """Normalise genre string to canonical form."""
    g = genre.strip()
    if g in GENRE_NORMALISE:
        return GENRE_NORMALISE[g]
    return g if g else None


def nas_path(jellyfin_path):
    """Convert Jellyfin path (/media/...) to NAS path (/volume1/media/...)."""
    if jellyfin_path.startswith("/media/"):
        return NAS_PATH_PREFIX + jellyfin_path
    return jellyfin_path


def write_genre_to_file(filepath, genre):
    """Write genre tag to an audio file. Returns True on success."""
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
            if audio is not None:
                if hasattr(audio, 'tags') and audio.tags is not None:
                    audio.tags['genre'] = genre
                    audio.save()
                    return True
    except Exception as e:
        log.warning(f"Failed to write genre to {filepath}: {e}")
    return False


def determine_genre(genre_counts, total_tagged):
    """
    Determine the dominant genre for an artist.
    Returns (genre, confidence) or (None, 0).

    Rules:
    - >=80% threshold for artists with <=50 tagged tracks
    - >=60% threshold for artists with >50 tagged tracks (larger sample)
    """
    if not genre_counts or total_tagged == 0:
        return None, 0

    # Normalise and merge genre counts
    normalised = Counter()
    for g, count in genre_counts.items():
        ng = normalise_genre(g)
        if ng:
            normalised[ng] += count

    if not normalised:
        return None, 0

    top_genre, top_count = normalised.most_common(1)[0]
    confidence = top_count / total_tagged

    threshold = 0.60 if total_tagged > 50 else 0.80

    if confidence >= threshold:
        return top_genre, confidence

    return None, confidence


def main():
    log.info("=== Phase 1: Artist-Based Genre Inference ===")

    # Load data
    log.info("Loading ungenred tracks...")
    with open(UNGENRED_JSON) as f:
        ungenred = json.load(f)
    log.info(f"  {len(ungenred)} ungenred tracks")

    log.info("Loading genred artist data...")
    with open(GENRED_JSON) as f:
        genred_by_artist = json.load(f)
    log.info(f"  {len(genred_by_artist)} artists with genre data")

    # Group ungenred tracks by artist
    ungenred_by_artist = {}
    for track in ungenred:
        artist = track.get("artist", "")
        if artist not in ungenred_by_artist:
            ungenred_by_artist[artist] = []
        ungenred_by_artist[artist].append(track)

    log.info(f"  {len(ungenred_by_artist)} unique artists with ungenred tracks")

    # Process each artist
    stats = {
        "artists_processed": 0,
        "artists_matched": 0,
        "artists_skipped_ambiguous": 0,
        "artists_skipped_no_data": 0,
        "artists_skipped_blocklist": 0,
        "tracks_tagged": 0,
        "tracks_failed": 0,
        "tracks_remaining": 0,
    }
    results = []
    tagged_track_ids = set()

    start_time = time.time()

    for artist, tracks in sorted(ungenred_by_artist.items(), key=lambda x: -len(x[1])):
        stats["artists_processed"] += 1

        if artist in SKIP_ARTISTS:
            stats["artists_skipped_blocklist"] += 1
            stats["tracks_remaining"] += len(tracks)
            continue

        if artist not in genred_by_artist:
            stats["artists_skipped_no_data"] += 1
            stats["tracks_remaining"] += len(tracks)
            continue

        genre_counts = genred_by_artist[artist]
        total_tagged = sum(genre_counts.values())

        genre, confidence = determine_genre(genre_counts, total_tagged)

        if genre is None:
            stats["artists_skipped_ambiguous"] += 1
            stats["tracks_remaining"] += len(tracks)
            # Log top genres for debugging
            normalised = Counter()
            for g, c in genre_counts.items():
                ng = normalise_genre(g)
                if ng:
                    normalised[ng] += c
            top3 = normalised.most_common(3)
            log.debug(f"  Ambiguous: {artist} ({len(tracks)} tracks) - top genres: {top3}")
            continue

        # Apply genre to all ungenred tracks for this artist
        stats["artists_matched"] += 1
        tagged_count = 0
        failed_count = 0

        for track in tracks:
            fpath = nas_path(track["path"])
            if os.path.exists(fpath):
                if write_genre_to_file(fpath, genre):
                    tagged_count += 1
                    tagged_track_ids.add(track["id"])
                else:
                    failed_count += 1
            else:
                failed_count += 1
                log.warning(f"  File not found: {fpath}")

        stats["tracks_tagged"] += tagged_count
        stats["tracks_failed"] += failed_count

        results.append({
            "artist": artist,
            "genre": genre,
            "confidence": round(confidence, 3),
            "total_tagged_tracks": total_tagged,
            "ungenred_tracks": len(tracks),
            "newly_tagged": tagged_count,
            "failed": failed_count
        })

        if stats["artists_matched"] % 25 == 0:
            elapsed = time.time() - start_time
            log.info(f"  Progress: {stats['artists_matched']} artists matched, "
                     f"{stats['tracks_tagged']} tracks tagged ({elapsed:.0f}s)")

    elapsed = time.time() - start_time

    # Save results
    output = {
        "stats": stats,
        "results": results,
        "tagged_track_ids": list(tagged_track_ids),
        "elapsed_seconds": round(elapsed, 1)
    }
    with open(RESULT_JSON, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    log.info(f"\n{'='*60}")
    log.info(f"PHASE 1 COMPLETE in {elapsed:.0f}s")
    log.info(f"{'='*60}")
    log.info(f"Artists processed:          {stats['artists_processed']}")
    log.info(f"  Matched (genre applied):  {stats['artists_matched']}")
    log.info(f"  Skipped (no genre data):  {stats['artists_skipped_no_data']}")
    log.info(f"  Skipped (ambiguous):      {stats['artists_skipped_ambiguous']}")
    log.info(f"  Skipped (blocklist):      {stats['artists_skipped_blocklist']}")
    log.info(f"Tracks tagged:              {stats['tracks_tagged']}")
    log.info(f"Tracks failed:              {stats['tracks_failed']}")
    log.info(f"Tracks remaining ungenred:  {stats['tracks_remaining']}")
    log.info(f"\nResults saved to {RESULT_JSON}")


if __name__ == '__main__':
    main()
