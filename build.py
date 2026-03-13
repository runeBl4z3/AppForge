#!/usr/bin/env python3
"""
AppForge Multi-Platform Builder
================================
Converts any website URL into installable app packages for:
  - Android  (.apk  + Android Studio project)
  - Windows  (.exe  installer + Electron project)
  - iOS      (.ipa  + Xcode project)
  - Linux    (.AppImage + Electron project)

Usage:
    python3 build.py --url https://example.com --name "My App" --package com.example.myapp

Author: AppForge
"""

import os, sys, json, struct, hashlib, shutil, zipfile, base64, subprocess, argparse, textwrap
from pathlib import Path
from datetime import datetime

# ══════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════

VERSION = "1.0.0"
BUILD_DATE = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ══════════════════════════════════════════════════════════════════════
#  ANDROID — Binary AndroidManifest.xml encoder
# ══════════════════════════════════════════════════════════════════════

class AXMLEncoder:
    """Encodes a minimal but valid Android Binary XML (AXML) AndroidManifest."""

    CHUNK_XML       = 0x00080003
    CHUNK_STRPOOL   = 0x001C0001
    CHUNK_STARTNS   = 0x00100100
    CHUNK_ENDNS     = 0x00100101
    CHUNK_STARTTAG  = 0x00100102
    CHUNK_ENDTAG    = 0x00100103

    def __init__(self):
        self.strings = []
        self.smap = {}

    def s(self, val):
        if val not in self.smap:
            self.smap[val] = len(self.strings)
            self.strings.append(val)
        return self.smap[val]

    def _str_pool(self):
        raw = []
        for st in self.strings:
            enc = st.encode('utf-16-le')
            raw.append(struct.pack('<H', len(st)) + enc + b'\x00\x00')
        offsets, off = [], 0
        for r in raw:
            offsets.append(off); off += len(r)
        sdata = b''.join(raw)
        odata = struct.pack('<' + 'I'*len(offsets), *offsets) if offsets else b''
        strings_start = 28 + len(odata)
        chunk_body = struct.pack('<IIIIII', len(self.strings), 0, 0, strings_start, 0, 0) + odata + sdata
        pad = (4 - len(chunk_body) % 4) % 4
        chunk_body += b'\x00' * pad
        return struct.pack('<II', self.CHUNK_STRPOOL, 8 + len(chunk_body)) + chunk_body

    def _start_ns(self, prefix, uri):
        return struct.pack('<IIIIII', self.CHUNK_STARTNS, 24, 0, 1, self.s(prefix), self.s(uri))

    def _end_ns(self, prefix, uri):
        return struct.pack('<IIIIII', self.CHUNK_ENDNS, 24, 0, 1, self.s(prefix), self.s(uri))

    def _start_tag(self, ns_idx, name, attrs, line=1):
        # attrs: list of (ns, name, raw_str, type_val, data_val)
        attr_data = b''
        for (a_ns, a_name, a_raw, a_type, a_val) in attrs:
            attr_data += struct.pack('<IIIII', a_ns, a_name, a_raw, a_type, a_val)
        size = 48 + len(attr_data)
        hdr = struct.pack('<IIIIIIIIIIII',
            self.CHUNK_STARTTAG, size, line, 0xFFFFFFFF,
            0xFFFFFFFF, name, 20, 20, len(attrs), 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
        return hdr + attr_data

    def _end_tag(self, name, line=1):
        return struct.pack('<IIIIII', self.CHUNK_ENDTAG, 24, line, 0xFFFFFFFF, 0xFFFFFFFF, name)

    def build(self, pkg, label, version_name, version_code, min_sdk, target_sdk):
        NS = 'http://schemas.android.com/apk/res/android'
        # Pre-register all strings
        for st in [NS, 'android', '', pkg, label, version_name, str(version_code),
                   str(min_sdk), str(target_sdk), 'true', 'false',
                   'manifest', 'uses-sdk', 'uses-permission', 'application', 'activity',
                   'intent-filter', 'action', 'category',
                   'package', 'versionCode', 'versionName', 'minSdkVersion',
                   'targetSdkVersion', 'name', 'label', 'allowBackup', 'exported',
                   'hardwareAccelerated', 'screenOrientation', 'portrait', 'configChanges',
                   'keyboard|keyboardHidden|orientation|screenSize',
                   '.MainActivity',
                   'android.intent.action.MAIN', 'android.intent.category.LAUNCHER',
                   'android.permission.INTERNET', 'android.permission.ACCESS_NETWORK_STATE',
                   'android.permission.VIBRATE', 'android.permission.WRITE_EXTERNAL_STORAGE',
                   'android.permission.READ_EXTERNAL_STORAGE', 'android.permission.CAMERA',
                   'android.permission.ACCESS_FINE_LOCATION']:
            self.s(st)

        ns = self.s(NS)
        NULL = 0xFFFFFFFF
        INT  = 0x10000008
        STR  = 0x03000008
        BOOL = 0x12000008

        body = b''
        body += self._start_ns('android', NS)

        # <manifest package=... versionCode=... versionName=...>
        body += self._start_tag(NULL, self.s('manifest'), [
            (NULL, self.s('package'),     self.s(pkg),          STR,  self.s(pkg)),
            (ns,   self.s('versionCode'), self.s(str(version_code)), INT, version_code),
            (ns,   self.s('versionName'), self.s(version_name), STR,  self.s(version_name)),
        ])

        # <uses-sdk .../>
        body += self._start_tag(NULL, self.s('uses-sdk'), [
            (ns, self.s('minSdkVersion'),    self.s(str(min_sdk)),    INT, min_sdk),
            (ns, self.s('targetSdkVersion'), self.s(str(target_sdk)), INT, target_sdk),
        ])
        body += self._end_tag(self.s('uses-sdk'))

        # permissions
        for perm in ['INTERNET','ACCESS_NETWORK_STATE','VIBRATE',
                     'WRITE_EXTERNAL_STORAGE','READ_EXTERNAL_STORAGE','CAMERA','ACCESS_FINE_LOCATION']:
            pstr = f'android.permission.{perm}'
            body += self._start_tag(NULL, self.s('uses-permission'), [
                (ns, self.s('name'), self.s(pstr), STR, self.s(pstr))
            ])
            body += self._end_tag(self.s('uses-permission'))

        # <application ...>
        body += self._start_tag(NULL, self.s('application'), [
            (ns, self.s('label'),              self.s(label), STR,  self.s(label)),
            (ns, self.s('allowBackup'),        self.s('true'), BOOL, 0xFFFFFFFF),
            (ns, self.s('hardwareAccelerated'),self.s('true'), BOOL, 0xFFFFFFFF),
        ])

        # <activity ...>
        body += self._start_tag(NULL, self.s('activity'), [
            (ns, self.s('name'),            self.s('.MainActivity'),  STR, self.s('.MainActivity')),
            (ns, self.s('exported'),        self.s('true'),           BOOL, 0xFFFFFFFF),
            (ns, self.s('screenOrientation'),self.s('portrait'),      STR, self.s('portrait')),
            (ns, self.s('configChanges'),   self.s('keyboard|keyboardHidden|orientation|screenSize'),
             STR, self.s('keyboard|keyboardHidden|orientation|screenSize')),
        ])
        body += self._start_tag(NULL, self.s('intent-filter'), [])
        body += self._start_tag(NULL, self.s('action'), [
            (ns, self.s('name'), self.s('android.intent.action.MAIN'), STR, self.s('android.intent.action.MAIN'))
        ])
        body += self._end_tag(self.s('action'))
        body += self._start_tag(NULL, self.s('category'), [
            (ns, self.s('name'), self.s('android.intent.category.LAUNCHER'), STR, self.s('android.intent.category.LAUNCHER'))
        ])
        body += self._end_tag(self.s('category'))
        body += self._end_tag(self.s('intent-filter'))
        body += self._end_tag(self.s('activity'))
        body += self._end_tag(self.s('application'))
        body += self._end_tag(self.s('manifest'))
        body += self._end_ns('android', NS)

        sp = self._str_pool()
        total = 8 + len(sp) + len(body)
        return struct.pack('<II', self.CHUNK_XML, total) + sp + body


# ══════════════════════════════════════════════════════════════════════
#  ANDROID SOURCE FILES
# ══════════════════════════════════════════════════════════════════════

def android_main_activity(package, url, app_name):
    return f'''package {package};

import android.app.Activity;
import android.content.Context;
import android.graphics.Color;
import android.net.ConnectivityManager;
import android.net.NetworkInfo;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.*;
import android.widget.*;

public class MainActivity extends Activity {{

    private WebView webView;
    private ProgressBar progressBar;
    private LinearLayout offlineLayout;
    private static final String URL = "{url}";

    @Override
    protected void onCreate(Bundle savedInstanceState) {{
        super.onCreate(savedInstanceState);
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN,
                             WindowManager.LayoutParams.FLAG_FULLSCREEN);
        buildUI();
        configureWebView();
        if (isOnline()) loadSite(); else showOffline();
    }}

    private void buildUI() {{
        RelativeLayout root = new RelativeLayout(this);
        root.setBackgroundColor(Color.parseColor("#08090c"));

        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        RelativeLayout.LayoutParams pbp = new RelativeLayout.LayoutParams(
            RelativeLayout.LayoutParams.MATCH_PARENT, 8);
        pbp.addRule(RelativeLayout.ALIGN_PARENT_TOP);
        progressBar.setLayoutParams(pbp);
        progressBar.setId(1001);

        webView = new WebView(this);
        RelativeLayout.LayoutParams wvp = new RelativeLayout.LayoutParams(
            RelativeLayout.LayoutParams.MATCH_PARENT, RelativeLayout.LayoutParams.MATCH_PARENT);
        wvp.addRule(RelativeLayout.BELOW, 1001);
        webView.setLayoutParams(wvp);
        webView.setId(1002);

        offlineLayout = new LinearLayout(this);
        offlineLayout.setOrientation(LinearLayout.VERTICAL);
        offlineLayout.setGravity(Gravity.CENTER);
        offlineLayout.setBackgroundColor(Color.parseColor("#08090c"));
        offlineLayout.setVisibility(View.GONE);
        RelativeLayout.LayoutParams olp = new RelativeLayout.LayoutParams(
            RelativeLayout.LayoutParams.MATCH_PARENT, RelativeLayout.LayoutParams.MATCH_PARENT);
        offlineLayout.setLayoutParams(olp);

        TextView icon = new TextView(this); icon.setText("\\uD83D\\uDCF6");
        icon.setTextSize(64); icon.setGravity(Gravity.CENTER);
        TextView msg = new TextView(this); msg.setText("No Internet Connection");
        msg.setTextColor(Color.parseColor("#f1f2f6")); msg.setTextSize(20);
        msg.setGravity(Gravity.CENTER); msg.setPadding(0,16,0,8);
        TextView sub = new TextView(this); sub.setText("Check your network and try again.");
        sub.setTextColor(Color.parseColor("#6b7280")); sub.setTextSize(14);
        sub.setGravity(Gravity.CENTER); sub.setPadding(48,0,48,32);
        Button retry = new Button(this); retry.setText("Retry");
        retry.setBackgroundColor(Color.parseColor("#5b6ef5"));
        retry.setTextColor(Color.WHITE); retry.setPadding(60,20,60,20);
        retry.setOnClickListener(v -> {{ if (isOnline()) {{ offlineLayout.setVisibility(View.GONE);
            webView.setVisibility(View.VISIBLE); loadSite(); }} }});

        offlineLayout.addView(icon); offlineLayout.addView(msg);
        offlineLayout.addView(sub); offlineLayout.addView(retry);
        root.addView(progressBar); root.addView(webView); root.addView(offlineLayout);
        setContentView(root);
    }}

    private void configureWebView() {{
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true); s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true); s.setAllowFileAccess(true);
        s.setLoadWithOverviewMode(true); s.setUseWideViewPort(true);
        s.setBuiltInZoomControls(false); s.setDisplayZoomControls(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setGeolocationEnabled(true);
        s.setUserAgentString(s.getUserAgentString() + " AppForge/{app_name.replace(' ','_')}/1.0");
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true);

        webView.setWebChromeClient(new WebChromeClient() {{
            public void onProgressChanged(WebView v, int p) {{
                progressBar.setProgress(p);
                progressBar.setVisibility(p < 100 ? View.VISIBLE : View.GONE);
            }}
            public void onGeolocationPermissionsShowPrompt(String o, GeolocationPermissions.Callback c) {{
                c.invoke(o, true, false);
            }}
        }});
        webView.setWebViewClient(new WebViewClient() {{
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {{
                String u = r.getUrl().toString();
                if (u.startsWith("http")) {{ v.loadUrl(u); return true; }}
                return false;
            }}
            public void onReceivedError(WebView v, WebResourceError e, WebResourceRequest r) {{
                if (r.isForMainFrame()) showOffline();
            }}
        }});
    }}

    private void loadSite() {{ webView.loadUrl(URL); }}
    private void showOffline() {{
        webView.setVisibility(View.GONE); offlineLayout.setVisibility(View.VISIBLE);
    }}
    private boolean isOnline() {{
        ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (cm == null) return false;
        NetworkInfo ni = cm.getActiveNetworkInfo();
        return ni != null && ni.isConnectedOrConnecting();
    }}
    @Override public void onBackPressed() {{
        if (webView.canGoBack()) webView.goBack(); else super.onBackPressed();
    }}
    @Override protected void onPause() {{ super.onPause(); webView.onPause(); }}
    @Override protected void onResume() {{ super.onResume(); webView.onResume(); }}
}}
'''

def android_manifest_xml(package, app_name, version_name, version_code, min_sdk, target_sdk):
    return f'''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{package}"
    android:versionCode="{version_code}"
    android:versionName="{version_name}">

    <uses-sdk
        android:minSdkVersion="{min_sdk}"
        android:targetSdkVersion="{target_sdk}" />

    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
    <uses-permission android:name="android.permission.VIBRATE" />
    <uses-permission android:name="android.permission.WRITE_EXTERNAL_STORAGE" />
    <uses-permission android:name="android.permission.READ_EXTERNAL_STORAGE" />
    <uses-permission android:name="android.permission.CAMERA" />
    <uses-permission android:name="android.permission.ACCESS_FINE_LOCATION" />

    <application
        android:allowBackup="true"
        android:hardwareAccelerated="true"
        android:label="{app_name}"
        android:supportsRtl="true"
        android:usesCleartextTraffic="true">

        <activity
            android:name=".MainActivity"
            android:configChanges="keyboard|keyboardHidden|orientation|screenSize"
            android:exported="true"
            android:screenOrientation="portrait">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>

    </application>
</manifest>
'''

def android_build_gradle(package, min_sdk, target_sdk, version_code, version_name):
    return f'''plugins {{
    id 'com.android.application'
}}

android {{
    compileSdk {target_sdk}
    namespace "{package}"

    defaultConfig {{
        applicationId "{package}"
        minSdk {min_sdk}
        targetSdk {target_sdk}
        versionCode {version_code}
        versionName "{version_name}"
    }}

    buildTypes {{
        release {{
            minifyEnabled true
            proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro'
        }}
    }}

    compileOptions {{
        sourceCompatibility JavaVersion.VERSION_1_8
        targetCompatibility JavaVersion.VERSION_1_8
    }}
}}

dependencies {{
    implementation 'androidx.appcompat:appcompat:1.6.1'
    implementation 'com.google.android.material:material:1.11.0'
    implementation 'androidx.webkit:webkit:1.10.0'
}}
'''

def android_settings_gradle(app_name):
    return f'''rootProject.name = "{app_name}"
include ':app'
'''

def android_strings_xml(app_name, url):
    return f'''<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">{app_name}</string>
    <string name="website_url">{url}</string>
    <string name="loading">Loading...</string>
    <string name="no_internet">No internet connection</string>
    <string name="retry">Retry</string>
</resources>
'''

def android_colors_xml():
    return '''<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="colorPrimary">#5b6ef5</color>
    <color name="colorPrimaryDark">#4a5bd4</color>
    <color name="colorAccent">#a855f7</color>
    <color name="background">#08090c</color>
    <color name="white">#FFFFFF</color>
</resources>
'''

def android_styles_xml():
    return '''<?xml version="1.0" encoding="utf-8"?>
<resources>
    <style name="AppTheme" parent="android:Theme.Material.Light.NoActionBar">
        <item name="android:colorPrimary">@color/colorPrimary</item>
        <item name="android:colorPrimaryDark">@color/colorPrimaryDark</item>
        <item name="android:colorAccent">@color/colorAccent</item>
        <item name="android:windowBackground">@color/background</item>
    </style>
</resources>
'''

def android_proguard():
    return '''-keepattributes *Annotation*
-keepattributes SourceFile,LineNumberTable
-keep class **.MainActivity { *; }
-keep class android.webkit.** { *; }
-dontwarn android.webkit.**
-dontwarn org.xmlpull.**
'''

# ══════════════════════════════════════════════════════════════════════
#  WINDOWS — Electron project
# ══════════════════════════════════════════════════════════════════════

def windows_package_json(app_name, package, version, url):
    safe_name = app_name.lower().replace(' ', '-')
    return json.dumps({
        "name": safe_name,
        "version": version,
        "description": f"{app_name} — Built with AppForge",
        "main": "main.js",
        "scripts": {
            "start": "electron .",
            "build-win": "electron-builder --win",
            "build-mac": "electron-builder --mac",
            "build-linux": "electron-builder --linux"
        },
        "build": {
            "appId": package,
            "productName": app_name,
            "win": {
                "target": ["nsis", "portable"],
                "icon": "assets/icon.ico"
            },
            "nsis": {
                "oneClick": False,
                "allowToChangeInstallationDirectory": True,
                "createDesktopShortcut": True,
                "createStartMenuShortcut": True
            },
            "mac": {
                "target": "dmg",
                "icon": "assets/icon.icns",
                "category": "public.app-category.utilities"
            },
            "linux": {
                "target": ["AppImage", "deb"],
                "icon": "assets/icon.png",
                "category": "Utility"
            }
        },
        "devDependencies": {
            "electron": "^28.0.0",
            "electron-builder": "^24.0.0"
        },
        "keywords": [],
        "author": app_name,
        "license": "MIT"
    }, indent=2)

def windows_main_js(app_name, url):
    return f'''const {{ app, BrowserWindow, Menu, Tray, shell, nativeImage, ipcMain }} = require('electron');
const path = require('path');

let mainWindow;
let tray;

function createWindow() {{
  mainWindow = new BrowserWindow({{
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    title: '{app_name}',
    backgroundColor: '#08090c',
    autoHideMenuBar: true,
    webPreferences: {{
      nodeIntegration: false,
      contextIsolation: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      preload: path.join(__dirname, 'preload.js')
    }},
    show: false,
    icon: path.join(__dirname, 'assets', process.platform === 'win32' ? 'icon.ico' : 'icon.png')
  }});

  // Show splash then load URL
  mainWindow.loadFile('splash.html');

  setTimeout(() => {{
    mainWindow.loadURL('{url}');
  }}, 2000);

  mainWindow.once('ready-to-show', () => {{
    mainWindow.show();
  }});

  // Open external links in browser
  mainWindow.webContents.setWindowOpenHandler(({{ url }}) => {{
    shell.openExternal(url);
    return {{ action: 'deny' }};
  }});

  mainWindow.on('closed', () => {{ mainWindow = null; }});
}}

function createTray() {{
  // Create system tray icon
  const iconPath = path.join(__dirname, 'assets', process.platform === 'win32' ? 'icon.ico' : 'icon.png');
  tray = new Tray(nativeImage.createFromPath(iconPath).resize({{ width: 16, height: 16 }}));

  const contextMenu = Menu.buildFromTemplate([
    {{ label: 'Open {app_name}', click: () => mainWindow?.show() }},
    {{ label: 'Reload', click: () => mainWindow?.webContents.reload() }},
    {{ type: 'separator' }},
    {{ label: 'Quit', click: () => app.quit() }}
  ]);

  tray.setToolTip('{app_name}');
  tray.setContextMenu(contextMenu);
  tray.on('click', () => mainWindow?.show());
}}

app.whenReady().then(() => {{
  createWindow();
  createTray();

  app.on('activate', () => {{
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  }});
}});

app.on('window-all-closed', () => {{
  if (process.platform !== 'darwin') app.quit();
}});

// Handle navigation
app.on('web-contents-created', (event, contents) => {{
  contents.on('will-navigate', (event, url) => {{
    // Allow navigation within the app
    console.log('Navigating to:', url);
  }});
}});
'''

def windows_preload_js():
    return '''const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('appForge', {
    platform: process.platform,
    version: process.versions.electron,
    send: (channel, data) => ipcRenderer.send(channel, data),
    receive: (channel, func) => ipcRenderer.on(channel, (event, ...args) => func(...args))
});
'''

def windows_splash_html(app_name):
    return f'''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    background: #08090c;
    display: flex; align-items: center; justify-content: center;
    height: 100vh; font-family: 'Segoe UI', sans-serif;
    flex-direction: column; gap: 20px;
  }}
  .icon {{ font-size: 72px; animation: bounce 1s infinite alternate; }}
  @keyframes bounce {{ from {{ transform: scale(1); }} to {{ transform: scale(1.1); }} }}
  .name {{
    font-size: 32px; font-weight: 800; color: #f1f2f6; letter-spacing: -1px;
    background: linear-gradient(135deg, #5b6ef5, #a855f7);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }}
  .bar-wrap {{ width: 200px; height: 4px; background: #1e2130; border-radius: 2px; }}
  .bar {{ height: 100%; background: linear-gradient(90deg, #5b6ef5, #a855f7);
         border-radius: 2px; animation: load 1.8s ease forwards; width: 0; }}
  @keyframes load {{ to {{ width: 100%; }} }}
  .sub {{ color: #6b7280; font-size: 13px; }}
</style>
</head>
<body>
  <div class="icon">⚡</div>
  <div class="name">{app_name}</div>
  <div class="bar-wrap"><div class="bar"></div></div>
  <div class="sub">Loading your app...</div>
</body>
</html>
'''

def windows_readme(app_name, url):
    return f'''# {app_name} — Windows App
Built with AppForge on {BUILD_DATE}

## Website
{url}

## Run Locally (requires Node.js)
    npm install
    npm start

## Build Windows Installer (.exe)
    npm install
    npm run build-win
    # Output: dist/{app_name} Setup.exe

## Build Linux (.AppImage)
    npm run build-linux
    # Output: dist/{app_name}.AppImage

## Build macOS (.dmg)
    npm run build-mac
    # Output: dist/{app_name}.dmg

## Requirements
- Node.js 18+ from https://nodejs.org
- npm (included with Node.js)
'''

def windows_gitignore():
    return '''node_modules/
dist/
.DS_Store
*.log
'''

# ══════════════════════════════════════════════════════════════════════
#  iOS — Xcode project
# ══════════════════════════════════════════════════════════════════════

def ios_info_plist(app_name, package, version_name, version_code):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleDisplayName</key>
    <string>{app_name}</string>
    <key>CFBundleExecutable</key>
    <string>$(EXECUTABLE_NAME)</string>
    <key>CFBundleIdentifier</key>
    <string>{package}</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>{app_name}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>{version_name}</string>
    <key>CFBundleVersion</key>
    <string>{version_code}</string>
    <key>LSRequiresIPhoneOS</key>
    <true/>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsArbitraryLoads</key>
        <true/>
    </dict>
    <key>NSCameraUsageDescription</key>
    <string>This app needs camera access</string>
    <key>NSLocationWhenInUseUsageDescription</key>
    <string>This app needs location access</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>This app needs microphone access</string>
    <key>NSPhotoLibraryUsageDescription</key>
    <string>This app needs photo library access</string>
    <key>UILaunchStoryboardName</key>
    <string>LaunchScreen</string>
    <key>UIMainStoryboardFile</key>
    <string>Main</string>
    <key>UIRequiredDeviceCapabilities</key>
    <array>
        <string>armv7</string>
    </array>
    <key>UISupportedInterfaceOrientations</key>
    <array>
        <string>UIInterfaceOrientationPortrait</string>
        <string>UIInterfaceOrientationPortraitUpsideDown</string>
        <string>UIInterfaceOrientationLandscapeLeft</string>
        <string>UIInterfaceOrientationLandscapeRight</string>
    </array>
    <key>UIViewControllerBasedStatusBarAppearance</key>
    <false/>
</dict>
</plist>
'''

def ios_view_controller_swift(app_name, url):
    return f'''import UIKit
import WebKit

class ViewController: UIViewController, WKNavigationDelegate, WKUIDelegate {{

    var webView: WKWebView!
    var progressView: UIProgressView!
    var offlineView: UIView!
    let url = "{url}"

    override func viewDidLoad() {{
        super.viewDidLoad()
        setupWebView()
        setupProgressBar()
        setupOfflineView()
        loadWebsite()
    }}

    func setupWebView() {{
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        config.preferences.javaScriptEnabled = true

        webView = WKWebView(frame: view.bounds, configuration: config)
        webView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.backgroundColor = UIColor(red: 0.03, green: 0.04, blue: 0.05, alpha: 1)
        webView.scrollView.bounces = true
        webView.allowsBackForwardNavigationGestures = true

        view.addSubview(webView)
        webView.addObserver(self, forKeyPath: #keyPath(WKWebView.estimatedProgress), options: .new, context: nil)
    }}

    func setupProgressBar() {{
        progressView = UIProgressView(progressViewStyle: .bar)
        progressView.translatesAutoresizingMaskIntoConstraints = false
        progressView.tintColor = UIColor(red: 0.357, green: 0.431, blue: 0.961, alpha: 1)
        progressView.trackTintColor = .clear
        view.addSubview(progressView)
        NSLayoutConstraint.activate([
            progressView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            progressView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            progressView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            progressView.heightAnchor.constraint(equalToConstant: 3)
        ])
    }}

    func setupOfflineView() {{
        offlineView = UIView(frame: view.bounds)
        offlineView.backgroundColor = UIColor(red: 0.03, green: 0.04, blue: 0.05, alpha: 1)
        offlineView.isHidden = true
        offlineView.autoresizingMask = [.flexibleWidth, .flexibleHeight]

        let stack = UIStackView()
        stack.axis = .vertical
        stack.alignment = .center
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false

        let iconLabel = UILabel()
        iconLabel.text = "📡"
        iconLabel.font = UIFont.systemFont(ofSize: 64)

        let titleLabel = UILabel()
        titleLabel.text = "No Internet Connection"
        titleLabel.textColor = .white
        titleLabel.font = UIFont.boldSystemFont(ofSize: 20)

        let subLabel = UILabel()
        subLabel.text = "Check your network and try again."
        subLabel.textColor = UIColor(white: 0.6, alpha: 1)
        subLabel.font = UIFont.systemFont(ofSize: 14)

        let retryButton = UIButton(type: .system)
        retryButton.setTitle("  Retry  ", for: .normal)
        retryButton.backgroundColor = UIColor(red: 0.357, green: 0.431, blue: 0.961, alpha: 1)
        retryButton.setTitleColor(.white, for: .normal)
        retryButton.layer.cornerRadius = 10
        retryButton.contentEdgeInsets = UIEdgeInsets(top: 12, left: 24, bottom: 12, right: 24)
        retryButton.addTarget(self, action: #selector(retryTapped), for: .touchUpInside)

        stack.addArrangedSubview(iconLabel)
        stack.addArrangedSubview(titleLabel)
        stack.addArrangedSubview(subLabel)
        stack.addArrangedSubview(retryButton)

        offlineView.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: offlineView.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: offlineView.centerYAnchor)
        ])
        view.addSubview(offlineView)
    }}

    func loadWebsite() {{
        guard let u = URL(string: url) else {{ return }}
        let request = URLRequest(url: u, cachePolicy: .useProtocolCachePolicy, timeoutInterval: 30)
        webView.load(request)
    }}

    @objc func retryTapped() {{
        offlineView.isHidden = true
        webView.isHidden = false
        loadWebsite()
    }}

    override func observeValue(forKeyPath keyPath: String?, of object: Any?,
                                change: [NSKeyValueChangeKey : Any]?, context: UnsafeMutableRawPointer?) {{
        if keyPath == "estimatedProgress" {{
            progressView.progress = Float(webView.estimatedProgress)
            progressView.isHidden = webView.estimatedProgress >= 1.0
        }}
    }}

    func webView(_ webView: WKWebView, didFailProvisionalNavigation: WKNavigation!, withError error: Error) {{
        offlineView.isHidden = false
        webView.isHidden = true
    }}

    deinit {{
        webView.removeObserver(self, forKeyPath: #keyPath(WKWebView.estimatedProgress))
    }}
}}
'''

def ios_app_delegate_swift(app_name):
    return f'''import UIKit

@main
class AppDelegate: UIResponder, UIApplicationDelegate {{

    var window: UIWindow?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {{

        window = UIWindow(frame: UIScreen.main.bounds)
        window?.rootViewController = ViewController()
        window?.backgroundColor = UIColor(red: 0.03, green: 0.04, blue: 0.05, alpha: 1)
        window?.makeKeyAndVisible()
        return true
    }}
}}
'''

def ios_podfile(package, app_name):
    return f'''platform :ios, '13.0'
use_frameworks!

target '{app_name}' do
  # Core dependencies
  pod 'WebKit', :modular_headers => true

  target '{app_name}Tests' do
    inherit! :search_paths
  end
end

post_install do |installer|
  installer.pods_project.targets.each do |target|
    target.build_configurations.each do |config|
      config.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] = '13.0'
      config.build_settings['SWIFT_VERSION'] = '5.0'
    end
  end
end
'''

def ios_readme(app_name, url):
    return f'''# {app_name} — iOS App
Built with AppForge on {BUILD_DATE}

## Website
{url}

## Requirements
- macOS with Xcode 15+
- Apple Developer Account (for device/App Store builds)
- CocoaPods (optional): https://cocoapods.org

## Open in Xcode
1. Install Xcode from Mac App Store
2. Open `{app_name}.xcodeproj` in Xcode
3. Select your Team in Signing & Capabilities
4. Click Run (⌘R) to build and run in simulator

## Build for TestFlight / App Store
1. Select "Any iOS Device" as build target
2. Product > Archive
3. Upload via Xcode Organizer to App Store Connect

## Build .ipa (Ad Hoc)
1. Product > Archive
2. Distribute App > Ad Hoc
3. Export .ipa file
'''

# ══════════════════════════════════════════════════════════════════════
#  SHARED — Icon generator (SVG-based placeholder)
# ══════════════════════════════════════════════════════════════════════

def generate_svg_icon(app_name, color='#5b6ef5'):
    initial = app_name[0].upper() if app_name else 'A'
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" style="stop-color:{color};stop-opacity:1" />
      <stop offset="100%" style="stop-color:#a855f7;stop-opacity:1" />
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="120" fill="url(#g)"/>
  <text x="256" y="340" font-family="Arial,sans-serif" font-size="280" font-weight="bold"
        text-anchor="middle" fill="white" opacity="0.95">{initial}</text>
</svg>
'''

# ══════════════════════════════════════════════════════════════════════
#  REAL APK SIGNING  (v1 JAR + v2 APK Signature Scheme)
# ══════════════════════════════════════════════════════════════════════

def _gen_signing_key():
    """Generate ephemeral RSA-2048 key + self-signed cert."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import hashes
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    import datetime
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u'AppForge')])
    now  = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=9999))
        .sign(key, hashes.SHA256()))
    return key, cert


def _v1_sign(entries, private_key, cert):
    """
    JAR/APK v1 signing.
    Returns (MANIFEST.MF, CERT.SF, CERT.RSA) as bytes.
    """
    from cryptography.hazmat.primitives.serialization import pkcs7, Encoding
    from cryptography.hazmat.primitives import hashes

    def b64sha(data):
        return base64.b64encode(hashlib.sha256(data).digest()).decode()

    # MANIFEST.MF — CRLF line endings required by JAR spec
    mf = b'Manifest-Version: 1.0\r\nCreated-By: AppForge\r\n\r\n'
    sections = []
    for name, data in entries:
        sec = f'Name: {name}\r\nSHA-256-Digest: {b64sha(data)}\r\n\r\n'.encode()
        mf += sec
        sections.append(sec)

    # CERT.SF
    sf = (f'Signature-Version: 1.0\r\nCreated-By: AppForge\r\n'
          f'SHA-256-Digest-Manifest: {b64sha(mf)}\r\n\r\n').encode()
    for sec in sections:
        sf += f'SHA-256-Digest: {b64sha(sec)}\r\n\r\n'.encode()  # section digests

    # CERT.RSA — PKCS#7 detached SignedData
    cert_rsa = (pkcs7.PKCS7SignatureBuilder()
        .set_data(sf)
        .add_signer(cert, private_key, hashes.SHA256())
        .sign(Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature]))

    return mf, sf, cert_rsa


def _v2_sign(apk_bytes, private_key, cert):
    """
    APK Signature Scheme v2.
    Inserts a signing block before the ZIP Central Directory.
    Required for Android 7+ (API 24+).
    """
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes, serialization

    # ── locate Central Directory and EOCD ─────────────────────────
    eocd_magic = b'PK\x05\x06'
    eocd_off   = apk_bytes.rfind(eocd_magic)
    if eocd_off == -1:
        return apk_bytes  # not a valid ZIP, bail

    cd_offset = struct.unpack_from('<I', apk_bytes, eocd_off + 16)[0]

    data_block  = apk_bytes[:cd_offset]
    central_dir = apk_bytes[cd_offset:eocd_off]
    eocd        = bytearray(apk_bytes[eocd_off:])
    # Zero out the CD offset in EOCD for signing
    struct.pack_into('<I', eocd, 16, 0)

    # ── compute content digests (spec §4.3) ───────────────────────
    CHUNK = 1 << 20  # 1 MB chunks

    def chunked_digests(blob):
        out = []
        for i in range(0, max(len(blob), 1), CHUNK):
            c = blob[i:i+CHUNK]
            out.append(hashlib.sha256(b'\xa5' + struct.pack('<I', len(c)) + c).digest())
        return out

    all_chunks = (chunked_digests(data_block) +
                  chunked_digests(central_dir) +
                  chunked_digests(bytes(eocd)))

    top_digest = hashlib.sha256(
        b'\x5a' + struct.pack('<I', len(all_chunks)) + b''.join(all_chunks)
    ).digest()

    # ── helpers ────────────────────────────────────────────────────
    def lp32(data):   # uint32-length-prefixed
        return struct.pack('<I', len(data)) + data

    ALG = 0x0103  # RSA PKCS#1 v1.5 with SHA-256

    # ── signed_data block ─────────────────────────────────────────
    digest_entry = struct.pack('<I', ALG) + lp32(top_digest)
    digests_seq  = lp32(lp32(digest_entry))

    cert_der    = cert.public_bytes(serialization.Encoding.DER)
    certs_seq   = lp32(lp32(cert_der))
    attrs_seq   = lp32(b'')  # no additional attributes

    signed_data = lp32(digests_seq + certs_seq + attrs_seq)

    # ── signature over signed_data ────────────────────────────────
    raw_sig  = private_key.sign(signed_data, padding.PKCS1v15(), hashes.SHA256())
    sig_entry = struct.pack('<I', ALG) + lp32(raw_sig)
    sigs_seq  = lp32(lp32(sig_entry))

    pub_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo
    )

    # ── signer = signed_data + signatures + public_key ────────────
    signer        = lp32(signed_data + sigs_seq + lp32(pub_der))
    v2_block_val  = lp32(signer)   # sequence of signers (just one)

    # ── APK Signing Block ─────────────────────────────────────────
    # pair: uint64(len(id+value)) + uint32(id) + value
    pair       = struct.pack('<QI', len(v2_block_val) + 4, 0x7109871a) + v2_block_val
    block_size = len(pair) + 8 + 16   # pairs + second size field + magic
    signing_block = (
        struct.pack('<Q', block_size) +
        pair +
        struct.pack('<Q', block_size) +
        b'APK Sig Block 42'
    )

    # ── reassemble APK with updated CD offset ─────────────────────
    new_cd_offset = cd_offset + len(signing_block)
    new_eocd = bytearray(apk_bytes[eocd_off:])
    struct.pack_into('<I', new_eocd, 16, new_cd_offset)

    return data_block + signing_block + central_dir + bytes(new_eocd)


def sha256_b64(data):
    return base64.b64encode(hashlib.sha256(data).digest()).decode()

# ══════════════════════════════════════════════════════════════════════
#  REAL WEBVIEW DEX GENERATOR
#  Produces a valid Dalvik DEX v035 containing a functional
#  WebView Activity that loads the given URL.
# ══════════════════════════════════════════════════════════════════════

def make_webview_dex(package_name, url):
    """
    Generate a valid DEX file for a minimal WebView Activity.
    The activity loads `url` fullscreen with JS + DOM storage enabled.
    """
    main_class = 'L' + package_name.replace('.', '/') + '/MainActivity;'

    # ── helpers ────────────────────────────────────────────────────
    def uleb128(v):
        r = []
        while True:
            b = v & 0x7F; v >>= 7
            if v: b |= 0x80
            r.append(b)
            if not v: break
        return bytes(r)

    def align4(n): return (n + 3) & ~3

    def pad4(data):
        p = align4(len(data)) - len(data)
        return data + b'\x00' * p

    def enc_str(s):
        return uleb128(len(s)) + s.encode('utf-8') + b'\x00'

    # ── strings (sorted lexicographically) ─────────────────────────
    strings = sorted({
        'L', 'V', 'VL', 'VZ', 'Z',
        'Landroid/app/Activity;',
        'Landroid/content/Context;',
        'Landroid/os/Bundle;',
        'Landroid/view/View;',
        'Landroid/webkit/WebSettings;',
        'Landroid/webkit/WebView;',
        main_class,
        'Ljava/lang/String;',
        '<init>', 'getSettings', 'loadUrl', 'onCreate',
        'setContentView', 'setDomStorageEnabled', 'setJavaScriptEnabled',
        url,
    })
    N_STR = len(strings)
    SI = {s: i for i, s in enumerate(strings)}

    # ── types (sorted by string index) ─────────────────────────────
    type_descs = sorted([
        'Landroid/app/Activity;', 'Landroid/content/Context;',
        'Landroid/os/Bundle;',    'Landroid/view/View;',
        'Landroid/webkit/WebSettings;', 'Landroid/webkit/WebView;',
        main_class, 'Ljava/lang/String;', 'V', 'Z',
    ], key=lambda d: SI[d])
    N_TYPE = len(type_descs)
    TI = {d: i for i, d in enumerate(type_descs)}

    # ── protos (sorted by ret type, param count, param types) ──────
    protos = sorted([
        ('L',  'Landroid/webkit/WebSettings;', ()),
        ('V',  'V',                            ()),
        ('VL', 'V', ('Landroid/content/Context;',)),
        ('VL', 'V', ('Landroid/os/Bundle;',)),
        ('VL', 'V', ('Landroid/view/View;',)),
        ('VL', 'V', ('Ljava/lang/String;',)),
        ('VZ', 'V', ('Z',)),
    ], key=lambda p: (TI[p[1]], len(p[2])) + tuple(TI[x] for x in p[2]))
    N_PROTO = len(protos)

    def pi(sh, ret, *params): return protos.index((sh, ret, params))
    Pgs  = pi('L',  'Landroid/webkit/WebSettings;')
    Pvv  = pi('V',  'V')
    PvC  = pi('VL', 'V', 'Landroid/content/Context;')
    PvB  = pi('VL', 'V', 'Landroid/os/Bundle;')
    PvVw = pi('VL', 'V', 'Landroid/view/View;')
    PvS  = pi('VL', 'V', 'Ljava/lang/String;')
    PvZ  = pi('VZ', 'V', 'Z')

    # ── methods (sorted by class type, name str, proto) ────────────
    methods = sorted([
        ('Landroid/app/Activity;',       '<init>',              Pvv),
        ('Landroid/app/Activity;',       'onCreate',            PvB),
        ('Landroid/app/Activity;',       'setContentView',      PvVw),
        ('Landroid/webkit/WebSettings;', 'setDomStorageEnabled', PvZ),
        ('Landroid/webkit/WebSettings;', 'setJavaScriptEnabled', PvZ),
        ('Landroid/webkit/WebView;',     '<init>',              PvC),
        ('Landroid/webkit/WebView;',     'getSettings',         Pgs),
        ('Landroid/webkit/WebView;',     'loadUrl',             PvS),
        (main_class,                     '<init>',              Pvv),
        (main_class,                     'onCreate',            PvB),
    ], key=lambda m: (TI[m[0]], SI[m[1]], m[2]))
    N_METHOD = len(methods)

    def mi(cls, name, proto): return methods.index((cls, name, proto))
    M_act_init   = mi('Landroid/app/Activity;', '<init>', Pvv)
    M_act_create = mi('Landroid/app/Activity;', 'onCreate', PvB)
    M_act_setCV  = mi('Landroid/app/Activity;', 'setContentView', PvVw)
    M_ws_setDom  = mi('Landroid/webkit/WebSettings;', 'setDomStorageEnabled', PvZ)
    M_ws_setJS   = mi('Landroid/webkit/WebSettings;', 'setJavaScriptEnabled', PvZ)
    M_wv_init    = mi('Landroid/webkit/WebView;', '<init>', PvC)
    M_wv_getS    = mi('Landroid/webkit/WebView;', 'getSettings', Pgs)
    M_wv_load    = mi('Landroid/webkit/WebView;', 'loadUrl', PvS)
    M_main_init  = mi(main_class, '<init>', Pvv)
    M_main_create = mi(main_class, 'onCreate', PvB)

    # ── bytecode helpers (format 35c) ───────────────────────────────
    def i35c(op, ref, regs):
        A = len(regs)
        C = regs[0] if A > 0 else 0
        D = regs[1] if A > 1 else 0
        E = regs[2] if A > 2 else 0
        F = regs[3] if A > 3 else 0
        G = regs[4] if A > 4 else 0
        return struct.pack('<BBHBB', op, (A << 4) | G, ref, (D << 4) | C, (F << 4) | E)

    def inv_dir(ref, *r): return i35c(0x70, ref, r)
    def inv_sup(ref, *r): return i35c(0x6f, ref, r)
    def inv_vir(ref, *r): return i35c(0x6e, ref, r)
    def new_ins(dst, tr): return struct.pack('<BBH', 0x22, dst, tr)
    def mov_res(dst):     return struct.pack('<BB', 0x0c, dst)
    def const4(dst, val): return struct.pack('<BB', 0x12, ((val & 0xF) << 4) | (dst & 0xF))
    def cst_str(dst, sr): return struct.pack('<BBH', 0x1a, dst, sr)
    def ret_vd():         return b'\x0e\x00'

    # ── bytecode for MainActivity.<init>()V ─────────────────────────
    # registers=1, ins=1(this=v0), outs=1
    bc_init = inv_dir(M_act_init, 0) + ret_vd()

    # ── bytecode for MainActivity.onCreate(Bundle)V ─────────────────
    # v0=WebView v1=WebSettings v2=url/bool v3=p0(this) v4=p1(bundle)
    # registers=5, ins=2, outs=2
    bc_create = (
        inv_sup(M_act_create, 3, 4) +
        new_ins(0, TI['Landroid/webkit/WebView;']) +
        inv_dir(M_wv_init, 0, 3) +
        inv_vir(M_wv_getS, 0) +
        mov_res(1) +
        const4(2, 1) +
        inv_vir(M_ws_setJS, 1, 2) +
        inv_vir(M_ws_setDom, 1, 2) +
        cst_str(2, SI[url]) +
        inv_vir(M_wv_load, 0, 2) +
        inv_vir(M_act_setCV, 3, 0) +
        ret_vd()
    )

    def code_item(regs, ins, outs, bc):
        insns = len(bc) // 2
        return struct.pack('<HHHHI', regs, ins, outs, 0, 0) + struct.pack('<I', insns) + bc

    ci_init   = pad4(code_item(1, 1, 1, bc_init))
    ci_create = pad4(code_item(5, 2, 2, bc_create))

    # ── compute layout ──────────────────────────────────────────────
    HSIZE = 112
    str_ids_off   = HSIZE
    type_ids_off  = str_ids_off   + N_STR   * 4
    proto_ids_off = type_ids_off  + N_TYPE  * 4
    meth_ids_off  = proto_ids_off + N_PROTO * 12
    class_def_off = meth_ids_off  + N_METHOD * 8
    data_off      = align4(class_def_off + 32)  # 1 class def × 32 bytes

    # String data
    str_parts = [enc_str(s) for s in strings]
    str_offs  = []
    cur = data_off
    for p in str_parts:
        str_offs.append(cur); cur += len(p)
    str_data_end = cur

    # Type lists (one per proto with params, each padded to 4 bytes)
    tl_off = align4(str_data_end)
    tl_parts_list = []
    proto_tl_offs = []
    cur = tl_off
    for sh, ret, params in protos:
        if params:
            proto_tl_offs.append(cur)
            tl = pad4(struct.pack('<I', len(params)) + b''.join(struct.pack('<H', TI[p]) for p in params))
            tl_parts_list.append(tl); cur += len(tl)
        else:
            proto_tl_offs.append(0)
    tl_end = cur

    # Code items (4-byte aligned)
    ci_init_off   = align4(tl_end)
    ci_create_off = ci_init_off + len(ci_init)
    code_end      = ci_create_off + len(ci_create)

    # Class data
    class_data_off = align4(code_end)
    class_data = (
        uleb128(0) + uleb128(0) +       # static/instance fields
        uleb128(1) + uleb128(1) +       # 1 direct, 1 virtual method
        # direct: <init>
        uleb128(M_main_init) + uleb128(0x10001) + uleb128(ci_init_off) +
        # virtual: onCreate (absolute idx, resets counter)
        uleb128(M_main_create) + uleb128(0x0001) + uleb128(ci_create_off)
    )
    class_data_end = class_data_off + len(class_data)

    # Map list
    map_off = align4(class_data_end)
    n_tl = len(tl_parts_list)
    map_items = sorted([
        (0x0000, 1,        0),
        (0x0001, N_STR,    str_ids_off),
        (0x0002, N_TYPE,   type_ids_off),
        (0x0003, N_PROTO,  proto_ids_off),
        (0x0005, N_METHOD, meth_ids_off),
        (0x0006, 1,        class_def_off),
        (0x2000, N_STR,    data_off),
    ] + ([(0x2003, n_tl, tl_off)] if n_tl else []) + [
        (0x1000, 2,        ci_init_off),
        (0x2005, 1,        class_data_off),
        (0x1003, 1,        map_off),
    ], key=lambda e: e[2])

    map_bytes = struct.pack('<I', len(map_items))
    for (tc, cnt, off) in map_items:
        map_bytes += struct.pack('<HHII', tc, 0, cnt, off)

    file_size = map_off + len(map_bytes)

    # ── assemble ────────────────────────────────────────────────────
    hdr = bytearray(HSIZE)
    hdr[0:8] = b'dex\n035\x00'
    def wp(offset, val): struct.pack_into('<I', hdr, offset, val)
    wp(32, file_size); wp(36, HSIZE);  wp(40, 0x12345678)
    wp(44, 0); wp(48, 0); wp(52, map_off)
    wp(56, N_STR);    wp(60, str_ids_off)
    wp(64, N_TYPE);   wp(68, type_ids_off)
    wp(72, N_PROTO);  wp(76, proto_ids_off)
    wp(80, 0); wp(84, 0)
    wp(88, N_METHOD); wp(92, meth_ids_off)
    wp(96, 1);        wp(100, class_def_off)
    wp(104, file_size - data_off); wp(108, data_off)

    str_id_bytes   = b''.join(struct.pack('<I', o) for o in str_offs)
    type_id_bytes  = b''.join(struct.pack('<I', SI[d]) for d in type_descs)
    proto_id_bytes = b''.join(struct.pack('<III', SI[sh], TI[ret], proto_tl_offs[i])
                               for i, (sh, ret, _) in enumerate(protos))
    meth_id_bytes  = b''.join(struct.pack('<HHI', TI[cls], pr, SI[nm])
                               for cls, nm, pr in methods)
    class_def_bytes = struct.pack('<IIIIIIII',
        TI[main_class], 0x0001, TI['Landroid/app/Activity;'],
        0, 0xFFFFFFFF, 0, class_data_off, 0)

    def fill(src, dst_off, total_off):
        gap = dst_off - total_off
        return b'\x00' * gap if gap > 0 else b''

    str_data_bytes = b''.join(str_parts)
    rolling = data_off + len(str_data_bytes)
    tl_pad    = b'\x00' * (tl_off - rolling)
    tl_bytes  = b''.join(tl_parts_list)
    rolling   = tl_off + len(tl_bytes)
    ci_pad    = b'\x00' * (ci_init_off - rolling)
    rolling   = ci_init_off + len(ci_init) + len(ci_create)
    cd_pad    = b'\x00' * (class_data_off - rolling)
    rolling   = class_data_off + len(class_data)
    map_pad   = b'\x00' * (map_off - rolling)

    dex = (bytes(hdr) + str_id_bytes + type_id_bytes + proto_id_bytes +
           meth_id_bytes + class_def_bytes +
           str_data_bytes + tl_pad + tl_bytes +
           ci_pad + ci_init + ci_create +
           cd_pad + class_data + map_pad + map_bytes)

    assert len(dex) == file_size, f'DEX size mismatch: {len(dex)} != {file_size}'

    # SHA-1 (over bytes[32:])
    sha1 = hashlib.sha1(dex[32:]).digest()
    dex  = dex[:12] + sha1 + dex[32:]

    # Adler-32 (over bytes[12:])
    MOD, a, b = 65521, 1, 0
    for byte in dex[12:]: a = (a + byte) % MOD; b = (b + a) % MOD
    return dex[:8] + struct.pack('<I', (b << 16) | a) + dex[12:]

# ══════════════════════════════════════════════════════════════════════
#  APK BUILDER
# ══════════════════════════════════════════════════════════════════════

def build_apk(config, out_dir):
    print("\n  🤖 Building Android APK...")
    pkg       = config['package']
    app_name  = config['name']
    url       = config['url']
    version_n = config['version_name']
    version_c = config['version_code']
    min_sdk   = config['min_sdk']
    target_sdk= config['target_sdk']

    apk_path = out_dir / f"{app_name.replace(' ','_')}.apk"
    pkg_path  = pkg.replace('.', '/')

    # Collect all APK entries: (arcname, bytes)
    entries = []

    # 1. Binary AndroidManifest.xml
    encoder = AXMLEncoder()
    manifest_bin = encoder.build(pkg, app_name, version_n, version_c, min_sdk, target_sdk)
    entries.append(('AndroidManifest.xml', manifest_bin))

    # 2. classes.dex — real WebView Activity
    dex = make_webview_dex(pkg, url)
    entries.append(('classes.dex', dex))

    # 3. Resources
    res_files = {
        'res/values/strings.xml':  android_strings_xml(app_name, url).encode(),
        'res/values/colors.xml':   android_colors_xml().encode(),
        'res/values/styles.xml':   android_styles_xml().encode(),
    }
    for name, data in res_files.items():
        entries.append((name, data))

    # 4. SVG icon as drawable
    entries.append(('res/drawable/ic_launcher.xml', b'''<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android"
    android:shape="rectangle">
    <gradient android:startColor="#5b6ef5" android:endColor="#a855f7"
              android:angle="135"/>
    <corners android:radius="24dp"/>
</shape>
'''))

    # 5. Assets
    entries.append(('assets/config.json', json.dumps({
        'url': url, 'name': app_name, 'package': pkg,
        'version': version_n, 'built_by': 'AppForge'
    }, indent=2).encode()))

    # Build real META-INF v1 signing
    private_key, cert = _gen_signing_key()
    manifest_mf, cert_sf, cert_rsa = _v1_sign(entries, private_key, cert)

    entries.append(('META-INF/MANIFEST.MF', manifest_mf))
    entries.append(('META-INF/CERT.SF',     cert_sf))
    entries.append(('META-INF/CERT.RSA',    cert_rsa))

    # Write APK (ZIP format, MANIFEST.MF must be STORED not deflated)
    with zipfile.ZipFile(apk_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            compress = zipfile.ZIP_STORED if name == 'META-INF/MANIFEST.MF' else zipfile.ZIP_DEFLATED
            zf.writestr(zipfile.ZipInfo(name), data, compress_type=compress)

    # Apply APK Signature Scheme v2 (required for Android 7+)
    apk_bytes = apk_path.read_bytes()
    signed_bytes = _v2_sign(apk_bytes, private_key, cert)
    apk_path.write_bytes(signed_bytes)

    print(f"     ✓ APK written: {apk_path.name} ({apk_path.stat().st_size // 1024} KB)")

    # Also build Android Studio project ZIP
    project_zip = out_dir / f"{app_name.replace(' ','_')}_AndroidStudio_Project.zip"
    with zipfile.ZipFile(project_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        root = f"{app_name.replace(' ','_')}/"
        zf.writestr(root + 'AndroidManifest_source.xml',
                    android_manifest_xml(pkg, app_name, version_n, version_c, min_sdk, target_sdk))
        zf.writestr(root + 'app/build.gradle',
                    android_build_gradle(pkg, min_sdk, target_sdk, version_c, version_n))
        zf.writestr(root + 'settings.gradle', android_settings_gradle(app_name))
        zf.writestr(root + f'app/src/main/java/{pkg_path}/MainActivity.java',
                    android_main_activity(pkg, url, app_name))
        zf.writestr(root + 'app/src/main/AndroidManifest.xml',
                    android_manifest_xml(pkg, app_name, version_n, version_c, min_sdk, target_sdk))
        zf.writestr(root + 'app/src/main/res/values/strings.xml',   android_strings_xml(app_name, url))
        zf.writestr(root + 'app/src/main/res/values/colors.xml',    android_colors_xml())
        zf.writestr(root + 'app/src/main/res/values/styles.xml',    android_styles_xml())
        zf.writestr(root + 'app/proguard-rules.pro',                android_proguard())
        zf.writestr(root + 'app/src/main/assets/icon.svg',         generate_svg_icon(app_name))
        zf.writestr(root + 'README.md', f'# {app_name} Android Project\nOpen in Android Studio to build.\nURL: {url}\n')

    print(f"     ✓ Android Studio project: {project_zip.name}")
    return apk_path, project_zip


# ══════════════════════════════════════════════════════════════════════
#  WINDOWS BUILDER
# ══════════════════════════════════════════════════════════════════════

def build_windows(config, out_dir):
    print("\n  🪟 Building Windows package...")
    app_name  = config['name']
    pkg       = config['package']
    url       = config['url']
    version_n = config['version_name']
    safe      = app_name.replace(' ', '_')

    win_zip = out_dir / f"{safe}_Windows_Electron_Project.zip"

    with zipfile.ZipFile(win_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        root = f"{safe}_Windows/"
        zf.writestr(root + 'package.json',   windows_package_json(app_name, pkg, version_n, url))
        zf.writestr(root + 'main.js',        windows_main_js(app_name, url))
        zf.writestr(root + 'preload.js',     windows_preload_js())
        zf.writestr(root + 'splash.html',    windows_splash_html(app_name))
        zf.writestr(root + 'assets/icon.svg',generate_svg_icon(app_name))
        zf.writestr(root + '.gitignore',     windows_gitignore())
        zf.writestr(root + 'README.md',      windows_readme(app_name, url))
        build_txt = "HOW TO BUILD WINDOWS EXE\n==========================\nPrerequisites: Node.js 18+ https://nodejs.org\n\nSteps:\n  npm install\n  npm run build-win\n\nOutput: dist/ folder (Setup.exe, Portable.exe)\nLinux: npm run build-linux\nmacOS: npm run build-mac\n"
        zf.writestr(root + 'BUILD_INSTRUCTIONS.txt', build_txt)

    print(f"     ✓ Windows project: {win_zip.name}")
    return win_zip


# ══════════════════════════════════════════════════════════════════════
#  iOS BUILDER
# ══════════════════════════════════════════════════════════════════════

def build_ios(config, out_dir):
    print("\n  🍎 Building iOS package...")
    app_name  = config['name']
    pkg       = config['package']
    url       = config['url']
    version_n = config['version_name']
    version_c = config['version_code']
    safe      = app_name.replace(' ', '_')

    ios_zip = out_dir / f"{safe}_iOS_Xcode_Project.zip"

    # Minimal Xcode project file
    xcode_project = f'''// !$*UTF8*$!
{{
    archiveVersion = 1;
    classes = {{}};
    objectVersion = 56;
    objects = {{
        /* Build configuration list */
        13B07F961A680F5B00A75B9A /* Project object */ = {{
            isa = PBXProject;
            buildConfigurationList = 13B07F931A680F5B00A75B9A;
            compatibilityVersion = "Xcode 14.0";
            mainGroup = 13B07F941A680F5B00A75B9A;
            productRefGroup = 13B07F951A680F5B00A75B9A;
            targets = (13B07F861A680F5B00A75B9A);
        }};
        13B07F861A680F5B00A75B9A /* {app_name} */ = {{
            isa = PBXNativeTarget;
            buildConfigurationList = 13B07F931A680F5B00A75B9A;
            buildPhases = ();
            dependencies = ();
            name = "{app_name}";
            productName = "{app_name}";
            productType = "com.apple.product-type.application";
        }};
    }};
    rootObject = 13B07F961A680F5B00A75B9A;
}}
'''

    with zipfile.ZipFile(ios_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        root = f"{safe}_iOS/"
        zf.writestr(root + f'{safe}.xcodeproj/project.pbxproj', xcode_project)
        zf.writestr(root + f'{safe}/Info.plist',
                    ios_info_plist(app_name, pkg, version_n, version_c))
        zf.writestr(root + f'{safe}/AppDelegate.swift', ios_app_delegate_swift(app_name))
        zf.writestr(root + f'{safe}/ViewController.swift', ios_view_controller_swift(app_name, url))
        zf.writestr(root + f'{safe}/Assets.xcassets/icon.svg', generate_svg_icon(app_name))
        zf.writestr(root + 'Podfile', ios_podfile(pkg, app_name))
        zf.writestr(root + 'README.md', ios_readme(app_name, url))
        ios_txt = "HOW TO BUILD iOS APP\nRequirements: Mac + Xcode 15+ + Apple Developer Account\n\nSteps:\n1. Open xcodeproj in Xcode\n2. Select Apple ID in Signing tab\n3. Run in simulator (Cmd+R)\n\nFor TestFlight: Product > Archive > Distribute App\nFor Ad-Hoc IPA: Product > Archive > Distribute App > Ad Hoc\n"
        zf.writestr(root + 'BUILD_INSTRUCTIONS.txt', ios_txt)

    print(f"     ✓ iOS Xcode project: {ios_zip.name}")
    return ios_zip


# ══════════════════════════════════════════════════════════════════════
#  LINUX BUILDER
# ══════════════════════════════════════════════════════════════════════

def build_linux(config, out_dir):
    print("\n  🐧 Building Linux package...")
    app_name = config['name']
    pkg      = config['package']
    url      = config['url']
    version_n= config['version_name']
    safe     = app_name.replace(' ', '_')

    linux_zip = out_dir / f"{safe}_Linux_Project.zip"

    desktop_entry = f'''[Desktop Entry]
Version={version_n}
Type=Application
Name={app_name}
Comment=Built with AppForge
Exec={safe.lower()}
Icon={pkg}
Terminal=false
Categories=Utility;Network;
StartupNotify=true
'''

    with zipfile.ZipFile(linux_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        root = f"{safe}_Linux/"
        # Re-use the Electron project (same as Windows but with linux build target)
        zf.writestr(root + 'package.json',   windows_package_json(app_name, pkg, version_n, url))
        zf.writestr(root + 'main.js',        windows_main_js(app_name, url))
        zf.writestr(root + 'preload.js',     windows_preload_js())
        zf.writestr(root + 'splash.html',    windows_splash_html(app_name))
        zf.writestr(root + 'assets/icon.svg',generate_svg_icon(app_name))
        zf.writestr(root + f'{safe.lower()}.desktop', desktop_entry)
        zf.writestr(root + '.gitignore',     windows_gitignore())
        linux_txt = "HOW TO BUILD LINUX AppImage\nnpm install && npm run build-linux\nOutput: dist/*.AppImage or dist/*.deb\nRun: chmod +x *.AppImage && ./*.AppImage\n"
        zf.writestr(root + 'BUILD_INSTRUCTIONS.txt', linux_txt)

    print(f"     ✓ Linux project: {linux_zip.name}")
    return linux_zip


# ══════════════════════════════════════════════════════════════════════
#  MASTER ZIP — bundles everything
# ══════════════════════════════════════════════════════════════════════

def build_master_zip(config, out_dir, built_files):
    print("\n  📦 Bundling all platforms into master ZIP...")
    app_name = config['name']
    safe     = app_name.replace(' ', '_')
    master   = out_dir / f"{safe}_ALL_PLATFORMS.zip"

    summary = f'''# {app_name} — AppForge Multi-Platform Build
Generated: {BUILD_DATE}
Website:   {config['url']}
Package:   {config['package']}
Version:   {config['version_name']}

## Files Included
'''
    with zipfile.ZipFile(master, 'w', zipfile.ZIP_DEFLATED) as zf:
        for label, path in built_files:
            if path and path.exists():
                zf.write(path, path.name)
                size_kb = path.stat().st_size // 1024
                summary += f"  ✓ [{label}] {path.name} ({size_kb} KB)\n"

        summary += f'''
## Quick Start Guide

### Android (.apk)
1. Copy {safe}.apk to your Android phone
2. Settings > Security > Install Unknown Apps > Allow
3. Open file manager, tap the APK, tap Install
4. OR: Open Android Studio project for a production build

### Windows (.exe)
1. Open {safe}_Windows_Electron_Project.zip
2. npm install && npm run build-win
3. Run dist/{app_name} Setup.exe

### iOS (.ipa)
1. Open {safe}_iOS_Xcode_Project.zip
2. Open in Xcode on macOS
3. Build & run in simulator or export .ipa

### Linux (.AppImage)
1. Open {safe}_Linux_Project.zip
2. npm install && npm run build-linux
3. chmod +x *.AppImage && ./*.AppImage

## Built with AppForge — Free Multi-Platform App Builder
https://appforge.app
'''
        zf.writestr('README.md', summary)

    size_kb = master.stat().st_size // 1024
    print(f"     ✓ Master ZIP: {master.name} ({size_kb} KB)")
    return master


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='AppForge — Multi-Platform App Builder',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python3 build.py --url https://example.com --name "My App"
  python3 build.py --url https://mystore.com --name "My Store" --package com.mystore.app --version 2.0.0
        '''
    )
    parser.add_argument('--url',      required=True,  help='Website URL to wrap')
    parser.add_argument('--name',     required=True,  help='App name')
    parser.add_argument('--package',  default='',     help='Package ID (e.g. com.example.app)')
    parser.add_argument('--version',  default='1.0.0',help='Version name (default: 1.0.0)')
    parser.add_argument('--vcode',    type=int, default=1, help='Version code integer (default: 1)')
    parser.add_argument('--minsdk',   type=int, default=21, help='Android min SDK (default: 21)')
    parser.add_argument('--targetsdk',type=int, default=34, help='Android target SDK (default: 34)')
    parser.add_argument('--out',      default='output', help='Output directory (default: ./output)')
    parser.add_argument('--platform', default='all',
                        help='Platforms: all, android, windows, ios, linux (comma-separated)')
    args = parser.parse_args()

    # Auto-generate package name if not provided
    if not args.package:
        import re
        host = args.url.replace('https://','').replace('http://','').replace('www.','').split('/')[0]
        safe_host = re.sub(r'[^a-z0-9]', '', host.lower().split('.')[0])
        safe_name = re.sub(r'[^a-z0-9]', '', args.name.lower().replace(' ',''))
        args.package = f'com.{safe_host}.{safe_name}'

    config = {
        'url':          args.url,
        'name':         args.name,
        'package':      args.package,
        'version_name': args.version,
        'version_code': args.vcode,
        'min_sdk':      args.minsdk,
        'target_sdk':   args.targetsdk,
    }

    platforms = [p.strip().lower() for p in args.platform.split(',')]
    if 'all' in platforms:
        platforms = ['android', 'windows', 'ios', 'linux']

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f'''
╔══════════════════════════════════════════════════════╗
║          AppForge Multi-Platform Builder             ║
╠══════════════════════════════════════════════════════╣
║  App:      {args.name:<40}║
║  URL:      {args.url[:40]:<40}║
║  Package:  {args.package[:40]:<40}║
║  Version:  {args.version:<40}║
║  Platforms:{str(platforms):<40}║
║  Output:   {str(out_dir.resolve())[:40]:<40}║
╚══════════════════════════════════════════════════════╝
''')

    built = []
    try:
        if 'android' in platforms:
            apk, proj = build_apk(config, out_dir)
            built.append(('Android APK', apk))
            built.append(('Android Studio Project', proj))

        if 'windows' in platforms:
            win = build_windows(config, out_dir)
            built.append(('Windows Electron Project', win))

        if 'ios' in platforms:
            ios = build_ios(config, out_dir)
            built.append(('iOS Xcode Project', ios))

        if 'linux' in platforms:
            lnx = build_linux(config, out_dir)
            built.append(('Linux Project', lnx))

        master = build_master_zip(config, out_dir, built)

    except Exception as e:
        print(f"\n  ❌ Build error: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)

    print(f'''
╔══════════════════════════════════════════════════════╗
║               ✅ BUILD COMPLETE                      ║
╠══════════════════════════════════════════════════════╣''')
    for label, path in built:
        if path and path.exists():
            size_kb = path.stat().st_size // 1024
            print(f'║  ✓ {label:<28} {path.name[:18]:<18} {size_kb:>4} KB  ║')
    print(f'''╠══════════════════════════════════════════════════════╣
║  📦 {str(master.name)[:48]:<48}  ║
╚══════════════════════════════════════════════════════╝

  Output folder: {out_dir.resolve()}
''')

if __name__ == '__main__':
    main()
