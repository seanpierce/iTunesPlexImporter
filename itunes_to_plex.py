#!/usr/bin/env python3
"""Import regular iTunes XML playlists into Plex without modifying original music.

Dry-run by default. Requires python-plexapi. Run --help for instructions.
"""
import argparse
import csv
import os
import plistlib
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit


def normalize(value):
    value = unicodedata.normalize('NFKD', str(value or '')).casefold()
    return ' '.join(re.findall(r'\w+', value))


def filename_from_location(location):
    if not location:
        return ''
    path = unquote(urlsplit(location).path) if location.startswith('file:') else location
    return normalize(os.path.basename(path.replace('\\', '/')))


def plex_file(track):
    for media in getattr(track, 'media', []) or []:
        for part in getattr(media, 'parts', []) or []:
            if getattr(part, 'file', None):
                return part.file
    return ''


def build_index(section):
    print('Indexing Plex music tracks (may take a while)...', flush=True)
    tracks = section.search(libtype='track')
    by_full, by_pair, by_file = defaultdict(list), defaultdict(list), defaultdict(list)
    for t in tracks:
        title = normalize(getattr(t, 'title', ''))
        artist = normalize(getattr(t, 'grandparentTitle', '') or getattr(t, 'originalTitle', ''))
        album = normalize(getattr(t, 'parentTitle', ''))
        if title:
            by_full[(title, artist, album)].append(t)
            by_pair[(title, artist)].append(t)
        name = filename_from_location(plex_file(t))
        if name:
            by_file[name].append(t)
    print(f'Indexed {len(tracks)} Plex tracks.')
    return by_full, by_pair, by_file


def duration_matches(source, candidates, tolerance_ms=2500):
    duration = source.get('Total Time')
    if not isinstance(duration, (int, float)) or duration <= 0:
        return candidates
    close = [t for t in candidates if abs((getattr(t, 'duration', 0) or 0) - duration) <= tolerance_ms]
    return close or candidates


def match(source, index):
    full, pair, files = index
    title = normalize(source.get('Name'))
    artist = normalize(source.get('Artist'))
    album_artist = normalize(source.get('Album Artist'))
    album = normalize(source.get('Album'))
    candidates = []
    for a in dict.fromkeys([artist, album_artist]):
        candidates = full.get((title, a, album), [])
        if candidates:
            break
    if not candidates:
        for a in dict.fromkeys([artist, album_artist]):
            candidates = pair.get((title, a), [])
            if candidates:
                break
    if not candidates:
        candidates = files.get(filename_from_location(source.get('Location')), [])
    candidates = duration_matches(source, candidates)
    unique = {str(getattr(t, 'ratingKey', '')): t for t in candidates}
    if len(unique) == 1:
        return next(iter(unique.values())), 'matched'
    if len(unique) > 1:
        return None, 'ambiguous'
    return None, 'missing'


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--xml', required=True, type=Path, help='iTunes File > Library > Export Library XML')
    p.add_argument('--url', default=os.environ.get('PLEX_URL', 'http://127.0.0.1:32400'), help='Plex server URL (default localhost:32400)')
    p.add_argument('--token', default=os.environ.get('PLEX_TOKEN'), help='Plex token, or set PLEX_TOKEN environment variable')
    p.add_argument('--library', default='Music', help='Name of Plex music library (default Music)')
    p.add_argument('--report', type=Path, default=Path('itunes_plex_report.csv'), help='CSV report path')
    p.add_argument('--playlist', help='Import only this exact iTunes playlist name')
    p.add_argument('--apply', action='store_true', help='Actually create playlists; otherwise dry-run')
    p.add_argument('--min-match', type=float, default=0.95, help='Minimum matched fraction to create playlist (default 0.95)')
    args = p.parse_args()
    if not args.token:
        p.error('A Plex token is required (--token or PLEX_TOKEN).')
    if not (0 <= args.min_match <= 1):
        p.error('--min-match must be between 0 and 1')
    try:
        with args.xml.open('rb') as f:
            library = plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        p.error(f'Cannot read iTunes XML file: {exc}')
    source_tracks = library.get('Tracks', {})
    playlists = [pl for pl in library.get('Playlists', [])
                 if pl.get('Playlist Items') and not pl.get('Smart Info') and not pl.get('Smart Criteria')
                 and not pl.get('Master') and not pl.get('Distinguished Kind')
                 and (not args.playlist or pl.get('Name') == args.playlist)]
    if not playlists:
        p.error('No regular playlists with tracks found. Smart playlists must be converted to regular playlists in iTunes first.')
    try:
        from plexapi.server import PlexServer
        plex = PlexServer(args.url, args.token)
        section = plex.library.section(args.library)
        index = build_index(section)
        existing = {x.title for x in plex.playlists()}
    except Exception as exc:
        sys.exit(f'Plex connection/library error: {exc}')
    rows = []
    for pl in playlists:
        name = pl.get('Name') or '(Unnamed)'
        matched, problems = [], 0
        for number, item in enumerate(pl['Playlist Items'], 1):
            source_id = item.get('Track ID')
            source = source_tracks.get(str(source_id))
            if source is None:
                status, track = 'not_in_xml', None
            else:
                track, status = match(source, index)
            if track is not None:
                matched.append(track)
            else:
                problems += 1
            rows.append({'playlist': name, 'position': number, 'itunes_track_id': source_id,
                         'title': (source or {}).get('Name', ''), 'artist': (source or {}).get('Artist', ''),
                         'album': (source or {}).get('Album', ''), 'status': status,
                         'plex_rating_key': getattr(track, 'ratingKey', '') if track else ''})
        total = len(pl['Playlist Items'])
        rate = len(matched) / total
        print(f'{name}: {len(matched)}/{total} matched ({rate:.1%}), {problems} unresolved', end='')
        if name in existing:
            print(' -> SKIP: a Plex playlist with this name already exists')
        elif rate < args.min_match:
            print(f' -> SKIP: below {args.min_match:.0%} threshold')
        elif not matched:
            print(' -> SKIP: no matched tracks')
        elif not args.apply:
            print(' -> DRY RUN')
        else:
            try:
                plex.createPlaylist(title=name, items=matched)
                existing.add(name)
                print(' -> CREATED')
            except Exception as exc:
                print(f' -> ERROR: {exc}')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['playlist', 'position', 'itunes_track_id', 'title', 'artist', 'album', 'status', 'plex_rating_key'])
        writer.writeheader()
        writer.writerows(rows)
    print(f'\nReport saved to {args.report.resolve()}')
    print('Original iTunes and Plex music files were not modified.')
    if not args.apply:
        print('Dry-run only. Review the report, then repeat with --apply to create qualifying playlists.')


if __name__ == '__main__':
    main()
