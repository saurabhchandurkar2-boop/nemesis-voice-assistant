"""
NEMESIS guard: looks for the signs of hidden programs on this PC, and checks that Nemesis' own files
have not been changed.  It only reads, except for the two clearly named actions at the bottom.
"""
import hashlib
import json
import os
import re
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"
REPORT_FILE = os.path.join(BASE_DIR, "security_report.txt")
MANIFEST = os.path.join(BASE_DIR, "nemesis_manifest.json")
SEALED = ("nemesis.py", "hud_server.py", "tools.py", "vision.py", "offline.py", "guard.py", "mind.py",
          "qrmini.py", "core.py", "selftest.py", "home.py", "faces.py", "nemesis_light.ino", "hud.html", "phone.html", "Start Nemesis.bat")

SYSTEM_NAMES = {"csrss", "svchost", "explorer", "lsass", "winlogon", "services", "smss", "wininit", "taskhost",
                "taskhostw", "dwm", "spoolsv", "conhost", "runtimebroker", "searchindexer", "sihost", "ctfmon",
                "fontdrvhost", "audiodg", "dllhost"}
LOLBINS_HIGH = {"addinprocess", "addinprocess32", "addinutil", "regsvcs", "regasm", "installutil", "jsc", "ieexec"}
LOLBINS_MED = {"aspnet_compiler"}
REMOTE_TOOLS = ("anydesk", "teamviewer", "ultraviewer", "rustdesk", "ammyy", "supremo", "screenconnect", "remcos")
RISKY_DIRS = ("\\appdata\\roaming\\", "\\appdata\\local\\temp\\", "\\users\\public\\", "\\programdata\\",
              "\\system32\\config\\systemprofile\\", "\\downloads\\")
KNOWN_GOOD = ("\\appdata\\local\\microsoft\\", "\\programdata\\microsoft\\windows defender\\", "\\google\\", "\\mozilla", "\\opera", "\\discord", "\\code\\", "jetbrains", "\\spotify",
              "whatsapp", "telegram", "\\zoom", "\\slack", "\\teams", "onedrive", "\\appdata\\local\\programs\\",
              "\\adobe\\", "\\mathworks\\", "\\nvidia", "\\intel\\", "\\ollama", "\\packages\\", "\\windowsapps\\",
              "\\easymc-launcher\\", "\\steam", "\\epic", "\\anydesk\\")
BULK_TASK = re.compile(r"^applications[a-z]?\d{5,}$", re.I)

COLLECT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$r = @{}
$r.startup = @(Get-CimInstance Win32_StartupCommand | Select-Object Name,Command,Location)
$r.tasks = @(Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\Microsoft\*' } | ForEach-Object {
  $a = $_.Actions | Select-Object -First 1
  [pscustomobject]@{ Name = $_.TaskName; Path = $_.TaskPath; State = [string]$_.State; Exec = $a.Execute; Args = $a.Arguments } })
$r.procs = @(Get-CimInstance Win32_Process | Select-Object Name,ProcessId,ParentProcessId,ExecutablePath)
$d = Get-MpComputerStatus
$r.defender = @{ realtime = $d.RealTimeProtectionEnabled; enabled = $d.AntivirusEnabled }
$r.threats = @(Get-MpThreat | Where-Object { $_.IsActive } | Select-Object ThreatName)
$r | ConvertTo-Json -Depth 5 -Compress
"""


def collect():
    if not IS_WIN:
        raise RuntimeError("the PC check needs Windows")
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", COLLECT],
                       capture_output=True, text=True, timeout=120, creationflags=0x08000000)
    out = (p.stdout or "").strip()
    if not out:
        raise RuntimeError((p.stderr or "no answer from Windows").strip()[:200])
    return json.loads(out)


def _lst(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _risky(path):
    p = (path or "").lower().replace("/", "\\")
    if "\\system32\\config\\systemprofile\\" in p:          # nothing normal starts from here
        return True
    if not any(d in p for d in RISKY_DIRS):
        return False
    return not any(k in p for k in KNOWN_GOOD)


def _dup_name(path):
    """...\\Roaming\\offest\\offest.exe : a program named like its own folder, a very common malware habit."""
    p = (path or "").replace("/", "\\").strip('"')
    parts = p.split("\\")
    if any(k in p.lower() for k in KNOWN_GOOD):
        return False
    if len(parts) >= 2 and "appdata\\roaming" in p.lower() and parts[-1].lower().endswith(".exe"):
        return parts[-1][:-4].lower() == parts[-2].lower()
    return False


def exe_of(command):
    m = re.match(r'\s*"([^"]+)"', command or "") or re.match(r"\s*(\S+\.exe)", command or "", re.I)
    return m.group(1) if m else (command or "").strip()


def analyse(data):
    """data = what collect() returns.  Returns a list of findings {sev, kind, name, detail, pid?}."""
    out = []
    for p in _lst(data.get("procs")):
        name = (p.get("Name") or "")
        base = name.lower().replace(".exe", "")
        path = p.get("ExecutablePath") or ""
        low = path.lower()
        why = None
        if base in SYSTEM_NAMES and path and "\\windows\\" not in low:
            why = ("high", f"pretends to be Windows' {name} but runs from {path}")
        elif base in LOLBINS_HIGH:
            why = ("high", f"{name} is running; it is a Microsoft tool that malware often hides inside")
        elif base in LOLBINS_MED:
            why = ("med", f"{name} is running; normal only while building a web project")
        elif path and (_dup_name(path) or _risky(path)):
            why = ("high" if _dup_name(path) else "med", f"runs from a hidden place: {path}")
        if why:
            out.append({"sev": why[0], "kind": "process", "name": name, "pid": p.get("ProcessId"), "detail": why[1]})
        if any(t in base for t in REMOTE_TOOLS):
            out.append({"sev": "info", "kind": "remote", "name": name, "pid": p.get("ProcessId"),
                        "detail": "remote-control software is running; make sure you installed it"})
    for s in _lst(data.get("startup")):
        cmd = s.get("Command") or ""
        exe = exe_of(cmd)
        if _dup_name(exe) or _risky(exe):
            out.append({"sev": "high" if (_dup_name(exe) or "systemprofile" in exe.lower()) else "med",
                        "kind": "startup", "name": s.get("Name"), "detail": f"starts with Windows from {exe}"})
        if any(t in (cmd or "").lower() for t in REMOTE_TOOLS):
            out.append({"sev": "info", "kind": "remote", "name": s.get("Name"),
                        "detail": "remote-control software starts with Windows"})
    bulk = 0
    for t in _lst(data.get("tasks")):
        name = t.get("Name") or ""
        exe = (t.get("Exec") or "")
        if BULK_TASK.match(name):
            bulk += 1
            out.append({"sev": "high", "kind": "task", "name": name, "detail": "bulk-made scheduled task", "bulk": True})
        elif _dup_name(exe) or _risky(exe):
            out.append({"sev": "high" if _dup_name(exe) else "med", "kind": "task", "name": name,
                        "detail": f"scheduled task runs {exe}"})
    d = data.get("defender") or {}
    if d and d.get("realtime") is False:
        out.append({"sev": "high", "kind": "defender", "name": "Windows Defender", "detail": "real-time protection is OFF"})
    for th in _lst(data.get("threats")):
        out.append({"sev": "high", "kind": "threat", "name": th.get("ThreatName"), "detail": "Defender reports it is active"})
    rank = {"high": 0, "med": 1, "info": 2}
    out.sort(key=lambda f: (rank.get(f["sev"], 3), f["kind"], str(f["name"])))
    return out


def summarise(findings):
    """(spoken text, full report text)"""
    real = [f for f in findings if f["sev"] in ("high", "med")]
    info = [f for f in findings if f["sev"] == "info"]
    bulk = sum(1 for f in real if f.get("bulk"))
    lines = []
    for f in findings:
        if f.get("bulk"):
            continue
        lines.append(f"[{f['sev'].upper():4}] {f['kind']:8} {f['name']}: {f['detail']}")
    if bulk:
        lines.append(f"[HIGH] task     {bulk} scheduled tasks named like applications0123456789 (made in bulk)")
    report = "NEMESIS PC CHECK\n" + "\n".join(lines) + "\n"
    if not real:
        tail = f" I did see {len(info)} remote-control program, make sure you installed it." if info else ""
        return "I found nothing suspicious in your startup items, tasks and running programs." + tail, report
    kinds = {}
    for f in real:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + (1 if not f.get("bulk") else 0)
    if bulk:
        kinds["task"] = kinds.get("task", 0) + bulk
    parts = []
    if kinds.get("process"):
        parts.append(f"{kinds['process']} running program{'s' if kinds['process'] != 1 else ''}")
    if kinds.get("startup"):
        parts.append(f"{kinds['startup']} startup item{'s' if kinds['startup'] != 1 else ''}")
    if kinds.get("task"):
        parts.append(f"{kinds['task']} scheduled task{'s' if kinds['task'] != 1 else ''}")
    for k in ("defender", "threat"):
        if kinds.get(k):
            parts.append("a Windows Defender warning")
    worst = next((f for f in real if not f.get("bulk")), None)
    spoken = f"Warning. I found {len(real)} suspicious item{'s' if len(real) != 1 else ''}: " + ", ".join(parts) + "."
    if worst:
        spoken += f" The worst is {worst['name']}: {worst['detail']}."
    spoken += " The full list is saved as security report dot text. Say disable suspicious tasks or stop suspicious programs."
    return spoken, report


def pc_check():
    data = collect()
    findings = analyse(data)
    spoken, report = summarise(findings)
    try:
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            f.write(report)
    except OSError:
        pass
    return spoken, findings


# ---------------------------------------------------------------- the two actions (reversible / safe)
def _ps(script):
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=120, creationflags=0x08000000)


def disable_tasks(findings):
    """Switches the flagged scheduled tasks OFF (they can be switched on again; nothing is deleted)."""
    names = [f["name"] for f in findings if f["kind"] == "task" and f["sev"] in ("high", "med")]
    if not names:
        return "There are no suspicious scheduled tasks to switch off."
    ok = 0
    denied = False
    for i in range(0, len(names), 40):
        chunk = names[i:i + 40]
        arr = ",".join("'" + n.replace("'", "''") + "'" for n in chunk)
        r = _ps(f"$n=@({arr}); $c=0; foreach($x in $n){{ try {{ Disable-ScheduledTask -TaskName $x -TaskPath '\\' "
                f"-ErrorAction Stop | Out-Null; $c++ }} catch {{ }} }}; $c")
        try:
            ok += int((r.stdout or "0").strip().splitlines()[-1])
        except (ValueError, IndexError):
            pass
    if ok == 0:
        denied = True
    if denied:
        return ("I could not switch them off. Windows needs administrator rights for that. Close Nemesis, open PowerShell "
                "as administrator, and start Nemesis from there.")
    return f"I switched off {ok} of {len(names)} suspicious scheduled tasks. Restart the PC to be sure they stay quiet."


def stop_processes(findings):
    """Stops the flagged running programs (never the real Windows ones)."""
    ids = [f["pid"] for f in findings if f["kind"] == "process" and f["sev"] in ("high", "med") and f.get("pid")]
    if not ids:
        return "There are no suspicious programs running."
    done = 0
    for pid in ids:
        r = _ps(f"try {{ Stop-Process -Id {int(pid)} -Force -ErrorAction Stop; 'ok' }} catch {{ 'no' }}")
        done += 1 if "ok" in (r.stdout or "") else 0
    return f"I stopped {done} of {len(ids)} suspicious programs. They may start again after a restart, so also switch off the tasks."


# ---------------------------------------------------------------- file integrity
def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def seal(files=SEALED):
    data = {}
    for name in files:
        p = os.path.join(BASE_DIR, name)
        if os.path.exists(p):
            data[name] = _sha(p)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return len(data)


def verify():
    """Returns (status, changed_files, missing_files). status: 'ok', 'changed' or 'unsealed'."""
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            ref = json.load(f)
    except (OSError, ValueError):
        return "unsealed", [], []
    changed, missing = [], []
    for name, digest in ref.items():
        p = os.path.join(BASE_DIR, name)
        if not os.path.exists(p):
            missing.append(name)
        elif _sha(p) != digest:
            changed.append(name)
    return ("changed" if changed or missing else "ok"), changed, missing
