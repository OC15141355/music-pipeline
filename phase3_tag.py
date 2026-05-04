#!/usr/bin/env python3
"""
Phase 3: Tag albums that need work
- Parse folder names for artist/album/year hints
- Try MusicBrainz text search first (fast, no fingerprinting needed)
- Fall back to AcoustID fingerprinting if text search fails
- Write tags from MB data for matches
- Write baseline tags from folder name for non-matches
- Flag non-matches for manual review

Usage:
    source ~/music-pipeline-env/bin/activate
    python3 ~/music-pipeline/phase3_tag.py
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
AUDIT_CSV = os.path.join(os.path.dirname(__file__), "audit_report.csv")
LOG_FILE = os.path.join(os.path.dirname(__file__), "phase3_tag.log")
RESULT_CSV = os.path.join(os.path.dirname(__file__), "phase3_results.csv")
NEEDS_REVIEW_CSV = os.path.join(os.path.dirname(__file__), "needs_review.csv")

ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "YOUR_KEY_HERE")
MB_RATE_LIMIT = 1.1  # seconds between MB API calls
ACOUSTID_RATE_LIMIT = 0.35  # 3 req/sec for AcoustID

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
    """Fail fast if SSL certs are broken — don't waste hours silently failing."""
    for attempt in range(2):
        try:
            result = musicbrainzngs.search_releases(artist='test', release='test', limit=1)
            log.info("SSL/MusicBrainz connectivity OK")
            return True
        except Exception as e:
            if attempt == 0:
                log.warning(f"Connectivity check failed (attempt 1), retrying in 5s: {e}")
                time.sleep(5)
            else:
                log.error(f"FATAL: MusicBrainz API call failed after 2 attempts: {e}")
                log.error("Check network or SSL certs: ln -sf $(python3 -c 'import certifi; print(certifi.where())') /usr/local/etc/openssl@3/cert.pem")
                sys.exit(1)


def get_audio_files(folder_path):
    """Get all audio files in a folder (one level of recursion), sorted."""
    files = []
    try:
        for entry in sorted(os.listdir(folder_path)):
            full = os.path.join(folder_path, entry)
            if os.path.isfile(full):
                ext = os.path.splitext(entry)[1].lower()
                if ext in AUDIO_EXTENSIONS:
                    files.append(full)
            elif os.path.isdir(full):
                try:
                    for sub in sorted(os.listdir(full)):
                        sub_full = os.path.join(full, sub)
                        if os.path.isfile(sub_full):
                            ext = os.path.splitext(sub)[1].lower()
                            if ext in AUDIO_EXTENSIONS:
                                files.append(sub_full)
                except (PermissionError, OSError):
                    pass
    except (PermissionError, OSError):
        pass
    return files


def fingerprint_file(filepath):
    """Get AcoustID fingerprint for a file. Returns list of (score, recording_id, title, artist)."""
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
    """Search MusicBrainz by artist+album text. Returns (release_id, score) or (None, 0)."""
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
    """Given a set of recording IDs, find the most common release they belong to."""
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
        # Return the release that contains the most of our recordings
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
        release = result.get('release', {})
        return release
    except Exception as e:
        log.warning(f"MB release lookup failed for {release_id}: {e}")
        return None


def extract_artist_credit(artist_credit):
    """Extract artist name from MB artist-credit structure."""
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
    """Extract sort name from MB artist-credit."""
    if not artist_credit:
        return ''
    for credit in artist_credit:
        if isinstance(credit, dict):
            return credit.get('artist', {}).get('sort-name', '')
    return ''


def write_tags_to_file(filepath, tags_dict):
    """Write tags to an audio file. Only writes FLAC for now (99.6% of library)."""
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
            # Generic fallback - try mutagen
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


def download_cover_art(release_id, save_path):
    """Download front cover from CAA."""
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


def parse_track_info_from_filename(filename):
    """Extract track number and title from filename."""
    name = os.path.splitext(filename)[0]
    # Try common patterns
    # "01. Title" or "01 - Title" or "01 Title"
    m = re.match(r'^(\d{1,3})[\.\-\s]+(.+)', name)
    if m:
        return int(m.group(1)), m.group(2).strip()
    # "Disc-Track. Title" like "1-01. Title"
    m = re.match(r'^\d-(\d{1,3})[\.\-\s]+(.+)', name)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None, name


def tag_album_from_release(audio_files, release, folder_path):
    """Write tags to all audio files from a MusicBrainz release."""
    if not release:
        return 0

    # Extract release-level info
    album = release.get('title', '')
    artist_credit = release.get('artist-credit', [])
    album_artist = extract_artist_credit(artist_credit)
    artist_sort = extract_artist_sort(artist_credit)
    date = release.get('date', '')
    year = date[:4] if date else ''
    release_group = release.get('release-group', {})
    rg_type = release_group.get('primary-type', '')

    # Get label info
    label = ''
    catalog = ''
    label_info = release.get('label-info-list', [])
    if label_info:
        li = label_info[0]
        label = li.get('label', {}).get('name', '') if li.get('label') else ''
        catalog = li.get('catalog-number', '')

    # Build track list from all media
    mb_tracks = []
    media_list = release.get('medium-list', [])
    for medium in media_list:
        disc_num = medium.get('position', 1)
        disc_total = len(media_list)
        for track in medium.get('track-list', []):
            recording = track.get('recording', {})
            track_artist_credit = recording.get('artist-credit', artist_credit)
            mb_tracks.append({
                'position': int(track.get('position', track.get('number', 0))),
                'title': recording.get('title', ''),
                'artist': extract_artist_credit(track_artist_credit),
                'disc': disc_num,
                'disc_total': disc_total,
                'recording_id': recording.get('id', ''),
            })

    # Match our files to MB tracks by position
    tagged = 0
    for filepath in audio_files:
        filename = os.path.basename(filepath)
        track_num, file_title = parse_track_info_from_filename(filename)

        # Find matching MB track
        mb_track = None
        if track_num and track_num <= len(mb_tracks):
            mb_track = mb_tracks[track_num - 1]  # 0-indexed
        elif track_num:
            # Try exact position match
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
            # Use what we parsed from filename
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
    """Write minimal tags from folder name parsing. Better than nothing."""
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


def load_previous_results():
    """Load previous run results to skip already-matched albums on rerun."""
    matched = set()
    if os.path.exists(RESULT_CSV):
        with open(RESULT_CSV) as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('match_method') in ('text', 'fingerprint'):
                    matched.add(row['folder_name'])
    return matched


def main():
    rerun = '--rerun' in sys.argv
    log.info("=== Phase 3: Tag Albums That Need Work ===")
    if rerun:
        log.info("RERUN MODE: Only re-processing albums without MB matches")

    # Verify connectivity before starting
    verify_ssl_connectivity()

    # Load previous results if rerunning
    already_matched = load_previous_results() if rerun else set()

    # Load audit data
    targets = []
    with open(AUDIT_CSV) as f:
        reader = csv.DictReader(f)
        for row in reader:
            score = int(row['tag_score'])
            has_audio = int(row['track_count']) > 0
            if score < 60 and has_audio:
                if rerun and row['folder_name'] in already_matched:
                    continue
                targets.append(row)

    log.info(f"Found {len(targets)} albums to tag")

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

    review_file = open(NEEDS_REVIEW_CSV, 'w', newline='', encoding='utf-8')
    review_writer = csv.DictWriter(review_file, fieldnames=[
        'folder_name', 'folder_path', 'parsed_artist', 'parsed_album',
        'parsed_year', 'track_count', 'reason'
    ])
    review_writer.writeheader()

    for i, row in enumerate(targets):
        if (i + 1) % 50 == 0:
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
        parsed_year = row['parsed_year']

        audio_files = get_audio_files(folder_path)
        if not audio_files:
            stats['errors'] += 1
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

        # === Strategy 2: AcoustID fingerprinting (first 2 tracks) ===
        if not release:
            recording_ids = set()
            for fp_file in audio_files[:2]:
                time.sleep(ACOUSTID_RATE_LIMIT)
                try:
                    matches = fingerprint_file(fp_file)
                    for score, rec_id, title, artist in matches:
                        if score >= 0.6 and rec_id:
                            recording_ids.add(rec_id)
                            break  # Take best match per file
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

            # Try to fetch art
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
            # Write baseline tags from folder name
            if parsed_artist or parsed_album:
                tagged = write_baseline_tags(audio_files, parsed_artist, parsed_album, parsed_year)
                stats['tracks_tagged'] += tagged
                stats['baseline_only'] += 1
                status = 'baseline_tags'
            else:
                stats['no_match'] += 1
                status = 'no_match'

            results_writer.writerow({
                'folder_name': folder_name,
                'parsed_artist': parsed_artist,
                'parsed_album': parsed_album,
                'parsed_year': parsed_year,
                'match_method': 'none',
                'mb_release_id': '',
                'mb_artist': '',
                'mb_album': '',
                'tracks_tagged': tagged if (parsed_artist or parsed_album) else 0,
                'art_fetched': False,
                'status': status
            })

            # Add to review list
            review_writer.writerow({
                'folder_name': folder_name,
                'folder_path': folder_path,
                'parsed_artist': parsed_artist,
                'parsed_album': parsed_album,
                'parsed_year': parsed_year,
                'track_count': len(audio_files),
                'reason': 'no_mb_match' if parsed_artist else 'no_metadata'
            })

    results_file.close()
    review_file.close()

    elapsed = time.time() - start_time
    log.info(f"\n{'='*60}")
    log.info(f"PHASE 3 COMPLETE in {elapsed/60:.1f} minutes ({elapsed/3600:.1f} hours)")
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
    log.info(f"Needs review: {NEEDS_REVIEW_CSV}")


if __name__ == '__main__':
    main()
