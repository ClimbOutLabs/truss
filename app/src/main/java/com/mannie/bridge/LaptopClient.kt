package com.mannie.bridge

import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import org.json.JSONObject

/**
 * The only network client in the app, and it can only talk to the laptop: the base URL is
 * built from the one LAPTOP_IP value the person typed on Screen 1, always on port 8791.
 * Plain HttpURLConnection, so the app carries no extra HTTP or cloud library at all.
 */
class LaptopClient(laptopIp: String) {

  private val base: String

  init {
    val ip = laptopIp.trim()
    require(ip.isNotEmpty()) { "Type the laptop address on the Connect screen first." }
    require(!ip.contains("://") && !ip.contains("/") && !ip.contains(":") && ip.none { it.isWhitespace() }) {
      "The laptop address must be a plain address on your home network, like 192.168.1.20, with nothing else in it."
    }
    base = "http://$ip:8792"
  }

  private fun open(path: String): HttpURLConnection {
    val connection = URL(base + path).openConnection() as HttpURLConnection
    connection.connectTimeout = CONNECT_TIMEOUT_MS
    connection.readTimeout = READ_TIMEOUT_MS
    return connection
  }

  /** GET / : returns the HTTP status code. Throws with the real message when unreachable. */
  fun ping(): Int {
    val connection = open("/")
    try {
      connection.requestMethod = "GET"
      return connection.responseCode
    } finally {
      connection.disconnect()
    }
  }

  fun canReach(): Boolean =
      try {
        ping() in 200..399
      } catch (e: Exception) {
        false
      }

  /** POST /capture, multipart field "image" (JPEG). Returns the server's byte count. */
  fun postCapture(jpeg: File): Long {
    val body = postMultipart("/capture", "image", jpeg.name, "image/jpeg", jpeg.readBytes())
    return JSONObject(body).getLong("bytes")
  }

  /** POST /stt, multipart field "audio" (WAV). Returns the transcript text. */
  fun postStt(wav: File): String {
    val body = postMultipart("/stt", "audio", wav.name, "audio/wav", wav.readBytes())
    return JSONObject(body).getString("text")
  }

  /** POST /tts with a JSON body. Returns the WAV bytes the server sends back. */
  fun postTts(text: String): ByteArray {
    val connection = open("/tts")
    try {
      connection.requestMethod = "POST"
      connection.doOutput = true
      connection.setRequestProperty("Content-Type", "application/json")
      val payload = JSONObject().put("text", text).toString().toByteArray(Charsets.UTF_8)
      connection.outputStream.use { it.write(payload) }
      val code = connection.responseCode
      if (code !in 200..299) {
        val errorBody = connection.errorStream?.readBytes()?.toString(Charsets.UTF_8) ?: ""
        throw IllegalStateException("Laptop answered HTTP $code for /tts: $errorBody")
      }
      return connection.inputStream.use { it.readBytes() }
    } finally {
      connection.disconnect()
    }
  }

  private fun postMultipart(
      path: String,
      field: String,
      fileName: String,
      contentType: String,
      bytes: ByteArray,
  ): String {
    val boundary = "mannie-" + System.nanoTime().toString(16)
    val connection = open(path)
    try {
      connection.requestMethod = "POST"
      connection.doOutput = true
      connection.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
      connection.outputStream.use { out ->
        out.write(
            ("--$boundary\r\n" +
                    "Content-Disposition: form-data; name=\"$field\"; filename=\"$fileName\"\r\n" +
                    "Content-Type: $contentType\r\n\r\n")
                .toByteArray(Charsets.UTF_8),
        )
        out.write(bytes)
        out.write("\r\n--$boundary--\r\n".toByteArray(Charsets.UTF_8))
      }
      val code = connection.responseCode
      val stream = if (code in 200..299) connection.inputStream else connection.errorStream
      val body = stream?.readBytes()?.toString(Charsets.UTF_8) ?: ""
      if (code !in 200..299) {
        throw IllegalStateException("Laptop answered HTTP $code for $path: $body")
      }
      return body
    } finally {
      connection.disconnect()
    }
  }

  private companion object {
    const val CONNECT_TIMEOUT_MS = 5_000
    const val READ_TIMEOUT_MS = 15_000
  }
}
