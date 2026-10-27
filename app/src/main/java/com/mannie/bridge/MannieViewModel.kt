package com.mannie.bridge

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

data class MannieUiState(
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

class MannieViewModel(application: Application) : AndroidViewModel(application) {

  private val _uiState = MutableStateFlow(MannieUiState(laptopIp = loadLaptopIp(application)))
  val uiState: StateFlow<MannieUiState> = _uiState.asStateFlow()

  // AutoDeviceSelector automatically selects the first available wearable device,
  // exactly as the CameraAccess sample does.
  private val deviceSelector: DeviceSelector by lazy { AutoDeviceSelector() }

  private var monitoringStarted = false
  private val deviceNames = mutableMapOf<DeviceIdentifier, String>()

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
    _uiState.update {
      it.copy(
          photoBusy = true,
          photoLog = emptyList(),
          serverBytes = null,
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

      // Step 6: upload.
      val bytes = withContext(Dispatchers.IO) { client.postCapture(jpegFile) }
      _uiState.update { it.copy(serverBytes = bytes) }
      photoLog("Uploaded. The laptop says it received $bytes bytes.")
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

  // MARK: - Screen 3, voice (Gate 3)

  fun getAvailableCommunicationDevices(): List<String> {
    val audio = GlassesAudio(getApplication())
    return audio.listAvailableCommunicationDevices()
  }

  fun recordAndTranscribe(requestRecordAudio: suspend () -> Boolean) {
    if (_uiState.value.recordBusy) return
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

  private fun showError(e: Exception) {
    val message = e.message ?: e.toString()
    Log.e(TAG, message, e)
    _uiState.update { it.copy(lastError = message) }
  }

  companion object {
    const val TAG = "MannieBridge"
    const val TEST_SENTENCE = "Hello, this is Mannie. I can hear you."
    private const val PREFS = "mannie"
    private const val KEY_LAPTOP_IP = "laptop_ip"
    private const val STEP_TIMEOUT_MS = 15_000L
    private const val RETURN_TIMEOUT_MS = 15_000L
    private const val POLL_MS = 500L
  }
}
