from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "build"
CLASSES_DIR = OUT_DIR / "classes"
DEX_DIR = OUT_DIR / "dex"
GEN_DIR = OUT_DIR / "gen"
RES_DIR = ROOT / "res"
SRC_DIR = ROOT / "src"
APK_OUT = OUT_DIR / "pixel-oauth-helper.apk"
KEYSTORE = OUT_DIR / "debug.keystore"


def run(cmd: list[str], **kwargs) -> None:
    print(" ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def first_existing(paths: list[Path]) -> Path | None:
    for p in paths:
        if p.exists():
            return p
    return None


def find_sdk() -> Path:
    candidates = []
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = (os.environ.get(key) or "").strip()
        if value:
            candidates.append(Path(value))
    candidates.append(Path.home() / "AppData" / "Local" / "Android" / "Sdk")
    sdk = first_existing(candidates)
    if not sdk:
        raise SystemExit("Android SDK not found. Set ANDROID_HOME or install Android Studio SDK.")
    return sdk


def find_java_home() -> Path:
    candidates = []
    java_home_env = (os.environ.get("JAVA_HOME") or "").strip()
    if java_home_env:
        candidates.append(Path(java_home_env))
    candidates += [
        Path("C:/Program Files/Android/Android Studio/jbr"),
        Path("C:/Program Files/Android/Android Studio/jre"),
    ]
    java_home = first_existing([p for p in candidates if (p / "bin" / "javac.exe").exists()])
    if not java_home:
        raise SystemExit("JDK not found. Install Android Studio or set JAVA_HOME.")
    return java_home


def newest_child(parent: Path, predicate) -> Path:
    children = [p for p in parent.iterdir() if predicate(p)]
    if not children:
        raise SystemExit(f"No usable entry found under {parent}")
    return sorted(children, key=lambda p: p.name)[-1]


def ensure_res() -> None:
    values = RES_DIR / "values"
    values.mkdir(parents=True, exist_ok=True)
    strings = values / "strings.xml"
    if not strings.exists():
        strings.write_text(
            '<resources><string name="app_name">Pixel OAuth Helper</string></resources>\n',
            encoding="utf-8",
        )


def main() -> int:
    sdk = find_sdk()
    java_home = find_java_home()
    platform = newest_child(sdk / "platforms", lambda p: (p / "android.jar").exists())
    build_tools = newest_child(sdk / "build-tools", lambda p: (p / "aapt2.exe").exists())

    android_jar = platform / "android.jar"
    aapt2 = build_tools / "aapt2.exe"
    d8 = build_tools / "d8.bat"
    zipalign = build_tools / "zipalign.exe"
    apksigner = build_tools / "apksigner.bat"
    javac = java_home / "bin" / "javac.exe"
    jar = java_home / "bin" / "jar.exe"
    keytool = java_home / "bin" / "keytool.exe"

    shutil.rmtree(OUT_DIR, ignore_errors=True)
    OUT_DIR.mkdir(parents=True)
    CLASSES_DIR.mkdir()
    DEX_DIR.mkdir()
    GEN_DIR.mkdir()
    ensure_res()

    env = os.environ.copy()
    env["JAVA_HOME"] = str(java_home)
    env["PATH"] = str(java_home / "bin") + os.pathsep + env.get("PATH", "")

    compiled_res = OUT_DIR / "compiled.zip"
    unsigned_apk = OUT_DIR / "unsigned.apk"
    dexed_apk = OUT_DIR / "unsigned-dex.apk"
    aligned_apk = OUT_DIR / "aligned.apk"

    run([aapt2, "compile", "--dir", RES_DIR, "-o", compiled_res], env=env)
    run([
        aapt2,
        "link",
        "-o",
        unsigned_apk,
        "-I",
        android_jar,
        "--manifest",
        ROOT / "AndroidManifest.xml",
        "--java",
        GEN_DIR,
        compiled_res,
    ], env=env)

    sources = [str(p) for p in SRC_DIR.rglob("*.java")] + [str(p) for p in GEN_DIR.rglob("*.java")]
    run([
        javac,
        "-source",
        "8",
        "-target",
        "8",
        "-encoding",
        "UTF-8",
        "-classpath",
        android_jar,
        "-d",
        CLASSES_DIR,
        *sources,
    ], env=env)

    class_files = [str(p) for p in CLASSES_DIR.rglob("*.class")]
    run([d8, "--classpath", android_jar, "--output", DEX_DIR, *class_files], env=env)

    shutil.copyfile(unsigned_apk, dexed_apk)
    run([jar, "uf", dexed_apk, "-C", DEX_DIR, "classes.dex"], env=env)
    run([zipalign, "-f", "4", dexed_apk, aligned_apk], env=env)

    run([
        keytool,
        "-genkeypair",
        "-keystore",
        KEYSTORE,
        "-storepass",
        "android",
        "-keypass",
        "android",
        "-alias",
        "androiddebugkey",
        "-dname",
        "CN=Android Debug,O=Android,C=US",
        "-keyalg",
        "RSA",
        "-keysize",
        "2048",
        "-validity",
        "10000",
    ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    run([
        apksigner,
        "sign",
        "--ks",
        KEYSTORE,
        "--ks-pass",
        "pass:android",
        "--key-pass",
        "pass:android",
        "--out",
        APK_OUT,
        aligned_apk,
    ], env=env)
    run([apksigner, "verify", APK_OUT], env=env)

    print(f"Built {APK_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
