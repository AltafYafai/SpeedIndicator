package com.sipun.netspeedindicator.domain.repository

import com.sipun.netspeedindicator.domain.model.InstalledAppInfo

/**
 * Provides access to launcher-visible applications and per-UID app info.
 * Shared by the per-app usage history, the live rates poller and the
 * dashboard so expensive `PackageManager` queries are cached.
 */
interface InstalledAppsProvider {
    /** Launcher-visible apps keyed by UID (cached). */
    fun launcherApps(): Map<Int, InstalledAppInfo>

    /**
     * All installed applications keyed by UID (cached). Used by the live
     * rates poller so non-launcher apps are also accounted, and so per-app
     * rates can be derived from the same counters as the device total.
     */
    fun allApps(): Map<Int, InstalledAppInfo>

    /**
     * Resolve any UID — including non-launcher apps, shared/system UIDs —
     * to display info. Returns null when the UID cannot be attributed to
     * anything the user would recognize.
     */
    fun appInfoForUid(uid: Int): InstalledAppInfo?
}
