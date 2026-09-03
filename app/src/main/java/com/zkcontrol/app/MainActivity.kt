package com.zkcontrol.app

import android.Manifest
import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.view.animation.AlphaAnimation
import android.webkit.JavascriptInterface
import android.webkit.URLUtil
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.chaquo.python.PyException
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var loadingOverlay: View
    private val pendingDownloadIds = mutableSetOf<Long>()

    private val notificationPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* لا حاجة لإجراء إضافي */ }

    private val downloadCompleteReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1)
            if (id == -1L || !pendingDownloadIds.contains(id)) return
            pendingDownloadIds.remove(id)
            shareDownloadedFile(id)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webview)
        loadingOverlay = findViewById(R.id.loadingOverlay)

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                if (url.scheme == "mailto") {
                    try {
                        val intent = Intent(Intent.ACTION_SENDTO, url)
                        startActivity(intent)
                    } catch (e: Exception) {
                        Toast.makeText(this@MainActivity, "لا يوجد تطبيق بريد مثبت", Toast.LENGTH_SHORT).show()
                    }
                    return true
                }
                return false
            }

            override fun onPageFinished(view: WebView, url: String?) {
                super.onPageFinished(view, url)
                hideLoadingOverlay()
            }
        }
        webView.addJavascriptInterface(AndroidBridge(), "AndroidBridge")

        webView.setDownloadListener { url, _, contentDisposition, mimeType, _ ->
            try {
                val fileName = URLUtil.guessFileName(url, contentDisposition, mimeType)
                val request = DownloadManager.Request(Uri.parse(url))
                request.setDestinationInExternalPublicDir(android.os.Environment.DIRECTORY_DOWNLOADS, fileName)
                request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                request.setMimeType(mimeType)
                request.setTitle(fileName)
                request.setDescription("تنزيل من إدارة أجهزة البصمة")
                val dm = getSystemService(DOWNLOAD_SERVICE) as DownloadManager
                val id = dm.enqueue(request)
                pendingDownloadIds.add(id)
                Toast.makeText(this, "جارِ التنزيل...", Toast.LENGTH_SHORT).show()
            } catch (e: Exception) {
                Toast.makeText(this, "تعذّر بدء التنزيل: ${e.message}", Toast.LENGTH_LONG).show()
            }
        }

        val filter = IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(downloadCompleteReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(downloadCompleteReceiver, filter)
        }

        startPythonServer()
        restartContinuousMonitoringServiceIfNeeded()

        Handler(Looper.getMainLooper()).postDelayed({
            webView.loadUrl("http://127.0.0.1:5000/")
        }, 2000)
    }

    /** يُخفي شاشة التحميل الترحيبية بتلاشٍ ناعم بمجرد جاهزية الواجهة فعليًا،
     * بدل ظهور خلفية سوداء/فارغة أثناء إقلاع خادم بايثون. */
    private fun hideLoadingOverlay() {
        if (loadingOverlay.visibility != View.VISIBLE) return
        val fadeOut = AlphaAnimation(1f, 0f).apply { duration = 350 }
        loadingOverlay.startAnimation(fadeOut)
        loadingOverlay.visibility = View.GONE
    }

    private fun shareDownloadedFile(downloadId: Long) {
        try {
            val dm = getSystemService(DOWNLOAD_SERVICE) as DownloadManager
            val uri = dm.getUriForDownloadedFile(downloadId) ?: return

            val query = DownloadManager.Query().setFilterById(downloadId)
            var mimeType = "*/*"
            dm.query(query)?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val mimeIdx = cursor.getColumnIndex(DownloadManager.COLUMN_MEDIA_TYPE)
                    if (mimeIdx >= 0) mimeType = cursor.getString(mimeIdx) ?: mimeType
                }
            }

            val shareIntent = Intent(Intent.ACTION_SEND).apply {
                type = mimeType
                putExtra(Intent.EXTRA_STREAM, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            startActivity(Intent.createChooser(shareIntent, "مشاركة الملف عبر").apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            })
        } catch (e: Exception) {
            // فشل المشاركة التلقائية ليس خطأ فادحًا - الملف موجود بالفعل في مجلد التنزيلات
        }
    }

    /** شبكة أمان: لو "وضع المراقبة المستمرة" كان مفعّلاً لكن النظام أوقف الخدمة
     * (نادر لكن ممكن)، نعيد تشغيلها تلقائيًا عند فتح التطبيق. ملاحظة: هذا
     * لا يغطي إعادة التشغيل التلقائي الكامل بعد إعادة تشغيل الهاتف مباشرة
     * (يتطلب ذلك مستقبِل BOOT_COMPLETED منفصل لم يُضَف بعد). */
    private fun restartContinuousMonitoringServiceIfNeeded() {
        if (Prefs.isContinuousMonitoringEnabled(this) && !ContinuousMonitoringService.isRunning) {
            try {
                val intent = Intent(this, ContinuousMonitoringService::class.java)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    startForegroundService(intent)
                } else {
                    startService(intent)
                }
            } catch (e: Exception) {
                // فشل إعادة تشغيل الخدمة لا يجب أن يمنع فتح التطبيق نفسه إطلاقًا -
                // ونصفّر الإعداد حتى لا تتكرر المحاولة الفاشلة في كل مرة يُفتح
                // فيها التطبيق (كان هذا يسبب دخولًا في حلقة تعطّل مستمرة)
                Prefs.setContinuousMonitoringEnabled(this, false)
            }
        }
    }

    private fun startPythonServer() {
        if (PythonServerState.started) return
        PythonServerState.started = true

        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        Thread {
            try {
                val py = Python.getInstance()
                val appModule = py.getModule("app")
                appModule.callAttr("start", filesDir.absolutePath)
            } catch (e: PyException) {
                runOnUiThread {
                    Toast.makeText(this, "خطأ في تشغيل خادم بايثون: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }

    /** الجسر بين واجهة الويب (JavaScript) والوظائف الأصلية في أندرويد */
    inner class AndroidBridge {

        @JavascriptInterface
        fun refreshBackgroundSchedule() {
            runOnUiThread {
                try {
                    Scheduler.refreshBackgroundSchedule(applicationContext)
                } catch (e: Exception) {
                    Toast.makeText(this@MainActivity, "تعذّر تطبيق إعدادات الخلفية: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }

        @JavascriptInterface
        fun requestIgnoreBatteryOptimizations() {
            runOnUiThread {
                val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
                if (!pm.isIgnoringBatteryOptimizations(packageName)) {
                    try {
                        val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                        intent.data = Uri.parse("package:$packageName")
                        startActivity(intent)
                    } catch (e: Exception) {
                        Toast.makeText(this@MainActivity, "غير مدعوم على هذا الجهاز", Toast.LENGTH_SHORT).show()
                    }
                } else {
                    Toast.makeText(this@MainActivity, "التطبيق مستثنى بالفعل من توفير البطارية", Toast.LENGTH_SHORT).show()
                }
            }
        }

        @JavascriptInterface
        fun requestNotificationPermission() {
            runOnUiThread {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    val granted = ContextCompat.checkSelfPermission(
                        this@MainActivity, Manifest.permission.POST_NOTIFICATIONS
                    ) == PackageManager.PERMISSION_GRANTED
                    if (!granted) {
                        notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                    }
                }
            }
        }

        @JavascriptInterface
        fun setAppLockEnabled(enabled: Boolean) {
            Prefs.setAppLockEnabled(applicationContext, enabled)
        }

        /** يعرض إشعار نظام حقيقي فورًا - تستدعيها الواجهة (JavaScript) حتى
         * أثناء بقاء التطبيق مفتوحًا في المقدمة، وليس فقط من مهمة الخلفية. */
        @JavascriptInterface
        fun postAlert(message: String) {
            NotificationHelper.postAlert(applicationContext, message)
        }

        /** يشغّل/يوقف "وضع المراقبة المستمرة" (Foreground Service). الأوقات
         * الثابتة تبقى دائمًا على WorkManager العادي بشكل مستقل تمامًا في
         * كل الأحوال (لا تُلغى أو تُعاد جدولتها عند هذا التبديل) - فقط
         * فترة التكرار وكشف الانحراف في الخلفية مرتبطان حصريًا بهذا الوضع. */
        @JavascriptInterface
        fun setContinuousMonitoringEnabled(enabled: Boolean) {
            runOnUiThread {
                try {
                    Prefs.setContinuousMonitoringEnabled(applicationContext, enabled)
                    if (enabled) {
                        val intent = Intent(this@MainActivity, ContinuousMonitoringService::class.java)
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            startForegroundService(intent)
                        } else {
                            startService(intent)
                        }
                    } else {
                        stopService(Intent(this@MainActivity, ContinuousMonitoringService::class.java))
                    }
                } catch (e: Exception) {
                    Toast.makeText(this@MainActivity, "تعذّر تفعيل وضع المراقبة المستمرة: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }

        /** يطلب تحديثًا فوريًا لمحتوى إشعار وضع المراقبة المستمرة (بدل
         * انتظار النبضة الدورية القادمة) - تستدعيها الواجهة بعد نجاح أي
         * مزامنة يدوية من داخل التطبيق. لا تأثير لها إن كانت الخدمة متوقفة. */
        @JavascriptInterface
        fun requestNotificationRefresh() {
            runOnUiThread {
                if (!ContinuousMonitoringService.isRunning) return@runOnUiThread
                try {
                    val intent = Intent(this@MainActivity, ContinuousMonitoringService::class.java)
                    intent.action = ContinuousMonitoringService.ACTION_REFRESH_NOW
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        startForegroundService(intent)
                    } else {
                        startService(intent)
                    }
                } catch (e: Exception) {
                    // تجاهل - التحديث الفوري تحسين إضافي وليس أساسيًا
                }
            }
        }
    }

    override fun onDestroy() {
        try {
            unregisterReceiver(downloadCompleteReceiver)
        } catch (e: Exception) {
            // كان غير مسجّل بالفعل
        }
        super.onDestroy()
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }
}
