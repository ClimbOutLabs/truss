package com.climboutlabs.truss

import android.graphics.Bitmap
import android.os.Build
import androidx.compose.foundation.Image
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.meta.wearable.dat.core.types.Permission
import com.meta.wearable.dat.core.types.PermissionStatus

/** Toolkit version pinned in gradle/libs.versions.toml, shown under Diagnostics. */
private const val TOOLKIT_VERSION = "1.0.1"

private val LightColors =
    lightColorScheme(
        primary = Color(0xFF2F5D50),
        onPrimary = Color.White,
        primaryContainer = Color(0xFFCDE8DD),
        onPrimaryContainer = Color(0xFF0B2019),
        secondaryContainer = Color(0xFFF1E6D3),
        onSecondaryContainer = Color(0xFF2A2116),
        errorContainer = Color(0xFFFFDAD6),
        onErrorContainer = Color(0xFF410002),
    )

private val DarkColors =
    darkColorScheme(
        primary = Color(0xFF9BD1BE),
        onPrimary = Color(0xFF00382A),
        primaryContainer = Color(0xFF17473A),
        onPrimaryContainer = Color(0xFFCDE8DD),
        secondaryContainer = Color(0xFF4A3F2E),
        onSecondaryContainer = Color(0xFFF1E6D3),
    )

private enum class Destination(val label: String, val glyph: String) {
  HOME("Home", "⌂"),
  QUESTIONS("Questions", "?"),
  HISTORY("History", "◷"),
  SETTINGS("Settings", "⚙"),
}

@OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)
@Composable
fun TrussApp(
    viewModel: TrussViewModel,
    onConnectGlasses: () -> Unit,
    onRequestWearablesPermission: suspend (Permission) -> PermissionStatus,
    onRequestRecordAudioPermission: suspend () -> Boolean,
) {
  val state by viewModel.uiState.collectAsState()
  var tab by rememberSaveable { mutableIntStateOf(0) }
  val destination = Destination.entries[tab]
  MaterialTheme(colorScheme = if (isSystemInDarkTheme()) DarkColors else LightColors) {
    Scaffold(
        topBar = { TopAppBar(title = { Text(if (tab == 0) "Truss" else destination.label) }) },
        bottomBar = {
          NavigationBar {
            Destination.entries.forEachIndexed { index, item ->
              NavigationBarItem(
                  selected = tab == index,
                  onClick = { tab = index },
                  icon = { Text(item.glyph, style = MaterialTheme.typography.titleLarge) },
                  label = { Text(item.label) },
              )
            }
          }
        },
    ) { padding ->
      Column(
          modifier =
              Modifier.fillMaxSize()
                  .padding(padding)
                  .verticalScroll(rememberScrollState())
                  .padding(16.dp),
          verticalArrangement = Arrangement.spacedBy(16.dp),
      ) {
        state.lastError?.let { ErrorCard(it, onDismiss = viewModel::clearError) }
        when (destination) {
          Destination.HOME ->
              HomeScreen(
                  state = state,
                  onConnectGlasses = onConnectGlasses,
                  onCheckLaptop = viewModel::checkLaptopNow,
                  onCheckNow = { viewModel.captureAndUpload(onRequestWearablesPermission) },
                  onWatch = { on ->
                    if (on) viewModel.startWatching(onRequestWearablesPermission)
                    else viewModel.stopWatching()
                  },
                  onWatchEvery = viewModel::setWatchEverySeconds,
                  onOpenSettings = { tab = Destination.SETTINGS.ordinal },
                  onOpenQuestions = { tab = Destination.QUESTIONS.ordinal },
              )
          Destination.QUESTIONS ->
              QuestionsScreen(
                  state = state,
                  viewModel = viewModel,
                  onRecordQuestion = { viewModel.recordPlantQuestion(onRequestRecordAudioPermission) },
                  onCaptureRef = { kind ->
                    viewModel.captureReference(kind, onRequestWearablesPermission)
                  },
              )
          Destination.HISTORY -> HistoryScreen(state = state, onRefresh = viewModel::loadHistory)
          Destination.SETTINGS ->
              SettingsScreen(
                  state = state,
                  viewModel = viewModel,
                  onConnectGlasses = onConnectGlasses,
                  onCapture = { viewModel.captureAndUpload(onRequestWearablesPermission) },
                  onRecord = { viewModel.recordAndTranscribe(onRequestRecordAudioPermission) },
              )
        }
      }
    }
  }
}

// MARK: - Shared pieces

@Composable
private fun SectionCard(
    title: String? = null,
    container: Color = MaterialTheme.colorScheme.surfaceVariant,
    content: @Composable ColumnScope.() -> Unit,
) {
  Card(
      modifier = Modifier.fillMaxWidth(),
      colors = CardDefaults.cardColors(containerColor = container),
  ) {
    Column(
        modifier = Modifier.padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
      title?.let { Text(it, style = MaterialTheme.typography.titleMedium) }
      content()
    }
  }
}

@Composable
private fun ErrorCard(message: String, onDismiss: () -> Unit) {
  SectionCard(container = MaterialTheme.colorScheme.errorContainer) {
    Text(message, color = MaterialTheme.colorScheme.onErrorContainer)
    TextButton(onClick = onDismiss) { Text("Dismiss") }
  }
}

@Composable
private fun StatusRow(label: String, value: String, good: Boolean?) {
  Row(verticalAlignment = Alignment.CenterVertically) {
    val dot =
        when (good) {
          true -> Color(0xFF2E7D32)
          false -> Color(0xFFC62828)
          null -> Color(0xFF9E9E9E)
        }
    Text("●", color = dot)
    Spacer(Modifier.width(8.dp))
    Text(label, fontWeight = FontWeight.SemiBold)
    Spacer(Modifier.width(8.dp))
    Text(value, style = MaterialTheme.typography.bodyMedium)
  }
}

@Composable
private fun Photo(bitmap: Bitmap, description: String) {
  Image(
      bitmap = bitmap.asImageBitmap(),
      contentDescription = description,
      contentScale = ContentScale.FillWidth,
      modifier = Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)),
  )
}

@Composable
private fun Working(line: String?) {
  Row(verticalAlignment = Alignment.CenterVertically) {
    CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
    Spacer(Modifier.width(12.dp))
    Text(
        line?.substringAfter("  ")?.ifBlank { null } ?: "Working...",
        style = MaterialTheme.typography.bodyMedium,
    )
  }
}

private fun questionStatus(item: QuestionInfo): String =
    when (item.state) {
      "standing" -> "Watching"
      "planting" ->
          when {
            !item.hasRefYes -> "Needs its yes look"
            !item.hasRefNo -> "Needs its no look"
            else -> "Planting"
          }
      "answered" ->
          item.answeredAt?.let { "Answered at ${it.substringAfter('T').take(5)}" } ?: "Answered"
      else -> item.state
    }

// MARK: - Home

@Composable
private fun HomeScreen(
    state: TrussUiState,
    onConnectGlasses: () -> Unit,
    onCheckLaptop: () -> Unit,
    onCheckNow: () -> Unit,
    onWatch: (Boolean) -> Unit,
    onWatchEvery: (Int) -> Unit,
    onOpenSettings: () -> Unit,
    onOpenQuestions: () -> Unit,
) {
  // The phone stays awake while watching: the photo loop runs in the app.
  val view = LocalView.current
  DisposableEffect(state.watching) {
    view.keepScreenOn = state.watching
    onDispose { view.keepScreenOn = false }
  }
  val watching = state.questionsList.count { it.state == "standing" }
  val ready = state.glassesReady && state.laptopReachable == true

  SectionCard(
      container = MaterialTheme.colorScheme.primaryContainer,
  ) {
    Text(
        when {
          state.busy -> "Mannie is looking..."
          ready && watching > 0 ->
              "Mannie is watching $watching question" + if (watching == 1) "." else "s."
          ready -> "Mannie is ready. Plant a question to start."
          else -> "Mannie needs a hand getting set up."
        },
        style = MaterialTheme.typography.headlineSmall,
        color = MaterialTheme.colorScheme.onPrimaryContainer,
    )
    StatusRow(
        "Glasses",
        when {
          state.glassesReady -> state.deviceNames.joinToString()
          !state.sdkReady -> "Bluetooth permission needed"
          else -> "Not connected"
        },
        state.glassesReady,
    )
    StatusRow(
        "Laptop",
        when {
          state.laptopIp.isBlank() -> "No address yet"
          state.laptopReachable == true -> "Connected"
          state.laptopReachable == false -> "Can't reach ${state.laptopIp}"
          else -> "Checking..."
        },
        state.laptopReachable,
    )
    if (!state.glassesReady && state.sdkReady) {
      OutlinedButton(onClick = onConnectGlasses) { Text("Connect glasses") }
    }
    if (state.laptopIp.isBlank()) {
      OutlinedButton(onClick = onOpenSettings) { Text("Set the laptop address") }
    } else if (state.laptopReachable == false) {
      OutlinedButton(onClick = onCheckLaptop) { Text("Try the laptop again") }
    }
  }

  Button(
      onClick = onCheckNow,
      enabled = state.glassesReady && !state.busy,
      modifier = Modifier.fillMaxWidth().height(64.dp),
  ) {
    Text(
        if (state.photoBusy) "Looking..." else "Look now",
        style = MaterialTheme.typography.titleMedium,
    )
  }
  if (state.photoBusy) {
    Working(state.photoLog.lastOrNull())
  } else if (watching == 0 && state.laptopReachable == true) {
    TextButton(onClick = onOpenQuestions) { Text("No questions yet. Plant one.") }
  }

  SectionCard {
    Row(verticalAlignment = Alignment.CenterVertically) {
      Column(modifier = Modifier.weight(1f)) {
        Text("Watch hands-free", style = MaterialTheme.typography.titleMedium)
        Text(
            if (state.watching) {
              "Since ${state.watchStartedAt}: ${state.watchSent} sent, " +
                  "${state.watchSkipped} skipped as the same view."
            } else {
              "A photo every ${state.watchEverySeconds} s while you wear the glasses. " +
                  "Keep this screen open."
            },
            style = MaterialTheme.typography.bodySmall,
        )
      }
      Switch(
          checked = state.watching,
          onCheckedChange = onWatch,
          enabled = state.watching || (state.glassesReady && state.laptopReachable == true),
      )
    }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      listOf(15, 30, 60).forEach { seconds ->
        if (state.watchEverySeconds == seconds) {
          Button(onClick = { onWatchEvery(seconds) }) { Text("${seconds} s") }
        } else {
          OutlinedButton(onClick = { onWatchEvery(seconds) }) { Text("${seconds} s") }
        }
      }
    }
  }

  if (state.lastSpoken.isNotEmpty()) {
    SectionCard(title = "Mannie said" + (state.lastSpokenAt?.let { " at $it" } ?: "")) {
      state.lastSpoken.forEach { line ->
        Text("“$line”", style = MaterialTheme.typography.bodyLarge)
      }
      state.lastSpokenPhoto?.let { Photo(it, "What Mannie saw") }
    }
  } else if (!state.photoBusy && state.serverBytes != null) {
    SectionCard {
      Text(
          "Nothing to say about the last look. Mannie only speaks when a question's answer is yes.",
          style = MaterialTheme.typography.bodyMedium,
      )
    }
  }
}

// MARK: - Questions

@Composable
private fun QuestionsScreen(
    state: TrussUiState,
    viewModel: TrussViewModel,
    onRecordQuestion: () -> Unit,
    onCaptureRef: (String) -> Unit,
) {
  LaunchedEffect(Unit) { viewModel.refreshQuestions() }
  val planting = state.plantSlug?.let { slug -> state.questionsList.firstOrNull { it.slug == slug } }

  if (state.plantSlug == null) {
    SectionCard(title = "Ask Mannie to watch for something") {
      OutlinedTextField(
          value = state.plantText,
          onValueChange = viewModel::setPlantText,
          label = { Text("Your question") },
          placeholder = { Text("Is the stove off?") },
          singleLine = true,
          modifier = Modifier.fillMaxWidth(),
      )
      Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Button(
            onClick = viewModel::plantQuestionFromText,
            enabled = !state.plantBusy && state.plantText.isNotBlank(),
        ) {
          Text("Next")
        }
        OutlinedButton(onClick = onRecordQuestion, enabled = !state.busy) {
          Text(if (state.plantBusy) "Listening..." else "Say it")
        }
      }
      Text(
          "Next, you'll show Mannie what yes looks like and what no looks like, " +
              "through the glasses.",
          style = MaterialTheme.typography.bodySmall,
      )
    }
    OutlinedButton(
        onClick = viewModel::plantBedtimeRound,
        enabled = !state.plantBusy,
        modifier = Modifier.fillMaxWidth(),
    ) {
      Text("Plant the bedtime round (stove, door, pills)")
    }
  } else {
    val needsYes = planting?.hasRefYes != true
    SectionCard(
        title = planting?.question ?: "New question",
        container = MaterialTheme.colorScheme.secondaryContainer,
    ) {
      Text(
          if (needsYes) "Step 2 of 3: look at what YES looks like, then tap below."
          else "Step 3 of 3: now look at what NO looks like, then tap below.",
          style = MaterialTheme.typography.bodyLarge,
      )
      if (state.photoBusy) {
        Working(state.photoLog.lastOrNull())
      }
      Button(
          onClick = { onCaptureRef(if (needsYes) "yes" else "no") },
          enabled = !state.busy,
          modifier = Modifier.fillMaxWidth(),
      ) {
        Text(if (needsYes) "Take the yes look" else "Take the no look")
      }
      if (!needsYes) {
        TextButton(onClick = { onCaptureRef("yes") }, enabled = !state.busy) {
          Text("Retake the yes look")
        }
      }
      TextButton(onClick = viewModel::cancelPlanting, enabled = !state.busy) { Text("Cancel") }
    }
  }

  val shown = state.questionsList.filter { it.state != "retired" }
  Text("Questions", style = MaterialTheme.typography.titleMedium)
  if (shown.isEmpty()) {
    Text(
        "None yet. Questions you plant show up here.",
        style = MaterialTheme.typography.bodyMedium,
    )
  }
  shown.forEach { item ->
    SectionCard {
      Text(item.question, style = MaterialTheme.typography.titleMedium)
      Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
            questionStatus(item) + if (item.kind == "done") " · done question" else "",
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.weight(1f),
        )
        TextButton(onClick = { viewModel.retireQuestion(item.slug) }, enabled = !state.busy) {
          Text("Remove")
        }
      }
    }
  }
  TextButton(onClick = viewModel::refreshQuestions) { Text("Refresh") }
}

// MARK: - History

@Composable
private fun HistoryScreen(state: TrussUiState, onRefresh: () -> Unit) {
  LaunchedEffect(Unit) { onRefresh() }
  Row(verticalAlignment = Alignment.CenterVertically) {
    Text(
        state.historyDay?.let { "Answers on $it" } ?: "Today's answers",
        style = MaterialTheme.typography.titleMedium,
        modifier = Modifier.weight(1f),
    )
    TextButton(onClick = onRefresh, enabled = !state.historyBusy) { Text("Refresh") }
  }
  if (state.historyBusy && state.history.isEmpty()) {
    Working("Loading")
  }
  if (!state.historyBusy && state.history.isEmpty()) {
    Text(
        "Nothing yet today. When Mannie answers a question, the answer and the photo it " +
            "was seen in are kept on your laptop and shown here.",
        style = MaterialTheme.typography.bodyMedium,
    )
  }
  state.history.forEach { item ->
    val label =
        when (item.kind) {
          "answer" -> "Yes"
          "redo" -> "Reminder"
          "state_check" -> "Checked after a missed window"
          else -> ""
        }
    SectionCard {
      Text(
          "${item.time.take(5)}  ·  ${item.question}",
          style = MaterialTheme.typography.titleSmall,
      )
      if (label.isNotEmpty()) {
        Text(label, fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.primary)
      }
      if (item.text.isNotBlank()) {
        Text(item.text, style = MaterialTheme.typography.bodyMedium)
      }
      val name = item.outline ?: item.evidence
      val photo = name?.let { state.historyPhotos["${state.historyDay}/$it"] }
      if (photo != null) {
        Photo(photo, "Evidence photo for ${item.question}")
      }
    }
  }
}

// MARK: - Settings and diagnostics

@Composable
private fun SettingsScreen(
    state: TrussUiState,
    viewModel: TrussViewModel,
    onConnectGlasses: () -> Unit,
    onCapture: () -> Unit,
    onRecord: () -> Unit,
) {
  var showDiagnostics by remember { mutableStateOf(false) }

  SectionCard(title = "Laptop") {
    OutlinedTextField(
        value = state.laptopIp,
        onValueChange = viewModel::setLaptopIp,
        label = { Text("Laptop address") },
        placeholder = { Text("192.168.1.20") },
        singleLine = true,
        modifier = Modifier.fillMaxWidth(),
    )
    Text(
        "The address the laptop prints when the Truss server starts. Kept on this phone.",
        style = MaterialTheme.typography.bodySmall,
    )
    StatusRow(
        "Status",
        when (state.laptopReachable) {
          true -> "Connected"
          false -> "Can't reach it"
          null -> "Not checked yet"
        },
        state.laptopReachable,
    )
    OutlinedButton(onClick = viewModel::checkLaptopNow) { Text("Check connection") }
  }

  SectionCard(title = "Glasses") {
    StatusRow("Glasses", if (state.glassesReady) state.deviceNames.joinToString() else "Not connected", state.glassesReady)
    if (!state.sdkReady) {
      Text(stringResource(R.string.why_bluetooth), style = MaterialTheme.typography.bodySmall)
    }
    OutlinedButton(onClick = onConnectGlasses, enabled = state.sdkReady) {
      Text(if (state.glassesReady) "Reconnect glasses" else "Connect glasses")
    }
  }

  TextButton(onClick = { showDiagnostics = !showDiagnostics }) {
    Text(if (showDiagnostics) "Hide diagnostics" else "Show diagnostics")
  }
  if (showDiagnostics) {
    Diagnostics(state, viewModel, onCapture, onRecord)
  }
}

@Composable
private fun Diagnostics(
    state: TrussUiState,
    viewModel: TrussViewModel,
    onCapture: () -> Unit,
    onRecord: () -> Unit,
) {
  SectionCard(title = "Connection") {
    Text("Toolkit version: $TOOLKIT_VERSION")
    Text("Phone SDK level: ${Build.VERSION.SDK_INT} (Android ${Build.VERSION.RELEASE})")
    Text("Registration: ${state.registrationState}")
    Text("Glasses link: ${state.linkState}")
    Button(onClick = viewModel::pingLaptop, enabled = !state.pingBusy) {
      Text(if (state.pingBusy) "Pinging..." else "Ping laptop")
    }
    state.pingResult?.let { Text("Ping laptop: $it") }
  }

  SectionCard(title = "Last capture") {
    Button(onClick = onCapture, enabled = !state.busy) {
      Text(if (state.photoBusy) "Working..." else "Capture and upload")
    }
    state.serverBytes?.let { Text("Laptop received: $it bytes") }
    state.photoElapsedMs?.let { Text("Total elapsed: $it ms") }
    state.wifiReturn?.let { Text("Return to home Wi-Fi: $it") }
    state.spokenLines.forEach { line -> Text("Mannie says: $line") }
    HorizontalDivider()
    LogLines(state.photoLog)
  }

  SectionCard(title = "Voice through the glasses") {
    Text(stringResource(R.string.why_record_audio), style = MaterialTheme.typography.bodySmall)
    Text(
        "Any glasses camera session is ended before Bluetooth audio opens: opening Bluetooth " +
            "SCO can end a toolkit session (a known upstream issue), so the two are never held " +
            "open together.",
        style = MaterialTheme.typography.bodySmall,
    )
    Text("Available communication devices:", style = MaterialTheme.typography.bodySmall)
    viewModel.getAvailableCommunicationDevices().forEach { device ->
      Text("  • $device", style = MaterialTheme.typography.bodySmall, fontFamily = FontFamily.Monospace)
    }
    Button(onClick = onRecord, enabled = !state.recordBusy && !state.photoBusy) {
      Text(if (state.recordBusy) "Recording..." else "Record 5 s via glasses")
    }
    state.inputDevice?.let { device ->
      if (device.isBluetooth) {
        Text("Recorded from: ${device.describe()}")
      } else {
        Text("FAIL: recorded from ${device.describe()}, not the glasses.", color = Color.Red)
      }
    }
    state.transcript?.let { Text("Transcript: $it") }
    state.sttMs?.let { Text("Speech end to transcript: $it ms") }
    Button(onClick = viewModel::speakTestSentence, enabled = !state.speakBusy && !state.photoBusy) {
      Text(if (state.speakBusy) "Playing..." else "Speak test sentence via glasses")
    }
    Text("Test sentence: \"${TrussViewModel.TEST_SENTENCE}\"")
    state.outputDevice?.let { device ->
      if (device.isBluetooth) {
        Text("Played through: ${device.describe()}")
      } else {
        Text("FAIL: played through ${device.describe()}, not the glasses.", color = Color.Red)
      }
    }
    state.ttsMs?.let { Text("TTS request to audio start: $it ms") }
  }

  if (state.plantLog.isNotEmpty()) {
    SectionCard(title = "Planting log") { LogLines(state.plantLog) }
  }
  Text(
      stringResource(R.string.why_location_wifi),
      style = MaterialTheme.typography.bodySmall,
      textAlign = TextAlign.Start,
  )
}

@Composable
private fun LogLines(lines: List<String>) {
  lines.forEach { line ->
    Text(line, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
  }
}
