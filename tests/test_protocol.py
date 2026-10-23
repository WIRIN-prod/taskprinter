from PIL import Image, ImageDraw

from taskprinter import protocol as p
from taskprinter.imaging import bitmap_rows, to_bitmap


def test_packets_match_documented_vectors():
    assert p.packet(p.Cmd.DEVICE_STATE, b"\x00").hex(" ") == "51 78 a3 00 01 00 00 00 ff"
    assert p.packet(p.Cmd.QUALITY, b"\x33").hex(" ") == "51 78 a4 00 01 00 33 99 ff"
    assert p.packet(p.Cmd.ENERGY, p.u16(17520)).hex(" ") == "51 78 af 00 02 00 70 44 79 ff"
    assert p.packet(p.Cmd.DRAW_MODE, b"\x00").hex(" ") == "51 78 be 00 01 00 00 00 ff"
    assert p.packet(p.Cmd.SPEED, b"\x23").hex(" ") == "51 78 bd 00 01 00 23 e9 ff"


def test_rows_are_msb_first_black_is_one():
    img = Image.new("1", (384, 1), 1)
    img.putpixel((0, 0), 0)  # leftmost pixel black
    rows = bitmap_rows(img)
    assert len(rows) == 1 and len(rows[0]) == 48
    assert rows[0][0] == 0x80 and not any(rows[0][1:])
    raw = p.row_packet(rows[0], 384, "raw")
    assert raw[2] == 0xA2 and raw[6] == 0x80
    assert p.row_packet(rows[0], 384, "raw-lsb")[6] == 0x01


def test_rle_round_trip():
    row = bytes([0xFF] * 10 + [0x00] * 38)
    rle = p.encode_rle(row, 384)
    pixels = []
    for b in rle:
        pixels += [b >> 7] * (b & 0x7F)
    assert len(pixels) == 384
    assert pixels[:80] == [1] * 80 and pixels[80:] == [0] * 304


def test_build_job_sequence():
    rows = [bytes(48)] * 3
    job = p.build_job(rows, p.PrintSettings())
    cmds = [cmd for cmd, _ in p.iter_packets(job)]
    assert cmds[:5] == [0xA3, 0xA4, 0xAF, 0xBE, 0xBD]
    assert cmds[5:8] == [0xA2] * 3
    assert cmds[8:] == [0xA1]  # 90 dots fits in one packet
    bd = p.build_job(rows, p.PrintSettings(feed=5, feed_mode="bd"))
    assert [cmd for cmd, _ in p.iter_packets(bd)][8:] == [0xBD] * 5
    assert 0xA6 not in cmds


def test_retract_comes_right_after_wake():
    job = p.build_job([bytes(48)], p.PrintSettings(retract=150))
    packets = list(p.iter_packets(job))
    assert packets[0][0] == 0xA3
    assert [(c, pl[0] | pl[1] << 8) for c, pl in packets[1:3]] == [(0xA0, 100), (0xA0, 50)]
    assert packets[3][0] == 0xA4


def test_backlash_pulls_back_extra_and_prints_blank_rows():
    content = [bytes([0xFF] * 48)]
    job = p.build_job(content, p.PrintSettings(retract=90, backlash=24, feed=0))
    packets = list(p.iter_packets(job))
    assert (packets[1][0], packets[1][1][0] | packets[1][1][1] << 8) == (0xA0, 100)
    assert (packets[2][0], packets[2][1][0] | packets[2][1][1] << 8) == (0xA0, 14)  # 90 + 24
    rows = [pl for c, pl in packets if c == 0xA2]
    assert len(rows) == 25 and not any(any(r) for r in rows[:24]) and rows[24] == bytes([0xFF] * 48)
    plain = p.build_job(content, p.PrintSettings(backlash=24, feed=0))  # no pull-back: no slack rows
    assert len([c for c, _ in p.iter_packets(plain) if c == 0xA2]) == 1


def test_feed_a1_chunks_to_100():
    data = p.feed_packets(250, "a1")
    payloads = [pl for cmd, pl in p.iter_packets(data)]
    assert [pl[0] | pl[1] << 8 for pl in payloads] == [100, 100, 50]


def test_status_heuristics():
    assert p.decode_status(bytes([0x00, 80])) == {"ready": True, "paper_out": False, "battery": 80}
    assert p.decode_status(bytes([0x04]))["paper_out"] is True


def test_image_flattens_transparency_to_white():
    img = Image.new("RGBA", (100, 50), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((10, 10, 20, 20), fill=(0, 0, 0, 255))
    bitmap = to_bitmap(img, 384, dither="threshold")
    assert bitmap.width == 384
    rows = bitmap_rows(bitmap)
    black = sum(bin(b).count("1") for r in rows for b in r)
    assert 0 < black < 384 * len(rows) * 0.1
