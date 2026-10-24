# sipling — be the phone

Voice agents answer SIP calls. Your tests should place them. sipling turns a
laptop or CI runner into a scriptable caller: dial an agent, assert that it
answers promptly, listen for audio energy, and keep a message ladder when it
fails. No carrier, interactive softphone, microphone or speaker required.

```python
from sipling import probe

with probe("sip:YOUR_AGENT@YOUR_HOST", transport="tcp", timeout=15) as result:
    assert result.answered, result.ladder
    assert result.ring_time is not None and result.ring_time < 5
    assert result.answer_time is not None and result.answer_time < 10
    assert result.heard_audio(within=5)
    print(result.codec)
assert result.clean_bye
result.save_ladder("ladder.json")
```

The example above uses the v0.1-compatible blocking `probe()` API. The CLI and
`sip_caller` fixture also retain that API. v0.2 `call()` returns an asynchronous
streaming handle; see below.

On macOS or Linux, install Python 3.10+ and a separate **pjsua executable**, then `pip install .`.
On macOS: `brew install pjproject`. On Debian/Ubuntu: try
`sudo apt-get install pjsip-tools`; distro availability varies, so confirm
`pjsua --help`. If unavailable, build upstream pjproject's pjsua application.
sipling itself is a dependency-free pure Python wheel; pip never builds pjproject.
The pytest plugin requires `pip install '.[test]'` for local development (or
install pytest separately when consuming the wheel).

```sh
sipling call "$SIPLING_TEST_URI" --expect-answer --expect-audio 5 --timeout 15 --json --ladder-file ladder.json
```

Exit codes: 0 completed (and requested expectations met), 1 unmet answer/audio
expectation, 2 invalid input or local/backend error. Without an expectation a
SIP rejection is a successful probe execution; inspect `final_code` in JSON.
The CLI always hangs up before reporting. Python callers should use `with` or
`try/finally: result.hangup()`. Setup timeout does not limit an answered call's
lifetime. No `say()` is provided yet; it will accept a WAV path, not synthesize TTS.

TCP (default) and UDP offer G.722, PCMU and PCMA; `codec` reports the selected
answer. `answered` means an INVITE 2xx, not detected speech. `ring_time` is time
from INVITE to first 180/183/2xx; `answer_time` is time to 2xx. Missing timings
are `None`. `duration` measures the answered dialog. `clean_bye` means a BYE
transaction 2xx was observed. `ladder` is an ordered list including CSeq method
on responses. `save_ladder()` writes JSON with metadata; no pcap support yet.

`heard_audio(within=5, floor=0.01)` polls newly recorded decoded PCM for a 20 ms
window above normalized RMS floor. It can detect tones/noise as well as speech;
it is not a transcript or proof of agent correctness. Recorder buffering adds
latency. It checks new observations after invocation and returns false after
cleanup. Null audio supplies silence; no local microphone contaminates capture.

Bind is 0.0.0.0, with ephemeral SIP/RTP ports. Zero-config NAT assumes your
platform replies to the observed signaling source and uses symmetric RTP
latching. sipling sends RTP silence to establish that mapping. This does not
solve arbitrary NAT/firewall traversal: permit outbound SIP and UDP RTP. TCP
also avoids fragmentation of large UDP INVITEs. No response? Check DNS, outbound
TCP reachability, platform source-IP allowlists and URI routing. A 200 without
energy points toward media negotiation, RTP reachability or a silent agent.
Registration, digest credentials, TLS and SRTP are outside v0.1.

SIP userinfo (including passwords) is redacted in public result/CLI URI output
unless `no_redact=True` / `--no-redact`. Ladder exports contain only message
summaries. Raw pjsua output is privately consumed through a PTY; audio exists in a private
temporary directory until hangup and is deleted then. Child argv is visible to local process inspection;
prefer credential-free URIs and avoid sharing process listings. sipling never
prints child diagnostics. Your shell/CI command itself may still expose its URI.

Pytest discovers `sip_caller`, a fixture that hangs up every call at teardown:

```python
import os
import pytest


@pytest.mark.live
def test_agent(sip_caller):
    result = sip_caller(os.environ["SIPLING_TEST_URI"])
    assert result.answered
    assert result.heard_audio(within=5)
```

Live tests are skipped unless `pytest --live`; the included live suite also
skips if the env variable is absent. Dial only a URI you are authorized to test.
`pytest -q` runs unit tests and local mock SIP/RTP integration tests, with no
external calls; integration tests skip if pjsua is missing.

Develop: `pip install -e '.[test]'`, `ruff check .`, `ruff format --check .`,
`mypy src`, `pytest -q`, `python -m build`.

The Python wrapper is MIT; pjproject is separately GPL/commercial licensed.
No pjproject binaries or bindings are bundled.
Windows support is deferred because the current backend uses POSIX PTYs.
The tested pjproject version is 2.17; older versions may differ in CLI/log format. See [the design decision](docs/design.md)
for alternatives, native wheel research and licensing limits. TLS, SRTP and native
Python bindings remain deferred.

On restricted macOS runners, Homebrew pjsua may fail with
`PJMEDIA_EAUD_INIT` before dialing despite `--null-audio`. Use a separately
built headless pjproject (`./configure --disable-sound --disable-video`, then
`make dep && make`) and put its `pjsua` executable first on PATH. This run
verified a pjproject 2.17 headless build; the wrapper does not patch or bundle it.


## v0.2 streaming and earshot

`call()` requires a separately compiled `sipling-stream` executable on PATH
(or set `SIPLING_STREAM_EXECUTABLE` to its absolute path). It returns immediately;
start receiving continuously, then wait for the normalized `answered` event.
Its `sample_rate` is 8000, with 320-byte / 20 ms mono PCM16LE frames including
silence. `send_audio` blocks on actual conference-port consumption; DTMF uses
RFC2833/4733 and errors if the peer did not negotiate telephone-event.
A short final frame is padded with silence to 20 ms. One receive consumer is
supported. Stop via `hangup()` or a context manager, even after remote hangup.

Build against a separate configured pjproject 2.17 (not downloaded by pip):

```sh
sh scripts/build-test-pjsua.sh /tmp/sipling-human-pjproject
make -C helpers PJDIR=/tmp/sipling-human-pjproject/pjproject-2.17 OUTPUT=/tmp/sipling-human-pjproject/bin/sipling-stream
export PATH="/tmp/sipling-human-pjproject/bin:$PATH"
```

From the parent directory containing both checkouts, after installing development
dependencies in `sipling/.venv`, run the whole local pairing with one command:

```sh
sh sipling/examples/run-local.sh /tmp/sipling-human-pjproject
```

This starts a second **real pjsua** auto-answering on loopback with a greeting WAV,
uses earshot's unchanged `SiplingTransport`, plays a caller turn, commands a reply,
and verifies received RFC2833 digits. It never contacts an external URI. See
`../INTEGRATION.md` for measured timings, complete checks, limitations and the
explicit `--uri` invocation for a future authorized endpoint.

Streaming currently offers PCMU only, with UDP/TCP on POSIX. The blocking probe
retains its G.722/PCMU/PCMA offer and recorder-based `heard_audio`. Streaming ladders
are immutable snapshots of `Event(name, at)` with monotonic timestamps, not the
probe's string SIP ladder. There is no implicit fallback: a recorder is unsuitable
for earshot timing measurements. Existing v0.1 Python callers must change their
import to `from sipling import probe as call` or migrate to asynchronous streaming.

Receive queues are bounded to five frames and fail on overflow or a frame older
than 60 ms at delivery. A stalled consumer or helper therefore raises instead of
silently dropping/replaying buffered speech. OS scheduling is not hard real time;
use timing budgets comfortably above a frame and remeasure on your host.

All sipling source, including the helper source, is MIT. The **locally linked
helper executable** has pjproject GPL/commercial and third-party obligations.
No helper binary, pjproject source, native wheel or Python binding is bundled.
See `docs/design.md` before distributing any compiled helper.
