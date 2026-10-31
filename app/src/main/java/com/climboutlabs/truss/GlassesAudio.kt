package com.climboutlabs.truss

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.os.Build
import android.os.SystemClock
import androidx.core.content.ContextCompat
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.resume

/** Which device actually did the work, in words the screen can show. */
data class RoutedDevice(val typeName: String, val productName: String, val isBluetooth: Boolean) {
  fun describe(): String = "$typeName ($productName)"
}

data class PlayResult(val routed: RoutedDevice, val audioStartDelayMs: Long)

/**
 * Microphone and speaker are not toolkit features: the glasses act as a Bluetooth headset,
 * so this uses the plain Android audio APIs with the glasses as the communication device,
 * exactly as the day-1 brief specifies.
 */
class GlassesAudio(private val context: Context) {

  private val audioManager: AudioManager =
      context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

  private var legacyScoStarted = false

  fun listAvailableCommunicationDevices(): List<String> =
      audioManager.availableCommunicationDevices.map { device ->
        val routed = describe(device)
        "${routed.typeName} (${routed.productName})"
      }

  fun describe(device: AudioDeviceInfo?): RoutedDevice {
    if (device == null) return RoutedDevice("unknown", "no routed device reported", false)
    val typeName =
        when (device.type) {
          AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "Bluetooth SCO"
          AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "Bluetooth A2DP"
          AudioDeviceInfo.TYPE_BLE_HEADSET -> "Bluetooth LE headset"
          AudioDeviceInfo.TYPE_BLE_SPEAKER -> "Bluetooth LE speaker"
          AudioDeviceInfo.TYPE_BUILTIN_MIC -> "built-in microphone"
          AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "built-in speaker"
          AudioDeviceInfo.TYPE_WIRED_HEADSET -> "wired headset"
          AudioDeviceInfo.TYPE_USB_HEADSET -> "USB headset"
          else -> "device type ${device.type}"
        }
    return RoutedDevice(
        typeName = typeName,
        productName = device.productName?.toString()?.ifBlank { "unnamed" } ?: "unnamed",
        isBluetooth = device.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || 
                      device.type == AudioDeviceInfo.TYPE_BLE_HEADSET || 
                      device.type == AudioDeviceInfo.TYPE_BLE_SPEAKER,
    )
  }

  private fun scoCommunicationDevice(): AudioDeviceInfo? =
      audioManager.availableCommunicationDevices.firstOrNull {
        it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || it.type == AudioDeviceInfo.TYPE_BLE_HEADSET
      }

  /**
   * Routes communication audio to the glasses. On API 31+ that is setCommunicationDevice
   * with the TYPE_BLUETOOTH_SCO entry; on older systems it is startBluetoothSco and a wait
   * for SCO_AUDIO_STATE_CONNECTED. Returns the glasses device, or null when the phone has
   * no Bluetooth SCO communication device at all.
   */
  suspend fun routeToGlasses(): AudioDeviceInfo? {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
      val device = scoCommunicationDevice() ?: return null
      check(audioManager.setCommunicationDevice(device)) {
        "Android refused to set the glasses as the communication device."
      }
      audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
      // Wait for the device to become active
      val deadline = SystemClock.elapsedRealtime() + 3_000L
      while (SystemClock.elapsedRealtime() < deadline) {
        if (audioManager.communicationDevice == device) {
          @Suppress("DEPRECATION")
          audioManager.isBluetoothScoOn = true
          return device
        }
        delay(100L)
      }
      throw IllegalStateException("The glasses never became the active communication device.")
    }
    @Suppress("DEPRECATION")
    audioManager.startBluetoothSco()
    legacyScoStarted = true
    val connected =
        withTimeoutOrNull(SCO_WAIT_MS) {
          suspendCancellableCoroutine { continuation ->
            val receiver =
                object : BroadcastReceiver() {
                  override fun onReceive(receiverContext: Context?, intent: Intent?) {
                    val state =
                        intent?.getIntExtra(
                            AudioManager.EXTRA_SCO_AUDIO_STATE,
                            AudioManager.SCO_AUDIO_STATE_ERROR,
                        ) ?: AudioManager.SCO_AUDIO_STATE_ERROR
                    if (state == AudioManager.SCO_AUDIO_STATE_CONNECTED &&
                        continuation.isActive) {
                      continuation.resume(true)
                    }
                  }
                }
            ContextCompat.registerReceiver(
                context,
                receiver,
                IntentFilter(AudioManager.ACTION_SCO_AUDIO_STATE_UPDATED),
                ContextCompat.RECEIVER_NOT_EXPORTED,
            )
            continuation.invokeOnCancellation {
              try {
                context.unregisterReceiver(receiver)
              } catch (e: IllegalArgumentException) {
                // Already unregistered; nothing to clean up.
              }
            }
          }
        }
    check(connected == true) { "Bluetooth SCO did not connect to the glasses in time." }
    return scoCommunicationDevice()
  }

  fun clearRouting() {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
      @Suppress("DEPRECATION")
      audioManager.isBluetoothScoOn = false
      audioManager.clearCommunicationDevice()
      audioManager.mode = AudioManager.MODE_NORMAL
    }
    if (legacyScoStarted) {
      @Suppress("DEPRECATION") audioManager.stopBluetoothSco()
      legacyScoStarted = false
    }
  }

  /** Plays a WAV (16-bit PCM) with voice-communication usage so it lands in the glasses. */
  suspend fun playWav(wavBytes: ByteArray, preferred: AudioDeviceInfo?, requestStartMs: Long): PlayResult =
      withContext(Dispatchers.IO) {
        val wav = parseWavPcm16(wavBytes)
        val channelMask =
            if (wav.channels == 2) AudioFormat.CHANNEL_OUT_STEREO else AudioFormat.CHANNEL_OUT_MONO
        val minBuffer =
            AudioTrack.getMinBufferSize(wav.sampleRate, channelMask, AudioFormat.ENCODING_PCM_16BIT)
        check(minBuffer > 0) { "This phone reported no usable playback buffer for the reply audio." }
        val track =
            AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build(),
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(wav.sampleRate)
                        .setChannelMask(channelMask)
                        .build(),
                )
                .setBufferSizeInBytes(maxOf(minBuffer, wav.pcm.size))
                .setTransferMode(AudioTrack.MODE_STATIC)
                .build()
        try {
          if (preferred != null) {
            track.setPreferredDevice(preferred)
          }
          track.write(wav.pcm, 0, wav.pcm.size)
          track.play()
          val startDelay = SystemClock.elapsedRealtime() - requestStartMs
          val routed = describe(track.routedDevice)
          // Wait until the static buffer has played out (plus a small tail) before releasing.
          val durationMs = wav.pcm.size * 1_000L / (wav.sampleRate * wav.channels * 2)
          delay(durationMs + 300L)
          PlayResult(routed = routed, audioStartDelayMs = startDelay)
        } finally {
          try {
            track.stop()
          } catch (e: IllegalStateException) {
            // Never started; releasing is enough.
          }
          track.release()
        }
      }

  private data class Wav(val sampleRate: Int, val channels: Int, val pcm: ByteArray)

  /** Small RIFF reader: finds the fmt and data chunks instead of assuming a fixed header. */
  private fun parseWavPcm16(bytes: ByteArray): Wav {
    require(bytes.size > 44 && String(bytes, 0, 4, Charsets.US_ASCII) == "RIFF") {
      "The laptop's reply was not a WAV file."
    }
    var sampleRate = 16_000
    var channels = 1
    var bitsPerSample = 16
    var offset = 12
    var data: ByteArray? = null
    while (offset + 8 <= bytes.size) {
      val id = String(bytes, offset, 4, Charsets.US_ASCII)
      val size =
          (bytes[offset + 4].toInt() and 0xff) or
              ((bytes[offset + 5].toInt() and 0xff) shl 8) or
              ((bytes[offset + 6].toInt() and 0xff) shl 16) or
              ((bytes[offset + 7].toInt() and 0xff) shl 24)
      val start = offset + 8
      if (start + size > bytes.size) break
      when (id) {
        "fmt " -> {
          channels = (bytes[start + 2].toInt() and 0xff) or ((bytes[start + 3].toInt() and 0xff) shl 8)
          sampleRate =
              (bytes[start + 4].toInt() and 0xff) or
                  ((bytes[start + 5].toInt() and 0xff) shl 8) or
                  ((bytes[start + 6].toInt() and 0xff) shl 16) or
                  ((bytes[start + 7].toInt() and 0xff) shl 24)
          bitsPerSample =
              (bytes[start + 14].toInt() and 0xff) or ((bytes[start + 15].toInt() and 0xff) shl 8)
        }
        "data" -> data = bytes.copyOfRange(start, start + size)
      }
      offset = start + size + (size and 1)
    }
    val pcm = data
    require(pcm != null) { "The laptop's WAV file had no sound data in it." }
    require(bitsPerSample == 16) { "The laptop's WAV file is not 16-bit PCM, which is what Truss plays." }
    return Wav(sampleRate = sampleRate, channels = channels, pcm = pcm)
  }

  companion object {
    private const val SCO_WAIT_MS = 10_000L
  }
}
