package com.example.blindassist.p2p

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.LocationManager
import android.os.Build

/** Wi-Fi Direct 运行时权限的统一入口（按系统版本分派）。 */
object P2pPermissions {

    /**
     * Android 13+ 用 NEARBY_WIFI_DEVICES（配合 neverForLocation），
     * Android 12 及以下 P2P 服务发现必须 ACCESS_FINE_LOCATION。
     */
    fun required(): Array<String> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            arrayOf(Manifest.permission.NEARBY_WIFI_DEVICES)
        } else {
            arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
        }

    fun hasAll(context: Context): Boolean =
        required().all {
            context.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED
        }

    /** Android 12 及以下的 P2P 发现还依赖系统定位开关（Android 13+ 不需要）。 */
    fun locationServicesEnabled(context: Context): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            val locationManager =
                context.getSystemService(Context.LOCATION_SERVICE) as? LocationManager
            return locationManager?.isLocationEnabled ?: true
        }
        return true
    }
}
