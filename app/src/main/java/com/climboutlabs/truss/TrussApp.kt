package com.climboutlabs.truss

import android.os.Build
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.meta.wearable.dat.core.types.Permission
import com.meta.wearable.dat.core.types.PermissionStatus

/** Toolkit version pinned in gradle/libs.versions.toml, shown on the Connect screen. */
private const val TOOLKIT_VERSION = "1.0.1"

@OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)
@Composable
fun TrussApp(
    viewModel: TrussViewModel,
    onConnectGlasses: () -> Unit,
    onRequestWearablesPermission: suspend (Permission) -> PermissionStatus,
    onRequestRecordAudioPermission: suspend () -> Boolean,
) {
  val state by viewModel.uiState.collectAsState()
  var tab by remember { mutableIntStateOf(0) }
  MaterialTheme {
    Scaffold(
        topBar = { TopAppBar(title = { Text("Truss") }) },
    ) { padding ->
      Column(modifier = Modifier.fillMaxSize().padding(padding)) {
        TabRow(selectedTabIndex = tab) {
          Tab(selected = tab == 0, onClick = { tab = 0 }, text = { Text("Connect") })
          Tab(selected = tab == 1, onClick = { tab = 1 }, text = { Text("Photo") })
          Tab(selected = tab == 2, onClick = { tab = 2 }, text = { Text("Voice") })
        }
        Column(
            modifier =
                Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
          when (tab) {
            0 ->
                ConnectScreen(
                    state = state,
                    onIpChange = viewModel::setLaptopIp,
                    onConnectGlasses = onConnectGlasses,
                    onPing = viewModel::pingLaptop,
                )
            1 ->
                PhotoScreen(
                    state = state,
                    onCapture = { viewModel.captureAndUpload(onRequestWearablesPermission) },
                )
            else ->
                VoiceScreen(
                    state = state,
                    viewModel = viewModel,
                    onRecord = { viewModel.recordAndTranscribe(onRequestRecordAudioPermission) },
                    onSpeak = viewModel::speakTestSentence,
                )
          }
          state.lastError?.let { error ->
            Text("Last error: $error", color = Color.Red)
          }
        }
      }
    }
  }
}

@Composable
private fun ConnectScreen(
    state: TrussUiState,
    onIpChange: (String) -> Unit,
    onConnectGlasses: () -> Unit,
    onPing: () -> Unit,
) {
  Text("Screen 1: Connect", style = MaterialTheme.typography.titleMedium)
  OutlinedTextField(
      value = state.laptopIp,
      onValueChange = onIpChange,
      label = { Text("Laptop address (LAPTOP_IP)") },
      placeholder = { Text("192.168.1.20") },
      singleLine = true,
      modifier = Modifier.fillMaxWidth(),
  )
  Text("The address is saved on this phone and used for every laptop call, on port 8792.")
  Button(onClick = onConnectGlasses, enabled = state.sdkReady) {
    Text("Connect glasses")
  }
  if (!state.sdkReady) {
    Text(stringResource(R.string.why_bluetooth))
  }
  Text("Toolkit version: $TOOLKIT_VERSION")
  Text("Phone SDK level: ${Build.VERSION.SDK_INT} (Android ${Build.VERSION.RELEASE})")
  Text("Registration: ${state.registrationState}")
  Text(
      "Glasses: " +
          if (state.deviceNames.isEmpty()) "none seen" else state.deviceNames.joinToString(),
  )
  Text("Glasses connection state: ${state.linkState}")
  Button(onClick = onPing, enabled = !state.pingBusy) {
    Text(if (state.pingBusy) "Pinging..." else "Ping laptop")
  }
  state.pingResult?.let { Text("Ping laptop: $it") }
}

@Composable
private fun PhotoScreen(state: TrussUiState, onCapture: () -> Unit) {
  Text("Screen 2: Photo (Gate 2)", style = MaterialTheme.typography.titleMedium)
  Text(stringResource(R.string.why_camera_permission))
  Text(stringResource(R.string.why_location_wifi))
  Button(onClick = onCapture, enabled = !state.photoBusy) {
    Text(if (state.photoBusy) "Working..." else "Capture and upload")
  }
  state.photo?.let { bitmap ->
    Image(
        bitmap = bitmap.asImageBitmap(),
        contentDescription = "Photo from the glasses",
        modifier = Modifier.fillMaxWidth(),
    )
  }
  state.serverBytes?.let { Text("Laptop received: $it bytes") }
  state.photoElapsedMs?.let { Text("Total elapsed: $it ms") }
  state.wifiReturn?.let { Text("Return to home Wi-Fi: $it") }
  state.photoLog.forEach { line ->
    Text(line, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
  }
}

@Composable
private fun VoiceScreen(
    state: TrussUiState,
    viewModel: TrussViewModel,
    onRecord: () -> Unit,
    onSpeak: () -> Unit
) {
  Text("Screen 3: Voice (Gate 3)", style = MaterialTheme.typography.titleMedium)
  Text(stringResource(R.string.why_record_audio))
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
  Button(onClick = onRecord, enabled = !state.recordBusy) {
    Text(if (state.recordBusy) "Recording..." else "Record 5 s via glasses")
  }
  state.inputDevice?.let { device ->
    if (device.isBluetooth) {
      Text("Recorded from: ${device.describe()}")
    } else {
      Text(
          "FAIL: recorded from ${device.describe()}, not the glasses.",
          color = Color.Red,
      )
    }
  }
  state.transcript?.let { Text("Transcript: $it") }
  state.sttMs?.let { Text("Speech end to transcript: $it ms") }
  Button(onClick = onSpeak, enabled = !state.speakBusy) {
    Text(if (state.speakBusy) "Playing..." else "Speak test sentence via glasses")
  }
  Text("Test sentence: \"${TrussViewModel.TEST_SENTENCE}\"")
  state.outputDevice?.let { device ->
    if (device.isBluetooth) {
      Text("Played through: ${device.describe()}")
    } else {
      Text(
          "FAIL: played through ${device.describe()}, not the glasses.",
          color = Color.Red,
      )
    }
  }
  state.ttsMs?.let { Text("TTS request to audio start: $it ms") }
}
