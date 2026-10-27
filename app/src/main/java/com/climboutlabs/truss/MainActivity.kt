package com.climboutlabs.truss

import android.Manifest.permission.BLUETOOTH
import android.Manifest.permission.BLUETOOTH_CONNECT
import android.Manifest.permission.INTERNET
import android.Manifest.permission.RECORD_AUDIO
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts.RequestMultiplePermissions
import androidx.activity.result.contract.ActivityResultContracts.RequestPermission
import androidx.activity.viewModels
import androidx.core.content.ContextCompat
import com.meta.wearable.dat.core.Wearables
import com.meta.wearable.dat.core.types.Permission
import com.meta.wearable.dat.core.types.PermissionStatus
import kotlin.coroutines.resume
import kotlinx.coroutines.CancellableContinuation
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * Entry point, with the registration and permission plumbing copied from the toolkit's
 * CameraAccess sample: Android permissions first, then Wearables.initialize, and wearable
 * permissions through Wearables.RequestPermissionContract, one at a time under a Mutex.
 */
class MainActivity : ComponentActivity() {

  val viewModel: TrussViewModel by viewModels()

  private val permissionCheckLauncher =
      registerForActivityResult(RequestMultiplePermissions()) { permissionsResult ->
        val granted = permissionsResult.entries.all { it.value }
        if (granted) {
          // Initialize the DAT SDK once the permissions are granted.
          // This is REQUIRED before using any Wearables APIs.
          Wearables.initialize(this)
          viewModel.onSdkReady()
        } else {
          viewModel.onSdkPermissionsDenied()
        }
      }

  private var permissionContinuation: CancellableContinuation<PermissionStatus>? = null
  private val permissionMutex = Mutex()

  // Requesting wearable device permissions via the Meta AI app.
  private val permissionsResultLauncher =
      registerForActivityResult(Wearables.RequestPermissionContract()) { result ->
        val permissionStatus = result.getOrDefault(PermissionStatus.Denied)
        permissionContinuation?.resume(permissionStatus)
        permissionContinuation = null
      }

  // Convenience method to make a permission request in a sequential manner.
  // Uses a Mutex to ensure requests are processed one at a time, preventing race conditions.
  suspend fun requestWearablesPermission(permission: Permission): PermissionStatus {
    return permissionMutex.withLock {
      suspendCancellableCoroutine { continuation ->
        permissionContinuation = continuation
        continuation.invokeOnCancellation { permissionContinuation = null }
        permissionsResultLauncher.launch(permission)
      }
    }
  }

  private var audioPermissionContinuation: CancellableContinuation<Boolean>? = null
  private val recordAudioPermissionLauncher =
      registerForActivityResult(RequestPermission()) { granted ->
        audioPermissionContinuation?.resume(granted)
        audioPermissionContinuation = null
      }

  suspend fun requestRecordAudioPermission(): Boolean {
    if (
        ContextCompat.checkSelfPermission(this, RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
    ) {
      return true
    }
    return permissionMutex.withLock {
      suspendCancellableCoroutine { continuation ->
        audioPermissionContinuation = continuation
        continuation.invokeOnCancellation { audioPermissionContinuation = null }
        recordAudioPermissionLauncher.launch(RECORD_AUDIO)
      }
    }
  }

  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    enableEdgeToEdge()
    setContent {
      TrussApp(
          viewModel = viewModel,
          onConnectGlasses = { viewModel.startRegistration(this@MainActivity) },
          onRequestWearablesPermission = ::requestWearablesPermission,
          onRequestRecordAudioPermission = ::requestRecordAudioPermission,
      )
    }
  }

  override fun onStart() {
    super.onStart()
    // First, ensure the app has the Android permissions the toolkit needs.
    permissionCheckLauncher.launch(PERMISSIONS)
  }

  companion object {
    // Required Android permissions for the DAT SDK to function properly (sample's list).
    val PERMISSIONS: Array<String> = arrayOf(BLUETOOTH, BLUETOOTH_CONNECT, INTERNET)
  }
}
