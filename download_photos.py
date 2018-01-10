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
from authentication import authenticate

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
              metavar='<password>')
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
@click.option('--only-print-filenames',
              help='Only prints the filenames of all files that will be downloaded. ' + \
                '(Does not download any files.)',
              is_flag=True)

@click.option('--smtp-username',
              help='Your SMTP username, for sending email notifications when two-step authentication expires.',
              metavar='<smtp_username>')
@click.option('--smtp-password',
              help='Your SMTP password, for sending email notifications when two-step authentication expires.',
              metavar='<smtp_password>')
@click.option('--smtp-host',
              help='Your SMTP server host. Defaults to: smtp.gmail.com',
              metavar='<smtp_host>',
              default='smtp.gmail.com')
@click.option('--smtp-port',
              help='Your SMTP server port. Default: 587 (Gmail)',
              metavar='<smtp_port>',
              type=click.IntRange(0),
              default=587)
@click.option('--smtp-no-tls',
              help='Pass this flag to disable TLS for SMTP (TLS is required for Gmail)',
              metavar='<smtp_no_tls>',
              is_flag=True)
@click.option('--notification-email',
              help='Email address where you would like to receive email notifications. Default: SMTP username',
              metavar='<notification_email>')


def download(directory, photostation, username, password, size, recent, \
    until_found, download_videos, force_size, auto_delete, \
    only_print_filenames, \
    smtp_username, smtp_password, smtp_host, smtp_port, smtp_no_tls, \
    notification_email):
    """Download all iCloud photos to a local directory"""

    if not notification_email:
        notification_email = smtp_username

    icloud = authenticate(username, password, \
        smtp_username, smtp_password, smtp_host, smtp_port, smtp_no_tls, notification_email)

    if photostation:
        directory = PhotoStationService(photostation, directory)
    else:
        directory = FileSystemStorage(directory)

    if not only_print_filenames:
        print("Looking up all photos...")
    photos = icloud.photos.all
    photos_count = len(photos)

    # Optional: Only download the x most recent photos.
    if recent is not None:
        photos_count = recent
        photos = itertools.islice(photos, recent)

    kwargs = {'total': photos_count}

    if until_found is not None:
        del kwargs['total']
        photos_count = '???'

        # ensure photos iterator doesn't have a known length
        photos = (p for p in photos)

    if not only_print_filenames:
        if download_videos:
            print("Downloading %s %s photos and videos to %s/ ..." % (photos_count, size, directory))
        else:
            print("Downloading %s %s photos to %s/ ..." % (photos_count, size, directory))

    consecutive_files_found = 0
    if only_print_filenames:
        progress_bar = photos
    else:
        progress_bar = tqdm(photos, **kwargs)

    for photo in progress_bar:
        for _ in range(MAX_RETRIES):
            try:
                if not download_videos \
                    and not photo.filename.lower().endswith(('.png', '.jpg', '.jpeg')):
                    if not only_print_filenames:
                        progress_bar.set_description(
                            "Skipping %s, only downloading photos." % photo.filename)
                    continue

                created_date = photo.created

                date_path = '{:%Y/%m/%d}'.format(created_date)

                album = directory.album(date_path, create=True)

                
                exists = download_photo(photo, size, force_size, album, progress_bar, only_print_filenames)

                if until_found is not None:
                    if exists:
                        consecutive_files_found += 1
                    else:
                        consecutive_files_found = 0
                break

            except (requests.exceptions.ConnectionError, socket.timeout):
                if not only_print_filenames:
                    tqdm.write('Connection failed, retrying after %d seconds...' % WAIT_SECONDS)
                time.sleep(WAIT_SECONDS)

        else:
            if not only_print_filenames:
                tqdm.write("Could not process %s! Maybe try again later." % photo.filename)

        if until_found is not None and consecutive_files_found >= until_found:
            if not only_print_filenames:
                tqdm.write('Found %d consecutive previusly downloaded photos. Exiting' % until_found)
                progress_bar.close()
            break

    if not only_print_filenames:
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
                    item = album.item(filename)
                    if item is not None:
                        print('deleting photo ' + str(item) + ' from album ' + album.path)
                        item.delete()

def truncate_middle(s, n):
    if len(s) <= n:
        return s
    n_2 = int(n) // 2 - 2
    n_1 = n - n_2 - 4
    if n_2 < 1: n_2 = 1
    return '{0}...{1}'.format(s[:n_1], s[-n_2:])

def filename_with_size(photo, size):
    if sys.version_info[0] >= 3:
        filename = photo.filename
    else:
        filename = photo.filename.encode('utf-8')

    if size == 'original':
        return filename
    else:
        return filename.decode('ascii', 'ignore').replace('.', '-%s.' % size)

def download_photo(photo, size, force_size, album, progress_bar, only_print_filenames):
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

    filesize = photo.size
    if 'resJPEGFullRes' in asset_fields:
        filesize = asset_fields['resJPEGFullRes']['value']['size']

    album_photo = album.create_item(
        filename = filename, 
        filetype = 'photo' if is_photo else 'video',
        created = asset_fields['assetDate']['value'],
        filesize = filesize,
        title = title,
        description = description,
        rating = asset_fields['isFavorite']['value'],
        latitude = latitude,
        longitude = longitude)

    if album_photo.merge():
        if not only_print_filenames:
            progress_bar.set_description("%s already exists." % truncated_path)
        return True

    if only_print_filenames:
        print(truncated_filename)
        return False

    # Fall back to original if requested size is not available
    if size not in photo.versions and not force_size and size != 'original':
        return download_photo(photo, 'original', True, album, progress_bar)

    progress_bar.set_description("Downloading %s to %s" % (truncated_filename.encode().decode('ascii', 'ignore'), truncated_path.encode().decode('ascii', 'ignore')))

    for _ in range(MAX_RETRIES):
        try:
            if 'resJPEGFullRes' in asset_fields:
                # Download edited file if available
                download_url = photo._service.session.get(
                    asset_fields['resJPEGFullRes']['value']['downloadURL'],
                    stream=True
                )
            else:
                # For supported file sizes
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
