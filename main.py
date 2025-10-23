import asyncio, base64, io, json, socket, struct, time, threading, traceback
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from PIL import Image
import pyfiglet

# --- HTML/CSS/JS ---

SCALAR_HTML = """
<!doctype html>
<html>
<head>
    <title>VNC Web Server API Docs</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <style>body{margin:0}</style>
</head>
<body>
    <script id="api-reference" data-url="/openapi.json"></script>
    <script>
        var configuration = { theme: 'dark' };
        var apiReference = document.getElementById('api-reference');
        apiReference.dataset.configuration = JSON.stringify(configuration);
    </script>
    <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
</body>
</html>
"""

VIEWER_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>VNC Viewer</title>
    <style>
        * {{ margin:0; padding:0; }}
        body {{ background:#000; overflow:hidden; }}
        #screen {{ position:absolute; image-rendering:crisp-edges; cursor:default; }}
    </style>
</head>
<body>
<canvas id="screen"></canvas>
<script>
    const canvas = document.getElementById('screen');
    const ctx = canvas.getContext('2d');
    
    const HOST = '{host}';
    const PORT = {port};
    let VNC_WIDTH = 1600, VNC_HEIGHT = 900;
    let scale = 1, offsetX = 0, offsetY = 0;
    let isMouseDown = false, buttonMask = 0, lastMousePos = {{ x: 0, y: 0 }};

    const ws = new WebSocket(`ws://${{location.host}}/ws/${{HOST}}:${{PORT}}`);

    function updateCanvasLayout() {{
        scale = Math.min(window.innerWidth / VNC_WIDTH, window.innerHeight / VNC_HEIGHT);
        const scaledWidth = VNC_WIDTH * scale;
        const scaledHeight = VNC_HEIGHT * scale;
        
        canvas.style.width = `${{scaledWidth}}px`;
        canvas.style.height = `${{scaledHeight}}px`;
        
        offsetX = (window.innerWidth - scaledWidth) / 2;
        offsetY = (window.innerHeight - scaledHeight) / 2;
        canvas.style.left = `${{offsetX}}px`;
        canvas.style.top = `${{offsetY}}px`;

        canvas.width = VNC_WIDTH;
        canvas.height = VNC_HEIGHT;
        
        // After resizing, the canvas is cleared. Request a full frame from the server.
        if (ws.readyState === 1) {{
            ws.send(JSON.stringify({{ type: 'request_full_frame' }}));
        }}
    }}

    function getVncCoords(event) {{
        const x = Math.floor((event.clientX - offsetX) / scale);
        const y = Math.floor((event.clientY - offsetY) / scale);
        return {{
            x: Math.max(0, Math.min(VNC_WIDTH - 1, x)),
            y: Math.max(0, Math.min(VNC_HEIGHT - 1, y))
        }};
    }}

    function sendPointerEvent(x, y, mask) {{
        if (ws.readyState === 1) {{
            ws.send(JSON.stringify({{ type: 'pointer', x, y, buttonMask: mask }}));
        }}
    }}
    
    function handleMouseMove(event) {{
        const pos = getVncCoords(event);
        lastMousePos = pos;
        sendPointerEvent(pos.x, pos.y, isMouseDown ? buttonMask : 0);
    }}

    function handleMouseDown(event) {{
        event.preventDefault();
        isMouseDown = true;
        buttonMask = 1 << event.button;
        const pos = getVncCoords(event);
        lastMousePos = pos;
        if (canvas.setPointerCapture) {{
            try {{ canvas.setPointerCapture(event.pointerId); }} catch(e) {{}}
        }}
        sendPointerEvent(pos.x, pos.y, buttonMask);
    }}

    function handleMouseUp(event) {{
        if (event) event.preventDefault();
        if (!isMouseDown) return;
        isMouseDown = false;
        buttonMask = 0;
        sendPointerEvent(lastMousePos.x, lastMousePos.y, 0);
    }}

    // Use Pointer events for better compatibility
    if ('onpointerdown' in canvas) {{
        canvas.onpointerdown = handleMouseDown;
        canvas.onpointermove = handleMouseMove;
        canvas.onpointerup = handleMouseUp;
        canvas.onpointercancel = handleMouseUp;
    }} else {{
        canvas.onmousedown = handleMouseDown;
        canvas.onmousemove = handleMouseMove;
        canvas.onmouseup = handleMouseUp;
        window.addEventListener('mouseup', handleMouseUp); // Catch mouse up outside the canvas
    }}
    
    window.addEventListener('blur', handleMouseUp); // Release mouse if window loses focus
    canvas.oncontextmenu = (e) => e.preventDefault();
    
    canvas.onwheel = (event) => {{
        if (ws.readyState !== 1) return;
        event.preventDefault();
        const pos = getVncCoords(event);
        const button = event.deltaY < 0 ? 4 : 5; // Wheel up: 4, Wheel down: 5
        const mask = 1 << (button - 1);
        sendPointerEvent(pos.x, pos.y, mask);
        // Release the scroll button shortly after
        setTimeout(() => sendPointerEvent(pos.x, pos.y, 0), 50);
    }};

    document.onkeydown = (event) => {{
        if (ws.readyState === 1) {{
            event.preventDefault();
            ws.send(JSON.stringify({{ type: 'key', key: event.key, down: true }}));
        }}
    }};
    
    document.onkeyup = (event) => {{
        if (ws.readyState === 1) {{
            event.preventDefault();
            ws.send(JSON.stringify({{ type: 'key', key: event.key, down: false }}));
        }}
    }};

    ws.onmessage = (event) => {{
        const msg = JSON.parse(event.data);
        if (msg.type === 'connected') {{
            VNC_WIDTH = msg.width;
            VNC_HEIGHT = msg.height;
            updateCanvasLayout();
        }} else if (msg.type === 'frame' || msg.type === 'update') {{
            const img = new Image();
            img.decoding = 'async';
            img.onload = () => ctx.drawImage(img, msg.x || 0, msg.y || 0);
            img.src = 'data:image/jpeg;base64,' + msg.data;
        }}
    }};
    
    window.onresize = updateCanvasLayout;

</script>
</body>
</html>
"""

# --- FastAPI App Setup ---
app = FastAPI(title="VNC Web Server API", description="An API to remotely control VNC sessions.", version="1.7")

vnc_sessions: Dict[str, 'VNCController'] = {}

# --- API Models ---
class MovePayload(BaseModel): x: int = Field(...); y: int = Field(...)
class ClickPayload(BaseModel): x: int; y: int; button: int = 1
class TypePayload(BaseModel): text: str; delay: float = 0.05
class ScrollPayload(BaseModel): direction: str = "down"; x: Optional[int] = None; y: Optional[int] = None
class DragPayload(BaseModel): x_start: int; y_start: int; x_end: int; y_end: int; button: int = 1; delay: float = 0.5
class ShortcutPayload(BaseModel): keys: List[str]

# --- VNC Controller Class ---
class VNCController:
    def __init__(self, host: str, port: int):
        self.host, self.port, self.key = host, port, f"{host}:{port}"
        self.sock: Optional[socket.socket] = None
        self.running = False
        self.width = 1600
        self.height = 900
        self.fb: Optional[Image.Image] = None
        self.fb_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.dirty_rects: List[Dict] = []
        self.dirty_event = threading.Event()
        self.button_mask = 0
        self.update_thread: Optional[threading.Thread] = None
        self.connected_clients: set[WebSocket] = set()
        self.broadcast_task: Optional[asyncio.Task] = None

    def connect(self) -> bool:
        try:
            print(f"Connecting to VNC server at {self.host}:{self.port}...")
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect((self.host, self.port))

            # VNC Handshake
            version_msg = self.sock.recv(12)
            if not version_msg or not version_msg.startswith(b'RFB '):
                raise Exception(f"Not a valid VNC server: {version_msg}")
            self.sock.send(b'RFB 003.008\n')
            
            # Security Handshake
            num_sec_types = self._recv_exact(1)
            if not num_sec_types: raise Exception("Handshake failed: connection closed")
            
            if num_sec_types[0] == 0:
                reason_len_data = self._recv_exact(4)
                if not reason_len_data: raise Exception("Connection failed: No reason length")
                reason_len = struct.unpack('!I', reason_len_data)[0]
                reason = self._recv_exact(reason_len).decode()
                raise Exception(f"Connection failed: {reason}")
            
            sec_types = self._recv_exact(num_sec_types[0])
            if not sec_types: raise Exception("Handshake failed: No security types")

            if 1 in sec_types:  # No security
                self.sock.send(b'\x01')
            elif 2 in sec_types: # VNC Authentication
                self.sock.send(b'\x02')
                # For simplicity, this client doesn't support password auth yet.
                # It will likely fail here if the server requires a password.
                challenge = self._recv_exact(16)
                if not challenge: raise Exception("VNC Auth failed")
                # Responding with a dummy value
                self.sock.send(b'\x00' * 16) 
            else:
                raise Exception(f"No supported security type found: {list(sec_types)}")
            
            # Security result
            result = self._recv_exact(4)
            if not result or struct.unpack('!I', result)[0] != 0:
                raise Exception("Security handshake failed")

            # ClientInit
            self.sock.send(b'\x01')  # Shared flag
            server_init = self._recv_exact(24)
            if not server_init: raise Exception("Failed to receive server initialization")

            self.width, self.height = struct.unpack('!HH', server_init[:4])
            name_len = struct.unpack('!I', server_init[20:24])[0]
            self._recv_exact(name_len)  # Discard server name
            
            self.fb = Image.new('RGB', (self.width, self.height), (30, 30, 30))
            
            # Set Pixel Format & Encodings
            self._configure_session()
            
            self.sock.settimeout(None)  # Switch to blocking mode
            self.running = True
            self.update_thread = threading.Thread(target=self._update_loop, daemon=True)
            self.update_thread.start()
            print(f"VNC session ready: {self.key}")
            return True
        except Exception as e:
            print(f"VNC connection error: {e}")
            traceback.print_exc()
            if self.sock:
                self.sock.close()
            self.sock = None
            return False

    def _configure_session(self):
        # SetPixelFormat (32bpp, true-color)
        pixel_format = struct.pack('!BBBBHHHBBBBBB', 32, 24, 0, 1, 255, 255, 255, 16, 8, 0, 0, 0, 0)
        with self.send_lock:
            self.sock.send(b'\x00\x00\x00\x00' + pixel_format)
            
        # SetEncodings: Raw(0), CopyRect(1), DesktopSize(-223)
        encodings = [0, 1, -223]
        payload = b''.join(struct.pack('!i', e) for e in encodings)
        with self.send_lock:
            self.sock.send(b'\x02\x00' + struct.pack('!H', len(encodings)) + payload)

    def _update_loop(self):
        try: # Initial non-incremental framebuffer update request
            with self.send_lock:
                self.sock.send(b'\x03\x00' + struct.pack('!HHHH', 0, 0, self.width, self.height))
        except Exception as e:
            print(f"Initial FramebufferUpdateRequest failed: {e}")
            self.running = False
            return

        while self.running:
            try:
                msg_type_data = self._recv_exact(1)
                if not msg_type_data:
                    print("Server closed connection.")
                    break
                msg_type = msg_type_data[0]

                if msg_type == 0:  # FramebufferUpdate
                    self._handle_framebuffer_update()
                elif msg_type == 1:  # SetColorMapEntries
                    self._recv_exact(1) # padding
                    self._recv_exact(2) # first-color
                    num_colors_data = self._recv_exact(2)
                    if not num_colors_data: break
                    num_colors = struct.unpack('!H', num_colors_data)[0]
                    self._recv_exact(6 * num_colors)
                elif msg_type == 2:  # Bell
                    pass
                elif msg_type == 3:  # ServerCutText
                    self._recv_exact(3) # padding
                    length_data = self._recv_exact(4)
                    if not length_data: break
                    length = struct.unpack('!I', length_data)[0]
                    self._recv_exact(length)
                else:
                    print(f"Unknown server message type: {msg_type}")

                # Request incremental updates
                with self.send_lock:
                    self.sock.send(b'\x03\x01' + struct.pack('!HHHH', 0, 0, self.width, self.height))

            except Exception as e:
                print(f"Error in update loop: {e}")
                break
        
        self.running = False
        print("VNC update loop stopped.")

    def _handle_framebuffer_update(self):
        self._recv_exact(1)  # padding
        num_rects_data = self._recv_exact(2)
        if not num_rects_data: return
        num_rects = struct.unpack('!H', num_rects_data)[0]

        for _ in range(num_rects):
            rect_header = self._recv_exact(12)
            if not rect_header: break
            x, y, w, h, encoding = struct.unpack('!HHHHi', rect_header)

            if encoding == 0:  # Raw
                data_size = w * h * 4
                pixel_data = self._recv_exact(data_size)
                if not pixel_data: break
                rect_img = Image.frombytes('RGB', (w, h), pixel_data, 'raw', 'BGRX')
                with self.fb_lock:
                    self.fb.paste(rect_img, (x, y))
                    self.dirty_rects.append({'x': x, 'y': y, 'w': w, 'h': h})
                self.dirty_event.set()
            elif encoding == 1:  # CopyRect
                src_pos_data = self._recv_exact(4)
                if not src_pos_data: break
                src_x, src_y = struct.unpack('!HH', src_pos_data)
                with self.fb_lock:
                    copied_rect = self.fb.crop((src_x, src_y, src_x + w, src_y + h))
                    self.fb.paste(copied_rect, (x, y))
                    self.dirty_rects.append({'x': x, 'y': y, 'w': w, 'h': h})
                self.dirty_event.set()
            elif encoding == -223:  # DesktopSize
                with self.fb_lock:
                    old_fb = self.fb
                    self.width, self.height = w, h
                    new_fb = Image.new('RGB', (w, h), (30, 30, 30))
                    if old_fb:
                        paste_w = min(old_fb.width, w)
                        paste_h = min(old_fb.height, h)
                        new_fb.paste(old_fb.crop((0, 0, paste_w, paste_h)), (0, 0))
                    self.fb = new_fb
                    self.dirty_rects.append({'x': 0, 'y': 0, 'w': w, 'h': h})
                self.dirty_event.set()
            else:
                print(f"Unsupported encoding {encoding} ({w}x{h} @ {x},{y})")

    def _recv_exact(self, n: int) -> Optional[bytes]:
        data = b''
        try:
            while len(data) < n:
                chunk = self.sock.recv(n - len(data))
                if not chunk:
                    return None
                data += chunk
            return data
        except (socket.timeout, ConnectionAbortedError, ConnectionResetError, OSError):
            return None

    def disconnect(self):
        self.running = False
        if self.sock:
            try: self.sock.close()
            except: pass
        self.sock = None
        if self.update_thread and self.update_thread.is_alive():
            try: self.update_thread.join(timeout=0.2)
            except: pass

    # --- Input Methods ---
    def move_mouse(self, x: int, y: int, button_mask: Optional[int] = None):
        if not self.sock or not self.running: return
        if button_mask is not None:
            self.button_mask = button_mask
        x_clamped = max(0, min(self.width - 1, int(x)))
        y_clamped = max(0, min(self.height - 1, int(y)))
        try:
            with self.send_lock:
                self.sock.send(struct.pack('!BBHH', 5, self.button_mask, x_clamped, y_clamped))
        except Exception as e:
            print(f"PointerEvent send error: {e}")

    def click(self, x: int, y: int, button: int = 1):
        mask = 1 << (button - 1)
        self.move_mouse(x, y, mask)
        time.sleep(0.05)
        self.move_mouse(x, y, 0)

    def scroll(self, x: int, y: int, direction: str):
        button = 4 if direction == "up" else 5
        self.click(x, y, button)

    def drag(self, x_start: int, y_start: int, x_end: int, y_end: int, button: int = 1, delay: float = 0.5):
        mask = 1 << (button - 1)
        self.move_mouse(x_start, y_start, mask)
        time.sleep(0.1)
        steps = max(1, int(delay / 0.05))
        for i in range(steps + 1):
            t = i / steps
            inter_x = int(x_start + (x_end - x_start) * t)
            inter_y = int(y_start + (y_end - y_start) * t)
            self.move_mouse(inter_x, inter_y, mask)
            if steps > 1:
                time.sleep(delay / steps)
        self.move_mouse(x_end, y_end, 0)

    def send_key_event(self, key: str, down: bool):
        if not self.sock or not self.running: return
        
        # Mapping from JS key names to X11 keysyms
        key_map = {
            'Backspace': 0xFF08, 'Tab': 0xFF09, 'Enter': 0xFF0D, 'Escape': 0xFF1B,
            'Delete': 0xFFFF, 'Home': 0xFF50, 'ArrowLeft': 0xFF51, 'ArrowUp': 0xFF52,
            'ArrowRight': 0xFF53, 'ArrowDown': 0xFF54, 'PageUp': 0xFF55,
            'PageDown': 0xFF56, 'End': 0xFF57, 'Control': 0xFFE3, 'Shift': 0xFFE1,
            'Alt': 0xFFE9, 'Meta': 0xFFEB, 'CapsLock': 0xFFE5
        }
        
        keysym = key_map.get(key, ord(key) if len(key) == 1 else 0)
        if not keysym:
            print(f"Warning: No keysym mapping for key '{key}'")
            return
            
        try:
            with self.send_lock:
                self.sock.send(struct.pack('!BBxxI', 4, 1 if down else 0, keysym))
        except Exception as e:
            print(f"KeyEvent send error: {e}")

    def type_text(self, text: str, delay: float = 0.05):
        for char in text:
            self.send_key_event(char, True)
            time.sleep(delay / 2)
            self.send_key_event(char, False)
            time.sleep(delay / 2)

    def key_combo(self, *keys):
        for k in keys:
            self.send_key_event(k, True)
            time.sleep(0.05)
        for k in reversed(keys):
            self.send_key_event(k, False)
            time.sleep(0.05)

    def get_screenshot(self) -> Optional[Image.Image]:
        if not self.fb:
            return None
        with self.fb_lock:
            return self.fb.copy()

# --- Helper Functions ---
def get_or_create_vnc_session(host: str, port: int) -> Optional[VNCController]:
    key = f"{host}:{port}"
    session = vnc_sessions.get(key)
    if not session or not session.running:
        print(f"No active session for {key}. Creating a new one.")
        session = VNCController(host, int(port))
        if not session.connect():
            return None
        vnc_sessions[key] = session
    return session

async def ensure_broadcaster_running(vnc: VNCController):
    if vnc.broadcast_task and not vnc.broadcast_task.done():
        return

    async def broadcast_loop():
        try:
            while vnc.running and vnc.connected_clients:
                try:
                    await asyncio.wait_for(asyncio.to_thread(vnc.dirty_event.wait), timeout=1/30)
                except asyncio.TimeoutError:
                    pass
                vnc.dirty_event.clear()

                with vnc.fb_lock:
                    if not vnc.dirty_rects:
                        continue
                    rects = vnc.dirty_rects
                    vnc.dirty_rects = []
                
                min_x = min(r['x'] for r in rects)
                min_y = min(r['y'] for r in rects)
                max_x = max(r['x'] + r['w'] for r in rects)
                max_y = max(r['y'] + r['h'] for r in rects)

                if max_x <= min_x or max_y <= min_y:
                    continue
                
                bbox = (min_x, min_y, max_x, max_y)
                area = (max_x - min_x) * (max_y - min_y)
                quality = 65 if (area / (vnc.width * vnc.height) > 0.2) else 85
                
                with vnc.fb_lock:
                    cropped_img = vnc.fb.crop(bbox)
                
                buffer = io.BytesIO()
                cropped_img.save(buffer, format='JPEG', quality=quality)
                b64_data = base64.b64encode(buffer.getvalue()).decode('ascii')
                
                disconnected_clients = []
                for ws in list(vnc.connected_clients):
                    try:
                        await ws.send_json({'type': 'update', 'x': min_x, 'y': min_y, 'data': b64_data})
                    except Exception:
                        disconnected_clients.append(ws)
                
                for ws in disconnected_clients:
                    vnc.connected_clients.discard(ws)
        finally:
            vnc.broadcast_task = None
            print(f"Broadcast loop for {vnc.key} stopped.")

    vnc.broadcast_task = asyncio.create_task(broadcast_loop())

# --- FastAPI Routes ---
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def get_docs():
    return SCALAR_HTML

@app.get("/vnc/{host}:{port}", response_class=HTMLResponse, include_in_schema=False)
async def get_vnc_viewer(host: str, port: int):
    return HTMLResponse(VIEWER_HTML.format(host=host, port=port))

@app.websocket("/ws/{host}:{port}")
async def websocket_endpoint(websocket: WebSocket, host: str, port: int):
    await websocket.accept()
    vnc = get_or_create_vnc_session(host, port)
    if not vnc:
        await websocket.send_json({'type': 'error', 'message': 'Failed to connect to VNC server'})
        await websocket.close()
        return

    vnc.connected_clients.add(websocket)
    await ensure_broadcaster_running(vnc)

    try:
        await websocket.send_json({'type': 'connected', 'width': vnc.width, 'height': vnc.height})
        
        # Send initial full frame
        img = vnc.get_screenshot()
        if img:
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=85)
            b64_data = base64.b64encode(buffer.getvalue()).decode('ascii')
            await websocket.send_json({'type': 'frame', 'x': 0, 'y': 0, 'data': b64_data})

        while vnc.running:
            try:
                msg = await websocket.receive_json()
                msg_type = msg.get('type')
                
                if msg_type == 'pointer':
                    vnc.move_mouse(msg['x'], msg['y'], msg.get('buttonMask', 0))
                elif msg_type == 'key':
                    vnc.send_key_event(msg['key'], msg['down'])
                elif msg_type == 'request_full_frame': # Handle resize request
                    img = vnc.get_screenshot()
                    if img:
                        buffer = io.BytesIO()
                        img.save(buffer, format='JPEG', quality=85)
                        b64_data = base64.b64encode(buffer.getvalue()).decode('ascii')
                        await websocket.send_json({'type': 'frame', 'x': 0, 'y': 0, 'data': b64_data})

            except WebSocketDisconnect:
                break
            except Exception as e:
                print(f"WebSocket error: {e}")
                break
                
    finally:
        vnc.connected_clients.discard(websocket)
        print(f"WebSocket client for {vnc.key} disconnected.")
        if not vnc.connected_clients:
            print(f"Last client for {vnc.key} disconnected. Cleaning up session.")
            if vnc.broadcast_task:
                vnc.broadcast_task.cancel()
            vnc.disconnect()
            if vnc.key in vnc_sessions:
                del vnc_sessions[vnc.key]
                print(f"VNC session {vnc.key} closed.")


# --- REST API Endpoints ---
def handle_vnc_error():
    return Response(status_code=503, content=json.dumps({"error": "VNC connection failed"}), media_type="application/json")

@app.post("/vnc/{host}:{port}/api/move", tags=["VNC Actions"])
async def api_move(host: str, port: int, payload: MovePayload):
    vnc = get_or_create_vnc_session(host, port)
    if not vnc: return handle_vnc_error()
    vnc.move_mouse(payload.x, payload.y)
    return {"status": "success"}

@app.post("/vnc/{host}:{port}/api/click", tags=["VNC Actions"])
async def api_click(host: str, port: int, payload: ClickPayload):
    vnc = get_or_create_vnc_session(host, port)
    if not vnc: return handle_vnc_error()
    vnc.click(payload.x, payload.y, payload.button)
    return {"status": "success"}

@app.post("/vnc/{host}:{port}/api/type", tags=["VNC Actions"])
async def api_type(host: str, port: int, payload: TypePayload):
    vnc = get_or_create_vnc_session(host, port)
    if not vnc: return handle_vnc_error()
    vnc.type_text(payload.text, payload.delay)
    return {"status": "success"}

@app.get("/vnc/{host}:{port}/api/screenshot", tags=["VNC Information"])
async def api_screenshot(host: str, port: int):
    vnc = get_or_create_vnc_session(host, port)
    if not vnc: return handle_vnc_error()
    img = vnc.get_screenshot()
    if img:
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        return Response(content=buffer.getvalue(), media_type='image/png')
    return Response(status_code=503, content=json.dumps({"error": "Screenshot not available"}), media_type="application/json")

@app.post("/vnc/{host}:{port}/api/scroll", tags=["VNC Actions"])
async def api_scroll(host: str, port: int, payload: ScrollPayload):
    vnc = get_or_create_vnc_session(host, port)
    if not vnc: return handle_vnc_error()
    x = payload.x if payload.x is not None else vnc.width // 2
    y = payload.y if payload.y is not None else vnc.height // 2
    vnc.scroll(x, y, payload.direction)
    return {"status": "success"}

@app.post("/vnc/{host}:{port}/api/drag", tags=["VNC Actions"])
async def api_drag(host: str, port: int, payload: DragPayload):
    vnc = get_or_create_vnc_session(host, port)
    if not vnc: return handle_vnc_error()
    vnc.drag(payload.x_start, payload.y_start, payload.x_end, payload.y_end, payload.button, payload.delay)
    return {"status": "success"}

@app.post("/vnc/{host}:{port}/api/shortcut", tags=["VNC Actions"])
async def api_shortcut(host: str, port: int, payload: ShortcutPayload):
    vnc = get_or_create_vnc_session(host, port)
    if not vnc: return handle_vnc_error()
    vnc.key_combo(*payload.keys)
    return {"status": "success"}

# --- Main Execution ---
if __name__ == "__main__":
    import uvicorn
    
    banner = pyfiglet.figlet_format("Solid VNC")
    lines = banner.split('\n')
    mid_index = len(lines) // 2 - 1
    if mid_index < len(lines):
        lines[mid_index] += f"   Version {app.version}"
    
    print("\n".join(lines))
    print("API Documentation: http://localhost:8080")
    print("VNC Viewer:        http://localhost:8080/vnc/host:port")
    
    uvicorn.run(app, host="0.0.0.0", port=8080)