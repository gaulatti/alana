"""The one audio chain: its settings, and its measured result on 60 s fixtures with the real ffmpeg."""

import io
import re
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
import wave
from array import array
from contextlib import redirect_stdout
from pathlib import Path

import audio_chain


FFMPEG = shutil.which("ffmpeg")
# Speech-like: band-limited pink noise with syllable modulation and pauses, mono in both channels.
SPEECH = (
    "anoisesrc=color=pink:amplitude=0.4:sample_rate=44100:seed=7:duration=60,"
    "bandpass=f=900:width_type=o:w=3,"
    "volume='(0.35+0.65*pow(abs(sin(2*PI*3.7*t)),2))*gt(sin(2*PI*0.21*t)+0.55*sin(2*PI*0.53*t),-0.6)':eval=frame,"
    "pan=stereo|c0=c0|c1=c0"
)
# Music-like: decorrelated stereo chords that change every 2 s, a slow swell and a percussive tick.
MUSIC = (
    "aevalsrc='(0.6+0.4*sin(2*PI*t/17))*(0.3*sin(2*PI*(220+55*floor(mod(t/2,4)))*t)+0.15*sin(4*PI*(220+55*floor(mod(t/2,4)))*t)"
    "+0.2*sin(2*PI*330*t)+0.1*sin(2*PI*165*t)+0.08*sin(2*PI*880*t)*pow(1-mod(t*2,1),6))"
    "|(0.6+0.4*sin(2*PI*t/17))*(0.3*sin(2*PI*(277+55*floor(mod(t/2,4)))*t)+0.15*sin(2*PI*440*t)"
    "+0.2*sin(2*PI*330*t)+0.1*sin(2*PI*110*t)+0.08*sin(2*PI*1320*t)*pow(1-mod(t*2,1),6))':s=44100:d=60"
)
SUMMARY = re.compile(r"^\s+I:\s+(?P<integrated>-?[\d.]+) LUFS.*?^\s+Peak:\s+(?P<peak>-?[\d.]+|-inf) dBFS", re.S | re.M)


class AudioChainSettingsTests(unittest.TestCase):
    def test_defaults_are_on_minus_16_lufs_and_minus_1_dbtp(self):
        self.assertTrue(audio_chain.enabled({}))
        self.assertTrue(audio_chain.enabled({"ALANA_PROCESSED_AUDIO": ""}))
        self.assertEqual(audio_chain.target_lufs({}), -16.0)
        self.assertEqual(audio_chain.true_peak_ceiling({}), -1.0)

    def test_kill_switch_and_configured_values(self):
        self.assertFalse(audio_chain.enabled({"ALANA_PROCESSED_AUDIO": "off"}))
        self.assertEqual(audio_chain.target_lufs({"ALANA_LOUDNESS_TARGET": "-23"}), -23.0)
        self.assertEqual(audio_chain.true_peak_ceiling({"ALANA_TRUE_PEAK_CEILING": "-2"}), -2.0)

    def test_invalid_settings_fail_closed(self):
        with self.assertRaises(ValueError):
            audio_chain.enabled({"ALANA_PROCESSED_AUDIO": "yes"})
        for name, value in (
            ("ALANA_LOUDNESS_TARGET", "loud"),
            ("ALANA_LOUDNESS_TARGET", "-40"),
            ("ALANA_LOUDNESS_TARGET", "nan"),
            ("ALANA_TRUE_PEAK_CEILING", "0"),
        ):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                audio_chain.configured_graph("1:a:0", "aout", {name: value})

    def test_graph_is_one_definition_from_input_to_labelled_output_with_a_side_meter(self):
        graph = audio_chain.filter_graph("1:a:0", "aout", -16.0, -1.0)
        self.assertTrue(graph.startswith("[1:a:0]aresample=48000,"))
        self.assertIn("sidechaincompress=threshold=0.025119:", graph)
        self.assertIn("makeup=5.623413:", graph)
        self.assertIn("alimiter=limit=0.841395:", graph)
        # Processed on a copy rebased to 0, then put back on the input's timeline (#34).
        self.assertIn("[1:a:0]aresample=48000,asplit=2[timeline][work];[work]asetpts=PTS-STARTPTS,asplit=2[program][key];", graph)
        self.assertIn("aresample=48000[processed];[timeline]volume=0[carrier];", graph)
        self.assertIn("[carrier][processed]amix=inputs=2:normalize=0:duration=first:dropout_transition=0,asplit=2[aout][meter];", graph)
        self.assertTrue(graph.endswith("[meter]ebur128=peak=true:metadata=1,ametadata=mode=print:file=/dev/stderr,anullsink"))

    def test_command_prints_the_configured_graph_and_rejects_bad_use(self):
        output = io.StringIO()
        with unittest.mock.patch.dict("os.environ", {}, clear=True), redirect_stdout(output):
            self.assertEqual(audio_chain.main(["filter", "1:a:0", "aout"]), 0)
        self.assertEqual(output.getvalue().strip(), audio_chain.filter_graph("1:a:0", "aout", -16.0, -1.0))
        with self.assertRaises(SystemExit):
            audio_chain.main(["filter", "1:a:0"])
        with unittest.mock.patch.dict("os.environ", {"ALANA_LOUDNESS_TARGET": "-99"}), self.assertRaises(SystemExit) as raised:
            audio_chain.main(["filter", "1:a:0", "aout"])
        self.assertIn("ALANA_LOUDNESS_TARGET", str(raised.exception))

    def test_script_entry_point_exits_with_the_command_status(self):
        import runpy
        import sys

        with unittest.mock.patch.object(sys, "argv", ["audio_chain.py", "filter", "1:a:0", "aout"]), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            runpy.run_path(audio_chain.__file__, run_name="__main__")
        self.assertEqual(raised.exception.code, 0)


def measure(path: Path, graph: str | None = None) -> tuple[float, float]:
    """Return (integrated LUFS, true peak dBTP) of ``path`` after the chain graph, if any."""
    complex_graph = f"{graph};[chain]ebur128=peak=true[out]" if graph else "[0:a]ebur128=peak=true[out]"
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-filter_complex", complex_graph, "-map", "[out]", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    final = list(SUMMARY.finditer(result.stderr))[-1]
    return float(final.group("integrated")), float(final.group("peak"))


@unittest.skipUnless(FFMPEG, "ffmpeg is required to measure the audio chain")
class AudioChainMeasurementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.fixtures = {}
        for name, source in (("speech", SPEECH), ("music", MUSIC)):
            raw = root / f"{name}-raw.wav"
            subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source, "-c:a", "pcm_f32le", str(raw)], check=True)
            integrated, _ = measure(raw)
            # Deliberately 6 dB too hot for the -16 LUFS default.
            hot = root / f"{name}-hot.wav"
            subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-af", f"volume={-10 - integrated:.2f}dB", "-c:a", "pcm_f32le", str(hot)],
                check=True,
            )
            cls.fixtures[name] = hot

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_fixtures_are_six_db_too_hot(self):
        for path in self.fixtures.values():
            self.assertAlmostEqual(measure(path)[0], -10.0, delta=0.2)

    def test_hot_speech_and_music_land_within_one_lu_of_minus_16_under_minus_one_dbtp(self):
        graph = audio_chain.filter_graph("0:a", "chain", -16.0, -1.0)
        for name, path in self.fixtures.items():
            with self.subTest(fixture=name):
                integrated, true_peak = measure(path, graph)
                self.assertLessEqual(abs(integrated + 16), 1.0, f"{name} measured {integrated} LUFS")
                self.assertLessEqual(true_peak, -1.0, f"{name} true peak {true_peak} dBTP")

    def test_configured_target_moves_the_programme_level(self):
        integrated, true_peak = measure(self.fixtures["speech"], audio_chain.filter_graph("0:a", "chain", -23.0, -1.0))
        self.assertLessEqual(abs(integrated + 23), 1.0, f"speech measured {integrated} LUFS")
        self.assertLessEqual(true_peak, -1.0)

    def test_chain_output_is_48_khz_stereo_and_adds_no_lookahead_delay(self):
        burst = "if(between(t\\,1\\,1.5)\\,0.1*sin(2*PI*1000*t)\\,0)"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "burst.wav"
            subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc={burst}|{burst}:s=44100:d=3",
                 "-filter_complex", audio_chain.filter_graph("0:a", "out", -16.0, -1.0), "-map", "[out]", "-c:a", "pcm_s16le", str(output)],
                check=True,
                capture_output=True,
            )
            with wave.open(str(output)) as handle:
                self.assertEqual((handle.getframerate(), handle.getnchannels()), (48000, 2))
                samples = array("h", handle.readframes(handle.getnframes()))
        onset = next(index for index in range(0, len(samples), 2) if abs(samples[index]) > 300) // 2
        self.assertLessEqual((onset - 48000) * 1000 / 48000, 10.0)

    def test_chain_keeps_the_input_timeline_and_its_samples_in_place(self):
        """Every encoder keeps its inputs on their shared wall clock (-copyts, #30), so
        the chain sees epoch-sized timestamps, and audio that opened after video starts
        later than 0. Both must come out as they went in (#34): before, ffmpeg 5.1's
        sidechaincompress restarted at 0 and the 4x resampler overflowed at epoch size,
        so the encoder placed audio ahead of video."""
        burst = "if(between(t\\,1\\,1.05)\\,0.1*sin(2*PI*1000*t)\\,0)"
        for start in (2.5, 1795431700.0):
            with self.subTest(start=start), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "timeline.mkv"
                subprocess.run(
                    [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-copyts", "-f", "lavfi",
                     "-i", f"aevalsrc={burst}|{burst}:s=44100:d=3,asetpts=PTS+{start}/TB",
                     "-filter_complex", audio_chain.filter_graph("0:a", "out", -16.0, -1.0), "-map", "[out]",
                     "-c:a", "pcm_s16le", "-f", "matroska", str(output)],
                    check=True,
                    capture_output=True,
                )
                probed = subprocess.run(
                    [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries", "stream=start_time", "-of", "csv=p=0", str(output)],
                    capture_output=True, text=True, check=True,
                ).stdout
                self.assertAlmostEqual(float(probed), start, delta=0.002)
                pcm = Path(directory) / "timeline.wav"
                subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(output), str(pcm)], check=True)
                with wave.open(str(pcm)) as handle:
                    samples = array("h", handle.readframes(handle.getnframes()))
                onset = next(index for index in range(0, len(samples), 2) if abs(samples[index]) > 300) // 2
                self.assertLessEqual(abs(onset - 48000) * 1000 / 48000, 10.0, f"burst {onset / 48:.1f} ms into the output")


if __name__ == "__main__":
    unittest.main()
