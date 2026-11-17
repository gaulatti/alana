"""Audio/video timestamp alignment of every encoder (gaulatti/alana#30).

ffmpeg zeroes each input's timestamps on its own unless told otherwise. The
PulseAudio input starts delivering about two seconds after x11grab, so zeroing
each input separately shifts audio that far ahead of video. Every encoder must
keep the inputs on their shared wallclock (-copyts) and shift all streams by one
common offset (-avoid_negative_ts make_zero) before its muxer.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from recording import capture_command


REPO = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
# Epoch-sized wallclock start times; PulseAudio starts 2 s after x11grab.
VIDEO_START = 1790000000
AUDIO_START = VIDEO_START + 2


def startup_encoders() -> dict[str, str]:
    """Each encoder's ffmpeg argument list in startup.sh, as one line."""
    text = (REPO / "startup.sh").read_text(encoding="utf-8")
    commands = {
        name: " " + " ".join(body.split()) + " "
        for name, body in re.findall(r"^\s*(rtmp|livekit)_command=\((.*?)\)$", text, re.M | re.S)
    }
    return {name: line for name, line in commands.items() if " ffmpeg " in line and "x11grab" in line}


class EncoderTimestampContractTests(unittest.TestCase):
    def test_rtmp_and_livekit_keep_inputs_on_one_clock(self):
        encoders = startup_encoders()
        self.assertEqual(set(encoders), {"rtmp", "livekit"})
        for name, line in encoders.items():
            with self.subTest(encoder=name):
                self.assertLess(line.index(" -copyts "), line.index(" -f x11grab "))
                muxers = re.findall(r"-avoid_negative_ts make_zero -f (\w+)", line)
                self.assertEqual(muxers, ["flv"] if name == "rtmp" else ["h264", "opus"])

    def test_recorder_keeps_inputs_on_one_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            command = capture_command(Path(directory), 0, 5)
        self.assertLess(command.index("-copyts"), command.index("x11grab"))
        shift = command.index("-avoid_negative_ts")
        self.assertEqual(command[shift + 1 : shift + 4], ["make_zero", "-f", "segment"])


def _flash_and_beep(root: Path) -> tuple[Path, Path]:
    """A flash and a beep at the same real instants, recorded by inputs that started 2 s apart."""
    video, audio = root / "video.mkv", root / "audio.mka"
    beep = "if(between(mod(t+2\\,10)\\,5\\,5.3)\\,0.1*sin(2*PI*1000*t)\\,0)"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "color=c=black:s=160x90:r=30:d=30,drawbox=c=white:t=fill:enable='between(mod(t,10),5,5.3)'",
         "-c:v", "libx264", "-preset", "ultrafast", "-output_ts_offset", str(VIDEO_START), str(video)],
        check=True,
    )
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc={beep}|{beep}:s=48000:d=28",
         "-c:a", "pcm_s16le", "-output_ts_offset", str(AUDIO_START), str(audio)],
        check=True,
    )
    return video, audio


def _offsets_ms(path: Path) -> list[float]:
    """Audio minus video onset, per flash/beep event, in the output's own timeline."""
    stats = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path), "-map", "0:v:0", "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stdout
    flashes, frame_time, last = [], 0.0, -10.0
    for line in stats.splitlines():
        if line.startswith("frame:"):
            frame_time = float(line.split("pts_time:")[1])
        elif "YAVG" in line and float(line.split("=")[1]) > 128:
            if frame_time - last > 1:
                flashes.append(frame_time)
            last = frame_time
    start = float(json.loads(subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=start_time", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout)["streams"][0]["start_time"])
    pcm = path.with_suffix(".wav")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-c:a", "pcm_s16le", str(pcm)], check=True)
    with wave.open(str(pcm)) as handle:
        rate, samples = handle.getframerate(), array("h", handle.readframes(handle.getnframes()))
    beeps, last = [], -10.0
    for index, sample in enumerate(samples):
        if abs(sample) > 1500:
            moment = start + index / rate
            if moment - last > 1:
                beeps.append(moment)
            last = moment
    return [(beep - min(flashes, key=lambda flash: abs(flash - beep))) * 1000 for beep in beeps]


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg and ffprobe are required to measure alignment")
class TimestampAlignmentMeasurementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.video, cls.audio = _flash_and_beep(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def encode(self, name: str, global_args: list[str], output_args: list[str]) -> Path:
        output = self.root / f"{name}.flv"
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *global_args, "-i", str(self.video), "-i", str(self.audio),
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
             "-c:a", "aac", "-ar", "48000", *output_args, "-f", "flv", str(output)],
            check=True,
        )
        return output

    def test_zeroing_each_input_puts_audio_two_seconds_ahead(self):
        offsets = _offsets_ms(self.encode("zeroed", [], []))
        self.assertEqual(len(offsets), 3)
        for offset in offsets:
            self.assertAlmostEqual(offset, -2000, delta=40)

    def test_shared_wallclock_keeps_every_event_within_40_ms(self):
        output = self.encode("aligned", ["-copyts"], ["-avoid_negative_ts", "make_zero"])
        offsets = _offsets_ms(output)
        self.assertEqual(len(offsets), 3)
        for offset in offsets:
            self.assertLessEqual(abs(offset), 40, offsets)
        start = json.loads(subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "stream=start_time", "-of", "json", str(output)],
            capture_output=True, text=True, check=True,
        ).stdout)["streams"]
        self.assertEqual(min(float(stream["start_time"]) for stream in start), 0.0, "streams start at 0, not at the epoch")

    def test_recorder_segments_keep_their_cadence_under_shared_wallclock(self):
        with tempfile.TemporaryDirectory() as directory:
            command = capture_command(Path(directory), 0, 5)
        keyframes = command[command.index("-force_key_frames") + 1]
        segments = self.root / "segments"
        segments.mkdir()
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-copyts", "-i", str(self.video), "-i", str(self.audio),
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "ultrafast", "-g", "150", "-sc_threshold", "0",
             "-force_key_frames", keyframes, "-c:a", "aac", "-avoid_negative_ts", "make_zero",
             "-f", "segment", "-segment_format", "matroska", "-segment_time", "5", "-reset_timestamps", "1",
             str(segments / "segment-%06d.mkv")],
            check=True,
        )
        first = segments / "segment-000000.mkv"
        flags = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v", "-show_entries", "packet=flags", "-of", "csv=p=0", str(first)],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertEqual(sum("K" in flag for flag in flags), 1, "one forced keyframe per 5 s segment, not one per frame")
        self.assertGreaterEqual(len(list(segments.glob("segment-*.mkv"))), 5)


if __name__ == "__main__":
    unittest.main()
