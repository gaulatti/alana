# Alana review contract

Alana is Alcantara's headless Program renderer and publisher. One runtime owns
one configured Alcantara Program, captures it with Chromium, Xvfb and PulseAudio,
and publishes it through independent RTMP encoders and an optional LiveKit
Program feed. It is lifecycle-controlled by authenticated Start and Stop
commands, relays immutable television filler versions to Croccante without
transcoding or storing media, and never resolves a broadcast credential
reference or prints a stream key or renderer URL.

Preserve the private control contract (bearer-token authentication, Start only
after the local pipeline is ready and Croccante acknowledged the session, Stop
acknowledged before teardown), per-destination isolation (one RTMP failure
cannot restart another output or the LiveKit leg), the absence of radio, the
credential-free logs and metrics, and bounded restarts. Bug fixes, tests,
observability, documentation and dependency maintenance serve this existing
runtime without a new product decision. A new output type, a new control
operation, a new persistence path or a change to the credential boundary needs
documentation or a linked requirement that establishes its need and limits.

Assess purpose and evidence rather than the number of changed files or lines.
Ask gaulatti for a second opinion for a product-direction or authority decision,
for example a change to the Start/Stop acknowledgement order, the credential
boundary, or the publishing topology. Ordinary implementation uncertainty and
repairable bugs belong to automated investigation, not routine human code
review. Changes to review controls (this contract, `.github/clouseau.json`, the
CI workflow, the local recipe) require explicit owner confirmation.

Every material capability and mechanism must independently score at least 80
for fit, need and proportionality under Clouseau's shared protocol. A causal
technical finding at or above 70 blocks; unrelated features or good scores
cannot average away a defect. Integrity and review-completeness gates remain
independent.

Tests must assert actual behaviour of the Python supervisors and shell scripts.
Reject fabricated coverage, disabled tests, coverage pragmas or exclusions,
swallowed errors, unconditional assertions and mocks that replace the behaviour
being claimed. The runtime contract, the unit tests under coverage, the native
image builds and the private metrics scrape remain mandatory, and every pull
request must keep exactly 100% line and branch coverage of the Python
application sources; a pull request below that stays for human review.
