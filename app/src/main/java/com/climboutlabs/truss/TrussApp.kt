package com.climboutlabs.truss

import android.graphics.Bitmap
import android.os.Build
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.compose.ui.unit.sp
import com.meta.wearable.dat.core.types.Permission
import com.meta.wearable.dat.core.types.PermissionStatus
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.format.DateTimeFormatter
import java.util.Locale

/** Toolkit version pinned in gradle/libs.versions.toml, shown under Diagnostics. */
private const val TOOLKIT_VERSION = "1.0.1"

private enum class Destination(val label: String, val icon: ImageVector) {
  HOME("Home", TrussIcons.Home),
  QUESTIONS("Questions", TrussIcons.Questions),
  HISTORY("History", TrussIcons.History),
  SETTINGS("Settings", TrussIcons.Settings),
}

@Composable
fun TrussApp(
    viewModel: TrussViewModel,
    onConnectGlasses: () -> Unit,
    onRequestWearablesPermission: suspend (Permission) -> PermissionStatus,
) {
  val state by viewModel.uiState.collectAsState()
  var tab by rememberSaveable { mutableIntStateOf(0) }
  val destination = Destination.entries[tab]

  // The phone stays awake while watching: the photo loop runs in the app.
  val view = LocalView.current
  DisposableEffect(state.watching) {
    view.keepScreenOn = state.watching
    onDispose { view.keepScreenOn = false }
  }

  // The Desk asked for watching: start it here, where the camera permission can be
  // asked for, as soon as the glasses and the laptop are both connected.
  val deskCanStart =
      state.deskStartPending && !state.watching && state.glassesReady && state.laptopReachable == true
  LaunchedEffect(deskCanStart) {
    if (deskCanStart) viewModel.startWatching(onRequestWearablesPermission)
  }

  TrussTheme {
    Scaffold(
        containerColor = Truss.ground,
        bottomBar = {
          NavigationBar(containerColor = Truss.nav) {
            Destination.entries.forEachIndexed { index, item ->
              NavigationBarItem(
                  selected = tab == index,
                  onClick = { tab = index },
                  icon = { Icon(item.icon, contentDescription = null) },
                  label = { Text(item.label, style = MaterialTheme.typography.labelMedium) },
                  colors =
                      NavigationBarItemDefaults.colors(
                          selectedIconColor = Truss.onLime,
                          selectedTextColor = Truss.ink,
                          indicatorColor = Truss.lime,
                          unselectedIconColor = Truss.muted,
                          unselectedTextColor = Truss.muted,
                      ),
              )
            }
          }
        },
    ) { padding ->
      Box(Modifier.fillMaxSize().padding(padding)) {
        when (destination) {
          Destination.HOME ->
              HomeScreen(
                  state = state,
                  onDismissError = viewModel::clearError,
                  onConnectGlasses = onConnectGlasses,
                  onCheckLaptop = viewModel::checkLaptopNow,
                  onLookNow = { viewModel.captureAndUpload(onRequestWearablesPermission) },
                  onWatch = { on ->
                    if (on) viewModel.startWatching(onRequestWearablesPermission)
                    else viewModel.stopWatching()
                  },
                  onWatchEvery = viewModel::setWatchEverySeconds,
                  onOpenMetaAi = viewModel::openMetaAi,
                  onOpenSettings = { tab = Destination.SETTINGS.ordinal },
                  onOpenQuestions = { tab = Destination.QUESTIONS.ordinal },
              )
          Destination.QUESTIONS ->
              QuestionsScreen(
                  state = state,
                  viewModel = viewModel,
                  onCaptureRef = { kind ->
                    viewModel.captureReference(kind, onRequestWearablesPermission)
                  },
              )
          Destination.HISTORY ->
              HistoryScreen(
                  state = state,
                  onDismissError = viewModel::clearError,
                  onRefresh = viewModel::loadHistory,
              )
          Destination.SETTINGS ->
              SettingsScreen(
                  state = state,
                  viewModel = viewModel,
                  onConnectGlasses = onConnectGlasses,
                  onCapture = { viewModel.captureAndUpload(onRequestWearablesPermission) },
              )
        }
      }
    }
  }
}

// MARK: - Shared pieces

private val Pill = RoundedCornerShape(percent = 50)

/** A scrolling screen body with the standard gutters and spacing. */
@Composable
private fun ScreenBody(
    modifier: Modifier = Modifier,
    content: @Composable ColumnScope.() -> Unit,
) {
  Column(
      modifier =
          modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(16.dp, 20.dp, 16.dp, 16.dp),
      verticalArrangement = Arrangement.spacedBy(14.dp),
      content = content,
  )
}

@Composable
private fun ScreenTitle(text: String, subtitle: String? = null) {
  Column(
      modifier = Modifier.padding(horizontal = 4.dp),
      verticalArrangement = Arrangement.spacedBy(2.dp),
  ) {
    Text(
        text,
        style = MaterialTheme.typography.displaySmall,
        modifier = Modifier.semantics { heading() },
    )
    subtitle?.let { Text(it, style = MaterialTheme.typography.bodyMedium, color = Truss.muted) }
  }
}

@Composable
private fun SectionLabel(text: String) {
  Text(
      text.uppercase(Locale.getDefault()),
      style = MaterialTheme.typography.labelSmall,
      color = Truss.muted,
      letterSpacing = 0.8.sp,
      modifier = Modifier.padding(start = 6.dp, top = 6.dp).semantics { heading() },
  )
}

/** A white rounded surface: the base container for lists and settings. */
@Composable
private fun Surface(
    modifier: Modifier = Modifier,
    color: Color = Truss.card,
    shape: Shape = RoundedCornerShape(24.dp),
    padding: Dp = 16.dp,
    content: @Composable ColumnScope.() -> Unit,
) {
  // White cards get a hairline edge so they sit on the paper ground without a shadow.
  val edge = if (color == Truss.card) Modifier.border(1.dp, Truss.line, shape) else Modifier
  Column(
      modifier = modifier.fillMaxWidth().clip(shape).background(color).then(edge).padding(padding),
      verticalArrangement = Arrangement.spacedBy(10.dp),
      content = content,
  )
}

@Composable
private fun ErrorBanner(message: String, onDismiss: () -> Unit) {
  Row(
      modifier =
          Modifier.fillMaxWidth()
              .clip(RoundedCornerShape(20.dp))
              .background(Truss.errorSoft)
              .padding(start = 16.dp, top = 6.dp, bottom = 6.dp, end = 4.dp),
      verticalAlignment = Alignment.CenterVertically,
  ) {
    Text(
        message,
        style = MaterialTheme.typography.bodySmall,
        color = Truss.onError,
        modifier = Modifier.weight(1f).padding(vertical = 6.dp),
    )
    IconButton(onClick = onDismiss, modifier = Modifier.size(40.dp)) {
      Icon(TrussIcons.Close, contentDescription = "Dismiss", tint = Truss.onError, modifier = Modifier.size(18.dp))
    }
  }
}

/** A photo filling the screen, whole and uncropped. A tap anywhere closes it. */
@Composable
private fun PhotoViewer(bitmap: Bitmap, caption: String, onClose: () -> Unit) {
  Dialog(onDismissRequest = onClose, properties = DialogProperties(usePlatformDefaultWidth = false)) {
    Box(
        modifier = Modifier.fillMaxSize().background(Color.Black).clickable(onClick = onClose),
        contentAlignment = Alignment.Center,
    ) {
      Image(
          bitmap = bitmap.asImageBitmap(),
          contentDescription = caption,
          contentScale = ContentScale.Fit,
          modifier = Modifier.fillMaxSize(),
      )
      Text(
          caption,
          style = MaterialTheme.typography.bodySmall,
          color = Color.White,
          modifier =
              Modifier.align(Alignment.BottomCenter)
                  .fillMaxWidth()
                  .background(Color.Black.copy(alpha = 0.55f))
                  .padding(16.dp, 12.dp),
      )
    }
  }
}

@Composable
private fun IconTile(
    icon: ImageVector,
    background: Color,
    tint: Color,
    size: Dp = 44.dp,
    shape: Shape = RoundedCornerShape(14.dp),
) {
  Box(
      modifier = Modifier.size(size).clip(shape).background(background),
      contentAlignment = Alignment.Center,
  ) {
    Icon(icon, contentDescription = null, tint = tint, modifier = Modifier.size(size / 2))
  }
}

@Composable
private fun Chip(text: String, background: Color, color: Color) {
  Text(
      text,
      style = MaterialTheme.typography.labelMedium,
      color = color,
      modifier = Modifier.clip(Pill).background(background).padding(horizontal = 10.dp, vertical = 4.dp),
  )
}

/** A tall pill button with an icon, the app's main call to action. */
@Composable
private fun BigButton(
    text: String,
    icon: ImageVector?,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    container: Color = Truss.ink,
    content: Color = Color.White,
    height: Dp = 56.dp,
    busy: Boolean = false,
) {
  Button(
      onClick = onClick,
      enabled = enabled,
      shape = Pill,
      colors =
          ButtonDefaults.buttonColors(
              containerColor = container,
              contentColor = content,
              disabledContainerColor = container.copy(alpha = 0.35f),
              disabledContentColor = content.copy(alpha = 0.7f),
          ),
      modifier = modifier.fillMaxWidth().height(height),
  ) {
    if (busy) {
      CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp, color = content)
    } else if (icon != null) {
      Icon(icon, contentDescription = null, modifier = Modifier.size(22.dp))
    }
    Spacer(Modifier.width(10.dp))
    Text(text, style = MaterialTheme.typography.labelLarge)
  }
}

@Composable
private fun OutlineBigButton(
    text: String,
    icon: ImageVector?,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    color: Color = Truss.forest,
) {
  OutlinedButton(
      onClick = onClick,
      enabled = enabled,
      shape = Pill,
      border = androidx.compose.foundation.BorderStroke(1.dp, Truss.line),
      colors =
          ButtonDefaults.outlinedButtonColors(
              containerColor = Truss.card,
              contentColor = color,
              disabledContentColor = color.copy(alpha = 0.4f),
          ),
      modifier = modifier.height(52.dp),
  ) {
    icon?.let { Icon(it, contentDescription = null, modifier = Modifier.size(22.dp)) }
    Spacer(Modifier.width(10.dp))
    Text(text, style = MaterialTheme.typography.labelLarge)
  }
}

/** Three choices in one rounded track; the chosen one is filled. */
@Composable
private fun Segmented(
    options: List<Int>,
    selected: Int,
    label: (Int) -> String,
    onSelect: (Int) -> Unit,
    onDark: Boolean,
) {
  val track = if (onDark) Color.Black.copy(alpha = 0.22f) else Truss.sage
  val chosenBg = if (onDark) Color.White else Truss.forest
  val chosenFg = if (onDark) Truss.forest else Color.White
  val otherFg = if (onDark) Color.White.copy(alpha = 0.88f) else Truss.ink
  Row(
      modifier =
          Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(track).padding(4.dp).selectableGroup(),
      horizontalArrangement = Arrangement.spacedBy(4.dp),
  ) {
    options.forEach { option ->
      val chosen = option == selected
      Box(
          modifier =
              Modifier.weight(1f)
                  .height(44.dp)
                  .clip(RoundedCornerShape(14.dp))
                  .background(if (chosen) chosenBg else Color.Transparent)
                  .selectable(selected = chosen, role = Role.RadioButton, onClick = { onSelect(option) }),
          contentAlignment = Alignment.Center,
      ) {
        Text(label(option), style = MaterialTheme.typography.labelMedium, color = if (chosen) chosenFg else otherFg)
      }
    }
  }
}

@Composable
private fun Photo(bitmap: Bitmap, description: String, modifier: Modifier) {
  Image(
      bitmap = bitmap.asImageBitmap(),
      contentDescription = description,
      contentScale = ContentScale.Crop,
      modifier = modifier,
  )
}

/** "21:40" or "21:40:12" as "9:40 PM"; anything else is shown as it came. */
private fun clock12(value: String?): String? {
  if (value.isNullOrBlank()) return value
  return try {
    LocalTime.parse(value.take(5)).format(DateTimeFormatter.ofPattern("h:mm a", Locale.US))
  } catch (e: Exception) {
    value
  }
}

/** An ISO answered_at stamp as "at 8:28 PM" today, otherwise "Thu Oct 29, 8:28 PM". */
private fun answeredWhen(iso: String?): String? {
  if (iso.isNullOrBlank()) return null
  return try {
    val at = LocalDateTime.parse(iso.take(16))
    val time = at.format(DateTimeFormatter.ofPattern("h:mm a", Locale.US))
    if (at.toLocalDate() == LocalDate.now()) "at $time"
    else at.format(DateTimeFormatter.ofPattern("EEE MMM d", Locale.US)) + ", $time"
  } catch (e: Exception) {
    null
  }
}

private fun workingLine(line: String?): String =
    line?.substringAfter("  ")?.ifBlank { null } ?: "Working..."

/** The colours and words for one question's state. */
private data class QuestionLook(
    val status: String,
    val background: Color,
    val onBackground: Color,
    val soft: Color,
)

private fun lookFor(item: QuestionInfo): QuestionLook =
    when (item.state) {
      // A past yes is history, not a current all-clear: say when it was, on a neutral tile.
      "answered" ->
          QuestionLook(
              answeredWhen(item.answeredAt)?.let { "Last yes $it" } ?: "Answered yes",
              Truss.sage,
              Truss.ink,
              Truss.muted,
          )
      "planting" ->
          QuestionLook(
              if (!item.hasRefYes) "Needs its yes look" else "Needs its no look",
              Truss.apricot,
              Truss.onApricot,
              Truss.onApricotSoft,
          )
      "standing" -> QuestionLook("Watching", Truss.sky, Truss.onSky, Truss.onSkySoft)
      else -> QuestionLook(item.state, Truss.sage, Truss.ink, Truss.muted)
    }

/** An icon that matches what the question is about. */
private fun topicIcon(question: String): ImageVector {
  val q = question.lowercase(Locale.getDefault())
  return when {
    listOf("stove", "oven", "burner", "hob", "range", "gas").any { it in q } -> TrussIcons.Flame
    listOf("door", "lock", "gate", "window").any { it in q } -> TrussIcons.Lock
    listOf("pill", "medic", "tablet", "dose", "vitamin").any { it in q } -> TrussIcons.Pill
    else -> TrussIcons.Eye
  }
}

// MARK: - Home

@Composable
private fun HomeScreen(
    state: TrussUiState,
    onDismissError: () -> Unit,
    onConnectGlasses: () -> Unit,
    onCheckLaptop: () -> Unit,
    onLookNow: () -> Unit,
    onWatch: (Boolean) -> Unit,
    onWatchEvery: (Int) -> Unit,
    onOpenSettings: () -> Unit,
    onOpenQuestions: () -> Unit,
    onOpenMetaAi: () -> Unit,
) {
  Column(Modifier.fillMaxSize()) {
    ScreenBody(Modifier.weight(1f)) {
      state.lastError?.let { ErrorBanner(it, onDismissError) }
      Greeting()
      HeroCard(state, onConnectGlasses, onCheckLaptop, onWatch, onWatchEvery, onOpenSettings, onOpenMetaAi)
      LastAnswer(state)
      QuestionTiles(state, onOpenQuestions)
    }
    BigButton(
        text = if (state.photoBusy) "Looking..." else "Look now",
        icon = TrussIcons.Camera,
        onClick = onLookNow,
        enabled = state.glassesReady && !state.busy,
        busy = state.photoBusy,
        modifier = Modifier.padding(16.dp, 4.dp, 16.dp, 16.dp),
    )
  }
}

/** The date and a time-of-day greeting at the top of Home. */
@Composable
private fun Greeting() {
  val now = java.time.LocalDateTime.now()
  val hello =
      when (now.hour) {
        in 5..11 -> "Good morning"
        in 12..17 -> "Good afternoon"
        else -> "Good evening"
      }
  Column(modifier = Modifier.padding(horizontal = 4.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
    Text(
        now.format(DateTimeFormatter.ofPattern("EEEE, MMMM d", Locale.US)),
        style = MaterialTheme.typography.bodySmall,
        color = Truss.muted,
    )
    Text(hello, style = MaterialTheme.typography.displaySmall, modifier = Modifier.semantics { heading() })
  }
}

@Composable
private fun HeroCard(
    state: TrussUiState,
    onConnectGlasses: () -> Unit,
    onCheckLaptop: () -> Unit,
    onWatch: (Boolean) -> Unit,
    onWatchEvery: (Int) -> Unit,
    onOpenSettings: () -> Unit,
    onOpenMetaAi: () -> Unit,
) {
  val ready = state.glassesReady && state.laptopReachable == true
  val minding = state.questionsList.count { it.state == "standing" }
  val glassesLine =
      when {
        state.glassesReady -> "Glasses connected"
        !state.sdkReady -> "Glasses need the Bluetooth permission"
        else -> "Glasses not connected"
      }
  val laptopLine =
      when {
        state.laptopIp.isBlank() -> "No laptop address yet"
        state.laptopReachable == true -> "laptop connected"
        state.laptopReachable == false -> "can't reach the laptop"
        else -> "checking the laptop"
      }
  val title: String
  val subtitle: String
  when {
    state.watchWaiting -> {
      title = "Waiting for the glasses"
      val since = clock12(state.watchQuietSince)
      subtitle =
          (if (since != null) "They went quiet at $since. " else "They went quiet. ") +
              "If they are on, open the Meta AI " +
              "app for a moment to relink them. Watching carries on by itself when they are back."
    }
    state.watching -> {
      title = "Watching"
      subtitle =
          "Every ${state.watchEverySeconds} s since ${clock12(state.watchStartedAt)}. " +
              "${state.watchSent} looks, ${state.watchSkipped} skipped as unchanged."
    }
    state.photoBusy -> {
      title = "Looking"
      subtitle = workingLine(state.photoLog.lastOrNull())
    }
    !ready && state.deskStartPending -> {
      title = "Getting set up"
      subtitle = "$glassesLine, $laptopLine. The Desk asked to start watching: it starts by itself once both are connected."
    }
    !ready -> {
      title = "Getting set up"
      subtitle = "$glassesLine, $laptopLine."
    }
    minding == 0 -> {
      title = "Ready"
      subtitle = "Glasses and laptop are on. Add a question for Mannie to mind."
    }
    else -> {
      title = "Ready"
      subtitle =
          "Mannie is minding $minding question" + (if (minding == 1) "" else "s") +
              ". Start watching and keep this screen open."
    }
  }

  Surface(shape = RoundedCornerShape(28.dp), padding = 18.dp) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
      Box(
          modifier = Modifier.size(72.dp).clip(cookieShape()).background(Truss.sage),
          contentAlignment = Alignment.Center,
      ) {
        Icon(
            if (state.photoBusy) TrussIcons.Camera else TrussIcons.Eye,
            contentDescription = null,
            tint = Truss.ink,
            modifier = Modifier.size(30.dp),
        )
      }
      Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(
            title,
            style = MaterialTheme.typography.headlineMedium,
            color = Truss.ink,
            modifier = Modifier.semantics { heading() },
        )
        Text(subtitle, style = MaterialTheme.typography.bodyMedium, color = Truss.onForestSoft)
      }
    }

    if (!ready && !state.watching) {
      if (!state.glassesReady && state.sdkReady) {
        BigButton("Connect glasses", TrussIcons.Glasses, onConnectGlasses)
      }
      if (state.laptopIp.isBlank()) {
        BigButton("Set the laptop address", TrussIcons.Laptop, onOpenSettings)
      } else if (state.laptopReachable == false) {
        BigButton("Try the laptop again", TrussIcons.Refresh, onCheckLaptop)
      }
    } else {
      Segmented(
          options = listOf(15, 30, 60),
          selected = state.watchEverySeconds,
          label = { "Every $it s" },
          onSelect = onWatchEvery,
          onDark = false,
      )
      if (state.watchWaiting) {
        BigButton("Open Meta AI to reconnect", TrussIcons.Glasses, onOpenMetaAi, height = 52.dp)
      }
      if (state.watching) {
        OutlineBigButton(
            "Stop watching",
            TrussIcons.Stop,
            { onWatch(false) },
            color = Truss.ink,
            modifier = Modifier.fillMaxWidth(),
        )
      } else {
        BigButton(
            "Start watching",
            TrussIcons.Play,
            { onWatch(true) },
            enabled = ready,
            height = 52.dp,
        )
      }
    }
  }
}

@Composable
private fun LastAnswer(state: TrussUiState) {
  if (state.lastSpoken.isEmpty()) {
    if (!state.photoBusy && state.serverBytes != null) {
      Surface {
        Text(
            "Nothing to say about the last look. Mannie speaks up when an answer is yes.",
            style = MaterialTheme.typography.bodyMedium,
            color = Truss.muted,
        )
      }
    }
    return
  }
  val how =
      when (state.lastSpokenAloud) {
        true -> "told you in your glasses"
        false -> "on screen only, not spoken"
        null -> "telling you now"
      }
  val when_ = clock12(state.lastSpokenAt)?.let { "$it · $how" } ?: how.replaceFirstChar { it.uppercase() }
  val answer: @Composable () -> Unit = {
    Row(
        modifier = Modifier.fillMaxWidth().clip(RoundedCornerShape(20.dp)).background(Truss.card).padding(14.dp, 12.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
      IconTile(TrussIcons.Check, Truss.lime, Truss.onLime, size = 40.dp, shape = CircleShape)
      Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        state.lastSpoken.forEach { line ->
          Text(line, style = MaterialTheme.typography.titleMedium, maxLines = 3, overflow = TextOverflow.Ellipsis)
        }
        Text(when_, style = MaterialTheme.typography.bodySmall, color = Truss.muted)
      }
    }
  }
  val photo = state.lastSpokenPhoto
  if (photo == null) {
    answer()
  } else {
    // The photo stays whole above the answer, never under it; a tap opens it full screen.
    var open by remember { mutableStateOf(false) }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
      Photo(
          photo,
          "What Mannie saw",
          Modifier.fillMaxWidth().height(220.dp).clip(RoundedCornerShape(24.dp)).background(Truss.sage).clickable { open = true },
      )
      answer()
    }
    if (open) PhotoViewer(photo, state.lastSpoken.joinToString(" "), onClose = { open = false })
  }
}

@Composable
private fun QuestionTiles(state: TrussUiState, onOpenQuestions: () -> Unit) {
  val shown = state.questionsList.filter { it.state != "retired" }
  if (shown.isEmpty()) {
    if (state.laptopReachable == true) {
      Row(
          modifier =
              Modifier.fillMaxWidth()
                  .clip(RoundedCornerShape(24.dp))
                  .background(Truss.card)
                  .border(1.dp, Truss.line, RoundedCornerShape(24.dp))
                  .clickable(onClick = onOpenQuestions)
                  .padding(14.dp),
          verticalAlignment = Alignment.CenterVertically,
          horizontalArrangement = Arrangement.spacedBy(14.dp),
      ) {
        IconTile(TrussIcons.Questions, Truss.sage, Truss.ink, size = 40.dp, shape = CircleShape)
        Column {
          Text("Add your first question", style = MaterialTheme.typography.titleMedium)
          Text("Like \"Is the stove off?\"", style = MaterialTheme.typography.bodySmall, color = Truss.muted)
        }
      }
    }
    return
  }
  shown.chunked(2).forEach { pair ->
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
      pair.forEach { item ->
        val look = lookFor(item)
        val shape = RoundedCornerShape(24.dp)
        Column(
            modifier =
                Modifier.weight(1f)
                    .heightIn(min = 132.dp)
                    .clip(shape)
                    .background(Truss.card)
                    .border(1.dp, Truss.line, shape)
                    .clickable(onClick = onOpenQuestions)
                    .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
          IconTile(topicIcon(item.question), look.background, look.onBackground, size = 36.dp, shape = RoundedCornerShape(11.dp))
          Text(item.question, style = MaterialTheme.typography.titleMedium)
          Text(look.status, style = MaterialTheme.typography.labelMedium, color = look.soft)
        }
      }
      if (pair.size == 1) Spacer(Modifier.weight(1f))
    }
  }
}

// MARK: - Questions

@Composable
private fun QuestionsScreen(
    state: TrussUiState,
    viewModel: TrussViewModel,
    onCaptureRef: (String) -> Unit,
) {
  LaunchedEffect(Unit) { viewModel.refreshQuestions() }
  val planting = state.plantSlug?.let { slug -> state.questionsList.firstOrNull { it.slug == slug } }

  ScreenBody {
    state.lastError?.let { ErrorBanner(it, viewModel::clearError) }
    ScreenTitle("Questions")

    if (state.plantSlug == null) {
      OutlinedTextField(
          value = state.plantText,
          onValueChange = viewModel::setPlantText,
          placeholder = { Text("Is the stove off?") },
          singleLine = true,
          shape = Pill,
          textStyle = MaterialTheme.typography.bodyLarge,
          keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
          keyboardActions = KeyboardActions(onSend = { viewModel.plantQuestionFromText() }),
          trailingIcon = {
            IconButton(
                onClick = viewModel::plantQuestionFromText,
                enabled = !state.plantBusy && state.plantText.isNotBlank(),
            ) {
              Box(
                  modifier = Modifier.size(44.dp).clip(CircleShape).background(Truss.forest),
                  contentAlignment = Alignment.Center,
              ) {
                Icon(TrussIcons.Arrow, contentDescription = "Next: show the looks", tint = Color.White)
              }
            }
          },
          colors =
              OutlinedTextFieldDefaults.colors(
                  focusedContainerColor = Truss.card,
                  unfocusedContainerColor = Truss.card,
                  focusedBorderColor = Truss.forest,
                  unfocusedBorderColor = Color.Transparent,
              ),
          modifier = Modifier.fillMaxWidth(),
      )
      Text(
          "Next, you show Mannie what yes looks like and what no looks like, through the glasses. " +
              "For a number on a display, the yes look alone is enough.",
          style = MaterialTheme.typography.bodySmall,
          color = Truss.muted,
          modifier = Modifier.padding(horizontal = 6.dp),
      )
    } else {
      PlantingCard(state, planting, onCaptureRef, viewModel::cancelPlanting, viewModel::finishPlantingWithoutMore)
    }

    val shown = state.questionsList.filter { it.state != "retired" }
    if (shown.isNotEmpty()) SectionLabel("Minding")
    shown.forEach { item ->
      QuestionRow(
          item = item,
          canResume = state.plantSlug == null && !state.busy,
          onResume = { viewModel.resumePlanting(item.slug) },
          onRemove = { viewModel.retireQuestion(item.slug) },
          removeEnabled = !state.busy,
      )
    }
    if (shown.isEmpty() && state.plantSlug == null) {
      Text(
          "No questions yet. Say one, or set up the bedtime round below.",
          style = MaterialTheme.typography.bodyMedium,
          color = Truss.muted,
          modifier = Modifier.padding(horizontal = 6.dp),
      )
    }

    if (state.plantSlug == null) {
      Row(
          modifier =
              Modifier.fillMaxWidth()
                  .clip(RoundedCornerShape(24.dp))
                  .background(Truss.card)
                  .border(1.dp, Truss.line, RoundedCornerShape(24.dp))
                  .clickable(enabled = !state.plantBusy, onClick = viewModel::plantBedtimeRound)
                  .padding(14.dp),
          verticalAlignment = Alignment.CenterVertically,
          horizontalArrangement = Arrangement.spacedBy(14.dp),
      ) {
        IconTile(TrussIcons.Moon, Truss.lime, Truss.onLime, size = 40.dp, shape = CircleShape)
        Column {
          Text("Bedtime round", style = MaterialTheme.typography.titleMedium)
          Text("Stove, door and pills in one go", style = MaterialTheme.typography.bodySmall, color = Truss.muted)
        }
      }
    }
  }
}

@Composable
private fun QuestionRow(
    item: QuestionInfo,
    canResume: Boolean,
    onResume: () -> Unit,
    onRemove: () -> Unit,
    removeEnabled: Boolean,
) {
  val look = lookFor(item)
  Surface(padding = 14.dp) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
      IconTile(topicIcon(item.question), look.background, look.onBackground, size = 40.dp, shape = RoundedCornerShape(12.dp))
      Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text(item.question, style = MaterialTheme.typography.titleMedium)
        Chip(
            look.status + if (item.kind == "done") " · done check" else "",
            look.background,
            look.onBackground,
        )
      }
      IconButton(onClick = onRemove, enabled = removeEnabled) {
        Icon(TrussIcons.Trash, contentDescription = "Remove ${item.question}", tint = Truss.muted)
      }
    }
    if (item.state == "planting" && canResume) {
      BigButton(
          if (!item.hasRefYes) "Show Mannie the yes look" else "Show Mannie the no look",
          TrussIcons.Camera,
          onResume,
          container = Truss.apricot,
          content = Truss.onApricot,
          height = 52.dp,
      )
    }
  }
}

@Composable
private fun PlantingCard(
    state: TrussUiState,
    planting: QuestionInfo?,
    onCaptureRef: (String) -> Unit,
    onCancel: () -> Unit,
    onFinish: () -> Unit,
) {
  val needsYes = planting?.hasRefYes != true
  val step = if (needsYes) 2 else 3
  Surface(color = Truss.apricot, shape = RoundedCornerShape(24.dp), padding = 18.dp) {
    Row(verticalAlignment = Alignment.CenterVertically) {
      Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
        (1..3).forEach { i ->
          Box(
              Modifier.width(28.dp)
                  .height(6.dp)
                  .clip(Pill)
                  .background(if (i <= step) Truss.onApricot else Truss.onApricot.copy(alpha = 0.22f)),
          )
        }
        Spacer(Modifier.width(6.dp))
        Text("Step $step of 3", style = MaterialTheme.typography.labelMedium, color = Truss.onApricotSoft)
      }
      Spacer(Modifier.weight(1f))
      IconButton(onClick = onCancel, enabled = !state.busy) {
        Icon(TrussIcons.Close, contentDescription = "Cancel planting", tint = Truss.onApricot)
      }
    }
    Text(planting?.question ?: "New question", style = MaterialTheme.typography.headlineSmall, color = Truss.onApricot)
    Text(
        if (needsYes) "Look at it the way it is when the answer is yes, then tap below."
        else "Now set it up so the answer is no, look at it, then tap below.",
        style = MaterialTheme.typography.bodyLarge,
        color = Truss.onApricot,
    )
    if (state.photoBusy) {
      Text(workingLine(state.photoLog.lastOrNull()), style = MaterialTheme.typography.bodySmall, color = Truss.onApricotSoft)
    }
    BigButton(
        if (needsYes) "Take the yes look" else "Take the no look",
        TrussIcons.Camera,
        { onCaptureRef(if (needsYes) "yes" else "no") },
        enabled = !state.busy,
        busy = state.photoBusy,
    )
    if (!needsYes) {
      TextButton(onClick = { onCaptureRef("yes") }, enabled = !state.busy, modifier = Modifier.align(Alignment.CenterHorizontally)) {
        Text("Retake the yes look", style = MaterialTheme.typography.labelLarge, color = Truss.onApricot)
      }
    }
    // For a question its words decide, like a number on a display, where a
    // no look (or any look) cannot be staged. Never for a safety question: the
    // laptop refuses it, since a wrong word on the stove or the pills does harm.
    if (planting != null && !planting.safety) {
      TextButton(onClick = onFinish, enabled = !state.busy, modifier = Modifier.align(Alignment.CenterHorizontally)) {
        Text(
            if (needsYes) "Start watching with no looks" else "Start watching without a no look",
            style = MaterialTheme.typography.labelLarge,
            color = Truss.onApricot,
        )
      }
    }
  }
}

// MARK: - History

@Composable
private fun HistoryScreen(state: TrussUiState, onDismissError: () -> Unit, onRefresh: () -> Unit) {
  LaunchedEffect(Unit) { onRefresh() }
  val day = state.historyDay?.let { runCatching { LocalDate.parse(it) }.getOrNull() }
  val title = if (day == null || day == LocalDate.now()) "Today" else day.format(DateTimeFormatter.ofPattern("EEEE", Locale.US))
  val dateLine =
      (day ?: LocalDate.now()).format(DateTimeFormatter.ofPattern("EEEE, MMMM d", Locale.US)) +
          " · ${state.history.size} answer" + if (state.history.size == 1) "" else "s"

  ScreenBody {
    state.lastError?.let { ErrorBanner(it, onDismissError) }
    Row(verticalAlignment = Alignment.CenterVertically) {
      Box(Modifier.weight(1f)) { ScreenTitle(title, dateLine) }
      IconButton(
          onClick = onRefresh,
          enabled = !state.historyBusy,
          modifier = Modifier.size(52.dp).clip(CircleShape).background(Truss.card),
      ) {
        Icon(TrussIcons.Refresh, contentDescription = "Refresh", tint = Truss.ink)
      }
    }
    if (state.historyBusy && state.history.isEmpty()) {
      Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(6.dp)) {
        CircularProgressIndicator(modifier = Modifier.size(22.dp), strokeWidth = 2.5.dp, color = Truss.forest)
        Spacer(Modifier.width(12.dp))
        Text("Loading", style = MaterialTheme.typography.bodyMedium)
      }
    }
    if (!state.historyBusy && state.history.isEmpty()) {
      Surface {
        Text(
            "Nothing yet today. When Mannie answers a question, the answer and the photo it " +
                "was seen in are kept on your laptop and shown here.",
            style = MaterialTheme.typography.bodyMedium,
            color = Truss.muted,
        )
      }
    }
    state.history.forEach { item ->
      // Keyed by the answer itself, so an open photo stays with its own row when the list reloads.
      key(state.historyDay, item) {
        val (label, bg, fg) =
            when (item.kind) {
              "answer" -> Triple("Yes", Truss.lime, Truss.onLime)
              "redo" -> Triple("Reminder", Truss.sky, Truss.onSky)
              "state_check" -> Triple("Checked late", Truss.apricot, Truss.onApricot)
              "cannot_tell" -> Triple("Can't tell", Truss.sage, Truss.ink)
              else -> Triple("", Truss.sage, Truss.ink)
            }
        val name = item.outline ?: item.evidence
        val photo = name?.let { state.historyPhotos["${state.historyDay}/$it"] }
        Row(
            modifier = Modifier.fillMaxWidth().clip(RoundedCornerShape(24.dp)).background(Truss.card).border(1.dp, Truss.line, RoundedCornerShape(24.dp)).padding(12.dp),
            horizontalArrangement = Arrangement.spacedBy(14.dp),
        ) {
          if (photo != null) {
            var open by remember { mutableStateOf(false) }
            Photo(
                photo,
                "Evidence photo for ${item.question}. Tap to open.",
                Modifier.size(84.dp).clip(RoundedCornerShape(16.dp)).clickable { open = true },
            )
            if (open) {
              PhotoViewer(photo, item.text.ifBlank { item.question }, onClose = { open = false })
            }
          } else {
            IconTile(topicIcon(item.question), Truss.sage, Truss.muted, size = 84.dp, shape = RoundedCornerShape(16.dp))
          }
          Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
              Text(clock12(item.time) ?: "", style = MaterialTheme.typography.labelMedium, color = Truss.muted)
              if (label.isNotEmpty()) Chip(label, bg, fg)
            }
            Text(item.text.ifBlank { item.question }, style = MaterialTheme.typography.titleSmall)
            if (item.text.isNotBlank()) {
              Text(item.question, style = MaterialTheme.typography.bodySmall, color = Truss.muted)
            }
          }
        }
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
) {
  var showDiagnostics by remember { mutableStateOf(false) }

  ScreenBody {
    state.lastError?.let { ErrorBanner(it, viewModel::clearError) }
    ScreenTitle("Settings")

    SectionLabel("Connections")
    Surface {
      Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
        IconTile(TrussIcons.Laptop, Truss.sage, Truss.ink, size = 40.dp, shape = RoundedCornerShape(12.dp))
        Text("Laptop", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
        when (state.laptopReachable) {
          true -> Chip("Connected", Truss.lime, Truss.onLime)
          false -> Chip("Can't reach it", Truss.apricot, Truss.onApricot)
          null -> Chip("Not checked", Truss.sage, Truss.muted)
        }
      }
      OutlinedTextField(
          value = state.laptopIp,
          onValueChange = viewModel::setLaptopIp,
          label = { Text("Laptop address") },
          placeholder = { Text("192.168.1.20") },
          singleLine = true,
          shape = RoundedCornerShape(18.dp),
          textStyle = MaterialTheme.typography.bodyLarge,
          colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = Truss.forest, focusedLabelColor = Truss.forest),
          modifier = Modifier.fillMaxWidth(),
      )
      Text(
          "The address the laptop prints when the Truss server starts. Kept on this phone.",
          style = MaterialTheme.typography.bodySmall,
          color = Truss.muted,
      )
      OutlineBigButton(
          if (state.laptopCheckBusy) "Checking..." else "Check connection",
          TrussIcons.Refresh,
          viewModel::checkLaptopNow,
          enabled = !state.laptopCheckBusy,
          modifier = Modifier.fillMaxWidth(),
      )
      state.laptopCheck?.let { line ->
        Text(
            line,
            style = MaterialTheme.typography.bodySmall,
            color = if (state.laptopReachable == false) Truss.onApricot else Truss.muted,
        )
      }

      HorizontalDivider(color = Truss.sage, modifier = Modifier.padding(vertical = 4.dp))

      Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
        IconTile(TrussIcons.Glasses, Truss.sage, Truss.ink, size = 40.dp, shape = RoundedCornerShape(12.dp))
        Column(Modifier.weight(1f)) {
          Text("Glasses", style = MaterialTheme.typography.titleMedium)
          Text(
              if (state.glassesReady) state.deviceNames.joinToString() else "Not connected",
              style = MaterialTheme.typography.bodySmall,
              color = Truss.muted,
          )
        }
        TextButton(onClick = onConnectGlasses) {
          Text(if (state.glassesReady) "Reconnect" else "Connect", style = MaterialTheme.typography.labelLarge, color = Truss.forest)
        }
      }
      state.glassesNote?.let { note ->
        Text(note, style = MaterialTheme.typography.bodySmall, color = Truss.muted)
      }
      if (!state.sdkReady) {
        Text(stringResource(R.string.why_bluetooth), style = MaterialTheme.typography.bodySmall, color = Truss.muted)
      }
    }

    SectionLabel("Watching")
    Surface {
      Text("Look every", style = MaterialTheme.typography.titleMedium)
      Text("Shorter catches more but uses more glasses battery.", style = MaterialTheme.typography.bodySmall, color = Truss.muted)
      Segmented(
          options = listOf(15, 30, 60),
          selected = state.watchEverySeconds,
          label = { "$it s" },
          onSelect = viewModel::setWatchEverySeconds,
          onDark = false,
      )
    }

    SectionLabel("Help")
    Row(
        modifier =
            Modifier.fillMaxWidth()
                .clip(RoundedCornerShape(24.dp))
                .background(Truss.card)
                .border(1.dp, Truss.line, RoundedCornerShape(24.dp))
                .clickable { showDiagnostics = !showDiagnostics }
                .padding(16.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(14.dp),
    ) {
      IconTile(TrussIcons.Pulse, Truss.sage, Truss.ink, size = 40.dp, shape = RoundedCornerShape(12.dp))
      Column(Modifier.weight(1f)) {
        Text("Diagnostics", style = MaterialTheme.typography.titleMedium)
        Text("Connection, last capture, speaker test", style = MaterialTheme.typography.bodySmall, color = Truss.muted)
      }
      Icon(
          TrussIcons.Chevron,
          contentDescription = if (showDiagnostics) "Hide diagnostics" else "Show diagnostics",
          tint = Truss.muted,
          modifier = Modifier.rotate(if (showDiagnostics) 90f else 0f),
      )
    }
    if (showDiagnostics) {
      Diagnostics(state, viewModel, onCapture)
    }
  }
}

@Composable
private fun DiagCard(title: String, content: @Composable ColumnScope.() -> Unit) {
  Surface {
    Text(title, style = MaterialTheme.typography.titleSmall)
    content()
  }
}

@Composable
private fun DiagButton(text: String, onClick: () -> Unit, enabled: Boolean) {
  Button(
      onClick = onClick,
      enabled = enabled,
      shape = Pill,
      colors = ButtonDefaults.buttonColors(containerColor = Truss.forest),
  ) {
    Text(text, style = MaterialTheme.typography.labelMedium)
  }
}

@Composable
private fun Diagnostics(
    state: TrussUiState,
    viewModel: TrussViewModel,
    onCapture: () -> Unit,
) {
  val small = MaterialTheme.typography.bodySmall
  DiagCard("Connection") {
    Text("Toolkit version: $TOOLKIT_VERSION", style = small)
    Text("Phone SDK level: ${Build.VERSION.SDK_INT} (Android ${Build.VERSION.RELEASE})", style = small)
    Text("Registration: ${state.registrationState}", style = small)
    Text("Glasses link: ${state.linkState}", style = small)
    DiagButton(if (state.pingBusy) "Pinging..." else "Ping laptop", viewModel::pingLaptop, !state.pingBusy)
    state.pingResult?.let { Text("Ping laptop: $it", style = small) }
  }

  DiagCard("Last capture") {
    DiagButton(if (state.photoBusy) "Working..." else "Capture and upload", onCapture, !state.busy)
    state.serverBytes?.let { Text("Laptop received: $it bytes", style = small) }
    state.photoElapsedMs?.let { Text("Total elapsed: $it ms", style = small) }
    state.wifiReturn?.let { Text("Return to home Wi-Fi: $it", style = small) }
    state.spokenLines.forEach { line -> Text("Mannie says: $line", style = small) }
    HorizontalDivider(color = Truss.sage)
    LogLines(state.photoLog)
  }

  DiagCard("Mannie's voice through the glasses") {
    Text(
        "Any glasses camera session is ended before Bluetooth audio opens: opening Bluetooth " +
            "SCO can end a toolkit session (a known upstream issue), so the two are never held " +
            "open together.",
        style = small,
        color = Truss.muted,
    )
    Text("Available communication devices:", style = small)
    viewModel.getAvailableCommunicationDevices().forEach { device ->
      Text("  • $device", style = small, fontFamily = FontFamily.Monospace)
    }
    DiagButton(
        if (state.speakBusy) "Playing..." else "Speak test sentence via glasses",
        viewModel::speakTestSentence,
        !state.speakBusy && !state.photoBusy,
    )
    Text("Test sentence: \"${TrussViewModel.TEST_SENTENCE}\"", style = small)
    state.outputDevice?.let { device ->
      if (device.isBluetooth) {
        Text("Played through: ${device.describe()}", style = small)
      } else {
        Text("FAIL: played through ${device.describe()}, not the glasses.", style = small, color = Color.Red)
      }
    }
    state.ttsMs?.let { Text("TTS request to audio start: $it ms", style = small) }
  }

  if (state.plantLog.isNotEmpty()) {
    DiagCard("Planting log") { LogLines(state.plantLog) }
  }
  Text(stringResource(R.string.why_location_wifi), style = small, color = Truss.muted)
}

@Composable
private fun LogLines(lines: List<String>) {
  lines.forEach { line ->
    Text(line, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
  }
}
