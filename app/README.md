# Truss companion app

An Android app that connects Oakley Meta Vanguard glasses to a server on the
owner's home network. Four screens:

- Home: whether the glasses and the laptop are connected, how many questions
  Mannie is watching, a Look now button for one glasses photo, the Watch
  hands-free switch, and the last thing Mannie said with the photo it was seen in.
- Questions: plant a question by typing it, then take its yes look and
  its no look through the glasses, step by step; plant the bedtime round in one
  tap; see every question's state and remove one.
- History: the day's answers from the laptop's log, each with its evidence photo
  (the outlined copy when SAM made one).
- Settings: the laptop address (saved in SharedPreferences) and connecting the
  glasses. Diagnostics underneath keeps the day 1 gates: toolkit and phone
  versions, ping, one capture with its step-by-step log (also to Logcat tag
  `Truss`), and a test sentence spoken through the glasses.

Watch hands-free takes a glasses photo every 15, 30 or 60 seconds and sends it
to the laptop, which checks it against whatever questions are due. Each photo is
the ordinary capture flow, so the camera session always ends before an answer is
spoken, and no photo is taken while Mannie speaks, records, or a question's looks
are being taken. A photo whose 16 by 16 grey thumbnail is within 8 levels of the
last one sent, less than two minutes ago, is not sent. Three failed photos in a
row stop watching. The screen stays on while watching. Every start, stop, sent,
skipped and failed photo goes into `watch-log.txt` in the app's files folder with
the battery percent, so a one-hour wear gives the battery cost per hour.

Settings, Watching, How to look picks one of two ways. **Live view**
keeps the glasses video open between looks. The phone reads a 16 by 16 grey thumbnail
from the video four times a second. It takes a photo only when the view differs from
the last one the laptop received (or that one is two minutes old) and has stayed still
for a second, so the photo shows something new and is not blurred by a turning head.
Nothing in this knows what is in view; it compares thumbnails only. The beat setting
becomes the shortest wait between looks. A hold ends without a photo, and the video
closes, when a tapped photo, a planting look, a missed-window alert or a test sentence
needs the glasses (an alert only when one is really waiting), or when watching stops. It also ends when the glasses stop the
video themselves, or after three minutes with nothing new. **Timed photos** is the
earlier way and the default: a photo on every beat, each in its own short session. Whichever way is
on, watching pauses while the glasses report they are off your face, and resumes when
they are back on. The toolkit (1.0.1) has no location or head-motion data, so no part
of this uses either. The Desk shows which way the phone is watching.

The only network destination the app can call is `http://LAPTOP_IP:8792`
(`GET /`, `POST /capture`, `POST /tts`, `POST /plant/question`,
`POST /plant/bedtime`, `POST /plant/ref`, `GET /questions`, `POST /questions/retire`,
`GET /answers`, `GET /evidence`, `GET /alerts`, `POST /alerts/ack`). No cloud SDKs, no
analytics, no crash reporters; the toolkit's own analytics and crash reporting are
opted out in the manifest, per the toolkit README.

## Build and run

1. Open the repository root (not this folder) in Android Studio (Narwhal or newer, JDK 17+), or use the Gradle
   wrapper on a machine with the Android SDK 36 installed.
2. `local.properties` needs `sdk.dir`, plus `mwdat_application_id` and
   `mwdat_client_token`. Both toolkit values can be `0` while Developer Mode is on in
   the Meta AI app; without Developer Mode they come from the Wearables Developer Center.
3. Build the debug variant, install on the phone, turn Developer Mode on in the Meta AI
   app, then use the Diagnostics gates under Settings to check the glasses, the laptop and the speaker path.

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
- The speaker follows the brief (plain Android audio, glasses as the Bluetooth SCO
  communication device), not the toolkit. One upstream catch, GitHub issue 192 on the
  toolkit repo: opening Bluetooth SCO can end a toolkit session, so this app never
  holds a toolkit session open while it speaks; the photo flow always ends its
  session first. The app does not record audio and does not ask for the microphone.
- Permissions the sample declares that the brief did not list: `BLUETOOTH` (the legacy
  permission, alongside `BLUETOOTH_CONNECT`) and `CAMERA`. The sample also declares
  `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_CONNECTED_DEVICE` and `WAKE_LOCK`, but
  those exist only for its backgrounded video recording service, which this harness
  does not have, so they are not declared here. The brief's `BLUETOOTH_SCAN`,
  `ACCESS_FINE_LOCATION` and `NEARBY_WIFI_DEVICES` are declared even though the sample
  does not declare them, because the brief asked for them.
