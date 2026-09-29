# MiCam

Usa la cámara de tu celular como webcam en tu PC con Windows, sin instalar ninguna app en el celular. Funciona con iPhone y Android, directo desde el navegador.

## Qué hace

- Video fluido de hasta 60 fps y hasta 4K, comprimido por el propio celular (H.264).
- Elige cualquier cámara del celular: principal, ultra gran angular (0.5x), teleobjetivo o frontal.
- Rotar, espejo, invertir y encuadre 16:9 desde el celular.
- Conexión por Wi‑Fi escaneando un código QR.
- Aparece como una cámara normal en Zoom, Meet, Teams, Discord, OBS y cualquier otro programa.

## Cómo usarlo

1. Instala [OBS Studio](https://obsproject.com/download) (es gratis). MiCam usa su controlador de cámara virtual.
2. Descarga `MiCam.exe` desde la sección **Releases** de esta página y ábrelo.
   - Si Windows muestra "Windows protegió su PC", toca **Más información** y luego **Ejecutar de todas formas**. Aparece porque el programa no está firmado digitalmente.
3. Conecta el celular al mismo Wi‑Fi que la PC y escanea el código QR que muestra MiCam.
4. Si el navegador avisa del certificado, toca **Mostrar detalles** y luego **visitar este sitio web**. El certificado lo crea MiCam en tu propia PC.
5. Toca el botón rojo y permite el acceso a la cámara.
6. En tu programa de videollamadas, elige la cámara **OBS Virtual Camera**.

## Si el celular no conecta

- Revisa que el celular esté en el mismo Wi‑Fi y sin VPN.
- Si usas un firewall aparte (Portmaster, el de tu antivirus, etc.), dale permiso a MiCam para recibir conexiones.
- Para el firewall de Windows, usa el botón **"¿El celular no conecta?"** dentro de la app.
- Si en OBS está activado **"Iniciar cámara virtual"**, desactívalo: MiCam y OBS no pueden usarla a la vez.

## Crear el .exe desde el código

Necesitas Python 3 instalado. Pon `micam.py`, `micam.ico` y `build.bat` en una carpeta y haz doble clic en `build.bat`. El programa queda en `dist\MiCam.exe`.

Para ejecutarlo sin crear el .exe:

```
pip install aiohttp pyvirtualcam opencv-python numpy cryptography av segno
python micam.py
```

## Limitaciones

- Solo Windows por ahora (en Linux funciona desde el código con `v4l2loopback`).
- No transmite el micrófono del celular.
- La pantalla del celular debe quedar encendida con la página abierta mientras se transmite.
