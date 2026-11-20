"""Paketlenen Windows Python'unda eksik DLL bağımlılığı olup olmadığını kontrol eder (pefile gerekir)."""
import glob
import os
import sys

try:
    import pefile
except ImportError:
    print("pefile yok, DLL kontrolü atlandı (pip install pefile)")
    sys.exit(0)

root = sys.argv[1]
have = set()
for d in (root, os.path.join(root, "DLLs")):
    have |= {f.lower() for f in os.listdir(d) if f.lower().endswith(".dll")}
system = {"kernel32.dll", "user32.dll", "advapi32.dll", "ws2_32.dll", "shell32.dll", "ole32.dll", "oleaut32.dll",
          "rpcrt4.dll", "crypt32.dll", "bcrypt.dll", "iphlpapi.dll", "pdh.dll", "psapi.dll", "powrprof.dll",
          "secur32.dll", "version.dll", "winmm.dll", "shlwapi.dll", "cabinet.dll", "netapi32.dll", "wtsapi32.dll",
          "userenv.dll", "mswsock.dll", "ntdll.dll", "gdi32.dll", "comctl32.dll", "propsys.dll", "pathcch.dll",
          "msvcrt.dll"}
files = [os.path.join(root, "python.exe"), *glob.glob(os.path.join(root, "*.dll")),
         *glob.glob(os.path.join(root, "DLLs", "*.pyd")), *glob.glob(os.path.join(root, "DLLs", "*.dll")),
         *glob.glob(os.path.join(root, "Lib", "site-packages", "**", "*.pyd"), recursive=True)]
missing = {}
for f in files:
    pe = pefile.PE(f, fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        n = e.dll.decode().lower()
        if n in have or n in system or n.startswith(("api-ms-win", "ext-ms")):
            continue
        missing.setdefault(n, []).append(os.path.relpath(f, root))
if missing:
    print("EKSİK DLL:", missing)
    sys.exit(1)
print("DLL kontrolü: %d dosya, eksik yok" % len(files))
