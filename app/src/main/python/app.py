# -*- coding: utf-8 -*-
"""
الخادم الخلفي (Flask) لتطبيق إدارة أجهزة البصمة.
تصميم API على شكل JSON بحيث تتفاعل الواجهة (index.html) معه عبر AJAX
بدون إعادة تحميل الصفحة بالكامل في كل عملية.
"""

from flask import Flask, jsonify, request, Response, send_from_directory
from datetime import datetime, timedelta
from zk import ZK
import os
import io
import csv
import json
import socket
import uuid
import threading
from urllib.parse import quote

app = Flask(__name__, static_folder=None)

BASE_DIR = os.path.expanduser("~/biometric_project")

DEVICES_FILE = "devices.json"
STATE_FILE = "state.json"
HISTORY_FILE = "history.txt"
SCHEDULE_FILE = "schedule.json"
NOTIFICATIONS_FILE = "notifications.json"
SETTINGS_FILE = "settings.json"

MAX_HISTORY_LINES = 200
MAX_NOTIFICATIONS = 100
CONNECT_TIMEOUT = 1.5  # فحص اتصال سريع (ثوانٍ)
ZK_TIMEOUT = 5

# ---------------------------------------------------------------------------
# قفل مستقل لكل جهاز (بعنوان IP) - يمنع تعارض أكثر من اتصال بروتوكول واحد
# في نفس اللحظة على نفس الجهاز (مثلاً: مزامنة مجدولة + مزامنة يدوية في نفس
# الثانية)، وهو خطر حقيقي أصبح ممكنًا بعد تفعيل threaded=True في الخادم.
# أجهزة ZK تدعم جلسة اتصال واحدة فعّالة فقط في نفس الوقت.
# ---------------------------------------------------------------------------
_device_locks = {}
_device_locks_guard = threading.Lock()


def get_device_lock(ip):
    with _device_locks_guard:
        if ip not in _device_locks:
            _device_locks[ip] = threading.Lock()
        return _device_locks[ip]


# ---------------------------------------------------------------------------
# أدوات عامة لتخزين JSON
# ---------------------------------------------------------------------------

def _path(name):
    return os.path.join(BASE_DIR, name)


def _load_json(name, default):
    path = _path(name)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save_json(name, data):
    with open(_path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# الأجهزة
# ---------------------------------------------------------------------------

def load_devices():
    devices = _load_json(DEVICES_FILE, None)
    if devices is None:
        devices = []
        _save_json(DEVICES_FILE, devices)
    return devices


def save_devices(devices):
    _save_json(DEVICES_FILE, devices)


def load_state():
    return _load_json(STATE_FILE, {"active_device_id": None})


def save_state(state):
    _save_json(STATE_FILE, state)


def get_device_by_id(device_id):
    for d in load_devices():
        if d["id"] == device_id:
            return d
    return None


def get_active_device():
    devices = load_devices()
    state = load_state()
    active_id = state.get("active_device_id")
    for d in devices:
        if d["id"] == active_id:
            return d
    if devices:
        state["active_device_id"] = devices[0]["id"]
        save_state(state)
        return devices[0]
    return None


def set_device_field(device_id, **fields):
    devices = load_devices()
    changed = False
    for d in devices:
        if d["id"] == device_id:
            d.update(fields)
            changed = True
            break
    if changed:
        save_devices(devices)


# ---------------------------------------------------------------------------
# فحص الاتصال بالجهاز (سريع) وقراءة وقته
# ---------------------------------------------------------------------------

def check_connectivity(ip, timeout=CONNECT_TIMEOUT):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip.strip(), 4370))
        s.close()
        return True
    except Exception:
        return False


def fetch_device_time(ip, comm_key=0):
    """يرجع وقت الجهاز الحالي، أو None لو تعذر الاتصال."""
    with get_device_lock(ip):
        conn = None
        try:
            zk = ZK(ip, port=4370, timeout=ZK_TIMEOUT, password=int(comm_key or 0), force_udp=True, ommit_ping=True)
            conn = zk.connect()
            return conn.get_time()
        except Exception:
            return None
        finally:
            if conn:
                try:
                    conn.disconnect()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# كشف تغيير الوقت اليدوي على الجهاز (ميزة مستقلة عن الجدولة)
# ---------------------------------------------------------------------------

DRIFT_THRESHOLD_SECONDS = 30      # فرق أكبر من 30 ثانية يُعتبر مشبوهًا
DRIFT_REMINDER_HOURS = 1          # أثناء استمرار الانحراف، تذكير كل ساعة


def check_drift_for_device(d, device_time_obj):
    """يقارن وقت الجهاز بوقت الهاتف بمنطق ذكي يتتبّع حالة التطابق:
    - أول لحظة يظهر فيها انحراف جديد (بعد ما كان متطابقًا) → تنبيه فوري.
    - أثناء استمرار الانحراف (لم يُصحَّح بعد) → تذكير كل ساعة فقط.
    - بمجرد ما يرجع الوقت متطابقًا → تُصفَّر الحالة، وأي انحراف جديد
      بعد كده يُعامَل كأول مرة (تنبيه فوري تاني).
    d: قاموس الجهاز (سيُعدَّل في مكانه أيضًا لضمان اتساقه مع الحفظ اللاحق
    الذي يقوم به المستدعي). device_time_obj: datetime لوقت الجهاز.
    يرجّع نص رسالة التنبيه لو تم إرسال شيء، أو None.

    ملاحظة أمان تزامن مهمة: هذه الدالة قد تُستدعى من أكثر من مسار في نفس
    اللحظة تقريبًا (استطلاع الواجهة الأمامية كل 30 ثانية + نبضة وضع المراقبة
    المستمرة كل دقيقة) - فلو اعتمدنا فقط على القيمة الممرَّرة في d (التي
    رُبما قُرئت من القرص قبل لحظات من مسار آخر)، قد يقرر المساران معًا أن
    هذا "انحراف جديد" ويرسل كل منهما تنبيهًا منفصلاً لنفس الحدث. لتفادي هذا
    تمامًا، القرار والكتابة يحصلان معًا تحت قفل الجهاز نفسه، وبالاعتماد على
    قراءة طازجة من القرص مباشرة تحت هذا القفل - وليس القيمة القديمة المحتملة
    في d."""
    now = datetime.now()
    diff_seconds = abs((device_time_obj - now).total_seconds())

    with get_device_lock(d["ip"]):
        fresh_devices = load_devices()
        fresh = next((x for x in fresh_devices if x["id"] == d["id"]), d)
        was_drifting = fresh.get("drift_active", False)

        if diff_seconds <= DRIFT_THRESHOLD_SECONDS:
            if was_drifting:
                fresh["drift_active"] = False  # رجع الوقت متطابقًا - تصفير الحالة بصمت
                save_devices(fresh_devices)
                d["drift_active"] = False
            return None

        minutes = int(diff_seconds // 60)

        if not was_drifting:
            # انحراف جديد ظهر بعد ما كان متطابقًا - تنبيه فوري
            message = (
                f"⚠️ رُصد فرق كبير (~{minutes} دقيقة) بين وقت جهاز ({d['name']}) ووقت الهاتف — "
                f"قد يكون أحدهم غيّر الوقت يدويًا من الجهاز مباشرة."
            )
            add_notification(d["name"], "تغيير وقت غير متوقع", message)
            fresh["drift_active"] = True
            fresh["last_drift_alert"] = now.strftime("%Y-%m-%d %H:%M:%S")
            save_devices(fresh_devices)
            d["drift_active"] = True
            d["last_drift_alert"] = fresh["last_drift_alert"]
            return message

        # الانحراف مستمر - تذكير كل ساعة فقط
        last_alert_str = fresh.get("last_drift_alert")
        if last_alert_str:
            try:
                last_alert = datetime.strptime(last_alert_str, "%Y-%m-%d %H:%M:%S")
                if now - last_alert < timedelta(hours=DRIFT_REMINDER_HOURS):
                    d["drift_active"] = True
                    d["last_drift_alert"] = last_alert_str
                    return None
            except Exception:
                pass

        message = (
            f"⚠️ ما زال وقت جهاز ({d['name']}) غير مطابق (~{minutes} دقيقة) — "
            f"لم يُصحَّح بعد."
        )
        add_notification(d["name"], "تذكير: تغيير وقت مستمر", message)
        fresh["last_drift_alert"] = now.strftime("%Y-%m-%d %H:%M:%S")
        save_devices(fresh_devices)
        d["drift_active"] = True
        d["last_drift_alert"] = fresh["last_drift_alert"]
        return message


def get_devices_with_live_status(focus_device_id=None, full_all=False):
    """يفحص كل الأجهزة، لكن يجلب الوقت الكامل (اتصال ببروتوكول الجهاز)
    فقط للجهاز المُركَّز عليه حاليًا في الكاروسيل (focus_device_id) أو
    الجهاز النشط كاحتياطي - أما باقي الأجهزة فيُكتفى بفحص اتصال خفيف
    (فتح منفذ فقط) لتقليل الحمل على الشبكة والبطارية.
    full_all=True: يجلب الوقت الكامل لكل الأجهزة دفعة واحدة (يُستخدم في
    "وضع المراقبة المستمرة" الذي يشغّل Foreground Service مستمرًا، حيث الدقة أهم من
    توفير البطارية لأن ثمن الإشعار الثابت مدفوع بالفعل).
    يرجّع (قائمة الأجهزة، قائمة رسائل تنبيه جديدة) - الرسائل الجديدة تُستخدم
    لعرض إشعار نظام حقيقي حتى أثناء بقاء التطبيق مفتوحًا في الواجهة الأمامية
    (وليس فقط من مهمة الخلفية)."""
    devices = load_devices()
    state = load_state()
    active_id = state.get("active_device_id")
    target_id = focus_device_id or active_id
    result = []
    alerts = []
    changed = False

    for d in devices:
        online = check_connectivity(d["ip"])
        device_time = None
        prev_status = d.get("last_status")

        if online and (full_all or d["id"] == target_id):
            t = fetch_device_time(d["ip"], d.get("comm_key", 0))
            if t:
                device_time = t.strftime("%Y-%m-%d %H:%M:%S")
                d["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                changed = True
                drift_msg = check_drift_for_device(d, t)
                if drift_msg:
                    changed = True
                    alerts.append(drift_msg)
            else:
                online = False  # اتصال TCP نجح لكن بروتوكول الجهاز لم يستجب
        elif online:
            d["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            changed = True

        new_status = "online" if online else "offline"
        if prev_status == "online" and new_status == "offline":
            msg = f"⚠️ انقطع الاتصال بالجهاز ({d['name']})"
            add_notification(d["name"], "انقطاع اتصال", msg)
            alerts.append(msg)
        if d.get("last_status") != new_status:
            d["last_status"] = new_status
            changed = True

        result.append({
            "id": d["id"],
            "name": d["name"],
            "ip": d["ip"],
            "active": d["id"] == active_id,
            "status": new_status,
            "device_time": device_time,
            "last_seen": d.get("last_seen"),
        })

    if changed:
        save_devices(devices)

    return result, alerts


# ---------------------------------------------------------------------------
# سجل العمليات
# ---------------------------------------------------------------------------

def log_action(device_name, action_label, message):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {device_name} | {action_label} | {message}\n"
    path = _path(HISTORY_FILE)
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    lines.append(line)
    lines = lines[-MAX_HISTORY_LINES:]
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def read_history(limit=50):
    path = _path(HISTORY_FILE)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = [l.strip() for l in f.readlines() if l.strip()]
    return list(reversed(lines[-limit:]))


# ---------------------------------------------------------------------------
# الإشعارات
# ---------------------------------------------------------------------------

def add_notification(device_name, ntype, message):
    notifications = _load_json(NOTIFICATIONS_FILE, [])
    notifications.append({
        "id": str(uuid.uuid4())[:8],
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "device_name": device_name,
        "type": ntype,
        "message": message,
        "read": False,
    })
    notifications = notifications[-MAX_NOTIFICATIONS:]
    _save_json(NOTIFICATIONS_FILE, notifications)


def get_notifications():
    return list(reversed(_load_json(NOTIFICATIONS_FILE, [])))


def mark_notifications_read():
    notifications = _load_json(NOTIFICATIONS_FILE, [])
    for n in notifications:
        n["read"] = True
    _save_json(NOTIFICATIONS_FILE, notifications)


def clear_notifications():
    _save_json(NOTIFICATIONS_FILE, [])


# ---------------------------------------------------------------------------
# الجدولة (لكل جهاز/مجموعة أجهزة على حدة) - الإعدادات فقط، التنفيذ الفعلي
# من WorkManager في Kotlin
# ---------------------------------------------------------------------------
# كل عنصر: {id, name, device_ids: [...], enabled, use_times, times: [...],
#           use_interval, interval_minutes}

def load_schedules():
    return _load_json(SCHEDULE_FILE, [])


def save_schedules(data):
    _save_json(SCHEDULE_FILE, data)


def get_schedule_by_id(schedule_id):
    for s in load_schedules():
        if s["id"] == schedule_id:
            return s
    return None


# ---------------------------------------------------------------------------
# الإعدادات العامة (قفل التطبيق، الوضع الليلي)
# ---------------------------------------------------------------------------

def default_settings():
    return {
        "app_lock_enabled": False,
        "dark_mode": False,
        "continuous_monitoring_enabled": False,
    }


def load_settings():
    s = default_settings()
    s.update(_load_json(SETTINGS_FILE, {}))
    return s


def save_settings(data):
    _save_json(SETTINGS_FILE, data)


# ---------------------------------------------------------------------------
# مساعد: اسم ملف تصدير آمن يدعم العربية (RFC 5987)
# ---------------------------------------------------------------------------

def build_content_disposition(filename):
    # الاحتياط: بعض متصفحات/أدوات تنزيل أندرويد لا تقرأ filename* (UTF-8)
    # وتكتفي بـ filename= العادي، لذلك نجعل الاسم الاحتياطي بنفس الامتداد الصحيح
    # حتى لا يفقد الملف صيغته حتى لو تجاهلت الأداة الاسم العربي الكامل.
    ext = filename.rsplit('.', 1)[-1] if '.' in filename else 'dat'
    ascii_fallback = f"export.{ext}"
    encoded = quote(filename)
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


# ===========================================================================
# الصفحة الرئيسية (تصميم صفحة واحدة SPA)
# ===========================================================================

@app.route('/')
def index():
    html_path = os.path.join(os.path.dirname(__file__), "web", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


@app.route('/static/<path:filename>')
def static_files(filename):
    static_dir = os.path.join(os.path.dirname(__file__), "web")
    return send_from_directory(static_dir, filename)


# ===========================================================================
# API: الأجهزة
# ===========================================================================

@app.route('/api/devices', methods=['GET'])
def api_devices():
    focus_id = request.args.get('focus')
    devices, alerts = get_devices_with_live_status(focus_id)
    return jsonify({"devices": devices, "new_alerts": alerts})


@app.route('/api/devices/test', methods=['POST'])
def api_devices_test():
    ip = (request.json or {}).get('ip', '').strip()
    reachable = check_connectivity(ip)
    return jsonify({"reachable": reachable})


@app.route('/api/devices', methods=['POST'])
def api_devices_add():
    data = request.json or {}
    name = data.get('name', '').strip()
    ip = data.get('ip', '').strip()
    if not name or not ip:
        return jsonify({"success": False, "message": "الاسم وعنوان IP مطلوبان"}), 400

    try:
        comm_key = int(data.get('comm_key', 0) or 0)
    except (TypeError, ValueError):
        comm_key = 0

    devices = load_devices()
    new_device = {"id": str(uuid.uuid4())[:8], "name": name, "ip": ip, "comm_key": comm_key, "last_status": None, "last_seen": None}
    devices.append(new_device)
    save_devices(devices)

    state = load_state()
    if not state.get("active_device_id"):
        state["active_device_id"] = new_device["id"]
        save_state(state)

    return jsonify({"success": True, "device": new_device})


@app.route('/api/devices/<device_id>', methods=['DELETE'])
def api_devices_delete(device_id):
    devices = load_devices()
    remaining = [d for d in devices if d["id"] != device_id]

    if len(remaining) == len(devices):
        return jsonify({"success": False, "message": "الجهاز غير موجود"}), 404

    save_devices(remaining)

    state = load_state()
    if state.get("active_device_id") == device_id:
        state["active_device_id"] = remaining[0]["id"] if remaining else None
        save_state(state)

    return jsonify({"success": True})


@app.route('/api/devices/<device_id>/activate', methods=['POST'])
def api_devices_activate(device_id):
    device = get_device_by_id(device_id)
    if not device:
        return jsonify({"success": False, "message": "الجهاز غير موجود"}), 404
    state = load_state()
    state["active_device_id"] = device_id
    save_state(state)
    return jsonify({"success": True})


# ===========================================================================
# API: الموظفون (إضافة + قائمة + بحث)
# ===========================================================================

@app.route('/api/employees', methods=['GET'])
def api_employees_list():
    device_id = request.args.get('device_id')
    query = request.args.get('q', '').strip().lower()

    device = get_device_by_id(device_id) if device_id else get_active_device()
    if not device:
        return jsonify({"success": False, "message": "لا يوجد جهاز محدد"}), 400
    if not check_connectivity(device["ip"]):
        return jsonify({"success": False, "message": f"الجهاز ({device['name']}) غير متصل حاليًا"}), 400

    try:
        with get_device_lock(device["ip"]):
            conn = None
            try:
                zk = ZK(device["ip"], port=4370, timeout=ZK_TIMEOUT, password=int(device.get("comm_key", 0) or 0), force_udp=True, ommit_ping=True)
                conn = zk.connect()
                users = conn.get_users()
            finally:
                if conn:
                    conn.disconnect()
    except Exception as e:
        return jsonify({"success": False, "message": f"تعذّر الاتصال: {e}"}), 500

    result = []
    for u in users:
        if query and query not in str(u.uid).lower() and query not in (u.name or "").lower():
            continue
        result.append({"uid": u.uid, "name": u.name, "privilege": u.privilege})

    return jsonify({"success": True, "employees": result, "count": len(result)})


@app.route('/api/employees', methods=['POST'])
def api_employees_add():
    data = request.json or {}
    device_id = data.get('device_id')
    device = get_device_by_id(device_id) if device_id else get_active_device()
    if not device:
        return jsonify({"success": False, "message": "لا يوجد جهاز محدد"}), 400
    if not check_connectivity(device["ip"]):
        return jsonify({"success": False, "message": f"الجهاز ({device['name']}) غير متصل حاليًا"}), 400

    try:
        uid = int(data.get('uid'))
        name = data.get('name', '').strip()
        password = data.get('password', '')
        privilege = int(data.get('privilege', 0))
        confirm_overwrite = bool(data.get('confirm_overwrite', False))

        with get_device_lock(device["ip"]):
            conn = None
            try:
                zk = ZK(device["ip"], port=4370, timeout=ZK_TIMEOUT, password=int(device.get("comm_key", 0) or 0), force_udp=True, ommit_ping=True)
                conn = zk.connect()

                if not confirm_overwrite:
                    existing = next((u for u in conn.get_users() if u.uid == uid), None)
                    if existing:
                        return jsonify({
                            "success": False,
                            "uid_exists": True,
                            "existing_name": existing.name,
                            "message": f"رقم المعرّف ({uid}) مستخدم بالفعل للموظف ({existing.name}) — الحفظ سيكتب فوق بياناته."
                        }), 409

                conn.set_user(uid=uid, name=name, privilege=privilege, password=password)
            finally:
                if conn:
                    conn.disconnect()

        log_action(device["name"], "إضافة موظف", f"✓ تمت إضافة ({name}) برقم {uid}")
        return jsonify({"success": True, "message": f"تمت إضافة الموظف ({name}) بنجاح"})
    except Exception as e:
        log_action(device["name"], "إضافة موظف", f"✕ فشل: {e}")
        return jsonify({"success": False, "message": f"فشل: {e}"}), 500


# ===========================================================================
# API: الحضور (عرض فقط) + التصدير (ملف فعلي)
# ===========================================================================

def _fetch_attendance_filtered(device, start_str, end_str, query=""):
    with get_device_lock(device["ip"]):
        conn = None
        try:
            zk = ZK(device["ip"], port=4370, timeout=ZK_TIMEOUT, password=int(device.get("comm_key", 0) or 0), force_udp=True, ommit_ping=True)
            conn = zk.connect()
            attendance = conn.get_attendance()
        finally:
            if conn:
                conn.disconnect()

    if start_str and end_str:
        start_dt = datetime.strptime(start_str, '%Y-%m-%d')
        end_dt = datetime.strptime(end_str, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
        attendance = [a for a in attendance if start_dt <= a.timestamp <= end_dt]

    if query:
        q = query.strip().lower()
        attendance = [
            a for a in attendance
            if q in str(a.user_id).lower() or q in str(a.uid).lower()
        ]

    attendance.sort(key=lambda a: a.timestamp, reverse=True)
    return attendance


@app.route('/api/attendance/view', methods=['GET'])
def api_attendance_view():
    device_id = request.args.get('device_id')
    start_str = request.args.get('start', '')
    end_str = request.args.get('end', '')
    query = request.args.get('q', '')

    device = get_device_by_id(device_id) if device_id else get_active_device()
    if not device:
        return jsonify({"success": False, "message": "لا يوجد جهاز محدد"}), 400
    if not check_connectivity(device["ip"]):
        return jsonify({"success": False, "message": f"الجهاز ({device['name']}) غير متصل حاليًا"}), 400

    try:
        attendance = _fetch_attendance_filtered(device, start_str, end_str, query)
    except Exception as e:
        return jsonify({"success": False, "message": f"تعذّر الاتصال: {e}"}), 500

    records = [{
        "user_id": a.user_id,
        "uid": a.uid,
        "timestamp": a.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "status": a.status,
    } for a in attendance]

    return jsonify({"success": True, "records": records, "count": len(records)})


@app.route('/api/attendance/export', methods=['GET'])
def api_attendance_export():
    """محفوظ للتوافق القديم فقط - يُفضَّل استخدام prepare + download (أسفل)
    لأنهما يفصلان الاتصال البطيء بالجهاز عن التنزيل الفعلي نفسه."""
    device_id = request.args.get('device_id')
    start_str = request.args.get('start', '')
    end_str = request.args.get('end', '')
    fmt = request.args.get('format', 'xlsx')

    device = get_device_by_id(device_id) if device_id else get_active_device()
    if not device:
        return Response("لا يوجد جهاز محدد", status=400)
    if not check_connectivity(device["ip"]):
        return Response(f"الجهاز ({device['name']}) غير متصل حاليًا", status=400)

    try:
        attendance = _fetch_attendance_filtered(device, start_str, end_str, "")
    except Exception as e:
        log_action(device["name"], "تصدير حضور", f"✕ فشل: {e}")
        return Response(f"تعذّر الاتصال: {e}", status=500)

    filename, mimetype, content = _build_export_file(device, attendance, start_str, end_str, fmt)
    log_action(device["name"], "تصدير حضور", f"✓ {fmt} — {len(attendance)} سجل")
    return Response(content, mimetype=mimetype, headers={"Content-Disposition": build_content_disposition(filename)})


def _build_export_file(device, attendance, start_str, end_str, fmt):
    """يبني محتوى الملف (بدون أي اتصال بالجهاز - البيانات جاهزة بالفعل)."""
    period_label = f"{start_str}_إلى_{end_str}" if start_str and end_str else "كل_السجلات"
    safe_device_name = device["name"].replace(" ", "_")
    base_filename = f"{safe_device_name}_{period_label}"

    header = ["ID الموظف", "UID", "الوقت", "الحالة"]
    rows = [[a.user_id, a.uid, a.timestamp.strftime("%Y-%m-%d %H:%M:%S"), a.status] for a in attendance]

    if fmt == 'csv':
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        writer.writerows(rows)
        content = ('\ufeff' + buf.getvalue()).encode('utf-8')  # BOM لدعم العربية في إكسل
        return f"{base_filename}.csv", "text/csv", content

    elif fmt == 'xlsx':
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "الحضور"
        ws.append(header)
        for row in rows:
            ws.append(row)
        bio = io.BytesIO()
        wb.save(bio)
        return (f"{base_filename}.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                bio.getvalue())

    else:  # dat
        lines = ["\t".join(header)] + ["\t".join(str(c) for c in row) for row in rows]
        content = "\n".join(lines).encode('utf-8')
        return f"{base_filename}.dat", "application/octet-stream", content


# تخزين مؤقت للملفات المُجهَّزة بانتظار التنزيل الفعلي (يفصل الاتصال البطيء
# بالجهاز عن التنزيل نفسه، ويمنع تعليق DownloadManager أثناء انتظار الجهاز)
_export_cache = {}
_EXPORT_TOKEN_TTL_SECONDS = 300  # 5 دقائق كحد أقصى للاحتفاظ بالملف الجاهز


@app.route('/api/attendance/export/prepare', methods=['POST'])
def api_attendance_export_prepare():
    """الخطوة الأولى: تتصل بالجهاز وتجهّز الملف بالكامل في الذاكرة، وترجع
    token فقط (بدون بدء أي تنزيل بعد) - لو الجهاز غير متصل، ترجع خطأ واضح
    فورًا بدل ما تترك أداة التنزيل معلّقة تستنى."""
    data = request.json or {}
    device_id = data.get('device_id')
    start_str = data.get('start', '')
    end_str = data.get('end', '')
    fmt = data.get('format', 'xlsx')

    device = get_device_by_id(device_id) if device_id else get_active_device()
    if not device:
        return jsonify({"success": False, "message": "لا يوجد جهاز محدد"}), 400
    if not check_connectivity(device["ip"]):
        return jsonify({"success": False, "message": f"الجهاز ({device['name']}) غير متصل حاليًا", "offline": True}), 400

    try:
        attendance = _fetch_attendance_filtered(device, start_str, end_str, "")
        filename, mimetype, content = _build_export_file(device, attendance, start_str, end_str, fmt)
    except Exception as e:
        log_action(device["name"], "تصدير حضور", f"✕ فشل: {e}")
        return jsonify({"success": False, "message": f"تعذّر الاتصال: {e}"}), 500

    token = str(uuid.uuid4())
    _export_cache[token] = {
        "filename": filename, "mimetype": mimetype, "content": content,
        "created": datetime.now(),
    }
    log_action(device["name"], "تصدير حضور", f"✓ {fmt} — {len(attendance)} سجل (جاهز للتنزيل)")

    # تنظيف أي رموز قديمة منتهية الصلاحية
    now = datetime.now()
    expired = [t for t, v in _export_cache.items() if (now - v["created"]).total_seconds() > _EXPORT_TOKEN_TTL_SECONDS]
    for t in expired:
        _export_cache.pop(t, None)

    return jsonify({"success": True, "token": token, "filename": filename})


@app.route('/api/attendance/export/download/<token>/<path:filename>', methods=['GET'])
def api_attendance_export_download(token, filename):
    """الخطوة الثانية: تنزيل فوري وسريع للملف الجاهز بالفعل - بدون أي
    اتصال بجهاز البصمة في هذه اللحظة، فلا يوجد احتمال تعليق.

    التوكن يبقى صالحًا حتى انتهاء مهلته الزمنية (وليس لاستخدام واحد فقط) -
    لأن أدوات التنزيل في أندرويد (DownloadManager) أحيانًا تُنشئ أكثر من
    طلب واحد لنفس الرابط (تحقق مبدئي + إعادة محاولة تلقائية)، وحذف الملف
    من الذاكرة بعد أول طلب فقط كان يسبب فشل الطلبات التالية بلا داعٍ.
    اسم الملف مُضمَّن في مسار الرابط نفسه (وليس فقط في ترويسة
    Content-Disposition) لضمان وصوله بشكل صحيح لأداة التنزيل في كل الحالات."""
    entry = _export_cache.get(token)
    if not entry:
        return Response("انتهت صلاحية رابط التنزيل، أعد التصدير من جديد", status=404)

    return Response(
        entry["content"],
        mimetype=entry["mimetype"],
        headers={"Content-Disposition": build_content_disposition(entry["filename"])}
    )


# ===========================================================================
# API: المزامنة
# ===========================================================================

def _sync_single_device(d):
    """ينفّذ مزامنة جهاز واحد بوقت الهاتف. يرجّع (نجح: bool, رسالة: str)."""
    if not check_connectivity(d["ip"]):
        return False, f"⚠️ تعذّرت مزامنة ({d['name']}) — الجهاز غير متصل"
    with get_device_lock(d["ip"]):
        conn = None
        try:
            zk = ZK(d["ip"], port=4370, timeout=ZK_TIMEOUT, password=int(d.get("comm_key", 0) or 0), force_udp=True, ommit_ping=True)
            conn = zk.connect()
            conn.set_time(datetime.now())

            # تصفير فورية لعلامة "الانحراف" بعد نجاح المزامنة مباشرة - بدون
            # هذا، يبقى الإشعار يقول "غير متزامن" حتى دورة الفحص القادمة
            # (قد تصل لدقيقة كاملة) رغم أن الوقت صحيح فعليًا بالفعل
            all_devices = load_devices()
            for x in all_devices:
                if x["id"] == d["id"] and x.get("drift_active"):
                    x["drift_active"] = False
                    save_devices(all_devices)
                    break

            return True, f"✓ تمت مزامنة ({d['name']}) مع وقت الهاتف"
        except Exception as e:
            return False, f"⚠️ تعذّرت مزامنة ({d['name']}): {e}"
        finally:
            if conn:
                try:
                    conn.disconnect()
                except Exception:
                    pass


def sync_devices_collect_alerts(device_ids=None):
    """يزامن أجهزة محددة (أو كل الأجهزة لو device_ids=None)، ويسجّل كل
    نتيجة، ويرجّع فقط رسائل الفشل (تُستخدم لإرسال إشعارات نظام من الخلفية)."""
    devices = load_devices()
    if device_ids is not None:
        devices = [d for d in devices if d["id"] in device_ids]
    alerts = []
    for d in devices:
        ok, msg = _sync_single_device(d)
        log_action(d["name"], "مزامنة", msg)
        if not ok:
            add_notification(d["name"], "فشل مزامنة", msg)
            alerts.append(msg)
    return alerts


@app.route('/api/sync/all', methods=['POST'])
def api_sync_all():
    devices = load_devices()
    success, failed = [], []
    for d in devices:
        ok, msg = _sync_single_device(d)
        log_action(d["name"], "مزامنة", msg)
        if ok:
            success.append(d["name"])
        else:
            failed.append(d["name"])
            add_notification(d["name"], "فشل مزامنة", msg)

    return jsonify({"success": True, "synced": success, "failed": failed})


@app.route('/api/sync/device/<device_id>', methods=['POST'])
def api_sync_device(device_id):
    device = get_device_by_id(device_id)
    if not device:
        return jsonify({"success": False, "message": "الجهاز غير موجود"}), 404

    ok, msg = _sync_single_device(device)
    log_action(device["name"], "مزامنة", msg)
    if not ok:
        add_notification(device["name"], "فشل مزامنة", msg)
        return jsonify({"success": False, "message": msg}), 400
    return jsonify({"success": True, "message": msg})


@app.route('/api/sync/custom', methods=['POST'])
def api_sync_custom():
    data = request.json or {}
    device_id = data.get('device_id')
    dt_str = data.get('datetime')

    device = get_device_by_id(device_id)
    if not device:
        return jsonify({"success": False, "message": "الجهاز غير موجود"}), 404
    if not check_connectivity(device["ip"]):
        return jsonify({"success": False, "message": f"الجهاز ({device['name']}) غير متصل حاليًا"}), 400

    try:
        custom_dt = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M')
        with get_device_lock(device["ip"]):
            conn = None
            try:
                zk = ZK(device["ip"], port=4370, timeout=ZK_TIMEOUT, password=int(device.get("comm_key", 0) or 0), force_udp=True, ommit_ping=True)
                conn = zk.connect()
                conn.set_time(custom_dt)
            finally:
                if conn:
                    conn.disconnect()
        log_action(device["name"], "تعيين وقت مخصص", f"✓ إلى {custom_dt.strftime('%Y-%m-%d %H:%M')}")
        return jsonify({"success": True, "message": f"تم ضبط وقت ({device['name']}) بنجاح"})
    except Exception as e:
        log_action(device["name"], "تعيين وقت مخصص", f"✕ فشل: {e}")
        return jsonify({"success": False, "message": f"فشل: {e}"}), 500


# ===========================================================================
# تُستدعى مباشرة من WorkManager (Kotlin) بدون المرور عبر خادم Flask -
# تُستخدم للمزامنة المجدولة حتى لو كان التطبيق مغلقًا بالكامل.
# ===========================================================================

def run_fixed_time_sync(base_dir, schedule_id=None, time_str=None):
    """تُستدعى من WorkManager (Kotlin) عند حلول أحد الأوقات الثابتة لمجموعة
    جدولة معيّنة. تزامن فقط أجهزة تلك المجموعة."""
    global BASE_DIR
    BASE_DIR = base_dir
    sched = get_schedule_by_id(schedule_id) if schedule_id else None
    if not sched or not sched.get("enabled", True):
        return json.dumps([], ensure_ascii=False)
    alerts = sync_devices_collect_alerts(sched.get("device_ids"))
    return json.dumps(alerts, ensure_ascii=False)


# ===========================================================================
# وضع "المراقبة المستمرة" - حلقة تنفيذ دقيقة داخل Foreground Service حقيقي
# (بدل الاعتماد على WorkManager)، تفحص كل جدولة "فترة تكرار" في موعدها
# بالضبط وتفحص انحراف الوقت بشكل متكرر. الأوقات الثابتة تبقى دائمًا على
# WorkManager العادي بشكل مستقل تمامًا (لا تحتاج دقة إضافية أصلاً).
# فترة التكرار وكشف الانحراف في الخلفية أصبحا حصريًا هنا فقط - لا وجود
# لأي "نبضة موحّدة تقريبية" بديلة لهما بعد الآن.
# ===========================================================================

def run_precise_tick(base_dir):
    """تُستدعى من خدمة المراقبة المستمرة (ContinuousMonitoringService) كل
    دقيقة تقريبًا. تُرجع JSON فيه: alerts (رسائل تنبيه جديدة) + summary
    (حالة تزامن كل الأجهزة لعرضها في محتوى الإشعار)."""
    global BASE_DIR
    BASE_DIR = base_dir
    alerts = []
    now = datetime.now()

    # 1) الأوقات الثابتة - فحص دقيق للدقيقة الحالية بالضبط
    fired_state = _load_json("precise_fired_state.json", {})
    fired_changed = False
    current_hm = now.strftime("%H:%M")
    now_minute_key = now.strftime("%Y-%m-%d %H:%M")

    for sched in load_schedules():
        if not sched.get("enabled", True):
            continue
        sid = sched["id"]

        if sched.get("use_times") and current_hm in sched.get("times", []):
            fire_key = f"{sid}_{now_minute_key}"
            if not fired_state.get(fire_key):
                alerts += sync_devices_collect_alerts(sched.get("device_ids"))
                fired_state[fire_key] = True
                fired_changed = True

        # 2) فترة التكرار - نفس منطق النبضة العادية لكن يُفحص كل دقيقة
        # بدل كل 15-30 دقيقة، فالدقة أعلى بكثير
        if sched.get("use_interval"):
            tick_state = _load_json("interval_tick_state.json", {})
            last_str = tick_state.get(sid)
            due = True
            if last_str:
                try:
                    last_dt = datetime.strptime(last_str, "%Y-%m-%d %H:%M:%S")
                    due = (now - last_dt).total_seconds() >= sched.get("interval_minutes", 60) * 60
                except Exception:
                    due = True
            if due:
                alerts += sync_devices_collect_alerts(sched.get("device_ids"))
                tick_state[sid] = now.strftime("%Y-%m-%d %H:%M:%S")
                _save_json("interval_tick_state.json", tick_state)

    if fired_changed:
        # تنظيف دوري لمفاتيح "تم التنفيذ" القديمة حتى لا يكبر الملف بلا حدود
        cutoff = now - timedelta(minutes=5)
        pruned = {}
        for k, v in fired_state.items():
            try:
                ts = datetime.strptime(k.split("_", 1)[1], "%Y-%m-%d %H:%M")
                if ts >= cutoff:
                    pruned[k] = v
            except Exception:
                continue
        _save_json("precise_fired_state.json", pruned)

    # 3) كشف انحراف الوقت + تحديث حالة كل الأجهزة (لمحتوى الإشعار). الكشف
    # يعمل تلقائيًا دائمًا الآن (لا يوجد مفتاح تفعيل/إيقاف منفصل)، فالاتصال
    # الكامل بكل الأجهزة يحصل في كل نبضة طالما وضع المراقبة المستمرة نفسه
    # مفعّل (ثمن الإشعار الثابت مدفوع بالفعل، فالدقة الكاملة منطقية هنا).
    devices_status, drift_alerts = get_devices_with_live_status(full_all=True)
    alerts += drift_alerts

    online_count = sum(1 for d in devices_status if d["status"] == "online")

    # حالة التزامن الفعلية (وليست مجرد الاتصال) - بالاعتماد على علم
    # drift_active المحفوظ لكل جهاز في devices.json من check_drift_for_device.
    # "غير متصل" و"منحرف" حالتان منفصلتان تمامًا - جهاز غير متصل لا يُعتبر
    # "متزامنًا" أبدًا حتى لو لم يكن مسجَّلاً كمنحرف (لأننا ببساطة لا نملك
    # طريقة للتأكد من وقته الحالي وهو غير متصل).
    devices_map = {d["id"]: d for d in load_devices()}
    offline_names = [d["name"] for d in devices_status if d["status"] == "offline"]
    drifted_names = [
        d["name"] for d in devices_status
        if d["status"] == "online" and devices_map.get(d["id"], {}).get("drift_active")
    ]

    summary = {
        "online_count": online_count,
        "total": len(devices_status),
        "offline_names": offline_names,
        "drifted_names": drifted_names,
        "devices": [
            {
                "id": d["id"], "name": d["name"], "status": d["status"],
                "last_seen": d.get("device_time") or d.get("last_seen") or "",
            }
            for d in devices_status
        ],
    }

    return json.dumps({"alerts": alerts, "summary": summary}, ensure_ascii=False)


def run_single_device_sync(base_dir, device_id):
    """مزامنة جهاز واحد فقط - تُستدعى من زر المزامنة الصغير داخل الإشعار
    الموسّع، بدون فتح التطبيق."""
    global BASE_DIR
    BASE_DIR = base_dir
    device = get_device_by_id(device_id)
    if not device:
        return json.dumps({"success": False, "message": "الجهاز غير موجود"}, ensure_ascii=False)

    ok, msg = _sync_single_device(device)
    log_action(device["name"], "مزامنة", msg)
    if not ok:
        add_notification(device["name"], "فشل مزامنة", msg)
    return json.dumps({"success": ok, "message": msg}, ensure_ascii=False)


# ===========================================================================
# API: الجدولة (مجموعات - كل مجموعة مرتبطة بجهاز واحد أو أكثر)
# ===========================================================================

@app.route('/api/schedules', methods=['GET'])
def api_schedules_get():
    return jsonify({"schedules": load_schedules()})


@app.route('/api/schedules', methods=['POST'])
def api_schedules_create():
    data = request.json or {}
    device_ids = data.get("device_ids", [])
    if not device_ids:
        return jsonify({"success": False, "message": "اختر جهازًا واحدًا على الأقل"}), 400

    schedules = load_schedules()
    new_schedule = {
        "id": str(uuid.uuid4())[:8],
        "name": data.get("name", "").strip() or "جدولة بدون اسم",
        "device_ids": device_ids,
        "enabled": True,
        "use_times": bool(data.get("use_times", False)),
        "times": data.get("times", []),
        "use_interval": bool(data.get("use_interval", False)),
        "interval_minutes": int(data.get("interval_minutes", 60)),
    }
    schedules.append(new_schedule)
    save_schedules(schedules)
    return jsonify({"success": True, "schedule": new_schedule})


@app.route('/api/schedules/<schedule_id>', methods=['DELETE'])
def api_schedules_delete(schedule_id):
    schedules = load_schedules()
    remaining = [s for s in schedules if s["id"] != schedule_id]
    if len(remaining) == len(schedules):
        return jsonify({"success": False, "message": "الجدولة غير موجودة"}), 404
    save_schedules(remaining)
    return jsonify({"success": True})


@app.route('/api/schedules/<schedule_id>', methods=['PUT'])
def api_schedules_update(schedule_id):
    """تعديل جدولة موجودة (بدلاً من حذفها وإضافة واحدة جديدة)."""
    data = request.json or {}
    device_ids = data.get("device_ids", [])
    if not device_ids:
        return jsonify({"success": False, "message": "اختر جهازًا واحدًا على الأقل"}), 400

    schedules = load_schedules()
    found = None
    for s in schedules:
        if s["id"] == schedule_id:
            s["name"] = data.get("name", "").strip() or "جدولة بدون اسم"
            s["device_ids"] = device_ids
            s["use_times"] = bool(data.get("use_times", False))
            s["times"] = data.get("times", [])
            s["use_interval"] = bool(data.get("use_interval", False))
            s["interval_minutes"] = int(data.get("interval_minutes", 60))
            found = s
            break
    if not found:
        return jsonify({"success": False, "message": "الجدولة غير موجودة"}), 404

    save_schedules(schedules)
    return jsonify({"success": True, "schedule": found})


@app.route('/api/schedules/<schedule_id>/toggle', methods=['POST'])
def api_schedules_toggle(schedule_id):
    """تفعيل/إيقاف فوري (حفظ تلقائي بدون الحاجة لزر حفظ منفصل)."""
    data = request.json or {}
    schedules = load_schedules()
    found = None
    for s in schedules:
        if s["id"] == schedule_id:
            s["enabled"] = bool(data.get("enabled", True))
            found = s
            break
    if not found:
        return jsonify({"success": False, "message": "الجدولة غير موجودة"}), 404
    save_schedules(schedules)
    return jsonify({"success": True, "schedule": found})


# ===========================================================================
# API: الإشعارات
# ===========================================================================

# ===========================================================================
# نقاط داخلية (Internal) - تُستدعى فقط من ContinuousMonitoringService عبر طلب
# شبكة محلي (بدل فتح اتصال بايثون منفصل من خيط مستقل، وهو ما كان يسبب
# تعطّلاً كاملاً للتطبيق نتيجة تعارض في طبقة الربط بين Kotlin وبايثون).
# ===========================================================================

@app.route('/api/internal/precise_tick', methods=['GET'])
def api_internal_precise_tick():
    result_json = run_precise_tick(BASE_DIR)
    return Response(result_json, mimetype="application/json")


@app.route('/api/internal/single_device_sync/<device_id>', methods=['GET'])
def api_internal_single_device_sync(device_id):
    result_json = run_single_device_sync(BASE_DIR, device_id)
    return Response(result_json, mimetype="application/json")


@app.route('/api/notifications', methods=['GET'])
def api_notifications_get():
    return jsonify({"notifications": get_notifications()})


@app.route('/api/notifications/read', methods=['POST'])
def api_notifications_read():
    mark_notifications_read()
    return jsonify({"success": True})


@app.route('/api/notifications/clear', methods=['POST'])
def api_notifications_clear():
    clear_notifications()
    return jsonify({"success": True})


# ===========================================================================
# API: السجل
# ===========================================================================

@app.route('/api/history', methods=['GET'])
def api_history_get():
    return jsonify({"history": read_history(50)})


@app.route('/api/history/clear', methods=['POST'])
def api_history_clear():
    path = _path(HISTORY_FILE)
    if os.path.exists(path):
        os.remove(path)
    return jsonify({"success": True})


# ===========================================================================
# API: الإعدادات
# ===========================================================================

@app.route('/api/settings', methods=['GET'])
def api_settings_get():
    return jsonify(load_settings())


@app.route('/api/settings', methods=['POST'])
def api_settings_set():
    data = request.json or {}
    settings = load_settings()
    settings.update({k: v for k, v in data.items() if k in default_settings()})
    save_settings(settings)
    return jsonify({"success": True, "settings": settings})


# ===========================================================================
# نقطة الدخول
# ===========================================================================

def start(base_dir=None):
    """نقطة الدخول التي يستدعيها تطبيق أندرويد (MainActivity.kt) عبر Chaquopy."""
    global BASE_DIR
    if base_dir:
        BASE_DIR = base_dir
    os.makedirs(BASE_DIR, exist_ok=True)
    try:
        app.run(host="127.0.0.1", port=5000, threaded=True)
    except SystemExit:
        # يحدث لو كان خادم سابق لا يزال شغّالًا فعليًا على نفس المنفذ
        # (مثلاً بعد إعادة إنشاء الشاشة من غير ما تُقفل العملية بالكامل) -
        # هذا متوقع وغير ضار: الخادم الأصلي لا يزال يعمل بشكل طبيعي.
        pass
    except OSError:
        pass


if __name__ == '__main__':
    start()
