"""
NEMESIS HUD + phone server.

Serves:
  /         the HUD screen (hud.html)
  /phone    the phone microphone page (phone.html)
  /events   live events (Server-Sent Events)
  /audio/N  spoken replies for the phone
  /utter    (POST) sentences heard by the phone

Anyone on your network can reach this server, so everything except the PC
itself needs a login:
  1. the secret key in the phone link (checked once, then swapped for a
     private session cookie so the key never stays in the address bar),
  2. your PIN, if you set one.
Wrong guesses lock a device out for longer and longer. Other websites cannot
talk to Nemesis (Host and Origin checks). Standard library only.
"""
import collections
import hmac
import ipaddress
import json
import os
import queue
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAGES = {"/": "hud.html", "/index.html": "hud.html", "/phone": "phone.html"}
LOCAL_ADDRESSES = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
ACCESS_KEY = ""
FAILS = {}                      # ip -> [wrong attempts, locked until, lockouts so far]
MAX_FAILS, LOCK_SECONDS = 6, 300
SESSION_HOURS = 12
SESSIONS = {}                   # cookie token -> {"ip", "ua", "created", "last"}
SESS_LOCK = threading.Lock()
CERT_DIR = os.path.join(BASE_DIR, "certs")
STOP_RE = re.compile(r"^(?:nemesis[ ,]*)?(?:please )?(?:stop|quiet|silence|shut up|be quiet|enough|cancel that)(?: talking| it| please)?[ .!]*$", re.I)


class Hub:
    """Collects events from Nemesis and fans them out to connected browsers."""

    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()
        self.state = "sleeping"
        self.logs = collections.deque(maxlen=40)
        self.info = {}
        self._last_level = 0.0
        self.inbox = queue.Queue()                 # sentences from the phone
        self.audio = collections.OrderedDict()     # id -> (bytes, content type)
        self._audio_id = 0
        self.phones = 0
        self.frame, self.frame_id = b"", 0          # latest camera picture from the phone
        self.privacy = False                       # True = camera frames are refused
        self.still, self.still_id = b"", 0          # latest sharp (hi-res) camera picture, on request
        self._still_evt = threading.Event()
        self.stop_callback = None                  # set by nemesis.py
        self.local_clients = set()                 # queues of browsers on the PC itself
        self.pin_required = lambda: False          # set by nemesis.py
        self.pin_check = lambda pin: False         # set by nemesis.py
        self.on_new_device = None                  # set by nemesis.py: called(ip, user_agent)
        self.stt_callback = None                   # set by nemesis.py: called(pcm16_bytes, lang) -> text (offline hearing)
        self.link_url = ""

    def emit(self, **msg):
        data = json.dumps(msg)
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    def request_still(self, timeout=5.0):
        """Ask the phone for one sharp picture (for reading text). Returns jpeg bytes or None."""
        if self.privacy or self.phones <= 0:
            return None
        before = self.still_id
        self._still_evt.clear()
        self.emit(t="snap")
        end = time.time() + timeout
        while time.time() < end:
            if self.still_id != before:
                return self.still
            self._still_evt.wait(0.2)
        return None

    def emit_local(self, **msg):
        """Send only to browsers on the PC itself (never to phones)."""
        data = json.dumps(msg)
        with self.lock:
            clients = [q for q in self.clients if id(q) in self.local_clients]
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    def set_link(self, url):
        self.link_url = url
        self.emit_local(t="link", url=url)

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.emit(t="state", v=state)

    def log(self, who, text):
        item = {"t": "log", "who": who, "text": str(text)}
        self.logs.append(item)
        self.emit(**item)

    def level(self, value, src="mic"):
        now = time.time()
        if value > 0 and now - self._last_level < 0.04:
            return
        self._last_level = now
        self.emit(t="level", v=round(float(value), 3), src=src)

    def set_info(self, **info):
        self.info.update(info)
        self.info.pop("phone_url", None)           # the secret link is only sent to the PC screen
        self.emit(t="info", **self.info)

    def add_audio(self, data, ctype="audio/wav"):
        with self.lock:
            self._audio_id += 1
            aid = str(self._audio_id)
            self.audio[aid] = (data, ctype)
            while len(self.audio) > 12:
                self.audio.popitem(last=False)
        self.emit(t="audio", id=aid)
        return aid

    def request_stop(self):
        """Stop talking right now (phone STOP button or the word 'stop')."""
        if self.stop_callback:
            try:
                self.stop_callback()
            except Exception:
                pass
        self.emit(t="stop")

    def add_image(self, data, ctype="image/jpeg"):
        """Show a picture (e.g. a screenshot) on the phone page."""
        with self.lock:
            self._audio_id += 1
            aid = str(self._audio_id)
            self.audio[aid] = (data, ctype)
            while len(self.audio) > 12:
                self.audio.popitem(last=False)
        self.emit(t="image", id=aid)
        return aid

    def phone_connected(self, delta):
        self.phones = max(0, self.phones + delta)
        self.set_info(phone="connected" if self.phones else "waiting")


hub = Hub()


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A browser closing or refreshing a page is normal - do not print it.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, OSError)):
            return
        super().handle_error(request, client_address)


# ----------------------------------------------------------- sessions + locks
def _count_sessions():
    with SESS_LOCK:
        return len(SESSIONS)


def new_session(ip, ua):
    token = secrets.token_urlsafe(32)
    now = time.time()
    with SESS_LOCK:
        SESSIONS[token] = {"ip": ip, "ua": ua[:120], "created": now, "last": now}
    hub.set_info(sessions=str(_count_sessions()))
    if hub.on_new_device:
        try:
            hub.on_new_device(ip, ua)
        except Exception:
            pass
    return token


def session_ok(token):
    if not token:
        return False
    now = time.time()
    with SESS_LOCK:
        entry = SESSIONS.get(token)
        if entry and now - entry["last"] > SESSION_HOURS * 3600:
            del SESSIONS[token]
            entry = None
        if entry:
            entry["last"] = now
        return entry is not None


def revoke_all():
    """Log every phone out. They must scan the QR code on the PC screen again."""
    with SESS_LOCK:
        n = len(SESSIONS)
        SESSIONS.clear()
    hub.set_info(sessions="0")
    hub.emit(t="lock")
    return n


def set_key(new_key):
    global ACCESS_KEY
    ACCESS_KEY = str(new_key)
    revoke_all()


def _locked(ip):
    return FAILS.get(ip, [0, 0.0, 0])[1] > time.time()


def _fail(ip, what):
    rec = FAILS.get(ip, [0, 0.0, 0])
    rec[0] += 1
    if rec[0] >= MAX_FAILS:
        rec[2] += 1
        secs = min(86400, LOCK_SECONDS * 2 ** (rec[2] - 1))
        rec[0], rec[1] = 0, time.time() + secs
        msg = f"{ip} failed {what} too often - blocked for {secs // 60} minutes"
        print(f"[security] {msg}")
        hub.log("sys", "SECURITY: " + msg)
    FAILS[ip] = rec


def _ok(ip):
    rec = FAILS.get(ip)
    if rec:
        rec[0] = 0


def _key_ok(given):
    return bool(ACCESS_KEY) and hmac.compare_digest(str(given).encode(), ACCESS_KEY.encode())


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>NEMESIS</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#05070a;color:#c9d6df;font-family:Consolas,monospace}
.b{width:min(320px,86vw);text-align:center}h1{letter-spacing:.45em;font-size:20px;color:#e8eef2;font-weight:700}
p{color:#5d6e7b;font-size:12px;letter-spacing:.12em;line-height:1.7}
input{width:100%;box-sizing:border-box;margin:12px 0;padding:14px;background:#0b1117;color:#fff;border:1px solid #2a3640;border-radius:8px;font-size:22px;text-align:center;letter-spacing:.4em}
button{width:100%;padding:13px;background:rgba(200,40,40,.25);color:#fff;border:1px solid #c82828;border-radius:8px;font:inherit;letter-spacing:.2em}
#e{color:#ff7a6b;min-height:18px;font-size:12px}</style></head><body><div class="b">
<h1>NEMESIS</h1><p>ENTER YOUR PIN</p><input id="p" type="password" inputmode="numeric" autocomplete="off" autofocus>
<button id="g">UNLOCK</button><div id="e"></div></div>
<script>
var key=new URLSearchParams(location.search).get("key")||"";
function go(){var pin=document.getElementById("p").value;
fetch("/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key:key,pin:pin})})
.then(function(r){return r.json().then(function(j){return[r.status,j];});})
.then(function(x){if(x[0]===200&&x[1].ok){location.replace(location.pathname+"?s="+encodeURIComponent(x[1].s||""));}else{document.getElementById("e").textContent=x[1].error||"Wrong PIN";document.getElementById("p").value="";}})
.catch(function(){document.getElementById("e").textContent="Cannot reach the PC";});}
document.getElementById("g").onclick=go;
document.getElementById("p").addEventListener("keydown",function(e){if(e.key==="Enter")go();});
</script></body></html>"""

DENIED_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>NEMESIS</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#05070a;color:#c9d6df;font-family:Consolas,monospace}
.b{width:min(340px,86vw);text-align:center}h1{letter-spacing:.45em;font-size:20px;color:#e8eef2}p{color:#9fb2bf;font-size:13px;line-height:1.7}</style></head>
<body><div class="b"><h1>NEMESIS</h1><p>LOCKED</p><p>Scan the QR code on the PC screen again.<br>On the PC HUD page press <b>P</b> to show it.</p></div></body></html>"""

CSP_PHONE = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
             "img-src 'self' data: blob:; media-src 'self' blob: data:; connect-src 'self'; "
             "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
CSP_HUD = CSP_PHONE


class HudHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    timeout = 60                                    # a stalled client cannot hold a thread forever

    def log_message(self, *args):  # keep the terminal clean
        pass

    # ---- helpers
    def _is_local(self):
        return self.client_address[0] in LOCAL_ADDRESSES

    def _is_tls(self):
        return isinstance(self.connection, ssl.SSLSocket)

    def _security_headers(self, hud=False):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP_HUD if hud else CSP_PHONE)

    def _send(self, code, body, ctype, extra=(), hud=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers(hud)
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj, extra=()):
        self._send(code, json.dumps(obj).encode(), "application/json", extra)

    def _redirect(self, where, extra=()):
        self.send_response(302)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self._security_headers()
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _cookie_header(self, token):
        flags = "; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (SESSION_HOURS * 3600)
        return ("Set-Cookie", f"nem_session={token}{flags}{'; Secure' if self._is_tls() else ''}")

    def _session_token(self):
        # Fallback for phones that refuse cookies on a self-made certificate: the same
        # private session token may come in the address (?s=...).
        qs = self._query().get("s", [""])[0]
        if qs and session_ok(qs):
            return qs
        raw = self.headers.get("Cookie") or ""
        try:
            c = SimpleCookie()
            c.load(raw)
            return c["nem_session"].value if "nem_session" in c else ""
        except Exception:
            return ""

    def _host_ok(self):
        """Only IP addresses and 'localhost' are accepted as the address in the browser.
        A web page cannot trick your browser into talking to Nemesis under another name."""
        h = (self.headers.get("Host") or "").strip().lower()
        host = h[1:h.find("]")] if h.startswith("[") and "]" in h else h.split(":")[0]
        if host == "localhost":
            return True
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    def _origin_ok(self):
        """Other websites must not be able to send commands from your browser."""
        if self.headers.get("Sec-Fetch-Site", "") == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            return urlparse(origin).netloc.lower() == (self.headers.get("Host") or "").lower()
        return origin is None

    def _authorized(self):
        return self._is_local() or session_ok(self._session_token())

    def _wants_html(self):
        return "text/html" in (self.headers.get("Accept") or "")

    # ---- entry: the phone link with ?key=...
    def _entry(self, path):
        """Phone link with ?key=... : check it, then swap it for a session cookie."""
        ip = self.client_address[0]
        if _locked(ip):
            self._send(429, DENIED_PAGE.encode(), "text/html; charset=utf-8")
            return True
        key = self._query().get("key", [""])[0]
        if not key:
            return False
        if not _key_ok(key):
            _fail(ip, "the access key")
            self._send(403, DENIED_PAGE.encode(), "text/html; charset=utf-8")
            return True
        if hub.pin_required():
            self._send(200, LOGIN_PAGE.encode(), "text/html; charset=utf-8")
            return True
        _ok(ip)
        token = new_session(ip, self.headers.get("User-Agent", ""))
        self._redirect(path + "?s=" + token, [self._cookie_header(token)])
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._host_ok():
            self._send(400, b"bad host", "text/plain")
            return
        if not self._is_local() and path in PAGES and self._entry(path):
            return
        if not self._authorized():
            if path in PAGES and self._wants_html():
                self._send(403, DENIED_PAGE.encode(), "text/html; charset=utf-8")
            else:
                self._json(403, {"error": "locked"})
            return
        if path in PAGES:
            try:
                with open(os.path.join(BASE_DIR, PAGES[path]), "rb") as f:
                    body = f.read()
            except OSError:
                self._send(404, f"{PAGES[path]} not found next to nemesis.py".encode(), "text/plain")
                return
            self._send(200, body, "text/html; charset=utf-8", hud=(PAGES[path] == "hud.html"))
        elif path == "/qr.svg":
            if not self._is_local():
                self._json(403, {"error": "local only"})
                return
            try:
                import qrmini
                svg = qrmini.qr_svg(hub.link_url) if hub.link_url else ""
            except Exception:
                svg = ""
            if svg:
                self._send(200, svg.encode(), "image/svg+xml")
            else:
                self._send(404, b"no link yet", "text/plain")
        elif path == "/auth-check":
            self._json(200, {"ok": True})
        elif path == "/bg":
            for name in ("hud_bg.jpg", "hud_bg.jpeg", "hud_bg.png", "hud_bg.webp"):
                fp = os.path.join(BASE_DIR, name)
                if os.path.exists(fp):
                    with open(fp, "rb") as f:
                        data = f.read()
                    ctype = "image/png" if name.endswith("png") else "image/webp" if name.endswith("webp") else "image/jpeg"
                    self._send(200, data, ctype)
                    return
            self._send(404, b"no background picture", "text/plain")
        elif path == "/events":
            self._stream(self._query())
        elif path.startswith("/audio/"):
            item = hub.audio.get(path[len("/audio/"):])
            if item:
                self._send(200, item[0], item[1])
            else:
                self._send(404, b"gone", "text/plain")
        elif path == "/state":
            info = dict(hub.info)
            self._json(200, {"state": hub.state, "info": info})
        else:
            self._send(404, b"not found", "text/plain")

    def _read_json(self, limit):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(min(max(n, 0), limit)) or b"{}")
        except (ValueError, OSError):
            return {}

    def do_POST(self):
        post_path = urlparse(self.path).path
        if not self._host_ok() or not self._origin_ok():
            self._send(403, b"blocked", "text/plain")
            return
        ip = self.client_address[0]
        if post_path == "/login" and not self._is_local():
            if _locked(ip):
                self._json(429, {"error": "Too many tries. Wait and try again."})
                return
            data = self._read_json(2000)
            if not _key_ok(data.get("key", "")):
                _fail(ip, "the access key")
                self._json(403, {"error": "Locked. Scan the QR code on the PC again."})
                return
            if hub.pin_required() and not hub.pin_check(str(data.get("pin", ""))[:12]):
                _fail(ip, "the PIN")
                self._json(403, {"error": "Wrong PIN"})
                return
            _ok(ip)
            token = new_session(ip, self.headers.get("User-Agent", ""))
            self._json(200, {"ok": True, "s": token}, [self._cookie_header(token)])
            return
        if not self._authorized():
            self._json(403, {"error": "locked"})
            return
        if post_path == "/frame":
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n < 6_000_000:
                body = self.rfile.read(n)
                if not hub.privacy:
                    if self._query().get("hires"):
                        hub.still = body
                        hub.still_id += 1
                        hub._still_evt.set()
                    elif n < 3_000_000:
                        hub.frame = body
                        hub.frame_id += 1
            self._json(200, {"ok": True})
            return
        if post_path == "/stt":
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 1_400_000:
                self._json(413, {"error": "audio too long"})
                return
            body = self.rfile.read(n)
            cb = hub.stt_callback
            if cb is None:
                self._json(503, {"error": "offline hearing is not available"})
                return
            try:
                lang = (self._query().get("lang", ["en"])[0] or "en")[:8]
                self._json(200, {"text": cb(body, lang)})
            except Exception as e:
                self._json(500, {"error": str(e)[:200]})
            return
        if post_path != "/utter":
            self._send(404, b"not found", "text/plain")
            return
        payload = self._read_json(10000)
        text = str(payload.get("text", "")).strip()
        if payload.get("stop") or STOP_RE.match(text):
            hub.request_stop()
        elif text:
            hub.inbox.put({"text": text[:500], "direct": bool(payload.get("direct")),
                           "lang": str(payload.get("lang", "en-IN"))[:8]})
        self._json(200, {"ok": True})

    def _stream(self, qs):
        is_phone = bool(qs.get("phone"))
        local = self._is_local()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self._security_headers()
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = queue.Queue(maxsize=300)
        registered = False

        def send(data):
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            send(json.dumps({"t": "info", **hub.info}))
            if local and hub.link_url:
                send(json.dumps({"t": "link", "url": hub.link_url}))
            send(json.dumps({"t": "state", "v": hub.state}))
            for item in list(hub.logs):
                send(json.dumps(item))
            with hub.lock:
                hub.clients.append(q)
                if local:
                    hub.local_clients.add(id(q))
            registered = True
            if is_phone:
                hub.phone_connected(+1)
            while True:
                try:
                    send(q.get(timeout=15))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except OSError:
            pass
        finally:
            if registered:
                with hub.lock:
                    if q in hub.clients:
                        hub.clients.remove(q)
                    hub.local_clients.discard(id(q))
                if is_phone:
                    hub.phone_connected(-1)


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def tls_available():
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def make_cert(ip):
    """Self-signed certificate for this PC's current address (kept in the certs folder)."""
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    os.makedirs(CERT_DIR, exist_ok=True)
    crt, keyf, ipf = (os.path.join(CERT_DIR, n) for n in ("nemesis.crt", "nemesis.key", "nemesis.ip"))
    try:
        with open(ipf) as f:
            same = f.read().strip() == ip
        fresh = time.time() - os.path.getmtime(crt) < 300 * 86400
        if same and fresh and os.path.exists(keyf):
            return crt, keyf
    except OSError:
        pass
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Nemesis")])
    sans = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
    except ValueError:
        pass
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    with open(keyf, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(crt, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(ipf, "w") as f:
        f.write(ip)
    return crt, keyf


def phone_url(port, key, tls=False):
    return f"{'https' if tls else 'http'}://{local_ip()}:{port}/phone?key={key}"


def start_hud(port=8765, lan=True, auto_open=True, key="", tls=False):
    """Start the web server in a background thread.

    tls=False: one HTTP server on all addresses (old behaviour).
    tls=True : HTTP only on this PC (port) + encrypted HTTPS for the phone (port + 1).
    """
    global ACCESS_KEY
    ACCESS_KEY = str(key)
    secure = None
    if tls:
        try:
            crt, keyf = make_cert(local_ip())
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(crt, keyf)
            secure = QuietServer(("0.0.0.0", port + 1), HudHandler)
            secure.socket = ctx.wrap_socket(secure.socket, server_side=True, do_handshake_on_connect=False)
            threading.Thread(target=secure.serve_forever, daemon=True).start()
        except Exception as e:
            print(f"[HUD] encrypted link could not start ({e}) - using plain HTTP")
            secure = None
    host = "127.0.0.1" if secure else ("0.0.0.0" if lan else "127.0.0.1")
    try:
        server = QuietServer((host, port), HudHandler)
    except OSError as e:
        print(f"[HUD] could not start on port {port}: {e}")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"HUD: {url}   (press F in the browser for fullscreen)")
    if secure:
        print(f"Phone link is ENCRYPTED (https, port {port + 1}).")
    if auto_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return server
