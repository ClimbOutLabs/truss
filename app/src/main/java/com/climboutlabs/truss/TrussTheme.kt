package com.climboutlabs.truss

import androidx.compose.foundation.shape.GenericShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.sin

/**
 * The "Big and clear" look: Material 3 Expressive in spirit, with large type,
 * one bold colour tile per question and big buttons. Forest green with a lime
 * highlight, set in Plus Jakarta Sans (bundled under res/font, SIL OFL 1.1).
 */
object Truss {
  val ground = Color(0xFFF3F4EF)
  val ink = Color(0xFF10140F)
  val muted = Color(0xFF4A524A)
  val card = Color(0xFFFFFFFF)
  val forest = Color(0xFF12372A)
  val onForestSoft = Color(0xFFC9DCCF)
  val lime = Color(0xFFD8F36A)
  val onLime = Color(0xFF1F2A00)
  val apricot = Color(0xFFFFD3B0)
  val onApricot = Color(0xFF3A1C00)
  val onApricotSoft = Color(0xFF6B3B10)
  val sky = Color(0xFFCFE5F2)
  val onSky = Color(0xFF0E2A3A)
  val onSkySoft = Color(0xFF33536A)
  val sage = Color(0xFFE3E8DD)
  val nav = Color(0xFFE9ECE4)
  val errorSoft = Color(0xFFFFDAD6)
  val onError = Color(0xFF410002)
}

val PlusJakartaSans =
    FontFamily(
        Font(R.font.plus_jakarta_sans_medium, FontWeight.Medium),
        Font(R.font.plus_jakarta_sans_semibold, FontWeight.SemiBold),
        Font(R.font.plus_jakarta_sans_bold, FontWeight.Bold),
        Font(R.font.plus_jakarta_sans_extrabold, FontWeight.ExtraBold),
    )

private val TrussColors =
    lightColorScheme(
        primary = Truss.forest,
        onPrimary = Color.White,
        primaryContainer = Truss.lime,
        onPrimaryContainer = Truss.onLime,
        secondaryContainer = Truss.lime,
        onSecondaryContainer = Truss.onLime,
        background = Truss.ground,
        onBackground = Truss.ink,
        surface = Truss.ground,
        onSurface = Truss.ink,
        surfaceVariant = Truss.card,
        onSurfaceVariant = Truss.muted,
        surfaceContainer = Truss.nav,
        outline = Truss.forest,
        errorContainer = Truss.errorSoft,
        onErrorContainer = Truss.onError,
    )

private fun style(size: Int, line: Int, weight: FontWeight, tracking: Double = 0.0) =
    TextStyle(
        fontFamily = PlusJakartaSans,
        fontSize = size.sp,
        lineHeight = line.sp,
        fontWeight = weight,
        letterSpacing = tracking.sp,
    )

private val TrussType =
    Typography(
        displaySmall = style(34, 40, FontWeight.ExtraBold, -0.6),
        headlineMedium = style(30, 34, FontWeight.ExtraBold, -0.5),
        headlineSmall = style(24, 30, FontWeight.ExtraBold, -0.3),
        titleLarge = style(20, 26, FontWeight.ExtraBold),
        titleMedium = style(18, 24, FontWeight.ExtraBold),
        titleSmall = style(16, 22, FontWeight.Bold),
        bodyLarge = style(17, 24, FontWeight.Medium),
        bodyMedium = style(16, 22, FontWeight.Medium),
        bodySmall = style(14, 20, FontWeight.Medium),
        labelLarge = style(16, 20, FontWeight.ExtraBold),
        labelMedium = style(14, 18, FontWeight.Bold),
        labelSmall = style(13, 16, FontWeight.Bold),
    )

@Composable
fun TrussTheme(content: @Composable () -> Unit) {
  MaterialTheme(colorScheme = TrussColors, typography = TrussType, content = content)
}

/** A scalloped "cookie" outline, the expressive shape behind the status icon. */
fun cookieShape(scallops: Int = 9, depth: Float = 0.08f) = GenericShape { size, _ ->
  val cx = size.width / 2f
  val cy = size.height / 2f
  val base = min(cx, cy) * (1f - depth)
  val steps = 144
  for (i in 0..steps) {
    val t = 2.0 * PI * i / steps
    val r = base + base * depth * cos(scallops * t).toFloat()
    val x = cx + (r * sin(t)).toFloat()
    val y = cy - (r * cos(t)).toFloat()
    if (i == 0) moveTo(x, y) else lineTo(x, y)
  }
  close()
}
