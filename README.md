# Music Library Auto-Tagging Guide

> Automated pipeline for tagging, art-fetching, and organising a messy music library (scene rips, RARs, mixed naming conventions). Originally built for a 10TB Japanese/citypop library.

## What This Does

1. **Audit** — Scans every album folder, reads existing tags, classifies quality, scores completeness
2. **Enrich** — Fetches missing album art for already-well-tagged albums (MusicBrainz + Cover Art Archive)
3. **Tag** — For poorly-tagged albums: parses folder names, searches MusicBrainz, falls back to AcoustID fingerprinting, writes tags
4. **Review** — Re-processes failures with improved parsing + fingerprinting
5. **Genre Classification** — Multi-phase genre tagging: artist inference, MusicBrainz lookup, CJK detection. Took 30,760 ungenred tracks to 99.7% coverage
6. **Playlist Rebuild** — Rebuilds all 92 genre playlists in Jellyfin via API
7. **Beets** — Final pass for dedup, genre tagging, path cleanup, embedded art

Handles scene rip naming (`(3AW3r)_Artist_-_Album_(Year)_(FLAC)`), bare `Artist - Album` folders, Japanese/CJK characters, multi-disc sets, and everything in between.

---

## Prerequisites

- **macOS or Linux** (tested on macOS Ventura, should work on any Unix)
- **Python 3.10+**
- **ffmpeg** (for AcoustID fingerprinting) — `brew install ffmpeg` or `apt install ffmpeg`
- **Chromaprint** (fpcalc) — `brew install chromaprint` or `apt install libchromaprint-tools`
- **beets** — `pip install beets` or `pipx install beets`
- Your music library accessible as a local path (mounted NAS, external drive, etc.)

## Setup

### 1. Create a Python virtual environment

```bash
mkdir -p ~/music-pipeline
cd ~/music-pipeline

python3 -m venv ~/music-pipeline-env
source ~/music-pipeline-env/bin/activate

pip install mutagen musicbrainzngs pyacoustid requests beets
pip install beets[chroma,fetchart,lastgenre]
```

### 2. Get an AcoustID API key

1. Go to https://acoustid.org/login
2. Register an application
3. Copy your API key
4. Export it: `export ACOUSTID_API_KEY=your_key_here`

### 3. Configure beets

Copy the included config and edit the `directory` path:

```bash
mkdir -p ~/.config/beets
cp beets-config.yaml ~/.config/beets/config.yaml
# Edit ~/.config/beets/config.yaml and set directory: /path/to/your/music/
```

Full beets config reference (also in `beets-config.yaml`):

```yaml
# Beets Configuration
# Adjust `directory` to point at YOUR music library path

# Library location
directory: /path/to/your/music/
library: ~/.config/beets/musiclibrary.db

# Plugins
plugins:
    - chroma
    - fetchart
    - embedart
    - lastgenre
    - mbsync
    - missing
    - duplicates
    - edit
    - info
    - inline
    - ftintitle
    - the

# Import settings — tag files in place, don't move or copy
import:
    copy: no
    move: no
    write: yes
    timid: no
    log: ~/.config/beets/import.log
    quiet_fallback: asis

# MusicBrainz — prefer Japanese metadata, use canonical artist names
# artist_credit: no = all releases by same MB artist get same canonical name
# (kanji for JP artists), which consolidates romaji/kanji dupes
musicbrainz:
    languages: ['ja', 'en']
    artist_credit: no
    ratelimit: 1

# Acoustic fingerprinting (replace with YOUR AcoustID API key)
acoustid:
    apikey: YOUR_ACOUSTID_API_KEY_HERE
chroma:
    auto: yes

# Match tuning for Japanese music
match:
    strong_rec_thresh: 0.10
    medium_rec_thresh: 0.40
    rec_gap_thresh: 0.25
    preferred:
        countries: ['JP', 'XW']
        media: ['Digital Media', 'CD']
        original_year: yes
    missing_penalty: 0.3

# Path templates — use romanized sort name for folders
paths:
    default: '%the{$albumartist_sort}/$album%aunique{}/%if{$disc,$disc-}$track $title'
    singleton: 'Singletons/%the{$albumartist_sort}/$title'
    comp: 'Compilations/$album%aunique{}/%if{$disc,$disc-}$track $title'

# Inline plugin — computed fields for romanized artist names
album_fields:
    artist_romaji: |
        if albumartist_sort:
            parts = albumartist_sort.split(', ', 1)
            if len(parts) == 2:
                return parts[1] + ' ' + parts[0]
        return albumartist_sort or albumartist

item_fields:
    artist_romaji: |
        if albumartist_sort:
            parts = albumartist_sort.split(', ', 1)
            if len(parts) == 2:
                return parts[1] + ' ' + parts[0]
        return albumartist_sort or albumartist

# Album art
fetchart:
    auto: yes
    minwidth: 500
    maxwidth: 1200
    sources:
        - filesystem
        - coverart
        - musicbrainz
        - itunes
        - amazon
    store_source: yes

embedart:
    auto: yes
    maxwidth: 1200
    remove_art_file: no

# Genre tagging from Last.fm
lastgenre:
    auto: yes
    count: 3
    source: album
    min_weight: 10

# Featured artists — move to title field
ftintitle:
    auto: yes
    format: 'feat. {0}'

# ID3v2.4 for proper UTF-8/CJK support
id3v23: no

original_date: yes
per_disc_numbering: no
```

### 4. Set `MUSIC_DIR`

Export the path to your music library:

```bash
export MUSIC_DIR="/path/to/your/music"   # mounted NAS, external drive, etc.
export ACOUSTID_API_KEY="your_key_here"  # from step 2
```

Or add to your shell profile (`~/.bashrc` / `~/.zshrc`) to persist.

---

## Running the Pipeline

Always activate the venv first:

```bash
source ~/music-pipeline-env/bin/activate
```

### Phase 1: Audit (read-only, safe to run anytime)

```bash
python3 ~/music-pipeline/audit.py
```

- Scans every folder in `MUSIC_DIR`
- Reads tags from the first audio file in each folder
- Classifies into populations:
  - **A** (well-tagged): artist, album, title all present — score 60+
  - **B** (scene rips): recognisable naming pattern, tags may be empty
  - **C** (bare): no tags, no recognisable pattern
- Outputs `audit_report.csv` with every album's status

**Output columns**: folder_name, population, tag_score, has_art_file, has_embedded_art, tag_artist, tag_album, parsed_artist, parsed_album, track_count, audio_format

**Time**: ~5 minutes per 10,000 albums (mostly I/O bound)

### Phase 2: Enrich (fetches art only, no tag overwrites)

```bash
python3 ~/music-pipeline/phase2_enrich.py
```

- Processes albums with tag_score >= 60 that are **missing album art**
- Searches MusicBrainz by artist+album text (score >= 80 required)
- Downloads front cover from Cover Art Archive (500px)
- Saves as `cover.jpg` in the album folder
- Outputs `phase2_results.csv`

**Time**: ~1 second per album (MusicBrainz rate limit)

### Phase 3: Tag (writes tags to files)

```bash
python3 ~/music-pipeline/phase3_tag.py
```

- Processes albums with tag_score < 60
- **Strategy 1**: Parse folder name for artist/album/year → MusicBrainz text search
- **Strategy 2**: AcoustID fingerprint first 2 tracks → find common MusicBrainz release
- If matched: writes full tags (title, artist, album, albumartist, date, tracknumber, discnumber, label, catalog, MusicBrainz IDs) + downloads cover art
- If not matched: writes baseline tags from folder name (better than nothing)
- Outputs `phase3_results.csv` and `needs_review.csv`

**Time**: ~2-5 seconds per album (MusicBrainz + optional AcoustID)

To re-run only failed albums (skips already-matched):
```bash
python3 ~/music-pipeline/phase3_tag.py --rerun
```

### Phase 3b: Review (improved parsing + re-fingerprint)

```bash
python3 ~/music-pipeline/phase3b_review.py
```

- Re-processes `needs_review.csv` with better folder name parsing
- Handles edge cases: timestamp prefixes, bare underscores, missing years
- Also reads existing embedded tags as fallback
- Outputs `phase3b_results.csv` and `still_unmatched.csv`

The `still_unmatched.csv` is your final "needs manual attention" list.

### Phase 4: Beets (optional, for cleanup)

After the automated phases, use beets for final polishing:

```bash
# Import library into beets database (tags in-place, no file moves)
beet import /path/to/your/music/

# Fetch any remaining missing art
beet fetchart

# Embed art into files
beet embedart

# Tag genres from Last.fm
beet lastgenre

# Find duplicates
beet duplicates

# List albums missing tracks (vs MusicBrainz)
beet missing
```

Beets import is **interactive** — it'll ask you to confirm matches for ambiguous albums. Use `A` to apply, `S` to skip, `U` to use as-is.

---

## Folder Name Patterns Handled

The parser recognises these common patterns from scene rips and manual rips:

| Pattern | Example |
|---------|---------|
| Scene prefix | `(3AW3r)_Artist_-_Album_(Year)_(FLAC)` |
| Scene no year | `(AZ3113c)_Artist_-_Album_(FLAC)` |
| Standard | `Artist - Album (Year) [FLAC]` |
| Underscore | `Artist_-_Album_(Year)_(FLAC)` |
| Year prefix | `(1980) Artist - Album` |
| Timestamp | `20180515.2231.4 Artist Album` |
| Japanese | `竹内まりや - Variety (1984)` |
| Mixed | `Mariya Takeuchi (竹内まりや) - Variety` |

---

## What Gets Written to Files

For MusicBrainz-matched albums, these tags are written:

| Tag | Source |
|-----|--------|
| `title` | MusicBrainz recording title |
| `artist` | MusicBrainz track artist |
| `album` | MusicBrainz release title |
| `albumartist` | MusicBrainz release artist |
| `artistsort` | MusicBrainz sort name (e.g. `Takeuchi, Mariya`) |
| `date` | Release year |
| `tracknumber` | Track position |
| `discnumber` / `disctotal` | Disc info (multi-disc sets) |
| `tracktotal` | Tracks per disc |
| `organization` | Record label |
| `catalognumber` | Catalog number |
| `musicbrainz_albumid` | MusicBrainz release ID |
| `musicbrainz_trackid` | MusicBrainz recording ID |

For unmatched albums, baseline tags are written from the folder name:
- `artist`, `albumartist`, `album`, `date` (if parseable)
- `tracknumber`, `title` (from filename like `01. Song Title.flac`)

**Album art**: Saved as `cover.jpg` (or `.png`) in the album folder, 500px from Cover Art Archive.

---

---

## Genre Classification (Apr 2026)

After the initial tagging pass and library expansion to 237,861 tracks, 30,760 (12.9%) had no genre tag. Four scripts brought coverage to **99.7%** in under an hour:

1. **`genre_phase1_artist_inference.py`** — For each artist with untagged tracks, checks their already-tagged tracks. If ≥80% share one genre (≥60% for >50 tracks), applies it. *5,761 tracks.*
2. **`genre_phase2_musicbrainz.py`** — Looks up albums by MusicBrainz Release ID, queries MB API for genre/tag data (release → release-group fallback). Rate-limited 1 req/sec. *7,083 tracks.*
3. **`genre_phase3_cjk_defaults.py`** — Detects Japanese (hiragana/katakana/CJK), Korean (hangul), Chinese artists via Unicode ranges. Applies J-Pop/K-Pop/Mandopop/Cantopop defaults with overrides for City Pop, Kayōkyoku, Shibuya-Kei. *20,825 tracks.*
4. **`genre_phase3b_remaining.py`** — Explicit artist→genre mappings for romanized Japanese and Western artists, plus path-based detection for JP release patterns. *2,368 tracks.*
5. **`genre_rebuild_playlists.py`** — Clears and repopulates all 92 Jellyfin genre playlists via API.

### Key Architecture Decision: Run on the NAS

Early in the project, all tagging ran from a remote machine accessing the NAS over SMB. This was catastrophically slow — Jellyfin library scans over SMB crawled at **~5%/hour** for 168k files, and long scans corrupted Jellyfin's SQLite database mid-write. The whole process dragged on for months.

The fix was simple: install `python3` + `mutagen` directly on the Synology NAS and run all tagging scripts locally. **What previously took months finished in under an hour.** Jellyfin then scans the already-tagged files over NFS, which is significantly faster and more reliable than SMB for metadata-heavy workloads.

**If you're tagging a music library on a NAS: run your scripts on the NAS, not over a network share.**

---

## Tips

- **Run Phase 1 first** — it's read-only and gives you a full picture before anything changes
- **Back up before Phase 3** — it writes tags to your actual audio files
- **Japanese music**: MusicBrainz has excellent Japanese release data. The `languages: ['ja', 'en']` config prefers Japanese metadata where available
- **Duplicate artists**: Beets with `artist_credit: no` consolidates `竹内まりや` / `Mariya Takeuchi` / `Mariya Takeuchi (竹内まりや)` into one canonical name
- **SSL errors on macOS**: If you get certificate errors, run:
  ```bash
  ln -sf $(python3 -c 'import certifi; print(certifi.where())') /usr/local/etc/openssl@3/cert.pem
  ```
- **Rate limits**: Scripts respect MusicBrainz (1 req/sec) and AcoustID (3 req/sec) limits. Don't try to parallelise.
- **Large libraries**: 18,000 albums took ~6 hours total across all phases. Plan accordingly.
- **Rerun-safe**: Phase 3 supports `--rerun` to skip already-matched albums

## Files

| File | Purpose |
|------|---------|
| `audit.py` | Phase 1: read-only library audit |
| `phase2_enrich.py` | Phase 2: fetch missing album art |
| `phase3_tag.py` | Phase 3: tag poorly-tagged albums |
| `phase3b_review.py` | Phase 3b: re-process failures with improved parsing |
| `genre_phase1_artist_inference.py` | Genre: infer from artist's existing tags |
| `genre_phase2_musicbrainz.py` | Genre: MusicBrainz API lookup |
| `genre_phase3_cjk_defaults.py` | Genre: CJK character detection defaults |
| `genre_phase3b_remaining.py` | Genre: explicit mappings + path detection |
| `genre_rebuild_playlists.py` | Rebuild all Jellyfin genre playlists |
