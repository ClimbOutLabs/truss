package com.climboutlabs.truss

import android.app.Activity
import android.app.Application
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.os.BatteryManager
import android.os.SystemClock
import android.util.Log
import androidx.exifinterface.media.ExifInterface
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.meta.wearable.dat.camera.addCamera
import com.meta.wearable.dat.camera.types.PhotoData
import com.meta.wearable.dat.camera.types.StreamConfiguration
import com.meta.wearable.dat.camera.types.StreamState
import com.meta.wearable.dat.camera.types.VideoQuality
import com.meta.wearable.dat.core.Wearables
import com.meta.wearable.dat.core.selectors.AutoDeviceSelector
import com.meta.wearable.dat.core.selectors.DeviceSelector
import com.meta.wearable.dat.core.session.DeviceSessionState
import com.meta.wearable.dat.core.types.DeviceSessionError
import com.meta.wearable.dat.core.types.PermissionError
import com.meta.wearable.dat.core.types.DeviceIdentifier
import com.meta.wearable.dat.core.types.Permission
import com.meta.wearable.dat.core.types.PermissionStatus
import com.meta.wearable.dat.core.types.RegistrationState
import java.io.ByteArrayInputStream
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.merge

data class TrussUiState(
    val laptopIp: String = "",
    val sdkReady: Boolean = false,
    val registrationState: RegistrationState = RegistrationState.UNAVAILABLE,
    val deviceNames: List<String> = emptyList(),
    val hasActiveDevice: Boolean = false,
    val streamActive: Boolean = false,
    val lastError: String? = null,
    val pingResult: String? = null,
    val pingBusy: Boolean = false,
    // Settings feedback: the last manual laptop check, and what Connect or Reconnect did.
    val laptopCheckBusy: Boolean = false,
    val laptopCheck: String? = null,
    val glassesNote: String? = null,
    val photoBusy: Boolean = false,
    val photoLog: List<String> = emptyList(),
    val photo: Bitmap? = null,
    val serverBytes: Long? = null,
    val photoElapsedMs: Long? = null,
    val wifiReturn: String? = null,
    val speakBusy: Boolean = false,
    val outputDevice: RoutedDevice? = null,
    val ttsMs: Long? = null,
    val spokenLines: List<String> = emptyList(),
    val plantBusy: Boolean = false,
    val plantText: String = "",
    val plantSlug: String? = null,
    val plantPrompt: String? = null,
    val plantLog: List<String> = emptyList(),
    val questionsList: List<QuestionInfo> = emptyList(),
    // Companion screens. laptopReachable is null until the first check.
    val laptopReachable: Boolean? = null,
    val lastSpoken: List<String> = emptyList(),
    val lastSpokenAt: String? = null,
    val lastSpokenPhoto: Bitmap? = null,
    // Null while the answer is being spoken, then whether it played through the glasses.
    val lastSpokenAloud: Boolean? = null,
    val historyDay: String? = null,
    val history: List<AnswerItem> = emptyList(),
    val historyBusy: Boolean = false,
    val historyPhotos: Map<String, Bitmap> = emptyMap(),
    // Hands-free watching: a glasses photo every watchEverySeconds while on.
    val watching: Boolean = false,
    /** While watching: when the glasses went away (off, folded, out of reach), or null. */
    val watchAwaySince: String? = null,
    val watchEverySeconds: Int = 30,
    val watchSent: Int = 0,
    val watchSkipped: Int = 0,
    val watchStartedAt: String? = null,
) {
  /** True while any glasses camera or audio work is running. */
  val busy: Boolean
    get() = photoBusy || speakBusy || plantBusy

  /** True once the glasses are registered with this app and one is in range. */
  val glassesReady: Boolean
    get() = registrationState == RegistrationState.REGISTERED && deviceNames.isNotEmpty()

  /** The glasses link in the brief's three words, derived from toolkit state. */
  val linkState: String
    get() =
        when {
          streamActive -> "Wi-Fi joined"
          hasActiveDevice || deviceNames.isNotEmpty() -> "BLE connected"
          else -> "disconnected"
        }
}

class TrussViewModel(application: Application) : AndroidViewModel(application) {

  private val _uiState = MutableStateFlow(TrussUiState(laptopIp = loadLaptopIp(application)))
  val uiState: StateFlow<TrussUiState> = _uiState.asStateFlow()

  init {
    startLaptopWatch()
  }

  // AutoDeviceSelector automatically selects the first available wearable device,
  // exactly as the CameraAccess sample does.
  private val deviceSelector: DeviceSelector by lazy { AutoDeviceSelector() }

  private var monitoringStarted = false
  private val deviceNames = mutableMapOf<DeviceIdentifier, String>()

  // When set, the next captured photo is a reference look for this question
  // instead of an ordinary capture. Set by the Plant screen, read and cleared
  // by the capture flow.
  private var pendingRefSlug: String? = null
  private var pendingRefKind: String? = null

  // The questions still waiting for their reference looks, in planting order.
  // A single planting holds one; the bedtime round holds three, and the Plant
  // screen walks them in order as each one completes.
  private var plantQueue: List<String> = emptyList()
  // True while the open planting was resumed from a question's row: closing it
  // then only closes the card, and the question keeps the look it already has.
  private var plantResumed = false

  fun setLaptopIp(value: String) {
    _uiState.update { it.copy(laptopIp = value, laptopReachable = null) }
    getApplication<Application>()
        .getSharedPreferences(PREFS, Activity.MODE_PRIVATE)
        .edit()
        .putString(KEY_LAPTOP_IP, value.trim())
        .apply()
  }

  private fun loadLaptopIp(application: Application): String =
      application
          .getSharedPreferences(PREFS, Activity.MODE_PRIVATE)
          .getString(KEY_LAPTOP_IP, "")
          ?.trim()
          .orEmpty()

  private fun laptopClient(): LaptopClient = LaptopClient(_uiState.value.laptopIp)

  /**
   * The Connect or Reconnect button. Registering again while the glasses are already
   * registered and in range does nothing in the toolkit, so that case only says so;
   * otherwise it starts registration through the Meta AI app. Every press leaves a
   * line on screen saying what happened.
   */
  fun connectGlasses(activity: Activity) {
    val now = _uiState.value
    val names = now.deviceNames.joinToString()
    val note =
        when {
          !now.sdkReady -> "Allow Bluetooth for Truss first, then try again."
          now.registrationState == RegistrationState.REGISTERED && now.deviceNames.isNotEmpty() ->
              "Already connected to $names. Nothing to redo."
          now.registrationState == RegistrationState.REGISTERING ->
              "Already connecting. Finish it in the Meta AI app."
          now.registrationState == RegistrationState.UNREGISTERING ->
              "The glasses are being removed. Try again in a moment."
          now.registrationState == RegistrationState.UNAVAILABLE ->
              "The Meta AI app isn't ready. Open it, check the glasses are paired there, then try again."
          now.registrationState == RegistrationState.REGISTERED ->
              "Truss is registered, but no glasses are in range. Turn them on and wear them; " +
                  "they reconnect on their own. Asking the Meta AI app again as well."
          else -> "Opening the Meta AI app to connect the glasses."
        }
    _uiState.update { it.copy(glassesNote = note) }
    val start =
        now.sdkReady &&
            (now.registrationState == RegistrationState.AVAILABLE ||
                (now.registrationState == RegistrationState.REGISTERED && now.deviceNames.isEmpty()))
    if (start) {
      try {
        Wearables.startRegistration(activity)
      } catch (e: Exception) {
        _uiState.update { it.copy(glassesNote = "Couldn't open the Meta AI app: ${e.message ?: e}") }
      }
    }
  }

  /** Called by MainActivity once the Android permissions are granted, as in the sample. */
  fun onSdkReady() {
    if (monitoringStarted) return
    monitoringStarted = true
    startAlertPolling()
    _uiState.update { it.copy(sdkReady = true) }
    viewModelScope.launch {
      Wearables.registrationState.collect { value ->
        _uiState.update {
          // Once a connect attempt lands, replace its "opening" line with the outcome.
          val note =
              if (it.glassesNote != null && value == RegistrationState.REGISTERED &&
                  it.registrationState != RegistrationState.REGISTERED) {
                "Registered with the glasses."
              } else {
                it.glassesNote
              }
          it.copy(registrationState = value, glassesNote = note)
        }
      }
    }
    viewModelScope.launch {
      Wearables.registrationErrorStream.collect { error ->
        _uiState.update { it.copy(glassesNote = "Connecting didn't work: $error") }
      }
    }
    viewModelScope.launch {
      Wearables.devices.collect { devices ->
        devices.forEach { deviceId ->
          if (!deviceNames.containsKey(deviceId)) {
            deviceNames[deviceId] = deviceId.toString()
            viewModelScope.launch {
              Wearables.devicesMetadata[deviceId]?.collect { metadata ->
                deviceNames[deviceId] = metadata.name.toString().ifEmpty { deviceId.toString() }
                _uiState.update { it.copy(deviceNames = deviceNames.values.toList()) }
              }
            }
          }
        }
        deviceNames.keys.retainAll(devices)
        _uiState.update { it.copy(deviceNames = deviceNames.values.toList()) }
      }
    }
    viewModelScope.launch {
      deviceSelector.activeDeviceFlow().collect { device ->
        _uiState.update { it.copy(hasActiveDevice = device != null) }
      }
    }
  }

  fun onSdkPermissionsDenied() {
    monitoringStarted = false
    _uiState.update {
      it.copy(sdkReady = false, lastError = "Bluetooth permission is required to talk to the glasses.")
    }
  }

  fun pingLaptop() {
    if (_uiState.value.pingBusy) return
    _uiState.update { it.copy(pingBusy = true, pingResult = null) }
    viewModelScope.launch {
      try {
        val code = withContext(Dispatchers.IO) { laptopClient().ping() }
        _uiState.update { it.copy(pingBusy = false, pingResult = "HTTP $code", lastError = null) }
      } catch (e: CancellationException) {
        throw e
      } catch (e: Exception) {
        showError(e)
        _uiState.update { it.copy(pingBusy = false, pingResult = "failed") }
      }
    }
  }

  // MARK: - Screen 2, photo (Gate 2)

  fun captureAndUpload(requestPermission: suspend (Permission) -> PermissionStatus) {
    if (!beginCapture(quiet = false)) return
    viewModelScope.launch { performCapture(requestPermission, fromWatch = false) }
  }

  /**
   * Claim the camera for one capture, or refuse. A refusal is said on screen unless
   * quiet (the watch loop just tries again later). Returns true when claimed; the
   * caller must then run performCapture, which releases it.
   */
  private fun beginCapture(quiet: Boolean): Boolean {
    if (_uiState.value.photoBusy) return false
    val now = _uiState.value
    if (now.speakBusy || now.plantBusy) {
      if (quiet) return false
      // A camera session must never open while Bluetooth audio is open or
      // about to open: the two are never held together (see the Voice screen).
      // A refused capture also disarms any reference routing a caller set
      // up for it, so no later photo can be misrouted by this refusal.
      pendingRefSlug = null
      pendingRefKind = null
      _uiState.update { it.copy(lastError = "Wait for the current work to finish first.") }
      return false
    }
    _uiState.update {
      it.copy(
          photoBusy = true,
          photoLog = emptyList(),
          serverBytes = null,
          spokenLines = emptyList(),
          photoElapsedMs = null,
          wifiReturn = null,
          lastError = null,
      )
    }
    return true
  }

  /** One capture after beginCapture claimed the camera. Returns what happened:
   * true when it ran to the end (sent, or skipped as the same view), false on a failure,
   * null when the glasses were off, folded or out of reach. */
  private suspend fun performCapture(
      requestPermission: suspend (Permission) -> PermissionStatus,
      fromWatch: Boolean,
  ): Boolean? {
    val startedAt = SystemClock.elapsedRealtime()
    try {
      runCapture(requestPermission, startedAt, fromWatch)
      return true
    } catch (e: CancellationException) {
      throw e
    } catch (e: GlassesAwayException) {
      // Permission does not change because the link dropped, so it is kept.
      photoLog("Glasses away: ${e.message}")
      if (!fromWatch) showError(e)
      return null
    } catch (e: Exception) {
      cameraGranted = false
      photoLog("FAILED: ${e.message ?: e.toString()}")
      showError(e)
      return false
    } finally {
      pendingRefSlug = null
      pendingRefKind = null
      _uiState.update {
        it.copy(photoBusy = false, photoElapsedMs = SystemClock.elapsedRealtime() - startedAt, streamActive = false)
      }
    }
  }

  /** Camera permission was granted earlier in this run; see runCapture step 1. */
  private var cameraGranted = false

  /** Open the Meta AI app, which reconnects its Bluetooth link to the glasses. */
  fun openMetaAi() {
    val app = getApplication<Application>()
    val intent = app.packageManager.getLaunchIntentForPackage(META_AI_PACKAGE)
    if (intent == null) {
      _uiState.update { it.copy(lastError = "The Meta AI app was not found on this phone.") }
      return
    }
    intent.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
    runCatching { app.startActivity(intent) }
        .onFailure { e -> _uiState.update { it.copy(lastError = "Could not open the Meta AI app: ${e.message}") } }
  }

  /** The glasses are off, folded or out of range: nothing is broken, so watching waits. */
  private class GlassesAwayException(message: String) : IllegalStateException(message)

  private fun isAway(error: DeviceSessionError) =
      error == DeviceSessionError.NO_ELIGIBLE_DEVICE ||
          error == DeviceSessionError.DEVICE_DISCONNECTED ||
          error == DeviceSessionError.SESSION_ENDED_BY_DEVICE

  /** Fail this capture over a session error, as away or as a real failure. */
  private fun failSession(error: DeviceSessionError): Nothing {
    if (isAway(error)) throw GlassesAwayException(sessionErrorAdvice(error))
    error(sessionErrorAdvice(error))
  }

  /** What the person should do about a session error, in plain words. */
  private fun sessionErrorAdvice(error: DeviceSessionError): String =
      when (error) {
        DeviceSessionError.DAT_APP_ON_THE_GLASSES_UPDATE_REQUIRED,
        DeviceSessionError.INSUFFICIENT_SDK_VERSION ->
            "The glasses need a software update before Truss can use the camera. In the Meta AI app, " +
                "open Devices, pick the glasses, then Settings, then General and Software update. " +
                "Keep the glasses in their case on charge until it finishes."
        DeviceSessionError.THERMAL_CRITICAL, DeviceSessionError.THERMAL_EMERGENCY ->
            "The glasses are too warm for the camera. Let them cool for a few minutes."
        DeviceSessionError.BATTERY_CRITICAL, DeviceSessionError.PEAK_POWER_SHUTDOWN ->
            "The glasses battery is too low for the camera. Charge them and try again."
        DeviceSessionError.SESSION_ALREADY_EXISTS ->
            "Another app is using the glasses camera. Close the camera in the Meta AI app and try again."
        DeviceSessionError.NO_ELIGIBLE_DEVICE, DeviceSessionError.DEVICE_DISCONNECTED,
        DeviceSessionError.SESSION_ENDED_BY_DEVICE ->
            "The glasses are off, folded or out of reach. Put them on and they are used again."
        else -> "The glasses session failed: ${error.description}."
      }

  private suspend fun runCapture(
      requestPermission: suspend (Permission) -> PermissionStatus,
      startedAt: Long,
      fromWatch: Boolean = false,
  ) {
    val state = _uiState.value
    check(state.sdkReady) { "The toolkit is not ready yet. Allow the Bluetooth permission first." }
    check(state.registrationState == RegistrationState.REGISTERED) {
      "Glasses are not connected. Press \"Connect glasses\" on the Connect screen first."
    }
    if (state.deviceNames.isEmpty()) {
      throw GlassesAwayException("No glasses are in range. Wake the glasses and try again.")
    }

    photoLog("Timer started.")
    // Step 1: camera permission through the toolkit, if not already granted. Once granted
    // in this run it is not asked again: the check is a round trip through the Meta AI
    // app's link to the glasses, and that link is what drops. A failed photo clears it.
    var status: PermissionStatus? = if (cameraGranted) PermissionStatus.Granted else null
    if (status == null) {
      var checkError: PermissionError? = null
      Wearables.checkPermissionStatus(Permission.CAMERA)
          .onSuccess { status = it }
          .onFailure { error, _ -> checkError = error }
      checkError?.let { error ->
        if (error == PermissionError.NO_DEVICE_WITH_CONNECTION || error == PermissionError.CONNECTION_ERROR) {
          throw GlassesAwayException(
              "The glasses aren't linked to the phone right now (${error.description}). " +
                  "Opening the Meta AI app usually brings them back.")
        }
        error(error.description)
      }
    }
    if (status != PermissionStatus.Granted) {
      photoLog("Asking for camera permission through the Meta AI app.")
      val requested = requestPermission(Permission.CAMERA)
      check(requested == PermissionStatus.Granted) {
        "Camera permission was not granted, so the photo cannot be taken."
      }
    }
    cameraGranted = true
    photoLog("Camera permission granted.")

    // Step 2: a session, then the camera, then one still. The session and stream are the
    // toolkit's own way onto the glasses' Wi-Fi; version 1.0.1 has no separate join call.
    var sessionError: DeviceSessionError? = null
    var session: com.meta.wearable.dat.core.session.DeviceSession? = null
    Wearables.createSession(deviceSelector)
        .onSuccess { session = it }
        .onFailure { error, _ -> sessionError = error }
    val activeSession = session
    if (activeSession == null) {
      sessionError?.let { failSession(it) }
      error("Could not start a session with the glasses.")
    }
    var camera: com.meta.wearable.dat.camera.Camera? = null
    var errorWatch: Job? = null
    try {
      // The first session error ends the wait below at once, with a plain
      // instruction, instead of a bare 15 second timeout.
      val firstError = CompletableDeferred<DeviceSessionError>()
      // Undispatched, so the collector is subscribed before start() runs: the
      // errors flow has no replay, and start() can emit its failure at once.
      errorWatch =
          viewModelScope.launch(start = CoroutineStart.UNDISPATCHED) {
            activeSession.errors.collect { error ->
              photoLog("Session error: ${error.description}")
              firstError.complete(error)
            }
          }
      activeSession.start()
      val outcome =
          withTimeoutOrNull(STEP_TIMEOUT_MS) {
            merge<Any>(
                    activeSession.state.filter {
                      it == DeviceSessionState.STARTED || it == DeviceSessionState.STOPPED
                    },
                    flow<Any> { emit(firstError.await()) },
                )
                .first()
          }
      if (outcome is DeviceSessionError) failSession(outcome)
      if (outcome == DeviceSessionState.STOPPED) {
        // A stop usually comes with its reason a moment later.
        val reason = withTimeoutOrNull(1_000L) { firstError.await() }
        if (reason != null) failSession(reason)
        throw GlassesAwayException(
            "The glasses ended the session before it started. Put the glasses on and they are used again.")
      }
      check(outcome != null) {
        "The glasses session did not start within 15 seconds (last state: ${activeSession.state.value}). " +
            "Wake the glasses, close the camera in the Meta AI app, and try again."
      }
      photoLog("Session started (glasses Wi-Fi is handled by the toolkit).")

      var addError: String? = null
      activeSession
          .addCamera(
              StreamConfiguration(
                  videoQuality = VideoQuality.MEDIUM,
                  frameRate = 24,
                  compressVideo = true,
              ),
          )
          .onSuccess { camera = it }
          .onFailure { error, _ -> addError = error.description }
      val activeCamera = camera
      check(activeCamera != null) { addError ?: "Could not add the glasses camera." }
      val stream = activeCamera.stream
      var streamError: String? = null
      stream.start().onFailure { error, _ -> streamError = error.description }
      check(streamError == null) { streamError ?: "Could not start the camera stream." }
      val streaming =
          withTimeoutOrNull(STEP_TIMEOUT_MS) {
            stream.state.first { it == StreamState.STREAMING }
          }
      check(streaming != null) { "The camera stream did not start within 15 seconds." }
      _uiState.update { it.copy(streamActive = true) }
      photoLog("Camera stream is up.")

      var photoError: String? = null
      var photoData: PhotoData? = null
      stream
          .capturePhoto()
          .onSuccess { photoData = it }
          .onFailure { error, _ -> photoError = error.description }
      check(photoError == null) { photoError ?: "Photo capture failed." }
      val captured = photoData
      check(captured != null) { "Photo capture returned nothing." }
      val bitmap = withContext(Dispatchers.Default) { decodePhoto(captured) }
      check(bitmap != null) { "The photo from the glasses could not be decoded." }
      val jpegFile =
          withContext(Dispatchers.IO) {
            val dir = File(getApplication<Application>().filesDir, "captures").apply { mkdirs() }
            val file = File(dir, "capture-${System.currentTimeMillis()}.jpg")
            file.outputStream().use { out ->
              bitmap.compress(Bitmap.CompressFormat.JPEG, 90, out)
            }
            file
          }
      _uiState.update { it.copy(photo = bitmap) }
      photoLog("Photo saved on the phone (${jpegFile.length()} bytes).")

      // Watching: a view the laptop already saw a moment ago is not sent again.
      // The camera and session are closed by the finally below either way.
      var signature: IntArray? = null
      if (fromWatch) {
        val current = withContext(Dispatchers.Default) { viewSignature(bitmap) }
        val previous = lastSentSignature
        if (previous != null &&
            SystemClock.elapsedRealtime() - lastSentAt < SAME_VIEW_WINDOW_MS &&
            signatureDistance(previous, current) < SAME_VIEW_THRESHOLD) {
          withContext(Dispatchers.IO) { jpegFile.delete() }
          photoLog("Same view as the last one sent, so it was not sent.")
          watchRecord("skipped")
          _uiState.update { it.copy(watchSkipped = it.watchSkipped + 1) }
          return
        }
        signature = current
      }

      // Step 3: is the laptop already reachable again, or must we leave explicitly?
      val client = laptopClient()
      val reachableBeforeLeave = withContext(Dispatchers.IO) { client.canReach() }
      photoLog("Laptop reachable before leaving the glasses session: $reachableBeforeLeave.")

      // Step 4: leave. Stopping the camera and the session is the toolkit's disconnect;
      // there is no separate Wi-Fi leave call in 1.0.1.
      runCatching { activeCamera.stop() }
      runCatching { activeSession.stop() }
      _uiState.update { it.copy(streamActive = false) }
      photoLog("Left the glasses session.")

      // Step 5: poll for the laptop, 15 seconds at most.
      val deadline = SystemClock.elapsedRealtime() + RETURN_TIMEOUT_MS
      var reachable = reachableBeforeLeave
      while (!reachable && SystemClock.elapsedRealtime() < deadline) {
        delay(POLL_MS)
        reachable = withContext(Dispatchers.IO) { client.canReach() }
      }
      check(reachable) {
        "The phone did not reach the laptop again within 15 seconds of leaving the glasses session."
      }
      val how = if (reachableBeforeLeave) "automatic" else "needed explicit leave"
      _uiState.update { it.copy(wifiReturn = how) }
      photoLog("Home Wi-Fi is back ($how).")

      // Step 6: upload. A photo taken for a planting goes to /plant/ref instead.
      // An ordinary capture goes to /capture, and any answers it fired are spoken
      // through the glasses now, with the camera session already over.
      val refSlug = pendingRefSlug
      val refKind = pendingRefKind
      if (refSlug != null && refKind != null) {
        pendingRefSlug = null
        pendingRefKind = null
        val reply = withContext(Dispatchers.IO) { client.postPlantRef(jpegFile, refSlug, refKind) }
        val next = reply.getString("next")
        val completed = reply.getString("state") == "standing"
        if (completed && plantQueue.firstOrNull() == refSlug) {
          advancePlantQueue(next)
        } else if (!completed) {
          _uiState.update { it.copy(plantPrompt = next) }
        }
        // A standing completion for a question that is not the queue head
        // (a planting replaced mid-capture) is only logged below: the
        // current question keeps its prompt and its place.
        plantLog(next)
        photoLog("Reference look saved. $next")
        runCatching { fetchQuestions() }.onSuccess { list ->
          _uiState.update { it.copy(questionsList = list) }
        }
      } else {
        val result = withContext(Dispatchers.IO) { client.postCapture(jpegFile) }
        _uiState.update { it.copy(serverBytes = result.bytes) }
        if (signature != null) {
          // Only a view the laptop really received counts as seen.
          lastSentSignature = signature
          lastSentAt = SystemClock.elapsedRealtime()
        }
        if (fromWatch) {
          watchRecord("sent", fired = result.fired.size)
          _uiState.update { it.copy(watchSent = it.watchSent + 1) }
        }
        photoLog("Uploaded. The laptop says it received ${result.bytes} bytes.")
        if (result.speak.isNotEmpty()) {
          val at = SimpleDateFormat("HH:mm", Locale.US).format(Date())
          _uiState.update {
            it.copy(
                spokenLines = result.speak,
                lastSpoken = result.speak,
                lastSpokenAt = at,
                lastSpokenPhoto = bitmap,
                lastSpokenAloud = null,
            )
          }
          result.speak.forEach { line -> photoLog("Mannie says: $line") }
          val spoken = speakViaGlasses(result.speak)
          _uiState.update { it.copy(lastSpokenAloud = spoken) }
        }
      }
      photoLog("Done in ${SystemClock.elapsedRealtime() - startedAt} ms.")
    } finally {
      errorWatch?.cancel()
      runCatching { camera?.close() }
      runCatching { session?.stop() }
    }
  }

  private fun photoLog(line: String) {
    val stamp = SimpleDateFormat("HH:mm:ss.SSS", Locale.US).format(Date())
    val entry = "$stamp  $line"
    Log.i(TAG, entry)
    _uiState.update { it.copy(photoLog = it.photoLog + entry) }
  }

  // Photo decoding, copied from the CameraAccess sample (HEIC carries its orientation
  // in an EXIF tag that the plain decoder does not apply).
  private fun decodePhoto(photo: PhotoData): Bitmap? =
      when (photo) {
        is PhotoData.Bitmap -> photo.bitmap
        is PhotoData.HEIC -> decodeWithOrientation(photo.data)
      }

  private fun decodeWithOrientation(data: java.nio.ByteBuffer): Bitmap? {
    val buffer = data.duplicate().apply { rewind() }
    val bytes = ByteArray(buffer.remaining())
    buffer.get(bytes)
    val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
    if (bitmap == null || bitmap.width == 0 || bitmap.height == 0) {
      bitmap?.recycle()
      Log.e(TAG, "Failed to decode captured photo")
      return null
    }
    val matrix = exifOrientationMatrix(bytes)
    if (matrix.isIdentity) return bitmap
    return try {
      Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true).also {
        bitmap.recycle()
      }
    } catch (e: OutOfMemoryError) {
      Log.e(TAG, "Failed to rotate captured photo", e)
      bitmap
    }
  }

  private fun exifOrientationMatrix(bytes: ByteArray): Matrix {
    val matrix = Matrix()
    try {
      val exif = ExifInterface(ByteArrayInputStream(bytes))
      when (
          exif.getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)
      ) {
        ExifInterface.ORIENTATION_ROTATE_90 -> matrix.postRotate(90f)
        ExifInterface.ORIENTATION_ROTATE_180 -> matrix.postRotate(180f)
        ExifInterface.ORIENTATION_ROTATE_270 -> matrix.postRotate(270f)
        ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> matrix.postScale(-1f, 1f)
        ExifInterface.ORIENTATION_FLIP_VERTICAL -> matrix.postScale(1f, -1f)
      }
    } catch (e: Exception) {
      Log.e(TAG, "Failed to read photo orientation", e)
    }
    return matrix
  }

  /**
   * Speak lines through the glasses, in order, returning true only when every
   * line was played. Only called once a capture's camera session has ended:
   * opening Bluetooth audio can end a toolkit session, so the two are never
   * held open together. A failure here never fails the capture itself; the
   * lines stay on screen. The missed-alert polling below relies on the true:
   * an alert is confirmed to the laptop only when this returns it.
   */
  private suspend fun speakViaGlasses(lines: List<String>): Boolean {
    val audio = GlassesAudio(getApplication())
    try {
      val glasses = audio.routeToGlasses()
      if (glasses == null) {
        photoLog("No glasses audio route, so the answers were not spoken. They are on screen.")
        return false
      }
      for (line in lines) {
        val requestStart = SystemClock.elapsedRealtime()
        val wavBytes = withContext(Dispatchers.IO) { laptopClient().postTts(line) }
        audio.playWav(wavBytes, glasses, requestStart)
      }
      return true
    } catch (e: CancellationException) {
      throw e
    } catch (e: Exception) {
      photoLog("Speaking the answers failed: ${e.message ?: e.toString()}")
      return false
    } finally {
      audio.clearRouting()
    }
  }

  // MARK: - Missed alerts

  private var alertPollingStarted = false

  /**
   * Poll the laptop for missed-window alerts while the app runs. An alert's
   * line is spoken through the same glasses path as capture answers, and the
   * laptop is told only after it was spoken, so an alert that could not be
   * spoken is offered again on the next poll instead of being lost.
   */
  private fun startAlertPolling() {
    if (alertPollingStarted) return
    alertPollingStarted = true
    viewModelScope.launch {
      while (true) {
        delay(ALERT_POLL_MS)
        pollAlertsOnce()
      }
    }
  }

  private suspend fun pollAlertsOnce() {
    val state = _uiState.value
    if (state.laptopIp.isBlank()) return
    // Never together: while a capture may be holding a toolkit camera
    // session, or the glasses audio is busy speaking, this cycle is
    // skipped rather than opening audio alongside it.
    if (state.photoBusy ||
        state.streamActive ||
        state.speakBusy ||
        state.plantBusy) return
    // Claimed in the same main-thread step as the check above, so a capture
    // (the watch loop's included) refuses to start until the alert is done.
    _uiState.update { it.copy(speakBusy = true) }
    try {
      val alerts = withContext(Dispatchers.IO) { laptopClient().getAlerts() }
      for (alert in alerts) {
        if (!speakViaGlasses(listOf(alert.line))) {
          Log.i(TAG, "Missed alert ${alert.id} could not be spoken; it stays pending.")
          continue
        }
        try {
          withContext(Dispatchers.IO) { laptopClient().ackAlert(alert.id) }
        } catch (e: Exception) {
          // Spoken but not confirmed: the laptop will offer it again, and
          // hearing it twice beats it never being heard at all.
          Log.i(TAG, "Missed alert ${alert.id} confirmation failed: ${e.message ?: e.toString()}")
        }
      }
    } catch (e: CancellationException) {
      throw e
    } catch (e: Exception) {
      // The laptop being unreachable between polls is ordinary. Nothing
      // here is a user-facing error, and no other flow waits on this.
      Log.i(TAG, "Missed alert poll failed: ${e.message ?: e.toString()}")
    } finally {
      _uiState.update { it.copy(speakBusy = false) }
    }
  }

  // MARK: - Screen 3, voice (Gate 3)

  fun getAvailableCommunicationDevices(): List<String> {
    val audio = GlassesAudio(getApplication())
    return audio.listAvailableCommunicationDevices()
  }

  fun speakTestSentence() {
    if (_uiState.value.speakBusy) return
    val now = _uiState.value
    if (now.photoBusy || now.plantBusy) {
      _uiState.update { it.copy(lastError = "Wait for the current work to finish first.") }
      return
    }
    _uiState.update { it.copy(speakBusy = true, ttsMs = null, lastError = null) }
    viewModelScope.launch {
      val audio = GlassesAudio(getApplication())
      try {
        val glasses = audio.routeToGlasses()
        check(glasses != null) {
          "No Bluetooth headset device is available. Are the glasses connected to this phone?"
        }
        val requestStart = SystemClock.elapsedRealtime()
        val wavBytes =
            withContext(Dispatchers.IO) { laptopClient().postTts(TEST_SENTENCE) }
        val result = audio.playWav(wavBytes, glasses, requestStart)
        _uiState.update { it.copy(outputDevice = result.routed, ttsMs = result.audioStartDelayMs) }
      } catch (e: CancellationException) {
        throw e
      } catch (e: Exception) {
        showError(e)
      } finally {
        audio.clearRouting()
        _uiState.update { it.copy(speakBusy = false) }
      }
    }
  }

  // MARK: - Screen 4, plant

  fun setPlantText(value: String) {
    _uiState.update { it.copy(plantText = value) }
  }

  private fun plantLog(line: String) {
    Log.i(TAG, line)
    _uiState.update { it.copy(plantLog = it.plantLog + line) }
  }

  private suspend fun fetchQuestions(): List<QuestionInfo> =
      withContext(Dispatchers.IO) { laptopClient().getQuestions() }

  fun refreshQuestions() {
    viewModelScope.launch {
      try {
        val questions = fetchQuestions()
        _uiState.update { it.copy(questionsList = questions) }
      } catch (e: CancellationException) {
        throw e
      } catch (e: Exception) {
        showError(e)
      }
    }
  }

  fun plantQuestionFromText() {
    val text = _uiState.value.plantText.trim()
    if (text.isEmpty() || _uiState.value.plantBusy) return
    _uiState.update { it.copy(plantBusy = true, lastError = null) }
    viewModelScope.launch {
      try {
        val reply = withContext(Dispatchers.IO) { laptopClient().plantQuestion(text) }
        val next = reply.getString("next")
        plantQueue = listOf(reply.getString("slug"))
        plantResumed = false
        _uiState.update { it.copy(plantSlug = reply.getString("slug"), plantPrompt = next) }
        plantLog("Planted: $text")
        plantLog(next)
        val questions = fetchQuestions()
        _uiState.update { it.copy(questionsList = questions) }
      } catch (e: CancellationException) {
        throw e
      } catch (e: Exception) {
        showError(e)
      } finally {
        _uiState.update { it.copy(plantBusy = false) }
      }
    }
  }

  fun plantBedtimeRound() {
    if (_uiState.value.plantBusy) return
    _uiState.update { it.copy(plantBusy = true, lastError = null) }
    viewModelScope.launch {
      try {
        val reply = withContext(Dispatchers.IO) { laptopClient().plantBedtime() }
        val slugs = reply.getJSONArray("slugs")
        val questions = reply.getJSONArray("questions")
        for (i in 0 until questions.length()) {
          plantLog("Round: ${questions.getString(i)}")
        }
        plantQueue = List(slugs.length()) { i -> slugs.getString(i) }
        plantResumed = false
        if (slugs.length() > 0) {
          _uiState.update {
            it.copy(plantSlug = slugs.getString(0), plantPrompt = "Show me what yes looks like.")
          }
        }
        plantLog("Show each question what yes looks like, then what no looks like, in order.")
        val list = fetchQuestions()
        _uiState.update { it.copy(questionsList = list) }
      } catch (e: CancellationException) {
        throw e
      } catch (e: Exception) {
        showError(e)
      } finally {
        _uiState.update { it.copy(plantBusy = false) }
      }
    }
  }

  /**
   * Take the next glasses photo as a reference look for the question being planted:
   * the ordinary capture flow runs, and its upload goes to /plant/ref instead.
   */
  fun captureReference(
      kind: String,
      requestPermission: suspend (Permission) -> PermissionStatus,
  ) {
    if (_uiState.value.photoBusy) return
    val now = _uiState.value
    if (now.speakBusy || now.plantBusy) {
      // Refuse before arming the reference routing, so a refused capture
      // can never leave the routing set for a later photo.
      _uiState.update { it.copy(lastError = "Wait for the current work to finish first.") }
      return
    }
    val slug = _uiState.value.plantSlug
    if (slug == null) {
      _uiState.update { it.copy(lastError = "Plant a question first, then show its looks.") }
      return
    }
    pendingRefSlug = slug
    pendingRefKind = kind
    captureAndUpload(requestPermission)
  }

  // MARK: - Hands-free watching

  private var watchJob: Job? = null
  private var watchCapturing = false
  private var watchStopAsked = false
  private var lastSentSignature: IntArray? = null
  private var lastSentAt = 0L

  /**
   * Start taking a glasses photo every watchEverySeconds and sending it to the laptop,
   * which checks it against whatever questions are due. Each photo is the ordinary
   * capture flow, so its camera session always ends before any answer is spoken and
   * nothing is taken while Mannie speaks, records, or plants. A view the laptop saw a
   * moment ago is not sent again. Three failed photos in a row stop watching.
   */
  fun startWatching(requestPermission: suspend (Permission) -> PermissionStatus) {
    if (watchJob?.isActive == true) {
      // Switched back on while a last photo was still finishing: keep going.
      if (watchStopAsked) {
        watchStopAsked = false
        _uiState.update { it.copy(watching = true) }
      }
      return
    }
    watchStopAsked = false
    val at = SimpleDateFormat("HH:mm", Locale.US).format(Date())
    lastSentSignature = null
    _uiState.update {
      it.copy(watching = true, watchSent = 0, watchSkipped = 0, watchStartedAt = at, lastError = null)
    }
    watchRecord("start")
    watchJob =
        viewModelScope.launch {
          var failures = 0
          var aways = 0
          try {
            while (true) {
              // While a question's looks are being taken, the camera is left to
              // the planting flow.
              // While the glasses are away, a photo is still tried on every beat (and at
              // once when the app comes back to the front): the device list can lag behind
              // the Meta AI app, and a photo that works is what brings watching back.
              val away = _uiState.value.watchAwaySince != null
              val registered = _uiState.value.registrationState == RegistrationState.REGISTERED
              val ready =
                  (_uiState.value.glassesReady || (away && registered)) && _uiState.value.plantSlug == null
              if (!_uiState.value.glassesReady && _uiState.value.watchAwaySince == null) {
                // The glasses dropped out of the device list: say so on Home.
                watchRecord("away")
                val since = SimpleDateFormat("HH:mm", Locale.US).format(Date())
                _uiState.update { it.copy(watchAwaySince = since) }
              }
              if (ready && beginCapture(quiet = true)) {
                watchCapturing = true
                // A capture runs to its end even if watching is switched off
                // meanwhile: once the laptop fired, its answer must be spoken.
                val ok =
                    try {
                      withContext(NonCancellable) {
                        performCapture(requestPermission, fromWatch = true)
                      }
                    } finally {
                      watchCapturing = false
                    }
                if (watchStopAsked) break
                if (ok == null) {
                  // A revoked camera permission can look like the glasses ending the
                  // session, so after a few aways in a row the permission is asked again.
                  aways += 1
                  if (aways % 3 == 0) cameraGranted = false
                  // Off, folded or out of reach: not a failure. Watching keeps trying on its usual beat and
                  // carries on by itself once the glasses are back.
                  if (_uiState.value.watchAwaySince == null) {
                    watchRecord("away")
                    val since = SimpleDateFormat("HH:mm", Locale.US).format(Date())
                    _uiState.update { it.copy(watchAwaySince = since) }
                  }
                  waitForBeat()
                  continue
                }
                aways = 0
                if (_uiState.value.watchAwaySince != null) {
                  watchRecord("back")
                  _uiState.update { it.copy(watchAwaySince = null) }
                }
                if (!ok) watchRecord("failed")
                failures = if (ok) 0 else failures + 1
                if (failures >= WATCH_MAX_FAILURES) {
                  _uiState.update {
                    it.copy(
                        lastError =
                            "Watching stopped after $WATCH_MAX_FAILURES photos in a row failed. " +
                                (it.lastError ?: ""),
                    )
                  }
                  break
                }
              }
              waitForBeat()
            }
          } finally {
            watchRecord("stop")
            _uiState.update { it.copy(watching = false, watchAwaySince = null) }
          }
        }
  }

  /** Wakes the watch loop's wait early, for example when the app comes back to the front. */
  private val watchNudge = MutableSharedFlow<Unit>(extraBufferCapacity = 1)

  private suspend fun waitForBeat() {
    withTimeoutOrNull(_uiState.value.watchEverySeconds * 1000L) { watchNudge.first() }
  }

  /** The app is in front again, as after a trip to the Meta AI app: if watching is
   * waiting for the glasses, try a photo now rather than at the next beat. */
  fun onAppForeground() {
    if (_uiState.value.watching && _uiState.value.watchAwaySince != null) watchNudge.tryEmit(Unit)
  }

  /** Stop watching. A photo already being taken finishes first, answer and all. */
  fun stopWatching() {
    watchStopAsked = true
    _uiState.update { it.copy(watching = false) }
    if (!watchCapturing) watchJob?.cancel()
  }

  fun setWatchEverySeconds(seconds: Int) {
    _uiState.update { it.copy(watchEverySeconds = seconds.coerceIn(10, 300)) }
  }

  /**
   * A tiny picture of the view: the photo shrunk to 16 by 16 and turned to grey. Two
   * photos of the same view, taken a moment apart, differ in it by only a few levels;
   * turning the head or the scene changing moves it far more.
   */
  private fun viewSignature(bitmap: Bitmap): IntArray {
    val small = Bitmap.createScaledBitmap(bitmap, SIGNATURE_SIDE, SIGNATURE_SIDE, true)
    val pixels = IntArray(SIGNATURE_SIDE * SIGNATURE_SIDE)
    small.getPixels(pixels, 0, SIGNATURE_SIDE, 0, 0, SIGNATURE_SIDE, SIGNATURE_SIDE)
    if (small != bitmap) small.recycle()
    return IntArray(pixels.size) { i ->
      val c = pixels[i]
      (((c shr 16) and 0xFF) * 299 + ((c shr 8) and 0xFF) * 587 + (c and 0xFF) * 114) / 1000
    }
  }

  /** Mean absolute difference between two signatures, in grey levels (0 to 255). */
  private fun signatureDistance(a: IntArray, b: IntArray): Int {
    var total = 0
    for (i in a.indices) total += kotlin.math.abs(a[i] - b[i])
    return total / a.size
  }

  /**
   * One line per watching event in watch-log.txt on the phone: time, event, battery
   * percent. A one-hour wear at a set cadence then gives the battery cost per hour.
   */
  private fun watchRecord(event: String, fired: Int? = null) {
    val app = getApplication<Application>()
    val battery =
        (app.getSystemService(android.content.Context.BATTERY_SERVICE) as? BatteryManager)
            ?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
    val stamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date())
    val every = _uiState.value.watchEverySeconds
    val line =
        "$stamp | $event" +
            (fired?.let { " | fired $it" } ?: "") +
            " | every ${every}s | battery ${battery ?: "?"}%\n"
    viewModelScope.launch(Dispatchers.IO) {
      runCatching { File(app.filesDir, "watch-log.txt").appendText(line) }
    }
  }

  // MARK: - Companion screens

  /**
   * Check the laptop now and then, so Home can say whether it is reachable. Skipped
   * while a capture holds the glasses session: the phone may be on the glasses'
   * network then, and the capture flow checks the laptop itself.
   */
  private fun startLaptopWatch() {
    viewModelScope.launch {
      while (true) {
        checkLaptopOnce()
        delay(LAPTOP_CHECK_MS)
      }
    }
  }

  private suspend fun checkLaptopOnce() {
    val state = _uiState.value
    if (state.laptopIp.isBlank()) {
      _uiState.update { it.copy(laptopReachable = null) }
      return
    }
    if (state.photoBusy || state.streamActive) return
    val reachable =
        try {
          withContext(Dispatchers.IO) { laptopClient().canReach() }
        } catch (e: CancellationException) {
          throw e
        } catch (e: Exception) {
          false
        }
    _uiState.update { it.copy(laptopReachable = reachable) }
    if (reachable) {
      // Keeps Home's count of watched questions current.
      try {
        val list = withContext(Dispatchers.IO) { laptopClient().getQuestions() }
        _uiState.update { it.copy(questionsList = list) }
      } catch (e: CancellationException) {
        throw e
      } catch (e: Exception) {
        Log.i(TAG, "Question list refresh failed: ${e.message ?: e.toString()}")
      }
    }
  }

  fun clearError() {
    _uiState.update { it.copy(lastError = null) }
  }

  /** The Check connection button: one timed request to the laptop, with the result on screen. */
  fun checkLaptopNow() {
    val now = _uiState.value
    if (now.laptopCheckBusy) return
    val ip = now.laptopIp
    if (ip.isBlank()) {
      _uiState.update { it.copy(laptopCheck = "Type the laptop address first.") }
      return
    }
    if (now.photoBusy || now.streamActive) {
      _uiState.update {
        it.copy(laptopCheck = "The glasses are busy with a photo. Try again when it finishes.")
      }
      return
    }
    _uiState.update { it.copy(laptopCheckBusy = true, laptopCheck = null) }
    viewModelScope.launch {
      val started = SystemClock.elapsedRealtime()
      var reachable = false
      val line =
          try {
            val code = withContext(Dispatchers.IO) { laptopClient().ping() }
            val ms = SystemClock.elapsedRealtime() - started
            reachable = code in 200..399
            if (reachable) "Connected to $ip:8792, answered in $ms ms."
            else "The laptop at $ip:8792 answered with HTTP $code."
          } catch (e: CancellationException) {
            _uiState.update { it.copy(laptopCheckBusy = false) }
            throw e
          } catch (e: Exception) {
            "Can't reach $ip:8792. ${e.message ?: e.toString()}"
          }
      val at = SimpleDateFormat("h:mm a", Locale.US).format(Date())
      _uiState.update {
        it.copy(laptopCheckBusy = false, laptopReachable = reachable, laptopCheck = "$line Checked at $at.")
      }
      if (reachable) {
        try {
          val list = withContext(Dispatchers.IO) { laptopClient().getQuestions() }
          _uiState.update { it.copy(questionsList = list) }
        } catch (e: CancellationException) {
          throw e
        } catch (e: Exception) {
          Log.i(TAG, "Question list refresh failed: ${e.message ?: e.toString()}")
        }
      }
    }
  }

  /** Stop watching one question. Its files and history stay on the laptop. */
  fun retireQuestion(slug: String) {
    viewModelScope.launch {
      try {
        withContext(Dispatchers.IO) { laptopClient().retireQuestion(slug) }
        if (slug in plantQueue) {
          plantQueue = plantQueue - slug
          if (_uiState.value.plantSlug == slug) {
            val nextSlug = plantQueue.firstOrNull()
            _uiState.update {
              it.copy(
                  plantSlug = nextSlug,
                  plantPrompt = if (nextSlug != null) "Show me what yes looks like." else null,
              )
            }
          }
        }
        val list = fetchQuestions()
        _uiState.update { it.copy(questionsList = list) }
      } catch (e: CancellationException) {
        throw e
      } catch (e: Exception) {
        showError(e)
      }
    }
  }

  /** The queue head is fully planted. If a round is being planted, move the Plant
   * screen on to its next question; if that was the last one, clear the planting
   * target so its looks cannot be overwritten by a stray tap. */
  private fun advancePlantQueue(next: String) {
    plantQueue = plantQueue.drop(1)
    val nextSlug = plantQueue.firstOrNull()
    if (nextSlug != null) {
      _uiState.update {
        it.copy(plantSlug = nextSlug, plantPrompt = "Show me what yes looks like.")
      }
      plantLog("Next question in the round: show it what yes looks like.")
    } else {
      _uiState.update { it.copy(plantSlug = null, plantPrompt = next) }
    }
  }

  /** Start the question being planted without the looks it is still missing: the
   * yes look alone, or none. For a question its words decide, like "Is my heart rate
   * above 160?" read off a treadmill display, where a no look cannot be staged. */
  fun finishPlantingWithoutMore() {
    val now = _uiState.value
    val slug = now.plantSlug ?: return
    if (now.busy) return
    _uiState.update { it.copy(plantBusy = true, lastError = null) }
    viewModelScope.launch {
      try {
        val reply = withContext(Dispatchers.IO) { laptopClient().finishPlanting(slug) }
        val next = reply.getString("next")
        if (reply.getString("state") == "standing" && plantQueue.firstOrNull() == slug) {
          pendingRefSlug = null
          pendingRefKind = null
          plantResumed = false
          advancePlantQueue(next)
        }
        plantLog(next)
        val list = fetchQuestions()
        _uiState.update { it.copy(questionsList = list) }
      } catch (e: CancellationException) {
        throw e
      } catch (e: Exception) {
        showError(e)
      } finally {
        _uiState.update { it.copy(plantBusy = false) }
      }
    }
  }

  /** Pick up a question that was left without one of its looks, so its planting
   * screen shows again. Does nothing while another planting is open. */
  fun resumePlanting(slug: String) {
    val now = _uiState.value
    if (now.plantSlug != null || now.plantBusy) return
    val item = now.questionsList.firstOrNull { it.slug == slug && it.state == "planting" } ?: return
    plantQueue = listOf(slug)
    plantResumed = true
    val prompt = if (!item.hasRefYes) "Show me what yes looks like." else "Show me what no looks like."
    _uiState.update { it.copy(plantSlug = slug, plantPrompt = prompt, lastError = null) }
  }

  /** Abandon the planting in progress: questions still waiting for their looks are
   * retired on the laptop, so none is left half planted. */
  fun cancelPlanting() {
    val waiting = if (plantResumed) emptyList() else plantQueue
    plantResumed = false
    plantQueue = emptyList()
    pendingRefSlug = null
    pendingRefKind = null
    _uiState.update { it.copy(plantSlug = null, plantPrompt = null, plantText = "") }
    viewModelScope.launch {
      try {
        val list = fetchQuestions()
        val stillPlanting = list.filter { it.slug in waiting && it.state == "planting" }
        for (item in stillPlanting) {
          withContext(Dispatchers.IO) { laptopClient().retireQuestion(item.slug) }
        }
        val refreshed = fetchQuestions()
        _uiState.update { it.copy(questionsList = refreshed) }
      } catch (e: CancellationException) {
        throw e
      } catch (e: Exception) {
        showError(e)
      }
    }
  }

  /** Load today's answers and their evidence photos for the History screen. */
  fun loadHistory() {
    if (_uiState.value.historyBusy) return
    _uiState.update { it.copy(historyBusy = true) }
    viewModelScope.launch {
      try {
        val client = laptopClient()
        val day = withContext(Dispatchers.IO) { client.getAnswers() }
        _uiState.update { it.copy(historyDay = day.day, history = day.answers) }
        for (item in day.answers) {
          val name = item.outline ?: item.evidence ?: continue
          val key = "${day.day}/$name"
          if (_uiState.value.historyPhotos.containsKey(key)) continue
          val photo =
              try {
                withContext(Dispatchers.IO) {
                  decodeThumbnail(client.getEvidence(day.day, name))
                }
              } catch (e: CancellationException) {
                throw e
              } catch (e: Exception) {
                Log.i(TAG, "Evidence photo $key could not be loaded: ${e.message}")
                null
              }
          if (photo != null) {
            _uiState.update { it.copy(historyPhotos = it.historyPhotos + (key to photo)) }
          }
        }
      } catch (e: CancellationException) {
        throw e
      } catch (e: Exception) {
        showError(e)
      } finally {
        _uiState.update { it.copy(historyBusy = false) }
      }
    }
  }

  /** Decode a kept photo at a quarter of its size: enough for a card, and a day of
   * them stays small in memory. */
  private fun decodeThumbnail(bytes: ByteArray): Bitmap? {
    val options = BitmapFactory.Options().apply { inSampleSize = 4 }
    return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)
  }

  private fun showError(e: Exception) {
    val message = e.message ?: e.toString()
    Log.e(TAG, message, e)
    _uiState.update { it.copy(lastError = plainError(e, message)) }
  }

  /** Network errors in plain words: "Failed to connect to /192.168.0.5:8792" reads as
   * the laptop not answering, with the likely reason. */
  private fun plainError(e: Exception, message: String): String {
    val connect =
        if (e is java.net.ConnectException || e is java.net.NoRouteToHostException) {
          Regex("""(?:Failed to connect to|connect to) /?([0-9.]+)(?::(\d+))?""").find(message)
        } else null
    if (connect != null) {
      val (ip, port) = connect.destructured
      return "Can't reach the laptop at $ip${if (port.isNotEmpty()) ":$port" else ""}. " +
          "Is the Truss server running, and is the phone on the same Wi-Fi?"
    }
    if (e is java.net.SocketTimeoutException) {
      return "The laptop took too long to answer. $message"
    }
    return message
  }

  companion object {
    const val TAG = "Truss"
    private const val META_AI_PACKAGE = "com.facebook.stella"
    const val TEST_SENTENCE = "Hello, this is Mannie. I can hear you."
    private const val PREFS = "mannie"
    private const val KEY_LAPTOP_IP = "laptop_ip"
    private const val STEP_TIMEOUT_MS = 15_000L
    private const val RETURN_TIMEOUT_MS = 15_000L
    private const val POLL_MS = 500L
    private const val ALERT_POLL_MS = 30_000L
    private const val LAPTOP_CHECK_MS = 20_000L
    private const val WATCH_MAX_FAILURES = 3
    private const val SIGNATURE_SIDE = 16
    // Mean grey-level difference below which two photos count as the same view.
    private const val SAME_VIEW_THRESHOLD = 8
    // A same view is sent again after this long anyway, so a question that has
    // just come due still gets its look.
    private const val SAME_VIEW_WINDOW_MS = 120_000L
  }
}
