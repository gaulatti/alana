# Local rendered-timecode latency benchmark

This harness measures **software display to software display**. It does not
measure a camera, a physical panel, HDMI, a public platform's player or WAN
latency. No production baseline or physical glass-to-glass qualification is
claimed. The JSON always records this boundary explicitly.

A browser draws a 16-bit sync marker and a 48-bit UTC millisecond timestamp in
pixels every animation frame. The unmodified Alana `startup.sh` captures that
browser with X11/PulseAudio and publishes its normal independent RTMP encoders
and one concurrent LiveKit encoder. FFplay displays each received RTMP stream;
a separate Chromium window displays the subscribed LiveKit track. The observer
reads those **receiver framebuffers**, decodes the pixel marker and subtracts
source time from receiver observation time. Room readiness, track discovery,
packet arrival and FFmpeg progress are never latency samples.

The case count is 1, 3 or 5 **RTMP outputs plus the existing single LiveKit leg**.
Alana has one LiveKit publisher, not a fan-out of five independent rooms. All
RTMP receivers are measured separately. Both sides share the container kernel's
UTC clock, avoiding a guessed cross-machine clock offset. Observing at 30 fps
adds up to one observation interval to a sample; browser composition and player
buffering remain part of the measurement. The result is not a pure network or
encoder latency measurement. FFplay uses its existing defaults, not tuned
low-latency playback flags.

## Prerequisites and one command

Use Docker in the existing default Colima profile on macOS. No extra VM or host
configuration is created. Build the repository's existing Dockerfile, and have
the existing `livekit/livekit-server:v1.13.4` image available. The harness checks
that every publisher source script in the image matches this checkout.

Supply an already installed **livekit-client 2.21.0** UMD artifact from
`node_modules/livekit-client/dist/livekit-client.umd.js`. The observer uses that
public client SDK only; it does not add an application/package dependency,
change a lockfile or copy/vendor the SDK into this repository. Its exact version
and SHA-256 are included in the report. It is mounted read-only and served only
inside the disposable worker. The pinned Alana image supplies FFmpeg, FFplay,
Chromium, Xvfb, Python and LiveKit CLI 2.18.2. No npm, pip or production login is
performed by the benchmark.

After these explicit prerequisites, from the repository root:

```sh
python3 test/latency/run.py --image alana:local \
  --sdk /absolute/path/to/livekit-client/dist/livekit-client.umd.js \
  --output test/latency/baseline.json
```

The command runs 1, 3 and 5 output cases and prints p50/p95/p99, the number of
unique rendered source timestamps and publisher CPU. Defaults are 10 seconds of
warmup after **every** receiver has displayed a valid beacon, then 30 seconds of
measurement per case. For a short diagnostic use `--outputs 1 --seconds 4
--warmup 1`, writing outside the committed baseline. A missing receiver, invalid
clock sample or insufficient changing timestamps fails the command and produces
no baseline. A frozen last frame cannot manufacture samples. Percentiles use
nearest rank, without interpolation, and empty samples are errors.

The reference profile is 640x360 at 30 fps, CPU libx264, with the publisher's
remaining encoding arguments unchanged. Profile, actual image IDs, runtime
versions, source commit, checkout cleanliness, host hardware and Docker CPU/memory
allocation are recorded. A diagnostic from a dirty checkout is labelled as such;
commit the harness before collecting the accepted baseline so its source commit
can be reproduced. A baseline commit may then add only the measured JSON.

CPU is the publisher's process session (Chromium, capture, encoder and its other
children), measured from Linux process CPU ticks. 100% means one CPU core. The
receiver/observer and LiveKit server are excluded from that CPU figure but still
share the same machine and compete for its resources. Cross-machine hardware,
allocation, resolution, software/version and player differences make baselines
incomparable without qualification. No performance threshold or optimization is
introduced by this test-only work.

## Isolation, credentials and cleanup

Each invocation creates an internal Docker network with no published host port.
It uses new private temporary credential files (0600 inside a private directory)
for the local server and control validation. No real credential, cloud service,
owner browser session, existing program or public output is used. Local browser
pages use the fictional Harbor Light fixture. Tokens/keys, environment contents,
raw Docker/server logs and URLs never enter the JSON or terminal diagnostics.
The source page and SDK never fetch a remote service.

The worker invokes the actual publisher startup script directly for a local
measurement; it does not claim to verify lifecycle API authentication or
Croccante readiness. On success, failure or interruption it removes its own
containers and network and deletes its temporary credential store. It never
prunes another task's containers, volumes, images or shared build cache.

Run the pixel-decoder/percentile boundary tests with:

```sh
python3 -m unittest discover -s test/latency -p 'test_*.py'
```

## Evidence status

The initial end-to-end qualification and committed baseline are pending. Do not
use an absent report, a test pass or an image build as measured latency.
