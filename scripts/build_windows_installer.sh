#!/usr/bin/env bash
# Windows kurulum dosyasını Linux üzerinde üretir (Windows gerekmez).
#  - conda-forge'dan Windows için taşınabilir Python 3.12 indirir (micromamba)
#  - gereksiz dosyaları ayıklar, psutil ekler
#  - NSIS ile build/OBS-Stream-Doctor-Kurulum-<sürüm>.exe oluşturur
# Gerekenler: curl, tar, bzip2, unzip, python3 + pip, makensis (apt install nsis)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
PYVER="${PYVER:-3.12}"
MAMBA_VER="${MAMBA_VER:-2.10.0}"
VERSION="${VERSION:-$(python3 -c "import re;print(re.search(r'VERSION = \"([^\"]+)\"', open('$ROOT/src/obs_stream_doctor.py', encoding='utf-8').read()).group(1))")}"
VERSION="${VERSION#v}"

echo ">> Sürüm: $VERSION"
rm -rf "$BUILD"
mkdir -p "$BUILD/tools" "$BUILD/app" "$BUILD/whl"

# 1) micromamba (Linux) -> Windows Python ortamı
if ! [ -x "$BUILD/tools/bin/micromamba" ]; then
  curl -fsSL -o "$BUILD/tools/mm.tar.bz2" "https://conda.anaconda.org/conda-forge/linux-64/micromamba-${MAMBA_VER}-0.tar.bz2"
  tar -xjf "$BUILD/tools/mm.tar.bz2" -C "$BUILD/tools" bin/micromamba
fi
export MAMBA_ROOT_PREFIX="$BUILD/tools/root"
"$BUILD/tools/bin/micromamba" create -y -q --platform win-64 -p "$BUILD/winpy" -c conda-forge "python=${PYVER}" >/dev/null

# 2) Ayıkla
P="$BUILD/app/python"
cp -r "$BUILD/winpy" "$P"
cd "$P"
rm -rf conda-meta include libs Scripts Tools lib etc share tcl .mambarc \
       Lib/test Lib/idlelib Lib/tkinter Lib/turtledemo Lib/ensurepip Lib/lib2to3 Lib/venv Lib/pydoc_data \
       Lib/site-packages/pip* Library/include Library/share Library/lib Library/mingw-w64 Library/ssl
rm -f DLLs/_test*.pyd DLLs/_ctypes_test.pyd DLLs/xxlimited*.pyd DLLs/_tkinter.pyd DLLs/_msi.pyd pythonw.exe vcamp140.dll
find . -name "*.pdb" -delete
find . -name __pycache__ -type d -prune -exec rm -rf {} +
if [ -d Library/bin ]; then
  ( cd Library/bin && rm -f api-ms-*.dll ucrtbase.dll vc*.dll msvcp*.dll concrt140.dll tcl*.dll tk*.dll *.exe *.pl )
  # Uzantı modülleri (pyd) bağımlı DLL'leri kendi klasöründe bulsun
  cp Library/bin/*.dll DLLs/ 2>/dev/null || true
fi
cd "$ROOT"

# 3) psutil (Windows wheel)
python3 -m pip download -q -d "$BUILD/whl" --only-binary=:all: --platform win_amd64 \
  --python-version "${PYVER/./}" --implementation cp psutil
python3 -c "import zipfile,glob,sys; zipfile.ZipFile(glob.glob(sys.argv[1]+'/psutil-*.whl')[0]).extractall(sys.argv[2])" \
  "$BUILD/whl" "$P/Lib/site-packages"

# 4) Uygulama
cp "$ROOT/src/obs_stream_doctor.py" "$BUILD/app/"
cp "$ROOT/LICENSE" "$BUILD/app/LICENSE.txt"

# 5) DLL bağımlılık kontrolü (isteğe bağlı, pefile varsa)
python3 "$ROOT/scripts/check_dlls.py" "$P" || { echo "DLL kontrolü başarısız"; exit 1; }

# 6) NSIS
OUT="$BUILD/OBS-Stream-Doctor-Kurulum-${VERSION}.exe"
makensis -V2 -DVERSION="$VERSION" -DAPPDIR="$BUILD/app" -DOUTFILE="$OUT" "$ROOT/installer/obs-stream-doctor.nsi"
echo ">> Hazır: $OUT ($(du -h "$OUT" | cut -f1))"
