import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZIP_BZIP2, ZipFile
import zlib

import voicepack


def sample(stereo=False, marker=0):
    # Synthetic MPEG headers and payload for testing metadata, without recorded sounds.
    raw_header = "fffb9404" if stereo else "fffb94c4"
    return (bytes.fromhex(raw_header) + bytes(379) + bytes([marker])) * 5


def archive(path, tracks, *, wrong_md5=False, compression=0):
    sections = []
    with ZipFile(path, "w", compression=compression) as result:
        for name, data in tracks.items():
            result.writestr(name, data)
            checksum = hashlib.md5(data).hexdigest() if not wrong_md5 else "0" * 32
            sections.append(f"[{name}]\nMD5={checksum}\n\n")
        result.writestr(voicepack.MANIFEST, "".join(sections))


class VoicePackTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.stock = self.root / "stock.zip"
        self.result = self.root / "custom.zip"
        self.clips = self.root / "clips"
        self.clips.mkdir()

    def test_build_preserves_other_tracks(self):
        archive(self.stock, {"first.mp3": sample(), "second.mp3": sample(marker=1)})
        replacement = sample(marker=2)
        (self.clips / "first.mp3").write_bytes(replacement)
        voicepack.build(self.stock, self.clips, self.result)
        self.assertEqual(voicepack.read_pack(self.result).tracks,
                         {"first.mp3": replacement, "second.mp3": sample(marker=1)})

    def test_stereo_template_and_verified_v19_mono(self):
        archive(self.stock, {"first.mp3": sample(stereo=True)})
        (self.clips / "first.mp3").write_bytes(sample())
        with self.assertRaisesRegex(ValueError, "формат шаблона"):
            voicepack.build(self.stock, self.clips, self.result)
        self.assertFalse(self.result.exists())
        voicepack.build(self.stock, self.clips, self.result, voicepack.V19)
        self.assertEqual(voicepack.read_pack(self.result).formats["first.mp3"].channels, 1)

    def test_reject_mismatched_checksums(self):
        archive(self.stock, {"first.mp3": sample()}, wrong_md5=True)
        with self.assertRaisesRegex(ValueError, "MD5"):
            voicepack.read_pack(self.stock)

    def test_reject_unsupported_compression(self):
        archive(self.stock, {"first.mp3": sample()}, compression=ZIP_BZIP2)
        with self.assertRaisesRegex(ValueError, "Deflate"):
            voicepack.read_pack(self.stock)

    def test_windows_manifest_line_endings_and_utf8_bom(self):
        audio = sample()
        checksum = hashlib.md5(audio).hexdigest()
        with ZipFile(self.stock, "w") as result:
            result.writestr("first.mp3", audio)
            result.writestr(voicepack.MANIFEST, f"[first.mp3]\r\nmD5={checksum}\r\n".encode("utf-8-sig"))
        self.assertEqual(voicepack.read_pack(self.stock).tracks, {"first.mp3": audio})

    def test_explains_manifest_encoding_error(self):
        with ZipFile(self.stock, "w") as result:
            result.writestr("first.mp3", sample())
            result.writestr(voicepack.MANIFEST, b"\xff")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            voicepack.read_pack(self.stock)

    def test_does_not_read_excess_replacement(self):
        archive(self.stock, {"first.mp3": sample()})
        (self.clips / "first.mp3").write_bytes(bytes(5000))
        with patch.object(voicepack, "MAX_UNPACKED_BYTES", 4096):
            with self.assertRaisesRegex(ValueError, "превысит лимит"):
                voicepack.build(self.stock, self.clips, self.result)
        self.assertFalse(self.result.exists())

    def test_explains_decompression_error(self):
        archive(self.stock, {"first.mp3": sample()})
        with patch.object(ZipFile, "read", side_effect=zlib.error("bad stream")):
            with self.assertRaisesRegex(ValueError, "повреждённый ZIP"):
                voicepack.read_pack(self.stock)

    def test_supports_only_safe_root_track_names(self):
        self.assertTrue(voicepack.valid_track_name("43sound_I_am_here.mp3"))
        self.assertFalse(voicepack.valid_track_name("folder/track.mp3"))
        self.assertFalse(voicepack.valid_track_name("NUL.mp3"))
        self.assertFalse(voicepack.valid_track_name("lpt1.test.mp3"))
        self.assertFalse(voicepack.valid_track_name("K.mp3"))


if __name__ == "__main__":
    unittest.main()
