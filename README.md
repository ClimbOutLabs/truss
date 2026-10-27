# Truss, day 1 test harness

A small Android app that connects Oakley Meta Vanguard glasses to a server on the
owner's home network. Three screens, three gates. It is a test harness, not a product.

- Screen 1, Connect: laptop address field (saved in SharedPreferences), Connect glasses,
  toolkit version, phone SDK level, glasses connection state, last error, Ping laptop.
- Screen 2, Photo (Gate 2): one glasses photo, then leave the glasses session, confirm the
  laptop is reachable again (15 second poll), upload, show server byte count and elapsed ms.
  Every step is logged on screen and to Logcat tag `Truss`.
- Screen 3, Voice (Gate 3): record 5 seconds through the glasses as a Bluetooth
  communication device, upload for transcription, show the text and the speech-end to
  transcript time; fetch the test sentence from the laptop and play it through the
  glasses, showing the request to audio-start time. If the phone mic or speaker is used
  instead of the glasses, the screen says so in red and that is a fail.

The only network destination the app can call is `http://LAPTOP_IP:8792`
(`GET /`, `POST /capture`, `POST /stt`, `POST /tts`). No cloud SDKs, no analytics, no
crash reporters; the toolkit's own analytics and crash reporting are opted out in the
manifest, per the toolkit README.

## Build and run

1. Open this folder in Android Studio (Narwhal or newer, JDK 17+), or use the Gradle
   wrapper on a machine with the Android SDK 36 installed.
2. `local.properties` needs `sdk.dir`, plus `mwdat_application_id` and
   `mwdat_client_token`. Both toolkit values can be `0` while Developer Mode is on in
   the Meta AI app; without Developer Mode they come from the Wearables Developer Center.
3. Build the debug variant, install on the phone, turn Developer Mode on in the Meta AI
   app, then run the three gates in order and fill in the report from the brief.

## Where the brief met the toolkit as it is today (checked Oct 26, version 1.0.1)

- The toolkit is on Maven Central as `com.meta.wearable:mwdat-core` and
  `mwdat-camera`, version `1.0.1`. There is no GitHub Packages repository and no
  `gpr.user` / `gpr.token` any more; `mavenCentral()` in `settings.gradle.kts` is enough.
- The CameraAccess sample is a Compose app, so this app is Compose too, and its
  registration and permission flow is copied: Android permissions first, then
  `Wearables.initialize`, registration with `Wearables.startRegistration`, wearable
  permissions through `Wearables.RequestPermissionContract`, one at a time.
- There is no separate Wi-Fi join or leave call in toolkit 1.0.1. The session and camera
  stream are the toolkit's own way onto the glasses' network, and stopping the camera
  and the session is the leave step. Screen 2 therefore checks whether the laptop is
  already reachable again before that leave, polls for it afterwards, and records
  "automatic" or "needed explicit leave" from what actually happened.
- The sample's photo path is `stream.capturePhoto()` inside a session; the standalone
  `camera.photo` path is documented as experimental, with no sample code in the repo,
  so day 1 uses the sample's path. The stream exists only to take the still; the photo
  is saved on the phone, the session is ended, and only then is anything uploaded.
- Microphone and speaker follow the brief (plain Android audio, glasses as the
  Bluetooth SCO communication device), not the toolkit. One upstream catch, GitHub
  issue 192 on the toolkit repo: opening Bluetooth SCO can end a toolkit session, so
  this app never holds a toolkit session open during the voice tests; the photo flow
  always ends its session, and the voice screen says so.
- Permissions the sample declares that the brief did not list: `BLUETOOTH` (the legacy
  permission, alongside `BLUETOOTH_CONNECT`) and `CAMERA`. The sample also declares
  `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_CONNECTED_DEVICE` and `WAKE_LOCK`, but
  those exist only for its backgrounded video recording service, which this harness
  does not have, so they are not declared here. The brief's `BLUETOOTH_SCAN`,
  `ACCESS_FINE_LOCATION` and `NEARBY_WIFI_DEVICES` are declared even though the sample
  does not declare them, because the brief asked for them.

## Building on the VM this was written on (not needed on a normal machine)

This machine denies direct TCP connections and TLS from Java, which breaks both the
Gradle daemon and normal dependency downloads. The build here therefore runs Gradle
in-process, offline, against a local Maven mirror fetched with curl into
`local-maven/` (about 600 MB, safe to delete; it is a cache, not source). The exact
invocation is in `~/workspace/tools/build-mannie.sh`, with the mirror injected by an
init script in the Gradle user home, so nothing VM-specific lives in the project files.
On the owner's machine none of this applies: Android Studio and `./gradlew` work as usual.
