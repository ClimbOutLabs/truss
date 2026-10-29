package com.climboutlabs.truss

import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import org.json.JSONObject

/**
 * The only network client in the app, and it can only talk to the laptop: the base URL is
 * built from the one LAPTOP_IP value the person typed on Screen 1, always on port 8792.
 * Plain HttpURLConnection, so the app carries no extra HTTP or cloud library at all.
 */

/** What the laptop answers to a capture: its byte count, the standing questions the
 * photo fired, and the lines Mannie should speak, in order. Empty lists when no
 * question fired. */
data class CaptureResult(val bytes: Long, val fired: List<String>, val speak: List<String>)

/** One standing question as GET /questions reports it. */
data class QuestionInfo(
    val slug: String,
    val question: String,
    val state: String,
    val hasRefYes: Boolean,
    val hasRefNo: Boolean,
)

/** One missed-window alert as GET /alerts reports it: an id and the line to
 * speak. The alert stays on the laptop until ackAlert confirms it was spoken. */
data class MissAlert(val id: String, val line: String)
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

  private fun open(path: String, readTimeoutMs: Int = READ_TIMEOUT_MS): HttpURLConnection {
    val connection = URL(base + path).openConnection() as HttpURLConnection
    connection.connectTimeout = CONNECT_TIMEOUT_MS
    connection.readTimeout = readTimeoutMs
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

  /** POST /capture, multipart field "image" (JPEG). Returns the byte count plus any
   * standing questions the photo fired and the lines to speak for them. */
  fun postCapture(jpeg: File): CaptureResult {
    // The laptop answers only after every due question has been checked, one
    // model call each (several seconds apiece, longer on a retry), so this one
    // call waits much longer than the rest. Giving up early would lose the
    // spoken answer: the question still fires and rests on the laptop.
    val body =
        postMultipart(
            "/capture", "image", jpeg.name, "image/jpeg", jpeg.readBytes(), CAPTURE_READ_TIMEOUT_MS)
    val json = JSONObject(body)
    fun lines(key: String): List<String> {
      val array = json.optJSONArray(key) ?: return emptyList()
      return List(array.length()) { i -> array.getString(i) }
    }
    return CaptureResult(json.getLong("bytes"), lines("fired"), lines("speak"))
  }

  /** POST /plant/question with the question text. Returns the laptop's reply: the slug,
   * the state, and the next prompt to show the wearer. */
  fun plantQuestion(text: String): JSONObject =
      JSONObject(postJson("/plant/question", JSONObject().put("text", text)))

  /** POST /plant/bedtime. Returns the reply: the round's slugs and questions, in
   * walk order. */
  fun plantBedtime(): JSONObject = JSONObject(postJson("/plant/bedtime", JSONObject()))

  /** POST /plant/ref for one reference look. The slug comes from the laptop's own
   * slug maker, so it is always plain letters, digits, and dashes and needs no
   * escaping in the query. Returns the reply: slug, state, and the next prompt. */
  fun postPlantRef(jpeg: File, slug: String, kind: String): JSONObject {
    val body =
        postMultipart("/plant/ref?slug=$slug&kind=$kind", "image", jpeg.name, "image/jpeg", jpeg.readBytes())
    return JSONObject(body)
  }

  /** GET /questions: the standing questions and their states. */
  fun getQuestions(): List<QuestionInfo> {
    val array = JSONObject(getJson("/questions")).getJSONArray("questions")
    return List(array.length()) { i ->
      val item = array.getJSONObject(i)
      QuestionInfo(
          slug = item.getString("slug"),
          question = item.getString("question"),
          state = item.getString("state"),
          hasRefYes = item.getBoolean("has_ref_yes"),
          hasRefNo = item.getBoolean("has_ref_no"),
      )
    }
  }

  /** GET /alerts: the missed-window alerts still waiting to be spoken. */
  fun getAlerts(): List<MissAlert> {
    val array = JSONObject(getJson("/alerts")).getJSONArray("alerts")
    return List(array.length()) { i ->
      val item = array.getJSONObject(i)
      MissAlert(id = item.getString("id"), line = item.getString("line"))
    }
  }

  /** POST /alerts/ack for one alert, confirming its line was spoken. Throws
   * on a non-2xx answer, like the other calls here, so the caller knows the
   * confirmation did not land and the alert will be offered again. */
  fun ackAlert(id: String) {
    postJson("/alerts/ack", JSONObject().put("id", id))
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

  /** POST a JSON body and return the reply body. Throws with the real message on a
   * non-2xx answer, like the other calls here. */
  private fun postJson(path: String, payload: JSONObject): String {
    val connection = open(path)
    try {
      connection.requestMethod = "POST"
      connection.doOutput = true
      connection.setRequestProperty("Content-Type", "application/json")
      connection.outputStream.use { it.write(payload.toString().toByteArray(Charsets.UTF_8)) }
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

  /** GET and return the reply body, with the same error shape as postJson. */
  private fun getJson(path: String): String {
    val connection = open(path)
    try {
      connection.requestMethod = "GET"
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

  private fun postMultipart(
      path: String,
      field: String,
      fileName: String,
      contentType: String,
      bytes: ByteArray,
      readTimeoutMs: Int = READ_TIMEOUT_MS,
  ): String {
    val boundary = "mannie-" + System.nanoTime().toString(16)
    val connection = open(path, readTimeoutMs)
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
    const val CAPTURE_READ_TIMEOUT_MS = 300_000
  }
}
