# Backend decision — 2026-10-24

Decision: v0.1 controls a separately installed `pjsua` executable over stdin and
reads its SIP trace from a private pseudo-terminal (PTY). No pjproject code or binary is
copied, linked, downloaded at runtime, or included in sipling distributions.
The Python package is MIT. This decision was written before implementation;
the attempted first commit was blocked by the workspace’s read-only .git policy.

## Evidence and alternatives

PyPI JSON returned HTTP 404 for `sipling` on 2026-10-24; availability is not a
reservation. `pjsua2-python` 2.15.1.post1 exists and publishes CPython 3.8–3.12
wheels for macOS arm64 (14+), macOS x86_64 (15+), manylinux2014 x86_64 and Windows.
It is a young community project, not an upstream support commitment, and lacks
3.13/3.14 coverage. `pjsua2-wheel` primarily supplies Windows wheels.
Sources: https://pypi.org/pypi/pjsua2-python/json and
https://pypi.org/project/pjsua2-wheel/ .

PJSIP is GPL-2.0-or-later or commercially licensed; third-party components carry
additional terms. MIT is GPL-compatible, but MIT licensing our wrapper does not
relicense a combined native distribution. Shipping linked bindings/wheels under
an MIT-only promise is inappropriate: GPL distribution obligations or a suitable
commercial agreement need review first. An optional dependency is not a legal
workaround. No `native` extra is offered until that decision and wheel coverage
are resolved. See https://www.pjsip.org/licensing.htm and
https://docs.pjsip.org/en/latest/overview/license_faq.html .

Separate programs communicating via ordinary process pipes/files provide a
stronger separation than linking. This is our architectural judgment, not a
blanket legal exemption: bundling pjsua or tightly integrating modified GPL code
would require another review. Users obtain and comply with their own pjsua
license. No claim is made that a proprietary native integration is cleared.

A pure Python SIP/RTP implementation would avoid linking but adds transaction,
retransmission, SDP, codec, dialog, NAT and interoperability risk. It is not
justified while the proven pjsua path works. Backend Protocol keeps future
implementations possible without promising unimplemented extras.

## Runtime contract

One child process per call; UDP or TCP explicitly selected. No registration,
redirect following, STUN, ICE or credentials configuration. Bind 0.0.0.0 and use
an ephemeral signaling port. Offer G722, PCMU, PCMA audio (plus pjsua’s telephone-event payload). Null sound device makes
headless operation deterministic and sends silence for RTP latching. TCP avoids
large UDP INVITE fragmentation. NAT requires a platform that routes responses to
the source connection/address and latches RTP to the observed source; this is
not universal NAT traversal.

`call()` waits for an INVITE final response or setup timeout, leaves answered
calls open, and cleans failed calls. Timings start at outbound INVITE; ring time
is first 180, 183 or successful final response. Timeout is setup-only. Caller
must use a context manager or hangup. BYE success requires observing the BYE
transaction's 2xx, or responding 2xx to a remote BYE. INVITE final code is never
overwritten by BYE responses. Ladder stores method/status summaries rather than
raw identities, headers or bodies. Debug export is a JSON ladder, not pcap.

Inbound energy is measured from decoded signed PCM recorded by pjsua with a
null microphone. It proves energy, not speech or agent correctness. Polling the
recorder introduces buffering latency. The window starts when heard_audio is
called; energy must be observed in that window, above a configurable normalized
RMS floor. No outgoing playback is included in v0.1. Private temporary recordings are deleted on hangup; they necessarily contain sensitive data while
running. PTY output is consumed privately, never echoed. A PTY is needed because pjsua
buffers file/pipe logs; macOS and Linux are supported in v0.1, Windows is deferred.
Child argv is visible to local process inspection; logs/JSON default
to redacting SIP userinfo. No raw child output is printed on errors.

## Verification and scope

Unit parser/privacy/lifecycle tests plus real pjsua against a loopback mock SIP
server exercise TCP/UDP, rejection, silence/audio and BYE. Live tests are opt-in
with --live and SIPLING_TEST_URI; never dial a fabricated production URI.
Production validation awaits a human-provided URI and permission to use network
services (this run is local-only). CI builds a checksum-pinned, separate
headless pjproject 2.17 executable for integration tests.

v0.2: verified TLS/5061 (CA and hostname validation), DTMF, WAV playback API,
native licensing decision and supported prebuilt wheel matrix. Pytest fixture
and live marker ship in v0.1. No publishing workflow is included.

## v0.2 decision — clocked helper, 2026-10-24

The streaming API uses a small long-running helper built separately against the
user's existing pjproject 2.17 **pjsua C library**. This is a variant of strategy 2:
an explicit native helper executable with a null sound clock and custom conference
port, rather than trying to reimplement RTP in Python or use a loopback OS sound
device. It is not the pjsua CLI and not pjsua2/Python bindings. The CLI subprocess
backend remains intact as `probe()` (the CLI and pytest `sip_caller` also retain
it). `call()` now returns the full asynchronous streaming handle required by
an unchanged earshot `SiplingTransport`. This is an explicit v0.2 Python API change.
All previous probe assertions still run; tests migrate their import to `probe`.

### Why not incremental CLI WAVs?

Inspection of the existing growing recorder and upstream WAV/player/CLI paths
showed no reliable consumption acknowledgment or atomic per-frame playback
submission through the CLI. Recorder stdio buffers expose data in bursts and
queued WAV players introduce file/bridge scheduling and gaps. v0.1's tests prove
energy observation, not bounded delivery latency. We did not establish an honest
20 ms bidirectional bound for that path and make no claimed CLI latency measurement.
Adding Python sleeps to drain recorder bursts would move apparent energy onsets
and corrupt turn-gap/barge-in measurements. Native pjsua2 is unnecessary: the
small external helper provides a directly clocked port using the same local build.

### Media and lifecycle contract

The helper offers PCMU/8000 only, telephone-event negotiation, UDP or TCP and no
registration. pjproject owns SIP/SDP, RTP, G.711 conversion, jitter buffering,
symmetric RTP and RFC2833/4733 generation. Python never implements RTP/codec logic.
The custom port exchanges exactly 160 signed samples every 20 ms at 8 kHz. IPC
converts native signed samples to/from **little endian** explicitly; stereo and
resampling are not exposed. This matches earshot's 8 kHz mocks, although its actual
Protocol accepts any positive `sample_rate`; 20 ms was a recommendation that we
adopted as an invariant. Null clock/port callbacks supply actual silence frames
before answer and during quiet, not an absence sentinel. Receive is clock-driven,
not a loop that invents timestamps from buffered samples.

A private inherited Unix datagram socketpair carries `P + uint64 monotonic-ns +
320 PCM bytes` inbound, `S + uint32 sequence + 320 PCM bytes` outbound, and
`A + sequence + uint64 consumption monotonic-ns` acknowledgments. Integers use
network byte order. Both ends explicitly use POSIX `CLOCK_MONOTONIC` for IPC age
checks, avoiding any platform difference in Python's default monotonic clock. The helper acknowledges a frame at `get_frame` consumption.
Python permits one outstanding frame and waits for that ACK before submitting the
next, then waits 20 ms for the last frame's playback interval. A partial final
frame is silence padded, so duration rounds up to 20 ms. Calls with irregular or
late Python scheduling may transmit silence in a missed slot; they are never
accelerated to conceal a stall. No file is used for streaming and nothing needs
to be tail-read. DTMF acknowledgment means queued by pjsua, not yet understood by
the peer; the real fixture independently checks received digits and method.

The receive queue holds at most five frames and fails on overflow or delivery age
above 60 ms. The age starts in the native callback, so kernel socket backlog
during a Python process pause cannot be mistaken for fresh media. ACK timestamps
also fail sends when consumption drifts more than 60 ms from the submitted PCM
timeline or acknowledgment delivery is more than 60 ms late. One reader is enforced. The helper fails rather than blocking its
media clock or dropping IPC frames on a full socket. Callers must continuously
consume receive, as earshot does. Python scheduling and pjmedia jitter buffers
still affect measurements; there is no hard real-time or one-way latency claim.
See root `INTEGRATION.md` and `integration-timing.json` for measured host bounds.

`call` returns after process launch, without waiting for DNS, ringing or answer.
A setup-only monotonic watchdog prevents late answer after timeout. Locked event
snapshots expose `dialed`, `ringing`, `answered`, `hangup_clean`/`hangup_abnormal`
with `time.monotonic()` observations. Direct answer also supplies a ringing event
at the same timestamp. There is no transfer inference. A disconnected state is
not proof of clean BYE: a pjproject transaction callback or the existing private
SIP trace parser must observe BYE 2xx. If disconnection precedes that evidence,
receive remains open until evidence arrives or a four-second grace expires;
termination is published before receive ends. `hangup` immediately signals receive
and send waiters, independently of process cleanup/peer response. Cleanup attempts
BYE and orderly destroy, then kills/reaps after four seconds. Dead helpers and
media-consumer overruns surface as sanitized receive errors.

Raw helper diagnostics/SIP traces are consumed privately, never printed, stored
in artifacts or included in exceptions. The existing trace parser retains only
sanitized summaries after parsing. Public URI defaults retain userinfo redaction;
explicit no-redact and the process-argv visibility caveat remain. earshot owns
any persisted audio recording; streaming sipling itself creates no recording.

### Additional licensing surface

The original helper source in `helpers/stream.c` and its build recipe are MIT.
It includes public pjproject headers but ships no upstream source, headers,
object files, archives or executables. The Python wheel remains pure Python with
zero runtime dependencies. The sdist includes our helper **source**, not a compiled
helper. Users build the helper explicitly; pip never fetches/builds native code.

The resulting linked executable is **not MIT-only**: it links pjproject and its
third-party codec libraries and must satisfy GPL-2.0-or-later distribution terms
or an appropriate commercial license, plus relevant third-party notices. Keeping
it optional or out of process does not remove those obligations. MIT source is
GPL-compatible, but does not relicense upstream code or imply closed-source
redistribution clearance. Local internal testing is the only use verified here.
Any helper/native binary redistribution needs a fresh dependency/license audit
and compliant source/notices or commercial terms. No native wheels or pjsua2 extra
are offered, and no pjproject is bundled. Rechecked upstream's
[dual-license statement](https://www.pjsip.org/licensing.htm) and
[licensing FAQ](https://docs.pjsip.org/en/latest/overview/license_faq.html) on
2026-10-24; upstream separately calls out third-party terms and internal-use versus
redistribution. This architecture is not a blanket legal exemption.

### Local fixture limits

A separate unmodified pjsua CLI auto-answers loopback, plays a greeting WAV and
allows explicit conference disconnect/reconnect and RTP echo. It receives actual
RFC2833 digits. Reply timing is commanded by the test, not speech recognition.
There is no stop-on-energy agent: we test a non-yielding barge-in failure, but
cannot validate a successful interruption policy or semantic reply correctness.
TLS/SRTP, credentials, production NAT, loss/jitter robustness and codecs other
than PCMU remain outside the streaming proof. POSIX macOS/Linux only; this run
executes macOS arm64/Python 3.14.6. CI builds the helper for Linux but has not run.
