package com.climboutlabs.truss

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.addPathNodes
import androidx.compose.ui.unit.dp

/**
 * Line icons drawn on a 24 x 24 grid with round 2 px strokes, so the app has one
 * consistent icon style without pulling in the large extended icon library.
 * Icon() tints them, so the stroke colour here does not matter.
 */
object TrussIcons {
  private fun circle(cx: Float, cy: Float, r: Float) =
      "M${cx - r},${cy}a$r,$r 0 1,0 ${2 * r},0a$r,$r 0 1,0 ${-2 * r},0"

  private fun rect(x: Float, y: Float, w: Float, h: Float, rx: Float) =
      "M${x + rx},${y}h${w - 2 * rx}a$rx,$rx 0 0 1 $rx,${rx}v${h - 2 * rx}" +
          "a$rx,$rx 0 0 1 ${-rx},${rx}h${-(w - 2 * rx)}a$rx,$rx 0 0 1 ${-rx},${-rx}" +
          "v${-(h - 2 * rx)}a$rx,$rx 0 0 1 $rx,${-rx}z"

  private fun icon(name: String, vararg paths: String, rotate: Float = 0f): ImageVector {
    val builder =
        ImageVector.Builder(
            name = name,
            defaultWidth = 24.dp,
            defaultHeight = 24.dp,
            viewportWidth = 24f,
            viewportHeight = 24f,
        )
    if (rotate != 0f) builder.addGroup(rotate = rotate, pivotX = 12f, pivotY = 12f)
    paths.forEach { d ->
      builder.addPath(
          pathData = addPathNodes(d),
          stroke = SolidColor(Color.Black),
          strokeLineWidth = 2f,
          strokeLineCap = StrokeCap.Round,
          strokeLineJoin = StrokeJoin.Round,
      )
    }
    if (rotate != 0f) builder.clearGroup()
    return builder.build()
  }

  val Home = icon("home", "M3 10.5L12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z")
  val Questions =
      icon(
          "questions",
          "M10 6h10M10 12h10M10 18h10",
          "M3.5 6l1.5 1.5L7.5 5M3.5 12l1.5 1.5 2.5-2.5M3.5 18l1.5 1.5 2.5-2.5",
      )
  val History = icon("history", circle(12f, 12f, 9f), "M12 7v5l3 2")
  val Settings =
      icon(
          "settings",
          "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12",
          circle(16f, 6f, 2f),
          circle(10f, 12f, 2f),
          circle(18f, 18f, 2f),
      )
  val Eye = icon("eye", "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z", circle(12f, 12f, 3f))
  val Camera =
      icon(
          "camera",
          "M4 8h3l2-3h6l2 3h3a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z",
          circle(12f, 13.5f, 3.5f),
      )
  val Check = icon("check", "M5 12.5l4.5 4.5L19 7.5")
  val Lock = icon("lock", rect(5f, 11f, 14f, 10f, 2f), "M8 11V8a4 4 0 0 1 8 0v3")
  val Pill = icon("pill", rect(3f, 9f, 18f, 6f, 3f), "M12 9v6", rotate = -45f)
  val Flame =
      icon("flame", "M12 3c1 3.5 5 5.5 5 10a5 5 0 0 1-10 0c0-2.5 1.5-4 2.5-5 .3 1.6 1 2.5 2 3 0-3 .5-5.5.5-8z")
  val Mic = icon("mic", rect(9f, 3f, 6f, 11f, 3f), "M5 11a7 7 0 0 0 14 0M12 18v3")
  val Keyboard =
      icon(
          "keyboard",
          rect(2.5f, 6f, 19f, 12f, 2f),
          "M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10",
      )
  val Moon = icon("moon", "M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z")
  val Trash = icon("trash", "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3")
  val Close = icon("close", "M6 6l12 12M18 6L6 18")
  val Chevron = icon("chevron", "M9 6l6 6-6 6")
  val Arrow = icon("arrow", "M5 12h14M13 6l6 6-6 6")
  val Glasses =
      icon(
          "glasses",
          circle(6.5f, 14f, 3.5f),
          circle(17.5f, 14f, 3.5f),
          "M10 14h4M3 14l1.5-6M21 14l-1.5-6",
      )
  val Laptop = icon("laptop", rect(4f, 5f, 16f, 11f, 1.5f), "M2 19h20")
  val Refresh = icon("refresh", "M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5")
  val Pulse = icon("pulse", "M3 12h4l3-7 4 14 3-7h4")
  val Stop = icon("stop", rect(6f, 6f, 12f, 12f, 2f))
  val Play = icon("play", "M8 5.5v13l10-6.5z")
}
