#!/usr/bin/env python3
"""
Phase 1: Library Audit Script
Scans every album folder on the NAS, reads tags from the first audio file,
classifies into Population A/B/C, scores tag completeness, and outputs a CSV report.

Read-only — makes NO changes to any files.

Usage:
    source ~/music-pipeline-env/bin/activate
    python3 ~/music-pipeline/audit.py
"""

import os
import sys
import csv
import re
import time
import logging
from pathlib import Path
from datetime import datetime

from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.apev2 import APEv2
from mutagen import File as MutagenFile

# === Config ===
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/path/to/your/music")
OUTPUT_CSV = os.path.join(os.path.dirname(__file__), "audit_report.csv")
LOG_FILE = os.path.join(os.path.dirname(__file__), "audit.log")

AUDIO_EXTENSIONS = {'.flac', '.mp3', '.m4a', '.mp4', '.ogg', '.opus', '.ape', '.wav', '.wma', '.wv'}

# === Logging ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)


def find_first_audio(folder_path):
    """Find the first audio file in a folder (recurse one level into subdirs)."""
    audio_files = []
    try:
        for entry in sorted(os.listdir(folder_path)):
            full = os.path.join(folder_path, entry)
            if os.path.isfile(full):
                ext = os.path.splitext(entry)[1].lower()
                if ext in AUDIO_EXTENSIONS:
                    audio_files.append(full)
            elif os.path.isdir(full):
                # Check one level deep (for multi-disc, nested scene folders)
                try:
                    for sub in sorted(os.listdir(full)):
                        sub_full = os.path.join(full, sub)
                        if os.path.isfile(sub_full):
                            ext = os.path.splitext(sub)[1].lower()
                            if ext in AUDIO_EXTENSIONS:
                                audio_files.append(sub_full)
                except (PermissionError, OSError):
                    pass
            if audio_files:
                return audio_files[0]
    except (PermissionError, OSError) as e:
        log.warning(f"Cannot read folder {folder_path}: {e}")
    return None


def count_audio_files(folder_path):
    """Count total audio files in folder (one level of recursion)."""
    count = 0
    try:
        for entry in os.listdir(folder_path):
            full = os.path.join(folder_path, entry)
            if os.path.isfile(full):
                ext = os.path.splitext(entry)[1].lower()
                if ext in AUDIO_EXTENSIONS:
                    count += 1
            elif os.path.isdir(full):
                try:
                    for sub in os.listdir(full):
                        sub_full = os.path.join(full, sub)
                        if os.path.isfile(sub_full):
                            ext = os.path.splitext(sub)[1].lower()
                            if ext in AUDIO_EXTENSIONS:
                                count += 1
                except (PermissionError, OSError):
                    pass
    except (PermissionError, OSError):
        pass
    return count


def read_tags(filepath):
    """Read tags from an audio file. Returns a dict of tag values (lowercase keys)."""
    tags = {}
    try:
        ext = os.path.splitext(filepath)[1].lower()
        if ext == '.flac':
            audio = FLAC(filepath)
            if audio.tags:
                for key, val in audio.tags:
                    tags[key.lower()] = val
        elif ext == '.mp3':
            audio = MP3(filepath)
            if audio.tags:
                for key, val in audio.tags.items():
                    k = key.lower()
                    if hasattr(val, 'text'):
                        tags[k] = str(val.text[0]) if val.text else ''
                    else:
                        tags[k] = str(val)
        elif ext in ('.m4a', '.mp4'):
            audio = MP4(filepath)
            if audio.tags:
                # MP4 tags use special keys
                mp4_map = {
                    '\xa9nam': 'title', '\xa9ART': 'artist', '\xa9alb': 'album',
                    '\xa9day': 'date', '\xa9gen': 'genre', 'trkn': 'tracknumber',
                    'aART': 'albumartist', 'disk': 'discnumber'
                }
                for mp4_key, tag_name in mp4_map.items():
                    if mp4_key in audio.tags:
                        val = audio.tags[mp4_key]
                        if isinstance(val, list) and val:
                            if isinstance(val[0], tuple):
                                tags[tag_name] = str(val[0][0])
                            else:
                                tags[tag_name] = str(val[0])
        else:
            # Generic fallback
            audio = MutagenFile(filepath)
            if audio and audio.tags:
                for key, val in audio.tags.items():
                    if hasattr(val, 'text'):
                        tags[key.lower()] = str(val.text[0]) if val.text else ''
                    elif isinstance(val, list) and val:
                        tags[key.lower()] = str(val[0])
                    else:
                        tags[key.lower()] = str(val)
    except Exception as e:
        log.debug(f"Failed to read tags from {filepath}: {e}")
    return tags


def has_art_file(folder_path):
    """Check if folder has album art (cover.jpg, folder.jpg, etc.)."""
    art_names = {'cover', 'folder', 'front', 'album', 'albumart', 'thumb'}
    art_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tif', '.tiff'}
    try:
        for entry in os.listdir(folder_path):
            name, ext = os.path.splitext(entry.lower())
            if ext in art_exts and (name in art_names or 'cover' in name or 'front' in name):
                return True
        # Check one level deep
        for entry in os.listdir(folder_path):
            subdir = os.path.join(folder_path, entry)
            if os.path.isdir(subdir):
                try:
                    for sub in os.listdir(subdir):
                        name, ext = os.path.splitext(sub.lower())
                        if ext in art_exts and (name in art_names or 'cover' in name or 'front' in name):
                            return True
                except (PermissionError, OSError):
                    pass
    except (PermissionError, OSError):
        pass
    return False


def has_embedded_art(filepath):
    """Check if audio file has embedded album art."""
    try:
        ext = os.path.splitext(filepath)[1].lower()
        if ext == '.flac':
            audio = FLAC(filepath)
            return bool(audio.pictures)
        elif ext == '.mp3':
            audio = MP3(filepath)
            if audio.tags:
                return any(k.startswith('APIC') for k in audio.tags.keys())
        elif ext in ('.m4a', '.mp4'):
            audio = MP4(filepath)
            if audio.tags:
                return 'covr' in audio.tags
    except Exception:
        pass
    return False


def classify_folder(folder_name):
    """Classify folder into population based on naming pattern."""
    # Population B: Scene rips
    if re.match(r'^\[AZ', folder_name):
        return 'B_scene_AZ'
    if re.match(r'^\(3AW', folder_name):
        return 'B_scene_3AW'
    if re.match(r'^\[AZH', folder_name):
        return 'B_scene_AZ'

    # Population A: Well-curated [Year] folders
    if re.match(r'^\[\d{4}\]', folder_name):
        return 'A_year_curated'

    # Population C: Everything else (bare names, timestamps, etc.)
    return 'C_bare_name'


def is_jpopru_junk(tags):
    """Check if tags are JPOP.ru garbage."""
    album = tags.get('album', '')
    comment = tags.get('comment', '')
    # JPOP.ru tags have Album=JPOP.ru and/or Comment=JPOP.ru
    if 'jpop.ru' in album.lower() or 'jpop.ru' in comment.lower():
        return True
    return False


def parse_folder_name(folder_name):
    """Extract artist, album, year from folder name patterns."""
    artist, album, year = '', '', ''

    # Pattern: [AZH8-M]_Artist_-_Album_(Year)_(FLAC)
    m = re.match(r'^\[[^\]]+\]_(.+?)_-_(.+?)_\((\d{4})', folder_name)
    if m:
        artist = m.group(1).replace('_', ' ')
        album = m.group(2).replace('_', ' ')
        # Clean up trailing format/edition info
        album = re.sub(r'\s*\((FLAC|Hi-Res.*|vinyl|Limited.*|First.*|Special.*|re-issue.*)\).*', '', album, flags=re.IGNORECASE)
        year = m.group(3)
        return artist, album, year

    # Pattern: (3AW3r)_Artist_-_Album_(Year)_(FLAC)
    m = re.match(r'^\([^)]+\)_(.+?)_-_(.+?)_\((\d{4})', folder_name)
    if m:
        artist = m.group(1).replace('_', ' ')
        album = m.group(2).replace('_', ' ')
        album = re.sub(r'\s*\((FLAC|Hi-Res.*|vinyl|Limited.*|First.*|Special.*|re-issue.*)\).*', '', album, flags=re.IGNORECASE)
        year = m.group(3)
        return artist, album, year

    # Pattern: [Year] Artist - Album
    m = re.match(r'^\[(\d{4})\]\s+(.+?)\s+-\s+(.+)', folder_name)
    if m:
        year = m.group(1)
        artist = m.group(2)
        album = m.group(3)
        return artist, album, year

    # Pattern: Year - Album or similar bare patterns
    m = re.match(r'^(\d{4})\s*[-–]\s*(.+)', folder_name)
    if m:
        year = m.group(1)
        album = m.group(2)
        return artist, album, year

    # Pattern: timestamp prefix: 20180515.2231.214 Artist - Album (Year) (FLAC)
    m = re.match(r'^\d{8}\.\d{4}\.\d+\s+(.+?)\s+-\s+(.+?)(?:\s+\((\d{4})\))?', folder_name)
    if m:
        artist = m.group(1)
        album = m.group(2)
        album = re.sub(r'\s*\((FLAC|Hi-Res.*)\).*', '', album, flags=re.IGNORECASE)
        year = m.group(3) or ''
        return artist, album, year

    return artist, album, year


def score_tags(tags, has_art):
    """Score tag completeness 0-100."""
    score = 0

    # Check for common tag key variations
    def has_tag(*keys):
        for k in keys:
            if k in tags and tags[k].strip():
                return True
        return False

    def get_tag(*keys):
        for k in keys:
            if k in tags and tags[k].strip():
                return tags[k].strip()
        return ''

    if has_tag('artist', 'tpe1'):
        score += 20
    if has_tag('album', 'talb'):
        score += 20
    if has_tag('title', 'tit2'):
        score += 15
    if has_tag('date', 'tdrc', 'year', 'tyer'):
        score += 15
    if has_tag('genre', 'tcon'):
        score += 10
    if has_tag('tracknumber', 'trck', 'track'):
        score += 10
    if has_art:
        score += 10

    return score


def get_audio_format(filepath):
    """Get audio format from file extension."""
    return os.path.splitext(filepath)[1].lstrip('.').upper()


def main():
    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    log.info(f"=== Music Library Audit Started ===")
    log.info(f"Scanning: {MUSIC_DIR}")
    log.info(f"Output: {OUTPUT_CSV}")

    # Get all top-level folders
    try:
        entries = sorted(os.listdir(MUSIC_DIR))
    except (PermissionError, OSError) as e:
        log.error(f"Cannot read music directory: {e}")
        sys.exit(1)

    folders = [e for e in entries if os.path.isdir(os.path.join(MUSIC_DIR, e)) and not e.startswith('.')]
    log.info(f"Found {len(folders)} folders to audit")

    # CSV output
    fieldnames = [
        'folder_name', 'folder_path', 'population', 'population_detail',
        'tag_score', 'is_jpopru', 'has_art_file', 'has_embedded_art',
        'tag_artist', 'tag_album', 'tag_title', 'tag_date', 'tag_genre',
        'parsed_artist', 'parsed_album', 'parsed_year',
        'track_count', 'audio_format', 'first_audio_path'
    ]

    # Stats
    stats = {
        'total': 0, 'no_audio': 0,
        'A_year_curated': 0, 'B_scene_AZ': 0, 'B_scene_3AW': 0, 'C_bare_name': 0,
        'jpopru_junk': 0, 'well_tagged': 0, 'needs_tagging': 0,
        'has_art': 0, 'no_art': 0
    }

    start_time = time.time()

    with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for i, folder_name in enumerate(folders):
            folder_path = os.path.join(MUSIC_DIR, folder_name)
            stats['total'] += 1

            if (i + 1) % 500 == 0:
                elapsed = time.time() - start_time
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                remaining = (len(folders) - i - 1) / rate if rate > 0 else 0
                log.info(f"Progress: {i+1}/{len(folders)} ({(i+1)/len(folders)*100:.1f}%) "
                        f"- {rate:.1f} folders/sec - ~{remaining/60:.0f} min remaining")

            # Classify by folder name
            population = classify_folder(folder_name)
            stats[population] = stats.get(population, 0) + 1

            # Parse folder name for metadata hints
            parsed_artist, parsed_album, parsed_year = parse_folder_name(folder_name)

            # Find first audio file
            first_audio = find_first_audio(folder_path)
            if not first_audio:
                stats['no_audio'] += 1
                writer.writerow({
                    'folder_name': folder_name,
                    'folder_path': folder_path,
                    'population': population,
                    'population_detail': 'no_audio',
                    'tag_score': 0,
                    'is_jpopru': '',
                    'has_art_file': has_art_file(folder_path),
                    'has_embedded_art': False,
                    'tag_artist': '', 'tag_album': '', 'tag_title': '',
                    'tag_date': '', 'tag_genre': '',
                    'parsed_artist': parsed_artist,
                    'parsed_album': parsed_album,
                    'parsed_year': parsed_year,
                    'track_count': 0,
                    'audio_format': '',
                    'first_audio_path': ''
                })
                continue

            # Read tags
            tags = read_tags(first_audio)
            jpopru = is_jpopru_junk(tags)
            if jpopru:
                stats['jpopru_junk'] += 1

            # Check art
            art_file = has_art_file(folder_path)
            embedded_art = has_embedded_art(first_audio)
            has_any_art = art_file or embedded_art
            if has_any_art:
                stats['has_art'] += 1
            else:
                stats['no_art'] += 1

            # Score
            tag_score = score_tags(tags, has_any_art)

            # Override classification based on actual tags
            if jpopru and population == 'A_year_curated':
                population = 'B_scene_AZ'  # Reclassify AZ* folders that look like [Year]

            if tag_score >= 60 and not jpopru:
                stats['well_tagged'] += 1
            else:
                stats['needs_tagging'] += 1

            # Extract tag values for CSV (handle key variations)
            def get_tag(*keys):
                for k in keys:
                    if k in tags and tags[k].strip():
                        return tags[k].strip()
                return ''

            track_count = count_audio_files(folder_path)
            audio_format = get_audio_format(first_audio)

            writer.writerow({
                'folder_name': folder_name,
                'folder_path': folder_path,
                'population': population,
                'population_detail': 'jpopru' if jpopru else ('well_tagged' if tag_score >= 60 else 'needs_work'),
                'tag_score': tag_score,
                'is_jpopru': jpopru,
                'has_art_file': art_file,
                'has_embedded_art': embedded_art,
                'tag_artist': get_tag('artist', 'tpe1'),
                'tag_album': get_tag('album', 'talb'),
                'tag_title': get_tag('title', 'tit2'),
                'tag_date': get_tag('date', 'tdrc', 'year', 'tyer'),
                'tag_genre': get_tag('genre', 'tcon'),
                'parsed_artist': parsed_artist,
                'parsed_album': parsed_album,
                'parsed_year': parsed_year,
                'track_count': track_count,
                'audio_format': audio_format,
                'first_audio_path': first_audio
            })

    elapsed = time.time() - start_time

    # Print summary
    log.info(f"\n{'='*60}")
    log.info(f"AUDIT COMPLETE in {elapsed/60:.1f} minutes")
    log.info(f"{'='*60}")
    log.info(f"Total folders scanned: {stats['total']}")
    log.info(f"  No audio files:     {stats['no_audio']}")
    log.info(f"")
    log.info(f"Population breakdown:")
    log.info(f"  A (curated [Year]): {stats['A_year_curated']}")
    log.info(f"  B (scene AZ*):      {stats['B_scene_AZ']}")
    log.info(f"  B (scene 3AW*):     {stats['B_scene_3AW']}")
    log.info(f"  C (bare name):      {stats['C_bare_name']}")
    log.info(f"")
    log.info(f"Tag quality:")
    log.info(f"  JPOP.ru junk tags:  {stats['jpopru_junk']}")
    log.info(f"  Well tagged (≥60):  {stats['well_tagged']}")
    log.info(f"  Needs tagging (<60):{stats['needs_tagging']}")
    log.info(f"")
    log.info(f"Album art:")
    log.info(f"  Has art:            {stats['has_art']}")
    log.info(f"  Missing art:        {stats['no_art']}")
    log.info(f"")
    log.info(f"Report saved to: {OUTPUT_CSV}")


if __name__ == '__main__':
    main()
