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
 * The "Paper and ink" look: near-white surfaces, black type and buttons, and one
 * muted slate accent, so colour is kept for meaning. Set in Hanken Grotesk
 * throughout (bundled under res/font, SIL OFL 1.1).
 *
 * The token names describe roles: forest is the dark primary (ink), lime and sky
 * are the soft accent fill, apricot is the soft attention fill.
 */
object Truss {
  val ground = Color(0xFFF6F6F4)
  val ink = Color(0xFF141414)
  val muted = Color(0xFF6B6B6B)
  val card = Color(0xFFFFFFFF)
  val line = Color(0xFFE6E6E2)
  val forest = Color(0xFF141414)
  val onForestSoft = Color(0xFF6B6B6B)
  val lime = Color(0xFFE9EDF1)
  val onLime = Color(0xFF34495E)
  val apricot = Color(0xFFF4ECE7)
  val onApricot = Color(0xFF8C4A2F)
  val onApricotSoft = Color(0xFF8C5A45)
  val sky = Color(0xFFE9EDF1)
  val onSky = Color(0xFF34495E)
  val onSkySoft = Color(0xFF6B6B6B)
  val sage = Color(0xFFEFEFEB)
  val nav = Color(0xFFFFFFFF)
  val errorSoft = Color(0xFFFFDAD6)
  val onError = Color(0xFF410002)
}

val HankenGrotesk =
    FontFamily(
        Font(R.font.hanken_grotesk_regular, FontWeight.Normal),
        Font(R.font.hanken_grotesk_medium, FontWeight.Medium),
        Font(R.font.hanken_grotesk_semibold, FontWeight.SemiBold),
    )

private val TrussColors =
    lightColorScheme(
        primary = Truss.ink,
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
        outline = Truss.line,
        errorContainer = Truss.errorSoft,
        onErrorContainer = Truss.onError,
    )

private fun style(
    size: Int,
    line: Int,
    weight: FontWeight,
    tracking: Double = 0.0,
    family: FontFamily = HankenGrotesk,
) =
    TextStyle(
        fontFamily = family,
        fontSize = size.sp,
        lineHeight = line.sp,
        fontWeight = weight,
        letterSpacing = tracking.sp,
    )

/** A calm scale: 28 sp titles, 16 sp semibold item titles, 13 sp secondary text. */
private val TrussType =
    Typography(
        displaySmall = style(28, 34, FontWeight.SemiBold, -0.4),
        headlineMedium = style(22, 28, FontWeight.SemiBold, -0.3),
        headlineSmall = style(22, 28, FontWeight.SemiBold, -0.3),
        titleLarge = style(18, 24, FontWeight.SemiBold),
        titleMedium = style(16, 22, FontWeight.SemiBold),
        titleSmall = style(15, 20, FontWeight.SemiBold),
        bodyLarge = style(16, 23, FontWeight.Normal),
        bodyMedium = style(15, 21, FontWeight.Normal),
        bodySmall = style(13, 18, FontWeight.Normal),
        labelLarge = style(15, 20, FontWeight.SemiBold),
        labelMedium = style(13, 18, FontWeight.Medium),
        labelSmall = style(12, 16, FontWeight.Medium),
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
