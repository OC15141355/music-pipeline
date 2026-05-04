#!/usr/bin/env python3
"""
Phase 3: CJK Music Default Genre Assignment
For remaining untagged tracks, detect Japanese/Korean/Chinese artists and apply
appropriate default genres.

Detection:
- Japanese: CJK chars (0x3040-0x9FFF) in artist name -> J-Pop default
- Korean: Hangul (0xAC00-0xD7AF) -> K-Pop
- Chinese (non-Japanese): -> Mandopop or Cantopop

For Japanese artists with existing tagged tracks suggesting City Pop or Kayōkyoku,
use that genre instead of J-Pop.

Run on NAS:
    python3 /volume1/homes/homelab/genre_phase3_cjk_defaults.py
"""

import json
import os
import sys
import logging
import time
import re
import unicodedata
from collections import Counter

from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TCON
from mutagen.mp4 import MP4
from mutagen import File as MutagenFile

# === Config ===
UNGENRED_JSON = "/volume1/homes/homelab/ungenred_tracks.json"
GENRED_JSON = "/volume1/homes/homelab/genred_by_artist.json"
PHASE1_RESULTS = "/volume1/homes/homelab/genre_phase1_results.json"
PHASE2_RESULTS = "/volume1/homes/homelab/genre_phase2_results.json"
LOG_FILE = "/volume1/homes/homelab/genre_phase3.log"
RESULT_JSON = "/volume1/homes/homelab/genre_phase3_results.json"
NAS_PATH_PREFIX = "/volume1"

SKIP_ARTISTS = {"?", "", "Various Artists", "V.A.", "Various", "VA",
                "various artists", "v.a.", "Unknown Artist", "Unknown"}

# Known Cantopop artists (common in the library based on the data we saw)
CANTOPOP_ARTISTS = {
    "容祖兒", "陳奕迅", "張學友", "王菲", "鄧麗君", "劉德華",
    "梅艷芳", "譚詠麟", "陳百強", "張國榮", "林憶蓮",
    "Beyond", "達明一派", "Twins", "何韻詩", "楊千嬅",
    "衛蘭", "側田", "古巨基", "許志安", "鄭秀文",
    "Joey Yung", "Eason Chan", "Jacky Cheung", "Faye Wong",
}

# Known Mandopop indicators
MANDOPOP_INDICATORS = {"周杰倫", "蔡依林", "林俊傑", "五月天", "S.H.E",
                       "張惠妹", "王力宏", "羅志祥", "蕭亞軒", "梁靜茹",
                       "Jay Chou", "Jolin Tsai"}

# Known romanized Japanese artists that won't be detected by CJK chars
KNOWN_JPOP_ARTISTS = {
    "Mai Kuraki", "paris match", "CheNelle", "capsule", "May J.",
    "JUJU", "hitomi", "Maki Goto", "Night Tempo", "Bonnie Pink",
    "Miharu Koshi", "Tatsuro Yamashita", "Mariya Takeuchi",
    "Taeko Ohnuki", "Toshiki Kadomatsu", "Akiko Yano",
    "Miki Matsubara", "Junko Ohashi", "Anri", "EPO",
    "Momoko Kikuchi", "Meiko Nakahara", "Akina Nakamori",
    "Seiko Matsuda", "Yumi Matsutoya", "Toshinobu Kubota",
    "Hikaru Utada", "Namie Amuro", "Ayumi Hamasaki",
    "BoA", "Crystal Kay", "CHEMISTRY", "Do As Infinity",
    "Every Little Thing", "globe", "Glay", "L'Arc~en~Ciel",
    "Mr.Children", "SPEED", "TRF", "w-inds.", "YUI",
    "Suara", "BBGIRLS", "Pogo", "Lorien Testard",
    "m-flo", "DOUBLE", "Misia", "bird", "UA",
    "Cornelius", "Pizzicato Five", "Fantastic Plastic Machine",
    "Flipper's Guitar", "Kahimi Karie", "Cibo Matto",
}

# Specific genre overrides for known artists
ARTIST_GENRE_OVERRIDE = {
    "Tatsuro Yamashita": "City Pop",
    "Mariya Takeuchi": "City Pop",
    "Taeko Ohnuki": "City Pop",
    "Toshiki Kadomatsu": "City Pop",
    "Miki Matsubara": "City Pop",
    "Junko Ohashi": "City Pop",
    "Anri": "City Pop",
    "EPO": "City Pop",
    "Momoko Kikuchi": "City Pop",
    "Meiko Nakahara": "City Pop",
    "山下達郎": "City Pop",
    "竹内まりや": "City Pop",
    "大貫妙子": "City Pop",
    "角松敏生": "City Pop",
    "松原みき": "City Pop",
    "大橋純子": "City Pop",
    "杏里": "City Pop",
    "菊池桃子": "City Pop",
    "中原めいこ": "City Pop",
    "Night Tempo": "Future Funk",
    "Pizzicato Five": "Shibuya-Kei",
    "Flipper's Guitar": "Shibuya-Kei",
    "Cornelius": "Shibuya-Kei",
    "Kahimi Karie": "Shibuya-Kei",
    "Fantastic Plastic Machine": "Shibuya-Kei",
    "Cibo Matto": "Shibuya-Kei",
    "小沢健二": "Shibuya-Kei",
    "Miharu Koshi": "Synth-Pop",
    "Pogo": "Instrumental",
    "Lorien Testard": "Soundtrack",
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


def has_japanese(text):
    """Check if text contains Japanese characters (Hiragana, Katakana, or CJK)."""
    for ch in text:
        cp = ord(ch)
        # Hiragana
        if 0x3040 <= cp <= 0x309F:
            return True
        # Katakana
        if 0x30A0 <= cp <= 0x30FF:
            return True
        # CJK Unified Ideographs (shared with Chinese but contextually Japanese)
        if 0x4E00 <= cp <= 0x9FFF:
            return True
        # Half-width Katakana
        if 0xFF65 <= cp <= 0xFF9F:
            return True
    return False


def has_only_cjk_no_kana(text):
    """Check if text has CJK ideographs but NO Japanese kana — likely Chinese."""
    has_cjk = False
    has_kana = False
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF:
            has_cjk = True
        if 0x3040 <= cp <= 0x30FF or 0xFF65 <= cp <= 0xFF9F:
            has_kana = True
    return has_cjk and not has_kana


def has_hangul(text):
    """Check if text contains Korean Hangul characters."""
    for ch in text:
        cp = ord(ch)
        # Hangul Syllables
        if 0xAC00 <= cp <= 0xD7AF:
            return True
        # Hangul Jamo
        if 0x1100 <= cp <= 0x11FF:
            return True
        # Hangul Compatibility Jamo
        if 0x3130 <= cp <= 0x318F:
            return True
    return False


def detect_language_from_path(path):
    """Try to detect language from folder path indicators."""
    path_lower = path.lower()
    # Common Japanese music indicators in folder names
    jp_indicators = ["(flac)", "jpop", "j-pop", "city pop",
                     "kayokyoku", "kayōkyoku", "enka", "shibuya"]
    for ind in jp_indicators:
        if ind in path_lower:
            return "japanese"
    return None


def get_artist_genre_hint(artist, genred_by_artist):
    """Check if an artist's existing tags suggest a specific genre."""
    if artist not in genred_by_artist:
        return None

    genre_counts = genred_by_artist[artist]
    total = sum(genre_counts.values())
    if total == 0:
        return None

    # Normalise
    normalised = Counter()
    for g, c in genre_counts.items():
        g = g.strip()
        if g:
            normalised[g] += c

    if not normalised:
        return None

    # Check for City Pop or Kayōkyoku preference
    japanese_genres = {"City Pop", "Kayōkyoku", "Kayokyoku", "Enka",
                       "J-Rock", "Shibuya-Kei", "Future Funk", "J-R&B"}
    for genre in japanese_genres:
        if genre in normalised:
            ratio = normalised[genre] / total
            if ratio >= 0.3:  # Lower threshold for specific Japanese genres
                # Normalise genre name
                if genre == "Kayokyoku":
                    genre = "Kayōkyoku"
                return genre

    return None


def main():
    log.info("=== Phase 3: CJK Music Default Genre Assignment ===")

    # Load data
    with open(UNGENRED_JSON) as f:
        ungenred = json.load(f)

    with open(GENRED_JSON) as f:
        genred_by_artist = json.load(f)

    # Load Phase 1 results to exclude already-tagged (Phase 2 may still be running)
    # Phase 3 can safely overlap with Phase 2 — both write genre tags,
    # and Phase 2's MB-sourced genre is more specific so it wins if both write.
    already_tagged = set()
    for results_file in [PHASE1_RESULTS, PHASE2_RESULTS]:
        if os.path.exists(results_file):
            try:
                with open(results_file) as f:
                    data = json.load(f)
                    already_tagged.update(data.get("tagged_track_ids", []))
            except (json.JSONDecodeError, KeyError):
                log.warning(f"Could not load {results_file}, skipping")

    remaining = [t for t in ungenred if t["id"] not in already_tagged]
    log.info(f"Remaining ungenred after Phase 1+2: {len(remaining)}")

    # Group by artist
    by_artist = {}
    for track in remaining:
        artist = track.get("artist", "")
        if artist not in by_artist:
            by_artist[artist] = []
        by_artist[artist].append(track)

    log.info(f"Unique artists: {len(by_artist)}")

    stats = {
        "artists_processed": 0,
        "artists_tagged_jpop": 0,
        "artists_tagged_kpop": 0,
        "artists_tagged_cantopop": 0,
        "artists_tagged_mandopop": 0,
        "artists_tagged_override": 0,
        "artists_tagged_citypop": 0,
        "artists_tagged_other_jp": 0,
        "artists_no_match": 0,
        "tracks_tagged": 0,
        "tracks_failed": 0,
        "tracks_remaining": 0,
    }
    results = []
    tagged_track_ids = set()
    start_time = time.time()

    for artist, tracks in sorted(by_artist.items(), key=lambda x: -len(x[1])):
        stats["artists_processed"] += 1

        if artist in SKIP_ARTISTS:
            stats["tracks_remaining"] += len(tracks)
            continue

        genre = None
        method = ""

        # Check explicit overrides first
        if artist in ARTIST_GENRE_OVERRIDE:
            genre = ARTIST_GENRE_OVERRIDE[artist]
            method = "override"
            stats["artists_tagged_override"] += 1

        # Check known J-Pop artists
        elif artist in KNOWN_JPOP_ARTISTS:
            # Check if existing tags suggest a more specific genre
            hint = get_artist_genre_hint(artist, genred_by_artist)
            if hint:
                genre = hint
                method = f"known_jpop_hint_{hint}"
            else:
                genre = "J-Pop"
                method = "known_jpop"
            stats["artists_tagged_jpop"] += 1

        # Detect Korean
        elif has_hangul(artist):
            genre = "K-Pop"
            method = "hangul_detect"
            stats["artists_tagged_kpop"] += 1

        # Check known Cantopop artists
        elif artist in CANTOPOP_ARTISTS:
            genre = "Cantopop"
            method = "known_cantopop"
            stats["artists_tagged_cantopop"] += 1

        # Check known Mandopop artists
        elif artist in MANDOPOP_INDICATORS:
            genre = "Mandopop"
            method = "known_mandopop"
            stats["artists_tagged_mandopop"] += 1

        # Detect Japanese (has kana or CJK with Japanese context)
        elif has_japanese(artist):
            # Check if existing tags hint at something specific
            hint = get_artist_genre_hint(artist, genred_by_artist)
            if hint:
                genre = hint
                method = f"japanese_hint_{hint}"
                if hint == "City Pop":
                    stats["artists_tagged_citypop"] += 1
                else:
                    stats["artists_tagged_other_jp"] += 1
            else:
                genre = "J-Pop"
                method = "japanese_detect"
                stats["artists_tagged_jpop"] += 1

        # Detect Chinese-only (CJK without kana)
        elif has_only_cjk_no_kana(artist):
            # Could be Mandopop or Cantopop - check hints
            if artist in CANTOPOP_ARTISTS:
                genre = "Cantopop"
                method = "chinese_cantopop"
                stats["artists_tagged_cantopop"] += 1
            else:
                genre = "Mandopop"
                method = "chinese_detect"
                stats["artists_tagged_mandopop"] += 1

        # Check path-based detection for romanized names
        elif not genre:
            # Check if any track paths suggest Japanese content
            sample_path = tracks[0]["path"] if tracks else ""
            lang = detect_language_from_path(sample_path)
            if lang == "japanese":
                hint = get_artist_genre_hint(artist, genred_by_artist)
                genre = hint if hint else "J-Pop"
                method = "path_detect"
                stats["artists_tagged_jpop"] += 1

        if genre:
            tagged = 0
            failed = 0
            for track in tracks:
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
                "artist": artist,
                "genre": genre,
                "method": method,
                "tracks": len(tracks),
                "tagged": tagged,
                "failed": failed
            })
        else:
            stats["artists_no_match"] += 1
            stats["tracks_remaining"] += len(tracks)

        if stats["artists_processed"] % 100 == 0:
            elapsed = time.time() - start_time
            log.info(f"  Progress: {stats['artists_processed']} artists, "
                     f"{stats['tracks_tagged']} tracks tagged ({elapsed:.0f}s)")

    elapsed = time.time() - start_time

    output = {
        "stats": stats,
        "results": results,
        "tagged_track_ids": list(tagged_track_ids),
        "elapsed_seconds": round(elapsed, 1)
    }
    with open(RESULT_JSON, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # Also dump the remaining untagged artists for reference
    remaining_artists = []
    for artist, tracks in by_artist.items():
        if artist in SKIP_ARTISTS:
            continue
        any_tagged = any(t["id"] in tagged_track_ids for t in tracks)
        if not any_tagged:
            remaining_artists.append({
                "artist": artist,
                "count": len(tracks),
                "sample_path": tracks[0]["path"] if tracks else ""
            })
    remaining_artists.sort(key=lambda x: -x["count"])

    with open("/volume1/homes/homelab/genre_still_untagged.json", "w") as f:
        json.dump(remaining_artists, f, indent=2, ensure_ascii=False)

    log.info(f"\n{'='*60}")
    log.info(f"PHASE 3 COMPLETE in {elapsed:.0f}s")
    log.info(f"{'='*60}")
    log.info(f"Artists processed:       {stats['artists_processed']}")
    log.info(f"  Tagged J-Pop:          {stats['artists_tagged_jpop']}")
    log.info(f"  Tagged K-Pop:          {stats['artists_tagged_kpop']}")
    log.info(f"  Tagged Cantopop:       {stats['artists_tagged_cantopop']}")
    log.info(f"  Tagged Mandopop:       {stats['artists_tagged_mandopop']}")
    log.info(f"  Tagged City Pop:       {stats['artists_tagged_citypop']}")
    log.info(f"  Tagged other JP:       {stats['artists_tagged_other_jp']}")
    log.info(f"  Tagged via override:   {stats['artists_tagged_override']}")
    log.info(f"  No match:              {stats['artists_no_match']}")
    log.info(f"Tracks tagged:           {stats['tracks_tagged']}")
    log.info(f"Tracks failed:           {stats['tracks_failed']}")
    log.info(f"Tracks still remaining:  {stats['tracks_remaining']}")
    log.info(f"\nResults: {RESULT_JSON}")
    log.info(f"Still untagged artists: /volume1/homes/homelab/genre_still_untagged.json")


if __name__ == '__main__':
    main()
