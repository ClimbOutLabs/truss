package com.climboutlabs.truss

import android.app.Activity
import android.app.Application
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
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
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

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
    val photoBusy: Boolean = false,
    val photoLog: List<String> = emptyList(),
    val photo: Bitmap? = null,
    val serverBytes: Long? = null,
    val photoElapsedMs: Long? = null,
    val wifiReturn: String? = null,
    val recordBusy: Boolean = false,
    val inputDevice: RoutedDevice? = null,
    val transcript: String? = null,
    val sttMs: Long? = null,
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
) {
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

  fun setLaptopIp(value: String) {
    _uiState.update { it.copy(laptopIp = value) }
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

  fun startRegistration(activity: Activity) {
    Wearables.startRegistration(activity)
  }

  /** Called by MainActivity once the Android permissions are granted, as in the sample. */
  fun onSdkReady() {
    if (monitoringStarted) return
    monitoringStarted = true
    _uiState.update { it.copy(sdkReady = true) }
    viewModelScope.launch {
      Wearables.registrationState.collect { value ->
        _uiState.update { it.copy(registrationState = value) }
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
    if (_uiState.value.photoBusy) return
    val now = _uiState.value
    if (now.recordBusy || now.speakBusy || now.plantBusy) {
      // A camera session must never open while Bluetooth audio is open or
      // about to open: the two are never held together (see the Voice screen).
      // A refused capture also disarms any reference routing a caller set
      // up for it, so no later photo can be misrouted by this refusal.
      pendingRefSlug = null
      pendingRefKind = null
      _uiState.update { it.copy(lastError = "Wait for the current work to finish first.") }
      return
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
    viewModelScope.launch {
      val startedAt = SystemClock.elapsedRealtime()
      try {
        runCapture(requestPermission, startedAt)
      } catch (e: CancellationException) {
        throw e
      } catch (e: Exception) {
        photoLog("FAILED: ${e.message ?: e.toString()}")
        showError(e)
      } finally {
        pendingRefSlug = null
        pendingRefKind = null
        _uiState.update {
          it.copy(photoBusy = false, photoElapsedMs = SystemClock.elapsedRealtime() - startedAt, streamActive = false)
        }
      }
    }
  }

  private suspend fun runCapture(
      requestPermission: suspend (Permission) -> PermissionStatus,
      startedAt: Long,
  ) {
    val state = _uiState.value
    check(state.sdkReady) { "The toolkit is not ready yet. Allow the Bluetooth permission first." }
    check(state.registrationState == RegistrationState.REGISTERED) {
      "Glasses are not connected. Press \"Connect glasses\" on the Connect screen first."
    }
    check(state.deviceNames.isNotEmpty()) {
      "No glasses are in range. Wake the glasses and try again."
    }

    photoLog("Timer started.")
    // Step 1: camera permission through the toolkit, if not already granted.
    var checkError: String? = null
    var status: PermissionStatus? = null
    Wearables.checkPermissionStatus(Permission.CAMERA)
        .onSuccess { status = it }
        .onFailure { error, _ -> checkError = error.description }
    check(checkError == null) { checkError ?: "Camera permission check failed." }
    if (status != PermissionStatus.Granted) {
      photoLog("Asking for camera permission through the Meta AI app.")
      val requested = requestPermission(Permission.CAMERA)
      check(requested == PermissionStatus.Granted) {
        "Camera permission was not granted, so the photo cannot be taken."
      }
    }
    photoLog("Camera permission granted.")

    // Step 2: a session, then the camera, then one still. The session and stream are the
    // toolkit's own way onto the glasses' Wi-Fi; version 1.0.1 has no separate join call.
    var sessionError: String? = null
    var session: com.meta.wearable.dat.core.session.DeviceSession? = null
    Wearables.createSession(deviceSelector)
        .onSuccess { session = it }
        .onFailure { error, _ -> sessionError = error.description }
    val activeSession = session
    check(activeSession != null) { sessionError ?: "Could not start a session with the glasses." }
    var camera: com.meta.wearable.dat.camera.Camera? = null
    try {
      viewModelScope.launch {
        activeSession.errors.collect { error -> photoLog("Session error: ${error.description}") }
      }
      activeSession.start()
      val started =
          withTimeoutOrNull(STEP_TIMEOUT_MS) {
            activeSession.state.first { it == DeviceSessionState.STARTED }
          }
      check(started != null) { "The glasses session did not start within 15 seconds." }
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
          // The queue head is fully planted. If a round is being planted, move
          // the Plant screen on to its next question; if that was the last one,
          // clear the planting target so its looks cannot be overwritten by a
          // stray tap.
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
        } else if (!completed) {
          _uiState.update { it.copy(plantPrompt = next) }
        }
        // A standing completion for a question that is not the queue head
        // (a planting replaced mid-capture) is only logged below: the
        // current question keeps its prompt and its place.
        plantLog(next)
        photoLog("Reference look saved. $next")
      } else {
        val result = withContext(Dispatchers.IO) { client.postCapture(jpegFile) }
        _uiState.update { it.copy(serverBytes = result.bytes) }
        photoLog("Uploaded. The laptop says it received ${result.bytes} bytes.")
        if (result.speak.isNotEmpty()) {
          _uiState.update { it.copy(spokenLines = result.speak) }
          result.speak.forEach { line -> photoLog("Mannie says: $line") }
          speakViaGlasses(result.speak)
        }
      }
      photoLog("Done in ${SystemClock.elapsedRealtime() - startedAt} ms.")
    } finally {
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
   * Speak lines through the glasses, in order. Only called once a capture's camera
   * session has ended: opening Bluetooth audio can end a toolkit session, so the two
   * are never held open together. A failure here never fails the capture itself; the
   * lines stay on screen.
   */
  private suspend fun speakViaGlasses(lines: List<String>) {
    val audio = GlassesAudio(getApplication())
    try {
      val glasses = audio.routeToGlasses()
      if (glasses == null) {
        photoLog("No glasses audio route, so the answers were not spoken. They are on screen.")
        return
      }
      for (line in lines) {
        val requestStart = SystemClock.elapsedRealtime()
        val wavBytes = withContext(Dispatchers.IO) { laptopClient().postTts(line) }
        audio.playWav(wavBytes, glasses, requestStart)
      }
    } catch (e: CancellationException) {
      throw e
    } catch (e: Exception) {
      photoLog("Speaking the answers failed: ${e.message ?: e.toString()}")
    } finally {
      audio.clearRouting()
    }
  }

  // MARK: - Screen 3, voice (Gate 3)

  fun getAvailableCommunicationDevices(): List<String> {
    val audio = GlassesAudio(getApplication())
    return audio.listAvailableCommunicationDevices()
  }

  fun recordAndTranscribe(requestRecordAudio: suspend () -> Boolean) {
    if (_uiState.value.recordBusy) return
    val now = _uiState.value
    if (now.photoBusy || now.speakBusy || now.plantBusy) {
      // Bluetooth audio must never open while a camera session is alive or
      // other audio work is running: the two are never held together.
      _uiState.update { it.copy(lastError = "Wait for the current work to finish first.") }
      return
    }
    _uiState.update { it.copy(recordBusy = true, transcript = null, sttMs = null, lastError = null) }
    viewModelScope.launch {
      val audio = GlassesAudio(getApplication())
      try {
        check(requestRecordAudio()) {
          "Microphone permission was not granted, so nothing was recorded."
        }
        val glasses = audio.routeToGlasses()
        check(glasses != null) {
          "No Bluetooth headset device is available. Are the glasses connected to this phone?"
        }
        val dir = File(getApplication<Application>().filesDir, "voice").apply { mkdirs() }
        val wav = File(dir, "voice-${System.currentTimeMillis()}.wav")
        val routed = audio.recordFiveSeconds(wav, glasses)
        val speechEnd = SystemClock.elapsedRealtime()
        val text = withContext(Dispatchers.IO) { laptopClient().postStt(wav) }
        _uiState.update {
          it.copy(
              inputDevice = routed,
              transcript = text,
              sttMs = SystemClock.elapsedRealtime() - speechEnd,
          )
        }
      } catch (e: CancellationException) {
        throw e
      } catch (e: Exception) {
        showError(e)
      } finally {
        audio.clearRouting()
        _uiState.update { it.copy(recordBusy = false) }
      }
    }
  }

  fun speakTestSentence() {
    if (_uiState.value.speakBusy) return
    val now = _uiState.value
    if (now.photoBusy || now.recordBusy || now.plantBusy) {
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

  /** Record the question through the glasses and put its words in the text field. */
  fun recordPlantQuestion(requestRecordAudio: suspend () -> Boolean) {
    if (_uiState.value.plantBusy) return
    val now = _uiState.value
    if (now.photoBusy || now.recordBusy || now.speakBusy) {
      // Same rule as the Voice screen: Bluetooth audio never opens while a
      // camera session is alive or other audio work is running.
      _uiState.update { it.copy(lastError = "Wait for the current work to finish first.") }
      return
    }
    _uiState.update { it.copy(plantBusy = true, lastError = null) }
    viewModelScope.launch {
      val audio = GlassesAudio(getApplication())
      try {
        check(requestRecordAudio()) {
          "Microphone permission was not granted, so nothing was recorded."
        }
        val glasses = audio.routeToGlasses()
        check(glasses != null) {
          "No Bluetooth headset device is available. Are the glasses connected to this phone?"
        }
        val dir = File(getApplication<Application>().filesDir, "voice").apply { mkdirs() }
        val wav = File(dir, "plant-${System.currentTimeMillis()}.wav")
        audio.recordFiveSeconds(wav, glasses)
        val text = withContext(Dispatchers.IO) { laptopClient().postStt(wav) }
        _uiState.update { it.copy(plantText = text) }
        plantLog("Heard: $text")
      } catch (e: CancellationException) {
        throw e
      } catch (e: Exception) {
        showError(e)
      } finally {
        audio.clearRouting()
        _uiState.update { it.copy(plantBusy = false) }
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
    if (now.recordBusy || now.speakBusy || now.plantBusy) {
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

  private fun showError(e: Exception) {
    val message = e.message ?: e.toString()
    Log.e(TAG, message, e)
    _uiState.update { it.copy(lastError = message) }
  }

  companion object {
    const val TAG = "Truss"
    const val TEST_SENTENCE = "Hello, this is Mannie. I can hear you."
    private const val PREFS = "mannie"
    private const val KEY_LAPTOP_IP = "laptop_ip"
    private const val STEP_TIMEOUT_MS = 15_000L
    private const val RETURN_TIMEOUT_MS = 15_000L
    private const val POLL_MS = 500L
  }
}
