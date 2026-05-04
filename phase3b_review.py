#!/usr/bin/env python3
"""
Phase 3b: Re-process needs_review albums with improved folder name parsing.
- Better parser handles bare Artist_-_Album_(Year)_(FLAC) format
- Also handles timestamp prefixes like "20170602.2344.1 Artist Album"
- AcoustID fingerprinting for hash/unidentifiable folders
- Reuses tagging functions from phase3_tag.py

Usage:
    source ~/music-pipeline-env/bin/activate
    python3 ~/music-pipeline/phase3b_review.py
"""

import os
import sys
import csv
import time
import re
import logging
import subprocess
from pathlib import Path

import musicbrainzngs
import acoustid
from mutagen.flac import FLAC, Picture
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen import File as MutagenFile
import requests

# === Config ===
NEEDS_REVIEW_CSV = os.path.join(os.path.dirname(__file__), "needs_review.csv")
LOG_FILE = os.path.join(os.path.dirname(__file__), "phase3b_review.log")
RESULT_CSV = os.path.join(os.path.dirname(__file__), "phase3b_results.csv")
STILL_UNMATCHED_CSV = os.path.join(os.path.dirname(__file__), "still_unmatched.csv")

ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "YOUR_KEY_HERE")
MB_RATE_LIMIT = 1.1
ACOUSTID_RATE_LIMIT = 0.35

AUDIO_EXTENSIONS = {'.flac', '.mp3', '.m4a', '.mp4', '.ogg', '.opus', '.ape', '.wav'}
CAA_BASE = "https://coverartarchive.org/release"

# === Setup ===
musicbrainzngs.set_useragent("MusicTaggingPipeline", "1.0", "your@email.com")
musicbrainzngs.set_rate_limit(False)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)


def verify_ssl_connectivity():
    """Fail fast if SSL certs are broken."""
    for attempt in range(2):
        try:
            musicbrainzngs.search_releases(artist='test', release='test', limit=1)
            log.info("SSL/MusicBrainz connectivity OK")
            return True
        except Exception as e:
            if attempt == 0:
                log.warning(f"Connectivity check failed (attempt 1), retrying in 5s: {e}")
                time.sleep(5)
            else:
                log.error(f"FATAL: MusicBrainz API call failed after 2 attempts: {e}")
                sys.exit(1)


def parse_folder_name_improved(folder_name):
    """Improved folder name parser that handles more patterns."""
    artist, album, year = '', '', ''

    # Strip trailing hash IDs like (H13MKA431BYDWF)
    clean = re.sub(r'\s*\([A-Z0-9]{10,}\)$', '', folder_name)

    # Pattern: (prefix)_Artist_-_Album_(Year)_(FLAC) — scene rips
    m = re.match(r'^\([^)]+\)_(.+?)_-_(.+?)(?:_\((\d{4}))', clean)
    if m:
        artist = m.group(1).replace('_', ' ')
        album = m.group(2).replace('_', ' ')
        album = re.sub(r'\s*\((FLAC|Hi-Res.*|vinyl|web.*)\).*$', '', album, flags=re.IGNORECASE)
        year = m.group(3) or ''
        return artist, album, year

    # Pattern: (prefix)_Artist_-_Album_(FLAC) — scene rips without year
    m = re.match(r'^\([^)]+\)_(.+?)_-_(.+?)_\((?:FLAC|Hi-Res)', clean)
    if m:
        artist = m.group(1).replace('_', ' ')
        album = m.group(2).replace('_', ' ')
        album = re.sub(r'\s*\((FLAC|Hi-Res.*|vinyl|web.*)\).*$', '', album, flags=re.IGNORECASE)
        return artist, album, year

    # Pattern: Artist_-_Album_(edition)_(Year)_(FLAC) — bare underscore with edition
    # Use rightmost (YYYY) as year to avoid grabbing re-issue years from album name
    m = re.match(r'^(.+?)_-_(.+)', clean)
    if m and re.search(r'\(\d{4}\)', m.group(2)):
        artist = m.group(1).replace('_', ' ')
        album_raw = m.group(2).replace('_', ' ')
        # Find rightmost year
        year_matches = list(re.finditer(r'\((\d{4})\)', album_raw))
        if year_matches:
            year = year_matches[-1].group(1)
            # Remove from the year onwards (year, format, hash suffix)
            year_pos = year_matches[-1].start()
            album = album_raw[:year_pos].strip()
        else:
            album = album_raw
        album = re.sub(r'\s*\((FLAC|Hi-Res.*|vinyl|web.*)\).*$', '', album, flags=re.IGNORECASE)
        # Clean trailing edition info
        album = re.sub(r'\s*\((re-issue|reissue|remaster).*$', '', album, flags=re.IGNORECASE)
        return artist, album, year

    # Pattern: Artist_-_Album_(FLAC) — bare underscore without year
    m = re.match(r'^(.+?)_-_(.+?)_\((?:FLAC|Hi-Res)', clean)
    if m:
        artist = m.group(1).replace('_', ' ')
        album = m.group(2).replace('_', ' ')
        album = re.sub(r'\s*\((FLAC|Hi-Res.*|vinyl|web.*)\).*$', '', album, flags=re.IGNORECASE)
        return artist, album, year

    # Pattern: Artist_-_Album_(Type_X)_(FLAC) — with type suffix, no year
    m = re.match(r'^(.+?)_-_(.+?)_\((Type_[A-Z]|Regular.*|Special.*|Limited.*)\)', clean)
    if m:
        artist = m.group(1).replace('_', ' ')
        album = m.group(2).replace('_', ' ')
        return artist, album, year

    # Pattern: "Artist - Album (Year) (FLAC)" with spaces
    m = re.match(r'^(.+?)\s+-\s+(.+?)(?:\s+\((\d{4})\))?(?:\s+\((?:FLAC|Hi-Res))', clean)
    if m:
        artist = m.group(1)
        album = m.group(2)
        album = re.sub(r'\s*\((?:FLAC|Hi-Res.*)\).*$', '', album, flags=re.IGNORECASE)
        year = m.group(3) or ''
        return artist, album, year

    # Pattern: "Artist - Album (Year)" or "Artist - Album" with spaces, no format tag
    m = re.match(r'^(.+?)\s+-\s+(.+?)(?:\s+\((\d{4})\))?$', clean)
    if m:
        artist = m.group(1)
        album = m.group(2)
        year = m.group(3) or ''
        return artist, album, year

    # Pattern: timestamp prefix: "20180515.2231.4 Artist Album"
    m = re.match(r'^\d{8}\.\d{4}\.\d+\s+(.+)', clean)
    if m:
        remainder = m.group(1)
        # Try to split on " - " first
        parts = remainder.split(' - ', 1)
        if len(parts) == 2:
            artist = parts[0].strip()
            album = parts[1].strip()
        else:
            # Try space-separated: assume first word(s) are artist
            # e.g. "Tomoko Aran Fuyuu Kuukan" — hard to split, just use whole thing as album
            album = remainder.strip()
        # Extract year if present
        ym = re.search(r'\((\d{4})\)', album)
        if ym:
            year = ym.group(1)
            album = re.sub(r'\s*\(\d{4}\).*', '', album)
        return artist, album, year

    # Pattern: bare name with underscores "Artist_-_Album"
    m = re.match(r'^(.+?)_-_(.+)', clean)
    if m:
        artist = m.group(1).replace('_', ' ')
        album = m.group(2).replace('_', ' ')
        album = re.sub(r'\s*\((FLAC|Hi-Res.*|vinyl|web.*)\).*$', '', album, flags=re.IGNORECASE)
        ym = re.search(r'\((\d{4})\)', album)
        if ym:
            year = ym.group(1)
            album = re.sub(r'\s*\(\d{4}\).*', '', album)
        return artist, album, year

    return artist, album, year


def get_audio_files(folder_path):
    """Get all audio files in a folder (one level of recursion), sorted."""
    files = []
    try:
        for entry in sorted(os.listdir(folder_path)):
            if entry.startswith('._'):
                continue
            full = os.path.join(folder_path, entry)
            if os.path.isfile(full):
                ext = os.path.splitext(entry)[1].lower()
                if ext in AUDIO_EXTENSIONS:
                    files.append(full)
            elif os.path.isdir(full):
                try:
                    for sub in sorted(os.listdir(full)):
                        if sub.startswith('._'):
                            continue
                        subfull = os.path.join(full, sub)
                        if os.path.isfile(subfull):
                            ext = os.path.splitext(sub)[1].lower()
                            if ext in AUDIO_EXTENSIONS:
                                files.append(subfull)
                except (PermissionError, OSError):
                    pass
    except (PermissionError, OSError):
        pass
    return files


def read_existing_tags(filepath):
    """Read existing tags from an audio file to help identify it."""
    try:
        audio = MutagenFile(filepath, easy=True)
        if audio and audio.tags:
            tags = {}
            for key in ('artist', 'albumartist', 'album', 'title', 'date'):
                val = audio.tags.get(key)
                if val:
                    tags[key] = val[0] if isinstance(val, list) else str(val)
            return tags
    except Exception:
        pass
    return {}


def fingerprint_file(filepath):
    """Get AcoustID fingerprint matches."""
    try:
        results = acoustid.match(ACOUSTID_API_KEY, filepath)
        matches = []
        for score, recording_id, title, artist in results:
            matches.append((score, recording_id, title, artist))
        return matches
    except Exception as e:
        log.warning(f"Fingerprint failed for {filepath}: {e}")
        return []


def search_mb_by_text(artist, album):
    """Search MusicBrainz by artist+album text."""
    try:
        time.sleep(MB_RATE_LIMIT)
        result = musicbrainzngs.search_releases(
            artist=artist, release=album, limit=5
        )
        if result.get('release-list'):
            for rel in result['release-list']:
                score = int(rel.get('ext:score', 0))
                if score >= 75:
                    return rel['id'], score
        return None, 0
    except Exception as e:
        log.warning(f"MB text search failed for {artist} - {album}: {e}")
        return None, 0


def find_release_from_recordings(recording_ids):
    """Given recording IDs, find the most common release."""
    release_counts = {}
    for rec_id in recording_ids:
        try:
            time.sleep(MB_RATE_LIMIT)
            result = musicbrainzngs.get_recording_by_id(rec_id, includes=['releases'])
            for rel in result.get('recording', {}).get('release-list', []):
                rid = rel['id']
                release_counts[rid] = release_counts.get(rid, 0) + 1
        except Exception as e:
            log.warning(f"MB recording lookup failed for {rec_id}: {e}")
    if release_counts:
        best = max(release_counts, key=release_counts.get)
        return best, release_counts[best]
    return None, 0


def get_release_tracks(release_id):
    """Get full track listing from a MusicBrainz release."""
    try:
        time.sleep(MB_RATE_LIMIT)
        result = musicbrainzngs.get_release_by_id(
            release_id,
            includes=['recordings', 'artist-credits', 'release-groups', 'labels', 'media']
        )
        return result.get('release', {})
    except Exception as e:
        log.warning(f"MB release lookup failed for {release_id}: {e}")
        return None


def extract_artist_credit(artist_credit):
    if not artist_credit:
        return ''
    parts = []
    for credit in artist_credit:
        if isinstance(credit, dict):
            parts.append(credit.get('artist', {}).get('name', ''))
            parts.append(credit.get('joinphrase', ''))
        elif isinstance(credit, str):
            parts.append(credit)
    return ''.join(parts).strip()


def extract_artist_sort(artist_credit):
    if not artist_credit:
        return ''
    for credit in artist_credit:
        if isinstance(credit, dict):
            return credit.get('artist', {}).get('sort-name', '')
    return ''


def parse_track_info_from_filename(filename):
    name = os.path.splitext(filename)[0]
    m = re.match(r'^(\d{1,3})[\.\-\s]+(.+)', name)
    if m:
        return int(m.group(1)), m.group(2).strip()
    m = re.match(r'^\d-(\d{1,3})[\.\-\s]+(.+)', name)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None, name


def write_tags_to_file(filepath, tags_dict):
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == '.flac':
            audio = FLAC(filepath)
            for key, val in tags_dict.items():
                if val:
                    audio[key] = str(val)
            audio.save()
            return True
        elif ext == '.mp3':
            from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, TRCK, TPE2, TSOP
            audio = MP3(filepath)
            if audio.tags is None:
                audio.add_tags()
            tag_map = {
                'title': lambda v: TIT2(encoding=3, text=[v]),
                'artist': lambda v: TPE1(encoding=3, text=[v]),
                'album': lambda v: TALB(encoding=3, text=[v]),
                'date': lambda v: TDRC(encoding=3, text=[v]),
                'genre': lambda v: TCON(encoding=3, text=[v]),
                'tracknumber': lambda v: TRCK(encoding=3, text=[v]),
                'albumartist': lambda v: TPE2(encoding=3, text=[v]),
                'artistsort': lambda v: TSOP(encoding=3, text=[v]),
            }
            for key, val in tags_dict.items():
                if val and key in tag_map:
                    audio.tags.add(tag_map[key](str(val)))
            audio.save()
            return True
        else:
            audio = MutagenFile(filepath)
            if audio and audio.tags is not None:
                for key, val in tags_dict.items():
                    if val:
                        audio.tags[key] = str(val)
                audio.save()
                return True
    except Exception as e:
        log.warning(f"Failed to write tags to {filepath}: {e}")
    return False


def tag_album_from_release(audio_files, release, folder_path):
    if not release:
        return 0

    album = release.get('title', '')
    artist_credit = release.get('artist-credit', [])
    album_artist = extract_artist_credit(artist_credit)
    artist_sort = extract_artist_sort(artist_credit)
    date = release.get('date', '')
    year = date[:4] if date else ''
    release_group = release.get('release-group', {})

    label = ''
    catalog = ''
    label_info = release.get('label-info-list', [])
    if label_info:
        li = label_info[0]
        label = li.get('label', {}).get('name', '') if li.get('label') else ''
        catalog = li.get('catalog-number', '')

    mb_tracks = []
    media_list = release.get('medium-list', [])
    for medium in media_list:
        disc_num = medium.get('position', 1)
        disc_total = len(media_list)
        for track in medium.get('track-list', []):
            rec = track.get('recording', {})
            track_artist = extract_artist_credit(rec.get('artist-credit', []))
            mb_tracks.append({
                'position': int(track.get('position', 0)),
                'title': rec.get('title', ''),
                'artist': track_artist,
                'recording_id': rec.get('id', ''),
                'disc': int(disc_num),
                'disc_total': int(disc_total),
            })

    tagged = 0
    for filepath in audio_files:
        filename = os.path.basename(filepath)
        track_num, file_title = parse_track_info_from_filename(filename)

        mb_track = None
        if track_num and track_num <= len(mb_tracks):
            mb_track = mb_tracks[track_num - 1]
        elif track_num:
            for t in mb_tracks:
                if t['position'] == track_num:
                    mb_track = t
                    break

        tags = {
            'album': album,
            'albumartist': album_artist,
            'artistsort': artist_sort,
            'date': year,
        }

        if mb_track:
            tags['title'] = mb_track['title']
            tags['artist'] = mb_track['artist'] or album_artist
            tags['tracknumber'] = str(mb_track['position'])
            tags['discnumber'] = str(mb_track['disc'])
            tags['disctotal'] = str(mb_track['disc_total'])
            tags['tracktotal'] = str(len([t for t in mb_tracks if t['disc'] == mb_track['disc']]))
            if mb_track['recording_id']:
                tags['musicbrainz_trackid'] = mb_track['recording_id']
        else:
            if track_num:
                tags['tracknumber'] = str(track_num)
            tags['title'] = file_title
            tags['artist'] = album_artist

        tags['musicbrainz_albumid'] = release.get('id', '')
        if label:
            tags['organization'] = label
        if catalog:
            tags['catalognumber'] = catalog

        if write_tags_to_file(filepath, tags):
            tagged += 1

    return tagged


def write_baseline_tags(audio_files, parsed_artist, parsed_album, parsed_year):
    tagged = 0
    for filepath in audio_files:
        filename = os.path.basename(filepath)
        track_num, file_title = parse_track_info_from_filename(filename)

        tags = {}
        if parsed_artist:
            tags['artist'] = parsed_artist
            tags['albumartist'] = parsed_artist
        if parsed_album:
            tags['album'] = parsed_album
        if parsed_year:
            tags['date'] = parsed_year
        if track_num:
            tags['tracknumber'] = str(track_num)
        if file_title:
            tags['title'] = file_title

        if tags and write_tags_to_file(filepath, tags):
            tagged += 1
    return tagged


def download_cover_art(release_id, save_path):
    url = f"{CAA_BASE}/{release_id}/front-500"
    try:
        resp = requests.get(url, timeout=30, allow_redirects=True)
        if resp.status_code == 200:
            ext = '.jpg'
            if 'png' in resp.headers.get('content-type', ''):
                ext = '.png'
            filepath = os.path.join(save_path, f"cover{ext}")
            with open(filepath, 'wb') as f:
                f.write(resp.content)
            return True
    except Exception:
        pass
    return False


def main():
    log.info("=== Phase 3b: Re-process Needs Review Albums ===")

    verify_ssl_connectivity()

    # Load needs_review albums
    targets = []
    with open(NEEDS_REVIEW_CSV) as f:
        reader = csv.DictReader(f)
        for row in reader:
            targets.append(row)

    log.info(f"Loaded {len(targets)} albums from needs_review.csv")

    # Re-parse folder names with improved parser
    parsed_count = 0
    for row in targets:
        artist, album, year = parse_folder_name_improved(row['folder_name'])
        row['parsed_artist'] = artist
        row['parsed_album'] = album
        row['parsed_year'] = year
        if artist or album:
            parsed_count += 1

    log.info(f"Improved parser identified {parsed_count}/{len(targets)} albums")

    # For albums where parser still fails, try reading embedded tags
    tag_read_count = 0
    for row in targets:
        if not row['parsed_artist'] and not row['parsed_album']:
            folder_path = row['folder_path']
            audio_files = get_audio_files(folder_path)
            if audio_files:
                tags = read_existing_tags(audio_files[0])
                if tags.get('artist') or tags.get('albumartist'):
                    row['parsed_artist'] = tags.get('albumartist') or tags.get('artist', '')
                if tags.get('album'):
                    row['parsed_album'] = tags.get('album', '')
                if tags.get('date'):
                    row['parsed_year'] = tags['date'][:4]
                if row['parsed_artist'] or row['parsed_album']:
                    tag_read_count += 1
                    log.info(f"Read tags from files: {row['folder_name']} -> {row['parsed_artist']} - {row['parsed_album']}")

    log.info(f"Read embedded tags for {tag_read_count} additional albums")

    stats = {
        'total': len(targets),
        'mb_text_match': 0,
        'mb_fingerprint_match': 0,
        'baseline_only': 0,
        'no_match': 0,
        'errors': 0,
        'tracks_tagged': 0,
        'art_fetched': 0,
    }

    start_time = time.time()
    result_fields = [
        'folder_name', 'parsed_artist', 'parsed_album', 'parsed_year',
        'match_method', 'mb_release_id', 'mb_artist', 'mb_album',
        'tracks_tagged', 'art_fetched', 'status'
    ]

    results_file = open(RESULT_CSV, 'w', newline='', encoding='utf-8')
    results_writer = csv.DictWriter(results_file, fieldnames=result_fields)
    results_writer.writeheader()

    unmatched_file = open(STILL_UNMATCHED_CSV, 'w', newline='', encoding='utf-8')
    unmatched_writer = csv.DictWriter(unmatched_file, fieldnames=[
        'folder_name', 'folder_path', 'parsed_artist', 'parsed_album',
        'parsed_year', 'track_count', 'reason'
    ])
    unmatched_writer.writeheader()

    for i, row in enumerate(targets):
        if (i + 1) % 25 == 0:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            remaining = (len(targets) - i - 1) / rate if rate > 0 else 0
            log.info(f"Progress: {i+1}/{len(targets)} ({(i+1)/len(targets)*100:.1f}%) "
                    f"- Text: {stats['mb_text_match']}, FP: {stats['mb_fingerprint_match']}, "
                    f"Baseline: {stats['baseline_only']}, NoMatch: {stats['no_match']} "
                    f"- ~{remaining/60:.0f} min remaining")

        folder_path = row['folder_path']
        folder_name = row['folder_name']
        parsed_artist = row['parsed_artist']
        parsed_album = row['parsed_album']
        parsed_year = row.get('parsed_year', '')

        audio_files = get_audio_files(folder_path)
        if not audio_files:
            stats['errors'] += 1
            log.warning(f"No audio files found: {folder_name}")
            continue

        release = None
        match_method = 'none'

        # === Strategy 1: Text search if we have artist + album ===
        if parsed_artist and parsed_album:
            release_id, score = search_mb_by_text(parsed_artist, parsed_album)
            if release_id:
                release = get_release_tracks(release_id)
                if release:
                    match_method = 'text'
                    stats['mb_text_match'] += 1

        # === Strategy 2: AcoustID fingerprinting ===
        if not release:
            recording_ids = set()
            for fp_file in audio_files[:2]:
                time.sleep(ACOUSTID_RATE_LIMIT)
                try:
                    matches = fingerprint_file(fp_file)
                    for score, rec_id, title, artist in matches:
                        if score >= 0.6 and rec_id:
                            recording_ids.add(rec_id)
                            break
                except Exception as e:
                    log.warning(f"Fingerprint error: {e}")

            if recording_ids:
                release_id, count = find_release_from_recordings(recording_ids)
                if release_id:
                    release = get_release_tracks(release_id)
                    if release:
                        match_method = 'fingerprint'
                        stats['mb_fingerprint_match'] += 1

        # === Apply tags ===
        if release:
            tagged = tag_album_from_release(audio_files, release, folder_path)
            stats['tracks_tagged'] += tagged

            art = download_cover_art(release.get('id', ''), folder_path)
            if art:
                stats['art_fetched'] += 1

            mb_artist = extract_artist_credit(release.get('artist-credit', []))
            results_writer.writerow({
                'folder_name': folder_name,
                'parsed_artist': parsed_artist,
                'parsed_album': parsed_album,
                'parsed_year': parsed_year,
                'match_method': match_method,
                'mb_release_id': release.get('id', ''),
                'mb_artist': mb_artist,
                'mb_album': release.get('title', ''),
                'tracks_tagged': tagged,
                'art_fetched': art,
                'status': 'tagged'
            })
        else:
            # Write baseline tags from parsed name
            if parsed_artist or parsed_album:
                tagged = write_baseline_tags(audio_files, parsed_artist, parsed_album, parsed_year)
                stats['tracks_tagged'] += tagged
                stats['baseline_only'] += 1
                status = 'baseline_tags'
            else:
                stats['no_match'] += 1
                status = 'no_match'
                tagged = 0

            results_writer.writerow({
                'folder_name': folder_name,
                'parsed_artist': parsed_artist,
                'parsed_album': parsed_album,
                'parsed_year': parsed_year,
                'match_method': 'none',
                'mb_release_id': '',
                'mb_artist': '',
                'mb_album': '',
                'tracks_tagged': tagged,
                'art_fetched': False,
                'status': status
            })

            unmatched_writer.writerow({
                'folder_name': folder_name,
                'folder_path': folder_path,
                'parsed_artist': parsed_artist,
                'parsed_album': parsed_album,
                'parsed_year': parsed_year,
                'track_count': len(audio_files),
                'reason': 'no_mb_match' if parsed_artist else 'no_metadata'
            })

    results_file.close()
    unmatched_file.close()

    elapsed = time.time() - start_time
    log.info(f"\n{'='*60}")
    log.info(f"PHASE 3b COMPLETE in {elapsed/60:.1f} minutes")
    log.info(f"{'='*60}")
    log.info(f"Total albums processed: {stats['total']}")
    log.info(f"  MB text match:       {stats['mb_text_match']}")
    log.info(f"  MB fingerprint match:{stats['mb_fingerprint_match']}")
    log.info(f"  Baseline tags only:  {stats['baseline_only']}")
    log.info(f"  No match at all:     {stats['no_match']}")
    log.info(f"  Errors:              {stats['errors']}")
    log.info(f"  Total tracks tagged: {stats['tracks_tagged']}")
    log.info(f"  Album art fetched:   {stats['art_fetched']}")
    log.info(f"")
    log.info(f"Results: {RESULT_CSV}")
    log.info(f"Still unmatched: {STILL_UNMATCHED_CSV}")


if __name__ == '__main__':
    main()
