#!/usr/bin/env python3
"""
MiCam - usa la cámara de tu celular como webcam en tu PC.

Versión con ventana, pensada para convertirse en MiCam.exe con build.bat.
También se puede ejecutar directo:  python micam.py

Requisitos (solo para ejecutar desde Python o para crear el .exe):
  pip install aiohttp pyvirtualcam opencv-python numpy cryptography av segno
En cada PC donde se use: OBS Studio instalado (aporta el controlador de cámara virtual).
"""
import asyncio
import base64
import concurrent.futures
import datetime
import io
import ipaddress
import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox

import av
import cv2
import numpy as np
import pyvirtualcam
import segno
from aiohttp import web

APP_NAME = "MiCam"
DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
os.makedirs(DATA_DIR, exist_ok=True)
CERT_FILE = os.path.join(DATA_DIR, "cert.pem")
KEY_FILE = os.path.join(DATA_DIR, "key.pem")
SETTINGS_FILE = os.path.join(DATA_DIR, "ajustes.json")
LOG_FILE = os.path.join(DATA_DIR, "micam.log")

PORTS = list(range(8443, 8454))
RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080), "4K": (3840, 2160)}
FPS_OPTIONS = ["30", "60"]
OBS_URL = "https://obsproject.com/download"
KIND_H264, KIND_JPEG = 1, 2

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(APP_NAME)


def resource_path(name):
    """Ruta de archivos incluidos en el .exe (o junto al script)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# ---------------------------------------------------------------- utilidades
def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def ensure_cert(ip):
    """Certificado propio: el navegador solo permite la cámara en HTTPS."""
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, APP_NAME)])
    san = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        san.append(x509.IPAddress(ipaddress.ip_address(ip)))
    except ValueError:
        pass
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(KEY_FILE, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(CERT_FILE, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    if s.get("res") not in RESOLUTIONS:
        s["res"] = "1080p"
    if str(s.get("fps")) not in FPS_OPTIONS:
        s["fps"] = "60"
    s["fps"] = str(s["fps"])
    return s


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except Exception:
        log.exception("No se pudieron guardar los ajustes")


def fit(img, w, h):
    """Ajusta la imagen al tamaño de la cámara sin deformarla."""
    ih, iw = img.shape[:2]
    if (iw, ih) == (w, h):
        return img
    s = min(w / iw, h / ih)
    nw, nh = max(1, int(iw * s)), max(1, int(ih * s))
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
    resized = cv2.resize(img, (nw, nh), interpolation=interp)
    out = np.zeros((h, w, 3), np.uint8)
    x, y = (w - nw) // 2, (h - nh) // 2
    out[y:y + nh, x:x + nw] = resized
    return out


def placeholder(w, h):
    img = np.full((h, w, 3), 40, np.uint8)
    text = "Esperando al celular..."
    scale = w / 1280
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.2 * scale, 2)
    cv2.putText(img, text, ((w - tw) // 2, (h + th) // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2 * scale, (200, 200, 200), 2, cv2.LINE_AA)
    return img


# ------------------------------------------------------ estado compartido
class Shared:
    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None
        self.stamp = 0.0
        self.size = (1920, 1080)
        self.clients = 0
        self.fps = 0.0


class Decoder:
    """Convierte lo que manda el celular (H.264 o JPEG) en imágenes BGR."""

    def __init__(self, shared):
        self.shared = shared
        self.codec = None

    def reset(self):
        self.codec = av.CodecContext.create("h264", "r")
        for value in ("SLICE", getattr(getattr(av.codec.context, "ThreadType", None), "SLICE", None)):
            if value is None:
                continue
            try:
                self.codec.thread_type = value  # evita retener cuadros (menos latencia)
                break
            except Exception:
                pass

    def decode(self, data):
        view = memoryview(data)
        kind, payload = view[0], view[1:]
        img = None
        if kind == KIND_JPEG:
            img = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
        elif kind == KIND_H264:
            if self.codec is None:
                self.reset()
            try:
                for frame in self.codec.decode(av.Packet(bytes(payload))):
                    img = frame.to_ndarray(format="bgr24")
            except Exception:
                return None  # se recupera con el próximo cuadro clave
        if img is None:
            return None
        w, h = self.shared.size
        return fit(img, w, h)


# ------------------------------------------------------------ cámara virtual
class VirtualCam:
    def __init__(self, shared):
        self.shared = shared
        self.thread = None
        self.stop_evt = None
        self.device = ""
        self.lock = threading.Lock()

    def start(self, w, h, fps):
        """Abre la cámara virtual. Devuelve None si funcionó, o el texto del error."""
        with self.lock:
            return self._start(w, h, fps)

    def _start(self, w, h, fps):
        self.stop()
        last = None
        # OBS tarda un momento en liberar la cámara al cerrarla: se reintenta unos segundos.
        for attempt in range(8):
            try:
                cam = pyvirtualcam.Camera(width=w, height=h, fps=fps, fmt=pyvirtualcam.PixelFormat.BGR)
                break
            except Exception as e:
                last = e
                log.warning("Intento %d de abrir la cámara virtual falló: %s", attempt + 1, e)
                time.sleep(0.5)
        else:
            return str(last) or last.__class__.__name__
        self.device = cam.device
        with self.shared.lock:
            self.shared.size = (w, h)
        self.stop_evt = threading.Event()
        self.thread = threading.Thread(target=self._loop, args=(cam, self.stop_evt), daemon=True)
        self.thread.start()
        log.info("Cámara virtual %s %dx%d@%d", cam.device, w, h, fps)
        return None

    def _loop(self, cam, stop):
        idle = placeholder(cam.width, cam.height)
        try:
            while not stop.is_set():
                with self.shared.lock:
                    frame, stamp = self.shared.frame, self.shared.stamp
                if frame is None or time.time() - stamp > 2:
                    frame = idle
                elif frame.shape[1] != cam.width or frame.shape[0] != cam.height:
                    frame = fit(frame, cam.width, cam.height)
                cam.send(frame)
                cam.sleep_until_next_frame()
        except Exception:
            log.exception("Error en la cámara virtual")
        finally:
            cam.close()

    def stop(self):
        if self.stop_evt:
            self.stop_evt.set()
            self.thread.join(timeout=3)
        self.stop_evt = self.thread = None


# ------------------------------------------------------------------ servidor
async def ws_handler(request):
    shared = request.app["shared"]
    ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024)
    await ws.prepare(request)
    with shared.lock:
        shared.clients += 1
    log.info("Celular conectado (%s)", request.remote)
    loop = asyncio.get_running_loop()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    dec = Decoder(shared)
    count, t0 = 0, time.time()
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT and msg.data == "reset":
                await loop.run_in_executor(pool, dec.reset)
            elif msg.type == web.WSMsgType.BINARY:
                frame = await loop.run_in_executor(pool, dec.decode, msg.data)
                if frame is not None:
                    with shared.lock:
                        shared.frame, shared.stamp = frame, time.time()
                    count += 1
                now = time.time()
                if now - t0 >= 1:
                    shared.fps = count / (now - t0)
                    count, t0 = 0, now
    finally:
        pool.shutdown(wait=False)
        with shared.lock:
            shared.clients -= 1
            if shared.clients <= 0:
                shared.clients, shared.fps = 0, 0.0
        log.info("Celular desconectado")
    return ws


async def index(_request):
    return web.Response(text=PHONE_PAGE, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


class Server:
    """Servidor web en su propio hilo, para no trabar la ventana."""

    def __init__(self, shared):
        self.shared = shared
        self.loop = None
        self.runner = None
        self.port = None
        self.error = None

    def start(self):
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), daemon=True).start()
        ready.wait(15)

    def _run(self, ready):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._startup())
        except Exception as e:
            log.exception("No se pudo iniciar el servidor")
            self.error = str(e)
        ready.set()
        if not self.error:
            self.loop.run_forever()

    async def _startup(self):
        ensure_cert(local_ip())
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(CERT_FILE, KEY_FILE)
        app = web.Application()
        app["shared"] = self.shared
        app.router.add_get("/", index)
        app.router.add_get("/ws", ws_handler)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        for port in PORTS:
            try:
                await web.TCPSite(self.runner, "0.0.0.0", port, ssl_context=ssl_ctx).start()
                self.port = port
                log.info("Servidor en el puerto %d", port)
                return
            except OSError:
                continue
        raise RuntimeError("Todos los puertos entre 8443 y 8453 están ocupados.")

    def stop(self):
        if not self.loop or not self.loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(timeout=3)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)


# ------------------------------------------------------------------- ventana
C = dict(bg="#1c2230", panel="#262d3d", line="#343c50", fg="#edeff3",
         mut="#9aa3b5", live="#ffb020", rec="#e5484d", ok="#3fb97f")
FONT = "Segoe UI"
STEPS = [
    "Conecta el celular al mismo Wi‑Fi que esta PC.",
    "Escanea el código con la cámara del celular, o escribe la dirección en su navegador.",
    "Si aparece un aviso de seguridad, toca \"Mostrar detalles\" y luego \"visitar este sitio web\".",
    "Toca el botón rojo y permite el acceso a la cámara.",
    "En Zoom, Meet, Discord u OBS, elige la cámara \"OBS Virtual Camera\".",
]
HELP_TEXT = (
    "Revisa que el celular esté en el mismo Wi‑Fi y sin VPN activada. "
    "Si usas un firewall aparte (como Portmaster o el de tu antivirus), dale permiso a MiCam "
    "para recibir conexiones. Para el firewall de Windows usa el botón de abajo; "
    "te pedirá permiso de administrador."
)


class App:
    def __init__(self):
        self.settings = load_settings()
        self.shared = Shared()
        self.vcam = VirtualCam(self.shared)
        self.server = Server(self.shared)
        self.ip = local_ip()
        self.url = ""
        self.qr_img = None
        self.ticks = 0
        self.cam_ok_once = False
        self.cam_job = 0
        self.cam_result = None

        r = self.root = tk.Tk()
        r.title(APP_NAME)
        r.configure(bg=C["bg"])
        r.resizable(False, False)
        r.report_callback_exception = lambda *exc: log.error("Error en la ventana", exc_info=exc)
        try:
            r.iconbitmap(resource_path("micam.ico"))
        except Exception:
            pass
        self._build()
        r.protocol("WM_DELETE_WINDOW", self.close)
        r.after(50, self._startup)

    # --- piezas de interfaz
    def _label(self, parent, text="", size=10, color="fg", bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=C[color],
                        font=(FONT, size, "bold" if bold else "normal"), **kw)

    def _button(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=C["bg"], fg=C["fg"],
                         activebackground=C["line"], activeforeground=C["fg"],
                         relief="flat", bd=0, padx=12, pady=5, cursor="hand2",
                         highlightthickness=1, highlightbackground=C["line"], font=(FONT, 9))

    def _menu(self, parent, var, values):
        m = tk.OptionMenu(parent, var, *values)
        m.configure(bg=C["bg"], fg=C["fg"], activebackground=C["line"], activeforeground=C["fg"],
                    relief="flat", bd=0, highlightthickness=1, highlightbackground=C["line"],
                    font=(FONT, 9), cursor="hand2", width=6)
        m["menu"].configure(bg=C["panel"], fg=C["fg"], activebackground=C["live"],
                            activeforeground=C["bg"], font=(FONT, 9))
        return m

    def _card(self):
        f = tk.Frame(self.root, bg=C["panel"], highlightbackground=C["line"], highlightthickness=1)
        f.pack(fill="x", padx=24, pady=(0, 14))
        return f

    def _build(self):
        head = tk.Frame(self.root, bg=C["bg"])
        head.pack(fill="x", padx=24, pady=(20, 14))
        self._label(head, APP_NAME, 20, bold=True).pack(anchor="w")
        self._label(head, "Usa la cámara de tu celular como webcam", 10, "mut").pack(anchor="w")

        # Código QR y dirección
        card = self._card()
        inner = tk.Frame(card, bg=C["panel"])
        inner.pack(fill="x", padx=16, pady=16)
        self.qr_label = tk.Label(inner, bg="#ffffff", bd=0, width=190, height=190)
        self.qr_label.pack(side="left")
        side = tk.Frame(inner, bg=C["panel"])
        side.pack(side="left", fill="both", expand=True, padx=(16, 0))
        self._label(side, "Escanea este código con la cámara de tu celular",
                    10, "mut", justify="left", wraplength=210, anchor="w").pack(anchor="w")
        self.url_label = self._label(side, "Iniciando…", 11, bold=True,
                                     justify="left", wraplength=210, anchor="w")
        self.url_label.pack(anchor="w", pady=(10, 10))
        self.copy_btn = self._button(side, "Copiar dirección", self.copy_url)
        self.copy_btn.pack(anchor="w")

        # Pasos
        steps = tk.Frame(self.root, bg=C["bg"])
        steps.pack(fill="x", padx=24, pady=(0, 14))
        for i, text in enumerate(STEPS, 1):
            row = tk.Frame(steps, bg=C["bg"])
            row.pack(fill="x", pady=2)
            self._label(row, str(i), 10, "live", bold=True, width=2, anchor="nw").pack(side="left", anchor="n")
            self._label(row, text, 10, justify="left", wraplength=390, anchor="w").pack(side="left", fill="x")

        # Estado y ajustes
        st = self._card()
        row = tk.Frame(st, bg=C["panel"])
        row.pack(fill="x", padx=14, pady=(12, 4))
        self.dot = tk.Canvas(row, width=12, height=12, bg=C["panel"], highlightthickness=0)
        self.dot_id = self.dot.create_oval(1, 1, 11, 11, fill=C["mut"], outline="")
        self.dot.pack(side="left", padx=(0, 8))
        self.phone_status = self._label(row, "Esperando al celular", 10, bold=True)
        self.phone_status.pack(side="left")
        self.cam_status = self._label(st, "", 9, "mut", justify="left", wraplength=400, anchor="w")
        self.cam_status.pack(fill="x", padx=14)
        self.cam_btns = tk.Frame(st, bg=C["panel"])
        self.obs_btn = self._button(self.cam_btns, "Descargar OBS Studio", lambda: webbrowser.open(OBS_URL))
        self.retry_btn = self._button(self.cam_btns, "Reintentar", self.apply_camera)
        self.opts = tk.Frame(st, bg=C["panel"])
        self.opts.pack(fill="x", padx=14, pady=(10, 14))
        self._label(self.opts, "Resolución", 9, "mut").pack(side="left")
        self.res_var = tk.StringVar(value=self.settings["res"])
        self._menu(self.opts, self.res_var, list(RESOLUTIONS)).pack(side="left", padx=(6, 18))
        self._label(self.opts, "FPS", 9, "mut").pack(side="left")
        self.fps_var = tk.StringVar(value=self.settings["fps"])
        self._menu(self.opts, self.fps_var, FPS_OPTIONS).pack(side="left", padx=(6, 0))
        self.res_var.trace_add("write", lambda *_: self.apply_camera())
        self.fps_var.trace_add("write", lambda *_: self.apply_camera())

        # Ayuda
        foot = tk.Frame(self.root, bg=C["bg"])
        foot.pack(fill="x", padx=24, pady=(0, 18))
        tk.Button(foot, text="¿El celular no conecta?", command=self.toggle_help,
                  bg=C["bg"], fg=C["live"], activebackground=C["bg"], activeforeground=C["fg"],
                  relief="flat", bd=0, cursor="hand2", font=(FONT, 9, "underline")).pack(anchor="w")
        self.help = tk.Frame(foot, bg=C["bg"])
        self._label(self.help, HELP_TEXT, 9, "mut", justify="left", wraplength=410).pack(anchor="w", pady=(6, 8))
        if os.name == "nt":
            self._button(self.help, "Permitir MiCam en el firewall de Windows", self.open_firewall).pack(anchor="w")

    # --- lógica
    def _startup(self):
        self.server.start()
        if self.server.error or not self.server.port:
            self.url_label.config(text="No se pudo iniciar", fg=C["rec"])
            messagebox.showerror(APP_NAME, f"No se pudo iniciar el servidor:\n{self.server.error}")
        else:
            self.refresh_url()
        self.apply_camera()
        self.root.after(500, self.tick)

    def apply_camera(self):
        res, fps = self.res_var.get(), int(self.fps_var.get())
        w, h = RESOLUTIONS[res]
        self.settings.update(res=res, fps=str(fps))
        save_settings(self.settings)
        self.cam_job += 1
        job = self.cam_job
        self.cam_status.config(text="Iniciando la cámara virtual…", fg=C["mut"])
        self.cam_btns.pack_forget()

        def work():
            self.cam_result = (job, self.vcam.start(w, h, fps), w, h, fps)
        threading.Thread(target=work, daemon=True).start()

    def show_camera(self, err, w, h, fps):
        for b in (self.obs_btn, self.retry_btn):
            b.pack_forget()
        if err is None:
            self.cam_ok_once = True
            self.cam_status.config(text=f"Cámara virtual: {self.vcam.device} · {w}x{h} a {fps} fps", fg=C["mut"])
            self.cam_btns.pack_forget()
            return
        if self.cam_ok_once:
            msg = ("La cámara virtual está ocupada. Si en OBS está activado \"Iniciar cámara virtual\", "
                   "desactívalo y toca Reintentar.")
        else:
            msg = ("No se pudo abrir la cámara virtual. Si no tienes OBS Studio, instálalo (es gratis) "
                   "y ábrelo una vez. Si ya lo tienes, revisa que su \"Iniciar cámara virtual\" esté apagado.")
            self.obs_btn.pack(side="left", padx=(0, 8))
        self.retry_btn.pack(side="left")
        self.cam_status.config(text=f"{msg}\nDetalle: {err}", fg=C["rec"])
        self.cam_btns.pack(anchor="w", padx=14, pady=(8, 0), before=self.opts)

    def refresh_url(self):
        self.url = f"https://{self.ip}:{self.server.port}"
        qr = segno.make(self.url, error="m")
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=5, border=3, dark=C["bg"], light="#ffffff")
        self.qr_img = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode())
        self.qr_label.config(image=self.qr_img, width=self.qr_img.width(), height=self.qr_img.height())
        self.url_label.config(text=self.url, fg=C["fg"])

    def tick(self):
        if self.cam_result:
            job, err, w, h, fps = self.cam_result
            self.cam_result = None
            if job == self.cam_job:
                self.show_camera(err, w, h, fps)
        with self.shared.lock:
            clients, fps = self.shared.clients, self.shared.fps
        if clients:
            self.phone_status.config(text=f"Celular conectado · {fps:.0f} fps")
            self.dot.itemconfig(self.dot_id, fill=C["ok"])
        else:
            self.phone_status.config(text="Esperando al celular")
            self.dot.itemconfig(self.dot_id, fill=C["mut"])
        self.ticks += 1
        if self.ticks % 10 == 0 and self.server.port:  # si cambia la red, se actualiza el QR
            ip = local_ip()
            if ip != self.ip:
                self.ip = ip
                self.refresh_url()
        self.root.after(500, self.tick)

    def copy_url(self):
        if not self.url:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url)
        self.copy_btn.config(text="Dirección copiada")
        self.root.after(1500, lambda: self.copy_btn.config(text="Copiar dirección"))

    def toggle_help(self):
        if self.help.winfo_ismapped():
            self.help.pack_forget()
        else:
            self.help.pack(fill="x")

    def open_firewall(self):
        import ctypes
        ports = f"{PORTS[0]}-{PORTS[-1]}"
        cmd = ("Remove-NetFirewallRule -DisplayName 'MiCam' -ErrorAction SilentlyContinue; "
               f"New-NetFirewallRule -DisplayName 'MiCam' -Direction Inbound -Protocol TCP "
               f"-LocalPort {ports} -Action Allow -Profile Any")
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "powershell.exe", f'-NoProfile -WindowStyle Hidden -Command "{cmd}"', None, 0)
        if result <= 32:
            messagebox.showwarning(APP_NAME, "No se pudo pedir el permiso de administrador.")
        else:
            messagebox.showinfo(APP_NAME, "Si aceptaste el aviso de Windows, el firewall ya deja pasar "
                                          "a MiCam. Prueba de nuevo desde el celular.")

    def close(self):
        self.root.withdraw()
        self.server.stop()
        self.vcam.stop()
        self.root.destroy()


def main():
    try:
        App().root.mainloop()
    except Exception:
        log.exception("Error fatal")
        raise


# ------------------------------------------------- página para el celular
PHONE_PAGE = r"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#1c2230">
<title>MiCam</title>
<style>
:root{--bg:#1c2230;--panel:#262d3d;--line:#343c50;--fg:#edeff3;--mut:#9aa3b5;--live:#ffb020;--rec:#e5484d}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);font:15px/1.4 "Segoe UI",Roboto,system-ui,sans-serif;
 display:flex;flex-direction:column;padding-bottom:env(safe-area-inset-bottom,0px)}
.view{position:relative;flex:1;min-height:120px;background:#000;overflow:hidden}
.view video,.view canvas{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:block}
.view canvas{background:#000}
.badge{position:absolute;top:calc(12px + env(safe-area-inset-top,0px));left:12px;max-width:calc(100% - 24px);
 display:flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;background:rgba(20,24,34,.75);font-size:13px;color:var(--mut)}
.dot{flex:none;width:9px;height:9px;border-radius:50%;background:var(--mut)}
body.on .dot{background:var(--rec);box-shadow:0 0 0 4px rgba(229,72,77,.25)}
body.on .badge{color:var(--fg)}
.controls{background:var(--panel);border-top:1px solid var(--line);padding:12px 16px 16px;
 display:grid;grid-template-columns:1fr auto 1fr;gap:10px 12px;align-items:center}
.full{grid-column:1/-1}
.col{display:grid;gap:8px}
select,.tool{width:100%;padding:10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg);font-size:14px}
.tools{display:grid;grid-template-columns:1fr 1fr 1fr 1.3fr;gap:8px}
.tool{padding:10px 4px;font-size:13px;cursor:pointer}
.tool[aria-pressed="true"]{border-color:var(--live);color:var(--live)}
select:focus-visible,button:focus-visible,input:focus-visible{outline:2px solid var(--live);outline-offset:2px}
.zoom{display:flex;align-items:center;gap:12px;color:var(--mut);font-size:13px}
.zoom[hidden]{display:none}
.zoom input{flex:1;accent-color:var(--live)}
.zoom output{min-width:3.5em;text-align:right;color:var(--fg);font-variant-numeric:tabular-nums}
#go{width:76px;height:76px;border-radius:50%;border:4px solid var(--fg);background:transparent;padding:0;cursor:pointer;display:grid;place-items:center}
#go span{display:block;width:54px;height:54px;border-radius:50%;background:var(--rec);transition:all .2s}
body.on #go span{width:28px;height:28px;border-radius:6px}
@media (prefers-reduced-motion:reduce){#go span{transition:none}}
#hint{text-align:center;color:var(--mut);font-size:13px;min-height:1.2em}
</style></head>
<body>
<div class="view">
  <video id="v" autoplay playsinline muted></video>
  <canvas id="out" width="1920" height="1080"></canvas>
  <div class="badge"><span class="dot"></span><span id="status">Detenida</span></div>
</div>
<div class="controls">
  <select id="cam" class="full" aria-label="Cámara"><option value="">Cámara trasera</option></select>
  <label class="zoom full" id="zoomRow" hidden>Zoom
    <input id="zoom" type="range" min="1" max="2" step="0.1" value="1"><output id="zoomVal">1.0x</output></label>
  <div class="tools full">
    <button class="tool" id="rot">Rotar 0°</button>
    <button class="tool" id="flipH" aria-pressed="false">Espejo</button>
    <button class="tool" id="flipV" aria-pressed="false">Invertir</button>
    <select id="fit" aria-label="Encuadre">
      <option value="fit">Ajustar</option><option value="fill">Llenar</option>
    </select>
  </div>
  <div class="col">
    <select id="res" aria-label="Resolución">
      <option value="1280x720">720p</option><option value="1920x1080" selected>1080p</option><option value="3840x2160">4K</option>
    </select>
    <select id="fps" aria-label="Cuadros por segundo">
      <option value="30">30 fps</option><option value="60" selected>60 fps</option>
    </select>
  </div>
  <button id="go" aria-label="Iniciar transmisión"><span></span></button>
  <div class="col">
    <select id="br" aria-label="Calidad del video">
      <option value="6">Calidad normal</option><option value="12" selected>Calidad alta</option>
      <option value="20">Calidad muy alta</option><option value="35">Calidad máxima</option>
    </select>
    <select id="mode" aria-label="Formato de envío">
      <option value="h264">Video H.264</option><option value="jpeg">JPEG (compatible)</option>
    </select>
  </div>
  <div id="hint" class="full">La vista de arriba es exactamente lo que recibe la PC.</div>
</div>
<script>
const $ = id => document.getElementById(id);
const v = $('v'), out = $('out'), statusEl = $('status'), hint = $('hint'), go = $('go');
const ctx = out.getContext('2d', { alpha: false });
const zoomRow = $('zoomRow'), zoom = $('zoom'), zoomVal = $('zoomVal');
const HAS_ENCODER = 'VideoEncoder' in window && 'VideoFrame' in window;
if (!HAS_ENCODER) { $('mode').value = 'jpeg'; $('mode').disabled = true; }

const save = (k, val) => { try { localStorage.setItem('micam.' + k, val); } catch (e) {} };
const load = k => { try { return localStorage.getItem('micam.' + k); } catch (e) { return null; } };
['res', 'fps', 'br', 'mode', 'fit'].forEach(id => { const x = load(id); if (x && !$(id).disabled) $(id).value = x; });

let rot = +(load('rot') || 0), flipH = load('flipH') === '1', flipV = load('flipV') === '1';
function showTools() {
  $('rot').textContent = `Rotar ${rot}°`;
  $('flipH').setAttribute('aria-pressed', flipH);
  $('flipV').setAttribute('aria-pressed', flipV);
}
showTools();

let stream = null, track = null, ws = null, running = false, wake = null, lastConnect = 0;
let enc = null, encKey = '', configuring = false, forceKey = true, lastKey = 0, lastSent = 0;
let nalLen = 4, paramSets = null;
let sentFrames = 0, sentBytes = 0, lastStat = performance.now();
let loopId = 0, useRaf = false, lastFrameAt = 0, jpegBusy = false;

/* ---------------- cámara ---------------- */
async function listCameras() {
  const devs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'videoinput');
  const sel = $('cam'), current = track && track.getSettings().deviceId;
  sel.innerHTML = '';
  devs.forEach((d, i) => {
    const o = document.createElement('option');
    o.value = d.deviceId; o.textContent = d.label || ('Cámara ' + (i + 1));
    sel.appendChild(o);
  });
  if (current && devs.some(d => d.deviceId === current)) sel.value = current;
}

async function startCamera() {
  if (stream) stream.getTracks().forEach(t => t.stop());
  stream = null;
  const [w, h] = $('res').value.split('x').map(Number);
  const fps = +$('fps').value;
  const id = $('cam').value || load('cam');
  const where = id ? { deviceId: { exact: id } } : { facingMode: 'environment' };
  const size = { width: { ideal: w }, height: { ideal: h } };
  // Primero se exige el fps pedido; si la cámara no puede, se acepta lo que dé.
  const tries = [];
  if (fps > 30) tries.push({ ...where, ...size, frameRate: { min: fps - 5, ideal: fps } });
  tries.push({ ...where, ...size, frameRate: { ideal: fps } });
  if (id) tries.push({ facingMode: 'environment', ...size, frameRate: { ideal: fps } });
  let lastErr = null;
  for (const video of tries) {
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: false, video }); break; }
    catch (e) { lastErr = e; }
  }
  if (!stream) throw lastErr;
  track = stream.getVideoTracks()[0];
  if (fps > 30 && (track.getSettings().frameRate || 0) < fps - 5) {
    try { await track.applyConstraints({ frameRate: { min: fps - 5, ideal: fps } }); } catch (e) {}
  }
  v.srcObject = stream;
  await v.play();
  await listCameras();
  setupZoom();
  const s = track.getSettings();
  const caps = track.getCapabilities ? track.getCapabilities() : {};
  const max = caps.frameRate && caps.frameRate.max;
  hint.textContent = `Cámara: ${s.width}x${s.height} a ${Math.round(s.frameRate || 0)} fps` +
    (max ? ` (máximo de esta cámara: ${Math.round(max)} fps)` : '');
}

function setupZoom() {
  const caps = track.getCapabilities ? track.getCapabilities() : {};
  if (caps.zoom && caps.zoom.max > caps.zoom.min) {
    zoom.min = caps.zoom.min;
    zoom.max = Math.min(caps.zoom.max, caps.zoom.min * 10);
    zoom.step = caps.zoom.step || 0.1;
    zoom.value = track.getSettings().zoom || caps.zoom.min;
    zoomRow.hidden = false;
    zoomVal.textContent = (+zoom.value).toFixed(1) + 'x';
  } else {
    zoomRow.hidden = true;
  }
}
zoom.oninput = () => {
  zoomVal.textContent = (+zoom.value).toFixed(1) + 'x';
  if (track) track.applyConstraints({ advanced: [{ zoom: +zoom.value }] }).catch(() => {});
};

/* ------- dibujo: orientación correcta, giro, espejo y encuadre 16:9 ------- */
function drawFrame() {
  const [W, H] = $('res').value.split('x').map(Number);
  if (out.width !== W || out.height !== H) { out.width = W; out.height = H; }
  const vw = v.videoWidth, vh = v.videoHeight;
  if (!vw || !vh) return false;
  const sideways = rot % 180 !== 0;
  const rw = sideways ? vh : vw, rh = sideways ? vw : vh;
  const s = $('fit').value === 'fill' ? Math.max(W / rw, H / rh) : Math.min(W / rw, H / rh);
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, W, H);
  ctx.translate(W / 2, H / 2);
  ctx.scale(flipH ? -1 : 1, flipV ? -1 : 1);
  ctx.rotate(rot * Math.PI / 180);
  ctx.drawImage(v, -vw * s / 2, -vh * s / 2, vw * s, vh * s);
  return true;
}

/* ---------------- conexión ---------------- */
function openSocket() {
  if (ws && ws.readyState <= 1) return;
  const now = performance.now();
  if (now - lastConnect < 1000) return;
  lastConnect = now;
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { ws.send('reset'); forceKey = true; };
  ws.onclose = () => { if (running) statusEl.textContent = 'Sin conexión, reintentando'; };
}

function send(kind, data) {
  if (!ws || ws.readyState !== 1) { forceKey = true; return; }
  const msg = new Uint8Array(data.byteLength + 1);
  msg[0] = kind; msg.set(data, 1);
  ws.send(msg);
  sentFrames++; sentBytes += msg.byteLength;
}

/* ---------------- H.264 ---------------- */
const START = new Uint8Array([0, 0, 0, 1]);
const toBytes = d => d instanceof ArrayBuffer ? new Uint8Array(d) : new Uint8Array(d.buffer, d.byteOffset, d.byteLength);
function concat(parts) {
  let n = 0; parts.forEach(p => n += p.length);
  const o = new Uint8Array(n); let i = 0;
  parts.forEach(p => { o.set(p, i); i += p.length; });
  return o;
}
function isAnnexB(b) { return b.length > 4 && b[0] === 0 && b[1] === 0 && (b[2] === 1 || (b[2] === 0 && b[3] === 1)); }
function parseAvcC(desc) {
  const b = toBytes(desc); if (b.length < 7) return;
  nalLen = (b[4] & 3) + 1;
  const parts = []; let p = 5;
  for (let pass = 0; pass < 2 && p < b.length; pass++) {
    const n = pass === 0 ? (b[p++] & 31) : b[p++];
    for (let i = 0; i < n && p + 2 <= b.length; i++) {
      const len = (b[p] << 8) | b[p + 1]; p += 2;
      parts.push(START, b.subarray(p, p + len)); p += len;
    }
  }
  paramSets = concat(parts);
}
function toAnnexB(b, isKey) {
  if (isAnnexB(b)) return b;
  const parts = []; if (isKey && paramSets) parts.push(paramSets);
  let p = 0;
  while (p + nalLen <= b.length) {
    let len = 0; for (let i = 0; i < nalLen; i++) len = len * 256 + b[p + i];
    p += nalLen; parts.push(START, b.subarray(p, p + len)); p += len;
  }
  return concat(parts);
}
function onChunk(chunk, meta) {
  if (meta && meta.decoderConfig && meta.decoderConfig.description) parseAvcC(meta.decoderConfig.description);
  const raw = new Uint8Array(chunk.byteLength); chunk.copyTo(raw);
  send(1, toAnnexB(raw, chunk.type === 'key'));
}

const CODECS = ['avc1.640033', 'avc1.64002A', 'avc1.640028', 'avc1.4D0033', 'avc1.4D0028', 'avc1.42E033', 'avc1.42E01F'];
async function makeEncoder(w, h) {
  if (enc) { try { enc.close(); } catch (e) {} enc = null; }
  const framerate = +$('fps').value, bitrate = +$('br').value * 1e6;
  for (const codec of CODECS) {
    for (const avc of [{ format: 'annexb' }, null]) {
      const cfg = { codec, width: w, height: h, bitrate, framerate, latencyMode: 'realtime' };
      if (avc) cfg.avc = avc;
      try {
        const r = await VideoEncoder.isConfigSupported(cfg);
        if (!r.supported) continue;
        enc = new VideoEncoder({ output: onChunk, error: e => { console.error(e); enc = null; encKey = ''; } });
        enc.configure(r.config);
        paramSets = null; forceKey = true;
        if (ws && ws.readyState === 1) ws.send('reset');
        return true;
      } catch (e) {}
    }
  }
  return false;
}

/* ---------------- envío ---------------- */
async function pump() {
  if (!ws || ws.readyState > 1) { openSocket(); return; }
  if (ws.readyState !== 1) return;
  const now = performance.now();
  if (now - lastSent < 1000 / +$('fps').value * 0.8) return;
  const maxBuffer = Math.max(200000, +$('br').value * 1e6 / 8 * 0.15);
  if (ws.bufferedAmount > maxBuffer) return;  // red atrasada: saltar cuadro en vez de acumular lag
  const W = out.width, H = out.height;

  if ($('mode').value === 'h264' && HAS_ENCODER) {
    if (configuring) return;
    const key = `${W}x${H}@${$('fps').value}/${$('br').value}`;
    if (!enc || enc.state !== 'configured' || key !== encKey) {
      configuring = true;
      const ok = await makeEncoder(W, H);
      configuring = false;
      if (ok) encKey = key;
      else { $('mode').value = 'jpeg'; hint.textContent = 'H.264 no disponible, usando JPEG.'; }
      return;
    }
    if (enc.encodeQueueSize > 2) return;
    const keyFrame = forceKey || now - lastKey > 2000;
    if (keyFrame) { forceKey = false; lastKey = now; }
    const frame = new VideoFrame(out, { timestamp: Math.round(now * 1000) });
    try { enc.encode(frame, { keyFrame }); } finally { frame.close(); }
    lastSent = now;
  } else {
    if (jpegBusy) return;
    jpegBusy = true; lastSent = now;
    out.toBlob(async b => {
      if (b) send(2, new Uint8Array(await b.arrayBuffer()));
      jpegBusy = false;
    }, 'image/jpeg', 0.85);
  }
}

function stats() {
  const now = performance.now(), dt = (now - lastStat) / 1000;
  if (dt < 1) return;
  if (ws && ws.readyState === 1) {
    statusEl.textContent = `En vivo · ${Math.round(sentFrames / dt)} fps · ${(sentBytes * 8 / dt / 1e6).toFixed(1)} Mbps · ${out.width}x${out.height}`;
  }
  sentFrames = 0; sentBytes = 0; lastStat = now;
}

function schedule(id) {
  const cb = () => onFrame(id);
  if (!useRaf && v.requestVideoFrameCallback) v.requestVideoFrameCallback(cb);
  else requestAnimationFrame(cb);
}
function onFrame(id) {
  if (!running || id !== loopId) return;
  lastFrameAt = performance.now();
  if (drawFrame()) pump().catch(e => console.error(e));
  stats();
  schedule(id);
}
// Si el navegador deja de avisar cuadros nuevos, se pasa a un reloj normal.
setInterval(() => {
  if (running && performance.now() - lastFrameAt > 700) { useRaf = true; loopId++; schedule(loopId); }
}, 500);

async function keepAwake() {
  try { if ('wakeLock' in navigator) wake = await navigator.wakeLock.request('screen'); } catch (e) {}
}

/* ---------------- iniciar / detener ---------------- */
async function start() {
  try { await startCamera(); }
  catch (e) { hint.textContent = 'No se pudo abrir la cámara. Revisa el permiso en el navegador.'; return; }
  running = true;
  document.body.classList.add('on');
  go.setAttribute('aria-label', 'Detener transmisión');
  statusEl.textContent = 'Conectando';
  openSocket();
  keepAwake();
  lastFrameAt = performance.now();
  loopId++; schedule(loopId);
}

function stop() {
  running = false;
  document.body.classList.remove('on');
  go.setAttribute('aria-label', 'Iniciar transmisión');
  statusEl.textContent = 'Detenida';
  if (ws) ws.close();
  if (enc) { try { enc.close(); } catch (e) {} enc = null; }
  encKey = '';
  if (stream) stream.getTracks().forEach(t => t.stop());
  stream = null; track = null;
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.fillStyle = '#000'; ctx.fillRect(0, 0, out.width, out.height);
  if (wake) { wake.release().catch(() => {}); wake = null; }
}

go.onclick = () => running ? stop() : start();
$('rot').onclick = () => { rot = (rot + 90) % 360; save('rot', rot); showTools(); };
$('flipH').onclick = () => { flipH = !flipH; save('flipH', flipH ? '1' : '0'); showTools(); };
$('flipV').onclick = () => { flipV = !flipV; save('flipV', flipV ? '1' : '0'); showTools(); };
$('fit').onchange = () => save('fit', $('fit').value);
$('cam').onchange = () => { save('cam', $('cam').value); if (running) startCamera().catch(() => {}); };
['res', 'fps'].forEach(id => $(id).onchange = () => { save(id, $(id).value); if (running) startCamera().catch(() => {}); });
['br', 'mode'].forEach(id => $(id).onchange = () => { save(id, $(id).value); encKey = ''; });
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && running) keepAwake();
});
if (!window.isSecureContext) hint.textContent = 'Abre esta página con https:// para poder usar la cámara.';
</script>
</body></html>
"""

if __name__ == "__main__":
    main()
