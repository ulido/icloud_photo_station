from PIL import Image
import pyheif

from icloudpd.logger import setup_logger

def convert_heic(filename):
    try:
        heif_file = pyheif.read(filename)
    except Exception as e:
        logger = setup_logger()
        logger.debug(f"Problem reading {filename}, skipping HEIC conversion")
        return
    meta = {e['type']: e['data'] for e in heif_file.metadata}
    image = Image.frombytes(
        heif_file.mode,
        heif_file.size,
        heif_file.data,
        "raw",
        heif_file.mode,
        heif_file.stride,
    )

    jpeg_path = filename.replace('.HEIC', '.JPG').replace('.heic', '.jpg')
    image.save(jpeg_path, 'JPEG', quality=95, exif=meta['Exif'])

    return jpeg_path
