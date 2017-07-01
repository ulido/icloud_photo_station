#!/usr/bin/env python
from __future__ import print_function
import click
import sys
import socket
import requests
import time
import itertools
from tqdm import tqdm
from dateutil.parser import parse
from filesystem import FileSystemStorage
from photostation import PhotoStationService
import pyicloud
from pprint import pprint
from base64 import b64decode
from biplist import readPlistFromString

# For retrying connection after timeouts and errors
MAX_RETRIES = 5
WAIT_SECONDS = 5


CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])
@click.command(context_settings=CONTEXT_SETTINGS, options_metavar='<options>')
@click.argument('directory', type=click.Path(exists=False), metavar='<directory>')
@click.option('--photostation',
              help='URL to your PhotoStation webapi with access rights')
@click.option('--username',
              help='Your iCloud username or email address',
              metavar='<username>',
              prompt='iCloud username/email')
@click.option('--password',
              help='Your iCloud password',
              metavar='<password>',
              prompt='iCloud password')
@click.option('--size',
              help='Image size to download (default: original)',
              type=click.Choice(['original', 'medium', 'thumb']),
              default='original')
@click.option('--recent',
              help='Number of recent photos to download (default: download all photos)',
              type=click.IntRange(0))
@click.option('--until-found',
              help='Download most recently added photos until we find x number of previously downloaded consecutive photos (default: download all photos)',
              type=click.IntRange(0))
@click.option('--download-videos',
              help='Download both videos and photos (default: only download photos)',
              is_flag=True)
@click.option('--force-size',
              help='Only download the requested size ' + \
                   '(default: download original if size is not available)',
              is_flag=True)
@click.option('--auto-delete',
              help='Scans the "Recently Deleted" folder and deletes any files found in there. ' + \
                   '(If you restore the photo in iCloud, it will be downloaded again.)',
              is_flag=True)


def download(directory, photostation, username, password, size, recent, \
    until_found, download_videos, force_size, auto_delete):
    """Download all iCloud photos to a local directory"""

    if photostation:
        directory = PhotoStationService(photostation, directory)
    else:
        directory = FileSystemStorage(directory)

    icloud = authenticate(username, password)

    print("Looking up all photos...")
    photos = icloud.photos.all
    photos_count = len(photos)

    # Optional: Only download the x most recent photos.
    if recent is not None:
        photos_count = recent
        photos = (p for i,p in enumerate(photos) if i < recent)

    kwargs = {'total': photos_count}

    if until_found is not None:
        del kwargs['total']
        photos_count = '???'

        # ensure photos iterator doesn't have a known length
        photos = (p for p in photos)

    if download_videos:
        print("Downloading %s %s photos and videos to %s/ ..." % (photos_count, size, directory))
    else:
        print("Downloading %s %s photos to %s/ ..." % (photos_count, size, directory))

    consecutive_files_found = 0
    progress_bar = tqdm(photos, **kwargs)

    for photo in progress_bar:
        for _ in range(MAX_RETRIES):
            try:
                if not download_videos \
                    and not photo.filename.lower().endswith(('.png', '.jpg', '.jpeg')):

                    progress_bar.set_description(
                        "Skipping %s, only downloading photos." % photo.filename)
                    continue

                created_date = photo.created

                date_path = '{:%Y/%m/%d}'.format(created_date)

                album = directory.album(date_path, create=True)

                exists = download_photo(photo, size, force_size, album, progress_bar)
                if until_found is not None:
                    if exists:
                        consecutive_files_found += 1
                    else:
                        consecutive_files_found = 0
                break

            except (requests.exceptions.ConnectionError, socket.timeout):
                tqdm.write('Connection failed, retrying after %d seconds...' % WAIT_SECONDS)
                time.sleep(WAIT_SECONDS)

        else:
            tqdm.write("Could not process %s! Maybe try again later." % photo.filename)

        if until_found is not None and consecutive_files_found >= until_found:
            tqdm.write('Found %d consecutive previusly downloaded photos.  Exiting' % until_found)
            progress_bar.close()
            break


    print("All photos have been downloaded!")

    if auto_delete:
        print("Deleting any files found in 'Recently Deleted'...")

        recently_deleted = icloud.photos.albums['Recently Deleted']

        for media in recently_deleted:
            created_date = media.created
            date_path = '{:%Y/%m/%d}'.format(created_date)

            album = directory.album(date_path, create=False)
            if album:
                filename = filename_with_size(media, size)
                album.photo(filename).delete()

def authenticate(username, password):
    print("Signing in...")
    icloud = pyicloud.PyiCloudService(username, password)

    if icloud.requires_2fa:
        print("Two-factor authentication required. Your trusted devices are:")

        devices = icloud.trusted_devices
        for i, device in enumerate(devices):
            print("  %s: %s" % (i, device.get('deviceName',
                "SMS to %s" % device.get('phoneNumber'))))

        device = click.prompt('Which device would you like to use?', default=0)
        device = devices[device]
        if not icloud.send_verification_code(device):
            print("Failed to send verification code")
            sys.exit(1)

        code = click.prompt('Please enter validation code')
        if not icloud.validate_verification_code(device, code):
            print("Failed to verify verification code")
            sys.exit(1)

    return icloud

def truncate_middle(s, n):
    if len(s) <= n:
        return s
    n_2 = int(n) // 2 - 2
    n_1 = n - n_2 - 4
    if n_2 < 1: n_2 = 1
    return '{0}...{1}'.format(s[:n_1], s[-n_2:])

def filename_with_size(photo, size):
    if size == 'original':
        return photo.filename.encode('utf-8')
    else:
        return photo.filename.encode('utf-8') \
            .decode('ascii', 'ignore').replace('.', '-%s.' % size)

def download_photo(photo, size, force_size, album, progress_bar):
    # Strip any non-ascii characters.
    filename = filename_with_size(photo, size)

    truncated_filename = truncate_middle(filename, 24)
    truncated_path = truncate_middle(album.path, 72)

    master_fields = photo._master_record['fields']
    asset_fields = photo._asset_record['fields']

    is_photo = master_fields['itemType']['value'] in ['public.jpeg', 'public.png']


    # PhotoStation can read photo coordinates from exif
    latitude = longitude = None
    if not is_photo and asset_fields.get('locationEnc') is not None:
        location = readPlistFromString(b64decode(asset_fields['locationEnc']['value']))
        latitude = location['lat']
        longitude = location['lon']

    description = ''
    if master_fields.get('mediaMetaDataEnc') is not None:
        metadata = readPlistFromString(b64decode(master_fields['mediaMetaDataEnc']['value']))
        description = metadata['ImageDescription']

    title = ''
    if asset_fields.get('captionEnc') is not None:
        title = b64decode(asset_fields['captionEnc']['value'])

    album_photo = album.create_item(
        filename = filename, 
        filetype = 'photo' if is_photo else 'video',
        mtime = asset_fields['assetDate']['value'],
        title = title,
        description = description,
        rating = asset_fields['isFavorite']['value'],
        latitude = latitude,
        longitude = longitude)

    if album_photo.merge():
        progress_bar.set_description("%s already exists." % truncated_path)
        return True

    # Fall back to original if requested size is not available
    if size not in photo.versions and not force_size and size != 'original':
        return download_photo(photo, 'original', True, album, progress_bar)

    progress_bar.set_description("Downloading %s to %s" % (truncated_filename.decode('ascii', 'ignore'), truncated_path.decode('ascii', 'ignore')))

    for _ in range(MAX_RETRIES):
        try:
            download_url = photo.download(size)

            if download_url:
                album_photo.save_content(download_url)
                break

            else:
                tqdm.write(
                    "Could not find URL to download %s for size %s!" %
                    (photo.filename, size))


        except (requests.exceptions.ConnectionError, socket.timeout):
            tqdm.write(
                '%s download failed, retrying after %d seconds...' %
                (photo.filename, WAIT_SECONDS))
            time.sleep(WAIT_SECONDS)
    else:
        tqdm.write("Could not download %s! Maybe try again later." % photo.filename)

    return False

if __name__ == '__main__':
    download()
