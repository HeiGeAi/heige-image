"""Offline pixel-decoding regressions; prior output is a genuinely valid image."""
import base64
import builtins
import io
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image, ImageFile
from scripts import edit, gen, image_output as output


def chunk(kind, payload):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))


def raw_png(rows=b'\0\0\0\0', width=1, height=1, depth=8, color=2, extra=b''):
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, depth, color, 0, 0, 0))
            + extra + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def image_bytes(mode='RGB', fmt='PNG', color=None):
    buffer = io.BytesIO()
    image = Image.new(mode, (2, 2), color)
    if mode == 'P':
        image.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
    image.save(buffer, format=fmt)
    return buffer.getvalue()


class PixelDecodingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.destination = self.root / 'existing.png'
        self.prior = image_bytes(color='green')
        with Image.open(io.BytesIO(self.prior)) as image:
            image.load()
        self.destination.write_bytes(self.prior)

    def assert_preserved(self, invalid):
        with self.assertRaises(output.ImageResponseError):
            output.atomic_write_image(self.destination, invalid)
        self.assertEqual(self.destination.read_bytes(), self.prior)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['existing.png'])

    def test_invalid_filter_cannot_replace_a_decodable_image(self):
        invalid = raw_png(b'\x05\0\0\0')
        with Image.open(io.BytesIO(invalid)) as image:
            with self.assertRaises(OSError):
                image.load()
        self.assert_preserved(invalid)

    def test_truncated_corrupt_and_header_only_images_preserve_previous(self):
        for invalid in (self.prior[:-12], self.prior[:33] + chunk(b'IEND', b''),
                        raw_png(b'\0\0'), self.prior[:-1] + b'\xff'):
            with self.subTest(invalid=invalid):
                self.assert_preserved(invalid)

    def test_all_supported_png_modes_and_scanline_filters_decode(self):
        for mode in ('1', 'L', 'LA', 'P', 'RGB', 'RGBA', 'I;16'):
            with self.subTest(mode=mode):
                valid = image_bytes(mode)
                self.assertEqual(output.validate_image_bytes(valid), valid)
        for filter_value in range(5):
            with self.subTest(filter=filter_value):
                output.validate_image_bytes(raw_png(bytes([filter_value, 0, 0, 0])))

    def test_valid_other_formats_do_not_change_the_png_contract(self):
        for fmt in ('JPEG', 'WEBP', 'GIF'):
            with self.subTest(format=fmt):
                self.assert_preserved(image_bytes(fmt=fmt))

    def test_animated_png_is_not_silently_reduced_to_first_frame(self):
        buffer = io.BytesIO()
        Image.new('RGB', (2, 2), 'red').save(buffer, format='PNG', save_all=True,
            append_images=[Image.new('RGB', (2, 2), 'blue')], duration=100, loop=0)
        self.assert_preserved(buffer.getvalue())

    def test_palette_is_required_and_must_precede_idat(self):
        self.assert_preserved(raw_png(b'\0\0', color=3))
        valid = raw_png(b'\0\0', color=3, extra=chunk(b'PLTE', b'\xff\0\0'))
        self.assertEqual(output.validate_image_bytes(valid), valid)
        self.assert_preserved(raw_png(b'\0\0', color=3, extra=chunk(b'PLTE', b'\xff\0')))

    def test_limits_are_checked_before_pixel_decoder(self):
        for invalid in (raw_png(width=32_000_001), raw_png(width=4000, height=4000, depth=16, color=6)):
            with self.subTest(length=len(invalid)), patch.object(output, '_decode_png') as decode:
                self.assert_preserved(invalid)
                decode.assert_not_called()
        with patch.object(output, 'MAX_IMAGE_BYTES', 1):
            # Explicit max_bytes is supported separately from the default.
            with self.assertRaises(output.ImageResponseError):
                output.validate_image_bytes(self.prior, max_bytes=1)

    def test_full_decoder_failure_precedes_any_output_change(self):
        with patch.object(Image, 'open', side_effect=OSError('cannot decode')):
            self.assert_preserved(self.prior)

    def test_permissive_decoder_setting_fails_closed_without_changing_it(self):
        with patch.object(ImageFile, 'LOAD_TRUNCATED_IMAGES', True):
            self.assert_preserved(self.prior)
            self.assertTrue(ImageFile.LOAD_TRUNCATED_IMAGES)

    def test_missing_decoder_preflights_before_provider_call(self):
        original_import = builtins.__import__
        def without_pillow(name, *args, **kwargs):
            if name == 'PIL' or name.startswith('PIL.'):
                raise ImportError('Pillow unavailable')
            return original_import(name, *args, **kwargs)
        for module, core in ((gen, gen._generate_core), (edit, edit._edit_core)):
            kwargs = dict(prompt='test', api_key='unused', base_url='https://example.test', model='unused',
                          output_path=str(self.destination), max_retries=0)
            if module is edit:
                kwargs['input_image'] = str(self.destination)
            with self.subTest(module=module.__name__), patch('builtins.__import__', side_effect=without_pillow), patch.object(module, '_request_once') as request:
                result = core(**kwargs)
            self.assertFalse(result['success'])
            self.assertIn('Pillow', result['error'])
            request.assert_not_called()
            self.assertEqual(self.destination.read_bytes(), self.prior)

    def test_atomic_replace_and_fsync_failure_preserve_real_previous_image(self):
        for operation in ('replace', 'fsync'):
            with self.subTest(operation=operation), patch.object(output.os, operation, side_effect=OSError('injected write failure')):
                with self.assertRaises(OSError):
                    output.atomic_write_image(self.destination, image_bytes(color='blue'))
            self.assertEqual(self.destination.read_bytes(), self.prior)
            self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['existing.png'])

    def test_generation_and_edit_reject_corrupt_success_response(self):
        invalid = raw_png(b'\x05\0\0\0')
        response = httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(invalid).decode()}]})
        for module, core in ((gen, gen._generate_core), (edit, edit._edit_core)):
            kwargs = dict(prompt='test', api_key='unused', base_url='https://example.test', model='unused',
                          output_path=str(self.destination), max_retries=0)
            if module is edit:
                kwargs['input_image'] = str(self.destination)
            with self.subTest(module=module.__name__), patch.object(module, '_request_once', return_value=response):
                self.assertFalse(core(**kwargs)['success'])
            self.assertEqual(self.destination.read_bytes(), self.prior)


if __name__ == '__main__':
    unittest.main()
