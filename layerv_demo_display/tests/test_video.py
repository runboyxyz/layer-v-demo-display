import struct
import unittest

from app.video import FragmentedMP4, ffmpeg_command


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


class VideoTests(unittest.TestCase):
    def test_fragment_parser_keeps_init_and_emits_complete_media(self):
        parser = FragmentedMP4()
        stream = box(b"ftyp", b"brand") + box(b"moov", b"metadata") + box(b"moof", b"one") + box(b"mdat", b"pixels")
        fragments = parser.feed(stream[:13]) + parser.feed(stream[13:])
        self.assertEqual(parser.init_segment, box(b"ftyp", b"brand") + box(b"moov", b"metadata"))
        self.assertEqual(fragments, [box(b"moof", b"one") + box(b"mdat", b"pixels")])

    def test_encoder_command_is_fixed_low_latency_software_h264(self):
        command = ffmpeg_command(1280, 720, 5)
        self.assertEqual(command[0], "/usr/bin/ffmpeg")
        self.assertIn("libx264", command)
        self.assertIn("zerolatency", command)
        self.assertIn("frag_keyframe+empty_moov+default_base_moof", command)
        self.assertIn("scale=1280:720:in_range=pc:out_range=tv", command)
        self.assertIn("baseline", command)
        self.assertIn("-flush_packets", command)
        self.assertNotIn("-hwaccel", command)


if __name__ == "__main__":
    unittest.main()
