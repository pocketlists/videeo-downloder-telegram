# -*- coding: utf-8 -*-
"""App Builder: Android app ke source code (ZIP) ko asli APK me build karta hai.

Flow: /buildapp -> user ZIP bhejta hai -> project pehchano -> customization
buttons (naam / package id) -> Gradle build -> APK wapas.

GitHub Actions ke ubuntu runner par JDK aur Android SDK pehle se hote hain;
jahan kuch missing ho (gradle wrapper jar, SDK platform, debug keystore)
wahi cheez yahin bana/download kar li jati hai.

FIX (v2): Package ID badalne par google-services.json me us package ka client
nahi hota tha -> ":app:processDebugGoogleServices FAILED - No matching client
found for package name ...". Ab build se pehle google-services.json me final
package ke liye client entry automatically add/patch ho jati hai.
"""
import asyncio
import copy
import json
import os
import re
import shutil
import subprocess
import time
import zipfile

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from modules.video_engine import humanbytes, sanitize_filename

WORK_ROOT = os.path.join(os.path.expanduser("~"), ".appbuilder")

OPT_INIT = """
allprojects { p ->
    p.afterEvaluate {
        try {
            def android = p.extensions.getByName('android')
            def rel = android.buildTypes.findByName('release')
            if (rel != null) {
                rel.minifyEnabled = true
                try { rel.shrinkResources = true } catch (Exception ignored) {}
                def sc = rel.signingConfig
                boolean bad = (sc == null) || (sc.storeFile != null && !sc.storeFile.exists())
                if (bad) { rel.signingConfig = android.signingConfigs.getByName('debug') }
            }
        } catch (Exception ignored) {}
    }
}
"""
MAX_ZIP = 200 * 1048576
MAX_EXTRACTED = 900 * 1048576
BUILD_TIMEOUT = 1800
BUILD_LOCK = asyncio.Lock()
WAIT = {}   # chat_id -> {"stage": ..., "dir": ..., "root": ..., "name": ..., "pkg": ...}


def esc(t):
    return (str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _sh(cmd, timeout=120, cwd=None, env=None):
    try:
        # string command (pipe/redirect wali) ko shell se chalao, list ko seedha
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=cwd, env=env, shell=isinstance(cmd, str))
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


def _find_project_root(base):
    def ok(d):
        names = os.listdir(d)
        return ("settings.gradle" in names or "settings.gradle.kts" in names
                or ("build.gradle" in names and "app" in names)
                or ("build.gradle.kts" in names and "app" in names))
    if ok(base):
        return base
    for dirpath, dirnames, _ in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "build"]
        if os.path.abspath(dirpath) != os.path.abspath(base) and ok(dirpath):
            return dirpath
        if dirpath.count(os.sep) - base.count(os.sep) > 2:
            dirnames[:] = []
    return None


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _app_gradle(root):
    for n in ("build.gradle.kts", "build.gradle"):
        p = os.path.join(root, "app", n)
        if os.path.exists(p):
            return p
    return None


def _project_info(root):
    info = {"name": "App", "pkg": "", "sdk": 34, "gradle": "", "suffixes": []}
    gp = _app_gradle(root)
    if gp:
        t = _read(gp)
        m = re.search(r'applicationId\s*=?\s*"([^"]+)"', t)
        if m:
            info["pkg"] = m.group(1)
        # compileSdk = 34 | compileSdk 34 | compileSdkVersion 34
        # compileSdk { version = release(36) { minorApiLevel = 1 } }
        m = (re.search(r"compileSdk(?:Version)?\s*=?\s*(\d+)", t)
             or re.search(r"compileSdk\s*\{[^}]*?release\(\s*(\d+)\s*\)", t, re.S)
             or re.search(r"compileSdk\s*\{[^}]*?version\s*=\s*(\d+)", t, re.S))
        if m:
            info["sdk"] = int(m.group(1))
        info["suffixes"] = re.findall(r'applicationIdSuffix\s*=?\s*"([^"]+)"', t)
    for cand in ("app/src/main/res/values/strings.xml",):
        t = _read(os.path.join(root, cand))
        m = re.search(r'<string name="app_name">([^<]+)</string>', t)
        if m:
            info["name"] = m.group(1).strip()
    if info["name"] == "App":
        # strings.xml na ho to settings.gradle ke rootProject.name se naam lo
        for sn in ("settings.gradle.kts", "settings.gradle"):
            m = re.search(r'rootProject\.name\s*=\s*["\']([^"\']+)["\']', _read(os.path.join(root, sn)))
            if m:
                info["name"] = m.group(1).strip()
                break
    wp = _read(os.path.join(root, "gradle/wrapper/gradle-wrapper.properties"))
    m = re.search(r"gradle-([\d.]+)-(?:bin|all)\.zip", wp)
    if m:
        info["gradle"] = m.group(1)
    return info


def _java_home():
    if os.environ.get("JAVA_HOME") and os.path.exists(os.environ["JAVA_HOME"]):
        return os.environ["JAVA_HOME"]
    if shutil.which("java"):
        return os.path.dirname(os.path.dirname(os.path.realpath(shutil.which("java"))))
    for base in ("/usr/lib/jvm", "/opt/java", "/opt/hostedtoolcache/Java_Temurin-Hotspot_jdk"):
        if os.path.isdir(base):
            for d in sorted(os.listdir(base), reverse=True):
                p = os.path.join(base, d)
                if os.path.exists(os.path.join(p, "bin", "java")):
                    return p
    return ""


def _android_sdk():
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var) and os.path.isdir(os.environ[var]):
            return os.environ[var]
    for p in ("/usr/local/lib/android-sdk", "/opt/android-sdk",
              os.path.expanduser("~/Android/Sdk"), os.path.expanduser("~/android-sdk")):
        if os.path.isdir(p):
            return p
    return ""


def _sdkmanager(sdk):
    for rel in ("cmdline-tools/latest/bin/sdkmanager", "tools/bin/sdkmanager",
                "cmdline-tools/bin/sdkmanager"):
        p = os.path.join(sdk, rel)
        if os.path.exists(p):
            return p
    return ""


def _ensure_sdk_packages(sdk, compile_sdk, env, log):
    plat = os.path.join(sdk, "platforms", f"android-{compile_sdk}")
    if os.path.isdir(plat):
        return
    sm = _sdkmanager(sdk)
    if not sm:
        log.append("sdkmanager nahi mila - platform install skip")
        return
    log.append(f"SDK platform android-{compile_sdk} install ho raha hai…")
    _sh(f"yes | '{sm}' --sdk_root='{sdk}' --licenses >/dev/null 2>&1", timeout=180, env=env)
    _sh([sm, f"--sdk_root={sdk}", f"platforms;android-{compile_sdk}"], timeout=600, env=env)
    # build-tools alag se - fail ho to bhi platform install ka nuksan nahi
    _sh([sm, f"--sdk_root={sdk}", f"build-tools;{compile_sdk}.0.0"], timeout=600, env=env)


def _gradle_cmd(root, info, env, log):
    wrapper_jar = os.path.join(root, "gradle/wrapper/gradle-wrapper.jar")
    for name in ("gradlew", "gradlew.bat"):
        p = os.path.join(root, name)
        if os.path.exists(p) and os.path.exists(wrapper_jar):
            try:
                os.chmod(p, 0o755)
            except Exception:
                pass
            return ["bash", p] if name == "gradlew" else [p]
    g = shutil.which("gradle")
    if g:
        return [g]
    ver = info.get("gradle") or "8.10.2"
    cache = os.path.join(WORK_ROOT, "tools", f"gradle-{ver}")
    exe = os.path.join(cache, "bin", "gradle")
    if not os.path.exists(exe):
        log.append(f"Gradle {ver} download ho raha hai…")
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        zpath = cache + ".zip"
        rc, _ = _sh(["curl", "-fsSL", "-o", zpath,
                     f"https://services.gradle.org/distributions/gradle-{ver}-bin.zip"],
                    timeout=600)
        if rc != 0:
            import requests
            r = requests.get(f"https://services.gradle.org/distributions/gradle-{ver}-bin.zip",
                             timeout=600)
            with open(zpath, "wb") as f:
                f.write(r.content)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(os.path.dirname(cache))
        try:
            os.remove(zpath)
        except Exception:
            pass
    return [exe]


def _fix_google_services(root, pkgs, log):
    """google-services.json me har zaroori package ke liye client entry pakki karo.

    Package ID badalne par plugin 'No matching client found for package name'
    se build tod deta hai. Yahan pehle client ko clone karke naye package ke
    saath add kar dete hain (purani entry bhi rahti hai).
    """
    pkgs = [p for p in dict.fromkeys(pkgs) if p]
    if not pkgs:
        return
    skip = {"build", ".git", "node_modules", ".gradle"}
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        if "google-services.json" not in files:
            continue
        path = os.path.join(dirpath, "google-services.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log.append(f"google-services.json padh nahi paya: {str(e)[:80]}")
            continue
        clients = data.get("client") or []
        if not clients:
            continue

        def cpkg(c):
            return (((c.get("client_info") or {}).get("android_client_info") or {})
                    .get("package_name"))

        have = {cpkg(c) for c in clients}
        added = []
        for p in pkgs:
            if p in have:
                continue
            new = copy.deepcopy(clients[0])
            new.setdefault("client_info", {}).setdefault("android_client_info", {})["package_name"] = p
            for oc in new.get("oauth_client") or []:
                if isinstance(oc.get("android_info"), dict):
                    oc["android_info"]["package_name"] = p
            clients.append(new)
            added.append(p)
        if added:
            data["client"] = clients
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            log.append("google-services.json fix: " + ", ".join(added))


def _apply_customizations(root, info, cust, log):
    gp = _app_gradle(root)
    if gp:
        t = _read(gp)
        new = t
        pkg = cust.get("pkg") or ""
        if not pkg and "aistudio" in (info.get("pkg") or ""):
            slug = re.sub(r"[^a-z0-9]+", "", (info.get("name") or "app").lower()) or "app"
            pkg = f"com.{slug}.app"
        if pkg and info.get("pkg") and pkg != info["pkg"]:
            new = new.replace(f'"{info["pkg"]}"', f'"{pkg}"')
            if new != t:
                log.append(f"Package ID: {pkg} (Google/AI Studio wala hata diya)")
        if new != t:
            with open(gp, "w", encoding="utf-8") as f:
                f.write(new)
    if cust.get("name"):
        sp = os.path.join(root, "app/src/main/res/values/strings.xml")
        t = _read(sp)
        if t and 'name="app_name"' in t:
            t2 = re.sub(r'(<string name="app_name">)[^<]*(</string>)',
                        lambda m: m.group(1) + cust["name"] + m.group(2), t)
            if t2 != t:
                with open(sp, "w", encoding="utf-8") as f:
                    f.write(t2)
                log.append(f"App naam: {cust['name']}")
        man = os.path.join(root, "app/src/main/AndroidManifest.xml")
        tm = _read(man)
        if tm and 'android:label="' in tm and "@string/app_name" not in tm:
            tm2 = re.sub(r'android:label="[^"]*"',
                         lambda m: f'android:label="{cust["name"]}"', tm, count=1)
            if tm2 != tm:
                with open(man, "w", encoding="utf-8") as f:
                    f.write(tm2)
    # --- FIX: final package ke liye google-services.json match karao ---
    try:
        fin = _project_info(root)
        pkgs = [fin["pkg"]] + [fin["pkg"] + s for s in fin.get("suffixes", []) if fin["pkg"]]
        _fix_google_services(root, pkgs, log)
    except Exception as e:
        log.append(f"google-services note: {str(e)[:100]}")
    rm = os.path.join(root, "README.md")
    t = _read(rm)
    if t and ("ai.google.dev" in t or "AI Studio" in t):
        lines = [ln for ln in t.splitlines()
                 if "ai.google.dev" not in ln and "ai.studio" not in ln.lower()
                 and "AI Studio app" not in ln]
        with open(rm, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).strip() + "\n")
        log.append("README se Google AI Studio banner hata diya")


def _prep_project(root, info, env, log):
    sdk = _android_sdk()
    if sdk:
        with open(os.path.join(root, "local.properties"), "w") as f:
            f.write(f"sdk.dir={sdk}\n")
        _ensure_sdk_packages(sdk, info.get("sdk") or 34, env, log)
    alltext = ""
    gp = _app_gradle(root)
    if gp:
        alltext += _read(gp)
    alltext += _read(os.path.join(root, "build.gradle.kts")) + _read(os.path.join(root, "build.gradle"))
    m = re.search(r'storeFile\s*=?\s*file\("\$\{rootDir\}/([^"]+)"\)', alltext)
    if m and m.group(1).endswith((".keystore", ".jks")) and "debug" in m.group(1).lower():
        ks = os.path.join(root, m.group(1))
        if not os.path.exists(ks):
            jh = env.get("JAVA_HOME", "")
            keytool = os.path.join(jh, "bin", "keytool") if jh else "keytool"
            rc, _ = _sh([keytool, "-genkeypair", "-v", "-keystore", ks,
                         "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", "2048",
                         "-validity", "10000", "-storepass", "android", "-keypass", "android",
                         "-dname", "CN=Android Debug,O=Android,C=US"], timeout=60, env=env)
            if rc == 0:
                log.append("Debug keystore banaya (missing tha)")
    if os.path.exists(os.path.join(root, ".env.example")) and not os.path.exists(os.path.join(root, ".env")):
        shutil.copyfile(os.path.join(root, ".env.example"), os.path.join(root, ".env"))
        log.append(".env banaya (.env.example se) - API key baad me dalna")


async def _run_build(client, chat_id, status, sess):
    root, info = sess["root"], sess["info"]
    mode = sess.get("mode", "debug")
    log = []
    env = dict(os.environ)
    jh = _java_home()
    if jh:
        env["JAVA_HOME"] = jh
        env["PATH"] = os.path.join(jh, "bin") + os.pathsep + env.get("PATH", "")
    sdk = _android_sdk()
    if sdk:
        env["ANDROID_HOME"] = sdk
        env["ANDROID_SDK_ROOT"] = sdk
    try:
        _apply_customizations(root, info, sess.get("cust", {}), log)
        _prep_project(root, info, env, log)
    except Exception as e:
        log.append(f"prep note: {str(e)[:120]}")
    gradle = _gradle_cmd(root, info, env, log)
    logpath = os.path.join(sess["dir"], "build.log")
    task = ":app:assembleRelease" if mode == "opt" else ":app:assembleDebug"
    cmd = gradle + [task, "--no-daemon", "--console=plain"]
    if mode == "opt":
        initp = os.path.join(sess["dir"], "optimize.init.gradle")
        with open(initp, "w", encoding="utf-8") as f:
            f.write(OPT_INIT)
        cmd += ["--init-script", initp]
        log.append("⚡ Optimized build: R8 minify + resources shrink ON")
    try:
        await status.edit_text("🏗 <b>Build shuru</b>\n" + "\n".join(f"• {esc(x)}" for x in log[-4:])
                               + "\n\nPehli build me libraries download hoti hain - 5-15 min lag sakte hain…")
    except Exception:
        pass
    lf = open(logpath, "wb")
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=root, env=env, stdout=lf, stderr=asyncio.subprocess.STDOUT)
    started = time.time()
    last_shown = ""
    timed_out = False
    while True:
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=25)
            break
        except asyncio.TimeoutError:
            if time.time() - started > BUILD_TIMEOUT:
                timed_out = True
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
                break
            tail = _read(logpath)
            lines = [ln for ln in tail.splitlines() if ln.strip().startswith("> Task")][-1:]
            cur = lines[0] if lines else "libraries download ho rahi hain…"
            if cur != last_shown:
                last_shown = cur
                try:
                    await status.edit_text(f"🏗 <b>Build chal raha hai</b> ({int(time.time()-started)//60} min)\n<code>{esc(cur[:180])}</code>")
                except Exception:
                    pass
    lf.close()
    tail = _read(logpath)
    apk = None
    dbg = os.path.join(root, "app/build/outputs/apk/" + ("release" if mode == "opt" else "debug"))
    if os.path.isdir(dbg):
        cands = [os.path.join(dbg, f) for f in os.listdir(dbg)
                 if f.endswith(".apk") and "unaligned" not in f]
        if cands:
            apk = max(cands, key=os.path.getsize)
    if apk and proc.returncode == 0:
        name = sess.get("cust", {}).get("name") or info.get("name") or "app"
        out = os.path.join(sess["dir"], sanitize_filename(name) + ".apk")
        shutil.copyfile(apk, out)
        final_pkg = _project_info(root).get("pkg") or sess.get("cust", {}).get("pkg") or info.get("pkg")
        cap = (f"✅ <b>{esc(name)}</b> - {'⚡ Optimized APK' if mode == 'opt' else 'APK'} taiyaar hai!\n"
               f"📦 Package: <code>{esc(final_pkg)}</code>\n"
               f"💾 Size: {humanbytes(os.path.getsize(out))}\n"
               "Phone me install karke khol lo. (Debug build hai - Play Store ke liye release keystore lagega.)")
        try:
            await client.send_document(chat_id, out, caption=cap)
            await status.delete()
        except Exception as e:
            await status.edit_text(f"⚠️ APK ban gaya par bhejna fail: {esc(str(e)[:150])}")
    else:
        err = "\n".join(tail.splitlines()[-35:])
        why = "⏱ Build timeout (30 min)" if timed_out else f"❌ Build fail (code {proc.returncode})"
        try:
            await status.edit_text(f"{why}\n\n<b>Log ke akhri hisse:</b>\n<code>{esc(err[-3200:])}</code>")
        except Exception:
            pass
        try:
            await client.send_document(chat_id, logpath, caption="📄 Pura build log")
        except Exception:
            pass
    shutil.rmtree(sess["dir"], ignore_errors=True)
    WAIT.pop(chat_id, None)


def _card_text(sess):
    info = sess["info"]
    cust = sess.get("cust", {})
    pkg = cust.get("pkg") or ("(AI Studio wala hai - build par khud badal dunga)"
                              if "aistudio" in (info.get("pkg") or "") else info.get("pkg"))
    return (f"📱 <b>{esc(cust.get('name') or info.get('name'))}</b>\n"
            f"🆔 Package: <code>{esc(str(pkg))}</code>\n"
            f"🛠 Compile SDK: {info.get('sdk')}\n\n"
            "Customization chaho to neeche ke button se naam/package badal lo, "
            "warna seedha Build dabao. Google/AI Studio ki branding (package + README banner) "
            "build par khud hat jayegi.\n"
            "⚡ <b>Optimized APK</b> = R8 minify + resources shrink - size chhota, performance behtar (thoda zyada time lagega).")


def _card_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏗 Build APK", callback_data="ab:build"),
         InlineKeyboardButton("⚡ Optimized APK", callback_data="ab:buildopt")],
        [InlineKeyboardButton("✏️ App ka naam badle", callback_data="ab:name"),
         InlineKeyboardButton("🆔 Package ID badle", callback_data="ab:pkg")],
        [InlineKeyboardButton("❌ Cancel", callback_data="ab:cancel")],
    ])


async def buildapp_command(client, message):
    chat_id = message.chat.id
    doc = None
    if getattr(message, "document", None):
        doc = message.document
    elif getattr(message, "reply_to_message", None) and getattr(message.reply_to_message, "document", None):
        doc = message.reply_to_message.document
    WAIT[chat_id] = {"stage": "zip"}
    if doc:
        return await _accept_zip(client, message, doc)
    await message.reply_text("📦 <b>App Builder</b>\n\nApne Android app ke source code ki <b>ZIP file</b> bhejo "
                             "(AI Studio / Android Studio project).\nMain usse asli <b>APK</b> bana dunga - "
                             "Google ki branding hatakar.\n\nZIP bhejo 👇")


async def try_build_input(client, message):
    sess = WAIT.get(message.chat.id)
    if not sess:
        return False
    if sess["stage"] == "zip":
        doc = getattr(message, "document", None)
        if doc:
            await _accept_zip(client, message, doc)
            return True
        return False
    if sess["stage"] in ("name", "pkg"):
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return False
        sess.setdefault("cust", {})[sess["stage"]] = text[:60] if sess["stage"] == "name" else text[:80].replace(" ", "")
        sess["stage"] = "ready"
        await message.reply_text(_card_text(sess), reply_markup=_card_kb())
        return True
    return False


async def _accept_zip(client, message, doc):
    chat_id = message.chat.id
    name = (doc.file_name or "app.zip")
    if not name.lower().endswith(".zip"):
        await message.reply_text("⚠️ Ye ZIP file nahi lagti. Android project ki .zip bhejo.")
        return
    if doc.file_size and doc.file_size > MAX_ZIP:
        await message.reply_text("⚠️ ZIP bahut bada hai (200 MB se zyada).")
        return
    status = await message.reply_text("⬇️ ZIP download ho raha hai…")
    work = os.path.join(WORK_ROOT, str(chat_id))
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    zpath = os.path.join(work, "src.zip")
    try:
        await message.download(file_name=zpath)
    except Exception as e:
        await status.edit_text(f"❌ ZIP download fail: <code>{esc(str(e)[:150])}</code>")
        WAIT.pop(chat_id, None)
        return
    try:
        total = 0
        dest = os.path.join(work, "src")
        with zipfile.ZipFile(zpath) as z:
            for item in z.infolist():
                target = os.path.abspath(os.path.join(dest, item.filename))
                if not target.startswith(os.path.abspath(dest)):
                    raise ValueError("unsafe zip path")
                total += item.file_size
                if total > MAX_EXTRACTED:
                    raise ValueError("extracted size too large")
            z.extractall(dest)
    except Exception as e:
        await status.edit_text(f"❌ ZIP khul nahi paya: <code>{esc(str(e)[:150])}</code>")
        shutil.rmtree(work, ignore_errors=True)
        WAIT.pop(chat_id, None)
        return
    root = _find_project_root(dest)
    if not root:
        await status.edit_text("❌ Is ZIP me Android project nahi mila (settings.gradle / app folder chahiye).")
        shutil.rmtree(work, ignore_errors=True)
        WAIT.pop(chat_id, None)
        return
    info = _project_info(root)
    WAIT[chat_id] = {"stage": "ready", "dir": work, "root": root, "info": info, "cust": {}}
    await status.edit_text(_card_text(WAIT[chat_id]), reply_markup=_card_kb())


async def app_callback(client, cq, parts):
    chat_id = cq.message.chat.id
    sess = WAIT.get(chat_id)
    action = parts[1] if len(parts) > 1 else ""
    if action == "cancel":
        if sess:
            shutil.rmtree(sess.get("dir", ""), ignore_errors=True)
            WAIT.pop(chat_id, None)
        await cq.answer("Cancel ho gaya")
        try:
            await cq.message.edit_text("❌ App build cancel.")
        except Exception:
            pass
        return
    if not sess:
        await cq.answer("Session khatam - /buildapp se phir shuru karo", show_alert=True)
        return
    if action in ("name", "pkg"):
        sess["stage"] = action
        await cq.answer()
        await cq.message.reply_text("✏️ Naya app naam bhejo:" if action == "name"
                                    else "🆔 Naya package ID bhejo (jaise com.example.myapp):")
        return
    if action in ("build", "buildopt"):
        sess["mode"] = "opt" if action == "buildopt" else "debug"
        if BUILD_LOCK.locked():
            await cq.answer("Ek build pehle se chal raha hai - thoda ruko", show_alert=True)
            return
        await cq.answer("Build shuru…")
        sess["stage"] = "building"
        try:
            await cq.message.edit_text("🏗 Build ki taiyaari…")
        except Exception:
            pass

        async def runner():
            async with BUILD_LOCK:
                await _run_build(client, chat_id, cq.message, sess)

        asyncio.create_task(runner())
        return
    await cq.answer()
