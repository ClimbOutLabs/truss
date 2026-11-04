package com.climboutlabs.truss

import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
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

/** One standing question as GET /questions reports it. answeredAt is the laptop's
 * local time of its last firing, or null. */
data class QuestionInfo(
    val slug: String,
    val question: String,
    val state: String,
    val hasRefYes: Boolean,
    val hasRefNo: Boolean,
    val kind: String = "state",
    val answeredAt: String? = null,
    val safety: Boolean = false,
)

/** One logged answer as GET /answers reports it. kind is "answer", "redo",
 * "state_check", "cannot_tell" (a spoken refusal) or "other"; evidence and outline are photo names for GET /evidence. */
data class AnswerItem(
    val time: String,
    val question: String,
    val kind: String,
    val text: String,
    val evidence: String?,
    val outline: String?,
)

/** One day's answers, newest first. */
data class DayAnswers(val day: String, val answers: List<AnswerItem>)

/** One check of a question as GET /memory reports it: the clock time the
 * check finished, the verdict in the server's words ("yes", "no" or
 * "cannot_tell"), the reason the checker wrote, and the evidence photo's
 * name when the server kept one (for GET /evidence). */
data class MemoryItem(
    val time: String,
    val verdict: String,
    val reason: String,
    val evidence: String?,
)

/** One question's checks for a day, in time order: every check, not only
 * the ones that fired. */
data class QuestionMemory(val day: String, val question: String, val checks: List<MemoryItem>)

/** One missed-window alert as GET /alerts reports it: an id and the line to
 * speak. The alert stays on the laptop until ackAlert confirms it was spoken. */
data class MissAlert(val id: String, val line: String)

/** The Desk's latest start (on) or stop request for watching, as POST /watch/phone
 * returns it. Each request has a fresh id; ageSeconds is how long ago it was made. */
data class WatchRequest(val id: String, val on: Boolean, val ageSeconds: Int)

/** What a watch check-in returns: the Desk's latest request, or null, how old a
 * request may be and still be acted on, and the id of a fresh Desk request for one
 * photo now (POST /watch/look), or null. */
data class WatchCheckIn(
    val request: WatchRequest?,
    val freshSeconds: Int,
    val lookId: String? = null,
    val lookAgeSeconds: Int = 0,
)
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
    val connection = open("/", PING_READ_TIMEOUT_MS)
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

  /** POST /watch/look/photo: a photo the Desk asked for, taken while another photo is
   * still being checked. The laptop only keeps it as the glasses' latest view (for the
   * Desk's yes or no look); no question is checked on it. */
  fun postLookPhoto(jpeg: File) {
    postMultipart("/watch/look/photo", "image", jpeg.name, "image/jpeg", jpeg.readBytes(), POLL_READ_TIMEOUT_MS)
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
        postMultipart(
            "/plant/ref?slug=$slug&kind=$kind", "image", jpeg.name, "image/jpeg", jpeg.readBytes(),
            CAPTURE_READ_TIMEOUT_MS)
    return JSONObject(body)
  }

  /** POST /plant/finish: start a planting question standing with the looks it has
   * (the yes look alone, or none), for a question its words decide, like a number on
   * a display. Returns the reply: slug, state, and the next line to show. */
  fun finishPlanting(slug: String): JSONObject =
      JSONObject(postJson("/plant/finish", JSONObject().put("slug", slug)))

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
          kind = item.optString("kind", "state"),
          answeredAt = item.optNullableString("answered_at"),
          safety = item.optBoolean("safety", false) || item.optString("kind", "state") == "done",
      )
    }
  }

  /** POST /questions/delete: remove one question for good. It is never checked again,
   * its waiting alerts are never spoken, and it leaves the Desk's and the phone's lists.
   * Its files stay on the laptop. */
  fun retireQuestion(slug: String) {
    postJson("/questions/delete", JSONObject().put("slug", slug))
  }

  /** GET /answers: today's answers on the laptop's clock, newest first. */
  fun getAnswers(): DayAnswers {
    val json = JSONObject(getJson("/answers"))
    val array = json.getJSONArray("answers")
    val items =
        List(array.length()) { i ->
          val item = array.getJSONObject(i)
          AnswerItem(
              time = item.getString("time"),
              question = item.getString("question"),
              kind = item.optString("kind", "answer"),
              text = item.optString("text", ""),
              evidence = item.optNullableString("evidence"),
              outline = item.optNullableString("outline"),
          )
        }
    return DayAnswers(json.getString("day"), items)
  }

  /** GET /evidence: one kept photo's JPEG bytes. The day and name come from
   * GET /answers, and the laptop only serves plain photo names. */
  fun getEvidence(day: String, name: String): ByteArray {
    val query =
        "?day=" + URLEncoder.encode(day, "UTF-8") + "&name=" + URLEncoder.encode(name, "UTF-8")
    val connection = open("/evidence$query")
    try {
      connection.requestMethod = "GET"
      val code = connection.responseCode
      if (code !in 200..299) {
        throw IllegalStateException("Laptop answered HTTP $code for /evidence")
      }
      return connection.inputStream.use { it.readBytes() }
    } finally {
      connection.disconnect()
    }
  }

  /** GET /memory: one question's checks today, in time order. Unlike
   * /answers, this is every check the laptop logged for the question,
   * including the ones that came back no or could not tell and were
   * silent everywhere else. */
  fun getMemory(slug: String): QuestionMemory {
    val query = "?slug=" + URLEncoder.encode(slug, "UTF-8")
    val json = JSONObject(getJson("/memory$query"))
    val array = json.getJSONArray("checks")
    val items =
        List(array.length()) { i ->
          val item = array.getJSONObject(i)
          MemoryItem(
              time = item.getString("time"),
              verdict = item.optString("verdict", "cannot_tell"),
              reason = item.optString("reason", ""),
              evidence = item.optNullableString("evidence"),
          )
        }
    return QuestionMemory(json.getString("day"), json.optString("question", ""), items)
  }

  /** A JSON string field, or null when it is missing or JSON null. */
  private fun JSONObject.optNullableString(key: String): String? =
      if (isNull(key)) null else optString(key)

  /** GET /alerts: the missed-window alerts still waiting to be spoken. */
  fun getAlerts(): List<MissAlert> {
    val array = JSONObject(getJson("/alerts", POLL_READ_TIMEOUT_MS)).getJSONArray("alerts")
    return List(array.length()) { i ->
      val item = array.getJSONObject(i)
      MissAlert(id = item.getString("id"), line = item.getString("line"))
    }
  }

  /** POST /alerts/ack for one alert, confirming its line was spoken. Throws
   * on a non-2xx answer, like the other calls here, so the caller knows the
   * confirmation did not land and the alert will be offered again. */
  fun ackAlert(id: String) {
    postJson("/alerts/ack", JSONObject().put("id", id), POLL_READ_TIMEOUT_MS)
  }

  /** POST /watch/phone: tell the laptop how watching stands here, so the Desk can show
   * it, and get back the Desk's latest start or stop request. */
  fun watchCheckIn(
      watching: Boolean,
      away: Boolean,
      glasses: Boolean,
      pending: Boolean,
      every: Int,
      mode: String,
      off: Boolean,
  ): WatchCheckIn {
    val payload =
        JSONObject()
            .put("watching", watching)
            .put("away", away)
            .put("glasses", glasses)
            .put("pending", pending)
            .put("every", every)
            .put("mode", mode)
            .put("off", off)
    val reply = JSONObject(postJson("/watch/phone", payload, POLL_READ_TIMEOUT_MS))
    val item = reply.optJSONObject("request")
    val request =
        item?.let {
          WatchRequest(id = it.getString("id"), on = it.getBoolean("on"), ageSeconds = it.optInt("age_seconds", 0))
        }
    val look = reply.optJSONObject("look")
    val lookId = look?.optString("id")?.takeIf { it.isNotEmpty() }
    return WatchCheckIn(request, reply.optInt("fresh_seconds", 60), lookId, look?.optInt("age_seconds", 0) ?: 0)
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
  private fun postJson(path: String, payload: JSONObject, readTimeoutMs: Int = READ_TIMEOUT_MS): String {
    val connection = open(path, readTimeoutMs)
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
  private fun getJson(path: String, readTimeoutMs: Int = READ_TIMEOUT_MS): String {
    val connection = open(path, readTimeoutMs)
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
    // The laptop serves one request at a time, so a quick call can queue behind a
    // capture that is checking with the model and SAM; 90 s covers that wait.
    const val READ_TIMEOUT_MS = 90_000
    const val PING_READ_TIMEOUT_MS = 10_000
    // The missed-alert poll holds the glasses audio while it waits, which keeps
    // photos from starting; a slow poll is simply tried again later.
    const val POLL_READ_TIMEOUT_MS = 15_000
    const val CAPTURE_READ_TIMEOUT_MS = 300_000
  }
}
