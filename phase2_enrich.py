#!/usr/bin/env python3
"""
Phase 2: Enrich well-tagged albums
- Fetch missing album art for albums that already have good tags
- Uses MusicBrainz text search + Cover Art Archive
- Does NOT overwrite any existing tags

Usage:
    source ~/music-pipeline-env/bin/activate
    python3 ~/music-pipeline/phase2_enrich.py
"""

import os
import sys
import csv
import time
import logging
import requests
from pathlib import Path

import musicbrainzngs

# === Config ===
AUDIT_CSV = os.path.join(os.path.dirname(__file__), "audit_report.csv")
LOG_FILE = os.path.join(os.path.dirname(__file__), "phase2_enrich.log")
RESULT_CSV = os.path.join(os.path.dirname(__file__), "phase2_results.csv")

MB_RATE_LIMIT = 1.1  # seconds between MB requests (be polite)
CAA_BASE = "https://coverartarchive.org/release"

# === Setup ===
musicbrainzngs.set_useragent("MusicTaggingPipeline", "1.0", "your@email.com")
musicbrainzngs.set_rate_limit(False)  # We handle rate limiting ourselves

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
    """Fail fast if SSL certs are broken. Retries once for transient errors."""
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


def search_mb_release(artist, album):
    """Search MusicBrainz for a release by artist and album name."""
    try:
        result = musicbrainzngs.search_releases(
            artist=artist, release=album, limit=5
        )
        if result.get('release-list'):
            for rel in result['release-list']:
                score = int(rel.get('ext:score', 0))
                if score >= 80:
                    return rel['id'], score
        return None, 0
    except Exception as e:
        log.warning(f"MB search failed for {artist} - {album}: {e}")
        return None, 0


def download_cover_art(release_id, save_path):
    """Download front cover art from Cover Art Archive."""
    url = f"{CAA_BASE}/{release_id}/front-500"
    try:
        resp = requests.get(url, timeout=30, allow_redirects=True)
        if resp.status_code == 200:
            content_type = resp.headers.get('content-type', '')
            ext = '.jpg'
            if 'png' in content_type:
                ext = '.png'
            filepath = os.path.join(save_path, f"cover{ext}")
            with open(filepath, 'wb') as f:
                f.write(resp.content)
            return True, filepath
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


def main():
    log.info("=== Phase 2: Enrich Well-Tagged Albums (Art Fetch) ===")

    # Verify connectivity before starting
    verify_ssl_connectivity()

    # Load audit data - only well-tagged albums missing art
    targets = []
    with open(AUDIT_CSV) as f:
        reader = csv.DictReader(f)
        for row in reader:
            score = int(row['tag_score'])
            has_art = row['has_art_file'] == 'True' or row['has_embedded_art'] == 'True'
            has_audio = int(row['track_count']) > 0
            if score >= 60 and not has_art and has_audio:
                targets.append(row)

    log.info(f"Found {len(targets)} well-tagged albums missing art")

    results = []
    found = 0
    not_found = 0
    errors = 0
    start_time = time.time()

    fieldnames = ['folder_name', 'artist', 'album', 'status', 'mb_release_id', 'mb_score', 'art_path', 'error']

    with open(RESULT_CSV, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for i, row in enumerate(targets):
            if (i + 1) % 100 == 0:
                elapsed = time.time() - start_time
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                remaining = (len(targets) - i - 1) / rate if rate > 0 else 0
                log.info(f"Progress: {i+1}/{len(targets)} ({(i+1)/len(targets)*100:.1f}%) "
                        f"- Found: {found}, Not found: {not_found} "
                        f"- ~{remaining/60:.0f} min remaining")

            artist = row['tag_artist'] or row['parsed_artist']
            album = row['tag_album'] or row['parsed_album']
            folder_path = row['folder_path']

            if not artist or not album:
                not_found += 1
                writer.writerow({
                    'folder_name': row['folder_name'], 'artist': artist, 'album': album,
                    'status': 'skip_no_metadata', 'mb_release_id': '', 'mb_score': 0,
                    'art_path': '', 'error': 'No artist or album to search'
                })
                continue

            # Search MusicBrainz
            time.sleep(MB_RATE_LIMIT)
            release_id, score = search_mb_release(artist, album)

            if not release_id:
                not_found += 1
                writer.writerow({
                    'folder_name': row['folder_name'], 'artist': artist, 'album': album,
                    'status': 'no_mb_match', 'mb_release_id': '', 'mb_score': score,
                    'art_path': '', 'error': ''
                })
                continue

            # Download art from CAA
            success, result = download_cover_art(release_id, folder_path)
            if success:
                found += 1
                writer.writerow({
                    'folder_name': row['folder_name'], 'artist': artist, 'album': album,
                    'status': 'art_fetched', 'mb_release_id': release_id, 'mb_score': score,
                    'art_path': result, 'error': ''
                })
            else:
                not_found += 1
                writer.writerow({
                    'folder_name': row['folder_name'], 'artist': artist, 'album': album,
                    'status': 'no_art_available', 'mb_release_id': release_id, 'mb_score': score,
                    'art_path': '', 'error': result
                })

    elapsed = time.time() - start_time
    log.info(f"\n{'='*60}")
    log.info(f"PHASE 2 COMPLETE in {elapsed/60:.1f} minutes")
    log.info(f"{'='*60}")
    log.info(f"Albums processed:  {len(targets)}")
    log.info(f"Art fetched:       {found}")
    log.info(f"No art found:      {not_found}")
    log.info(f"Errors:            {errors}")
    log.info(f"Results saved to:  {RESULT_CSV}")


if __name__ == '__main__':
    main()
