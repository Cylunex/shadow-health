# build.ps1 - manual APK pipeline for the shadow-health WebView shell.
# No Gradle. Pipeline: aapt2 compile -> aapt2 link -> javac -> d8 ->
# inject classes.dex -> zipalign -> apksigner.
#
# Toolchain layout (created once, gitignored, see android/README.md):
#   <repo>/tools/android/jdk-17/                JDK 17 (any jdk* dir works)
#   <repo>/tools/android/sdk/platforms/android-34/android.jar
#   <repo>/tools/android/sdk/build-tools/34.0.0/{aapt2,zipalign,d8,apksigner}
#
# Usage:  powershell -ExecutionPolicy Bypass -File android\build.ps1
# Output: <repo>/dist/shadow-health.apk

param(
    [string]$VersionName = "1.0",
    [int]$VersionCode = 1
)

$ErrorActionPreference = "Stop"

$AndroidDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot   = Split-Path -Parent $AndroidDir
$ToolsRoot  = Join-Path $RepoRoot "tools\android"
$BuildDir   = Join-Path $AndroidDir "build"
$DistDir    = Join-Path $RepoRoot "dist"
$ApkOut     = Join-Path $DistDir "shadow-health.apk"
$Keystore   = Join-Path $ToolsRoot "debug.keystore"

function Fail([string]$msg) {
    Write-Host "BUILD FAILED: $msg" -ForegroundColor Red
    exit 1
}

function Check-Exit([string]$step) {
    if ($LASTEXITCODE -ne 0) { Fail "$step exited with code $LASTEXITCODE" }
}

# ---- locate toolchain -------------------------------------------------------

if (-not (Test-Path $ToolsRoot)) {
    Fail "toolchain dir not found: $ToolsRoot  (see android/README.md for setup)"
}

$Jdk = Get-ChildItem $ToolsRoot -Directory -Filter "jdk*" |
    Where-Object { Test-Path (Join-Path $_.FullName "bin\javac.exe") } |
    Select-Object -First 1
if ($null -eq $Jdk) { Fail "no JDK found under $ToolsRoot (expected jdk*\bin\javac.exe)" }
$JdkBin = Join-Path $Jdk.FullName "bin"

$BtRoot = Join-Path $ToolsRoot "sdk\build-tools"
if (-not (Test-Path $BtRoot)) { Fail "no build-tools under $ToolsRoot\sdk" }
$BuildTools = Get-ChildItem $BtRoot -Directory | Sort-Object Name -Descending | Select-Object -First 1
if ($null -eq $BuildTools) { Fail "build-tools dir is empty: $BtRoot" }
$Bt = $BuildTools.FullName

$PlatRoot = Join-Path $ToolsRoot "sdk\platforms"
$Platform = Get-ChildItem $PlatRoot -Directory -Filter "android-*" -ErrorAction SilentlyContinue |
    Sort-Object { [int]($_.Name -replace "android-", "") } | Select-Object -Last 1
if ($null -eq $Platform) { Fail "no platform under $PlatRoot (expected android-34)" }
$AndroidJar = Join-Path $Platform.FullName "android.jar"
if (-not (Test-Path $AndroidJar)) { Fail "android.jar missing in $($Platform.FullName)" }

$Aapt2     = Join-Path $Bt "aapt2.exe"
$Zipalign  = Join-Path $Bt "zipalign.exe"
$D8        = Join-Path $Bt "d8.bat"
$ApkSigner = Join-Path $Bt "apksigner.bat"
foreach ($t in @($Aapt2, $Zipalign, $D8, $ApkSigner)) {
    if (-not (Test-Path $t)) { Fail "missing build tool: $t" }
}

# d8.bat / apksigner.bat resolve java via PATH or JAVA_HOME
$env:JAVA_HOME = $Jdk.FullName
$env:PATH = "$JdkBin;$env:PATH"

Write-Host "JDK:         $($Jdk.FullName)"
Write-Host "build-tools: $Bt"
Write-Host "platform:    $($Platform.FullName)"

# ---- clean ------------------------------------------------------------------

if (Test-Path $BuildDir) { Remove-Item $BuildDir -Recurse -Force -Confirm:$false }
New-Item -ItemType Directory -Force $BuildDir | Out-Null
New-Item -ItemType Directory -Force $DistDir  | Out-Null

# ---- 1. aapt2 compile resources ----------------------------------------------

Write-Host "[1/7] aapt2 compile"
& $Aapt2 compile --dir (Join-Path $AndroidDir "res") -o (Join-Path $BuildDir "res.zip")
Check-Exit "aapt2 compile"

# ---- 2. aapt2 link -> base apk + R.java ---------------------------------------

Write-Host "[2/7] aapt2 link"
$GenDir = Join-Path $BuildDir "gen"
New-Item -ItemType Directory -Force $GenDir | Out-Null
& $Aapt2 link `
    -o (Join-Path $BuildDir "base.apk") `
    --manifest (Join-Path $AndroidDir "AndroidManifest.xml") `
    -I $AndroidJar `
    --java $GenDir `
    --min-sdk-version 26 --target-sdk-version 34 `
    --version-code $VersionCode --version-name $VersionName `
    (Join-Path $BuildDir "res.zip")
Check-Exit "aapt2 link"

# ---- 3. javac -----------------------------------------------------------------

Write-Host "[3/7] javac"
$ClassesDir = Join-Path $BuildDir "classes"
New-Item -ItemType Directory -Force $ClassesDir | Out-Null
$Sources = @(Get-ChildItem (Join-Path $AndroidDir "src") -Recurse -Filter "*.java" | ForEach-Object { $_.FullName })
$Sources += @(Get-ChildItem $GenDir -Recurse -Filter "*.java" | ForEach-Object { $_.FullName })
# --release 8: java.* comes from the JDK's platform definition (android.jar
# lacks LambdaMetafactory, so -bootclasspath android.jar breaks lambdas);
# android.* comes from the classpath; d8 desugars for the device afterwards.
& (Join-Path $JdkBin "javac.exe") `
    --release 8 -Xlint:-options -nowarn `
    -classpath $AndroidJar `
    -encoding UTF-8 -d $ClassesDir @Sources
Check-Exit "javac"

# ---- 4. d8 -> classes.dex -------------------------------------------------------

Write-Host "[4/7] d8"
$DexDir = Join-Path $BuildDir "dex"
New-Item -ItemType Directory -Force $DexDir | Out-Null
$ClassFiles = @(Get-ChildItem $ClassesDir -Recurse -Filter "*.class" | ForEach-Object { $_.FullName })
& $D8 --release --lib $AndroidJar --min-api 26 --output $DexDir @ClassFiles
Check-Exit "d8"

# ---- 5. inject classes.dex into the apk ------------------------------------------

Write-Host "[5/7] add classes.dex"
Push-Location $DexDir
& (Join-Path $JdkBin "jar.exe") -uf (Join-Path $BuildDir "base.apk") classes.dex
$jarExit = $LASTEXITCODE
Pop-Location
if ($jarExit -ne 0) { Fail "jar update exited with code $jarExit" }

# ---- 6. zipalign ------------------------------------------------------------------

Write-Host "[6/7] zipalign"
& $Zipalign -f 4 (Join-Path $BuildDir "base.apk") (Join-Path $BuildDir "aligned.apk")
Check-Exit "zipalign"

# ---- 7. sign ----------------------------------------------------------------------

if (-not (Test-Path $Keystore)) {
    Write-Host "[7/7] generating debug keystore (first run)"
    & (Join-Path $JdkBin "keytool.exe") -genkeypair -keystore $Keystore `
        -alias shadowhealth -storepass android -keypass android `
        -keyalg RSA -keysize 2048 -validity 10950 `
        -dname "CN=shadow-health, OU=dev, O=shadowverse, C=CN"
    Check-Exit "keytool"
    Write-Host "NOTE: new signing key. Phones with an older build must uninstall first."
}

Write-Host "[7/7] apksigner sign"
& $ApkSigner sign --ks $Keystore --ks-pass pass:android --key-pass pass:android `
    --ks-key-alias shadowhealth --out $ApkOut (Join-Path $BuildDir "aligned.apk")
Check-Exit "apksigner sign"

& $ApkSigner verify $ApkOut
Check-Exit "apksigner verify"

$SizeMB = [math]::Round((Get-Item $ApkOut).Length / 1MB, 2)
Write-Host ""
Write-Host "OK: $ApkOut  ($SizeMB MB)" -ForegroundColor Green
