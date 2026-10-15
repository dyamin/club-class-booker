"""A tiny stand-in for mirroir-mcp, used by the tests. It speaks the same line-based JSON-RPC."""
import base64
import io
import json
import sys

from PIL import Image

SCALE = 2 if "--retina" in sys.argv else 1          # the real tool returns native pixels
FLAKY = "--flaky" in sys.argv       # the first screenshot fails like the real tool right after the app was activated
DOWN = "--down" in sys.argv         # every screenshot fails
LOSTWINDOW = "--lostwindow" in sys.argv   # the first tap fails like the real tool when it lost the window for a moment
NOWINDOW = "--nowindow" in sys.argv  # the status finds no window after the first answer (the calendar editor is open)
shots = 0
statuses = 0
taps = 0
_buf = io.BytesIO()
Image.new("RGB", (1024 * SCALE, 800 * SCALE), (200, 210, 220)).save(_buf, format="PNG")
PNG = base64.b64encode(_buf.getvalue()).decode("ascii")


def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    method, msg_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        reply(msg_id, {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake"}})
    elif method == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"]["arguments"]
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {}}) + "\n")
        if name == "tap" and LOSTWINDOW and taps == 0:
            taps += 1
            reply(msg_id, {"isError": True, "content": [{"type": "text", "text": "Target 'club' window not found. Is the app running?"}]})
        elif name == "tap":
            taps += 1
            reply(msg_id, {"content": [{"type": "text", "text": f"Tapped at ({args['x']}, {args['y']})"}]})
        elif name == "swipe":
            reply(msg_id, {"content": [{"type": "text", "text": f"Swiped from ({args['from_x']},{args['from_y']}) to ({args['to_x']},{args['to_y']})"}]})
        elif name == "screenshot" and (DOWN or (FLAKY and shots == 0)):
            shots += 1
            reply(msg_id, {"isError": True, "content": [{"type": "text", "text": "Target 'club' is not running"}]})
        elif name == "screenshot":
            shots += 1
            reply(msg_id, {"content": [{"type": "image", "data": PNG, "mimeType": "image/png"}]})
        elif name == "status" and NOWINDOW and statuses > 0:
            statuses += 1
            reply(msg_id, {"content": [{"type": "text", "text": "No window — app is running but no mirroring window found"}]})
        elif name == "status":
            statuses += 1
            reply(msg_id, {"content": [{"type": "text", "text": "Connected — mirroring active (window: 1024x800, pos=(-1377,101), landscape)"}]})
        elif name == "broken":
            reply(msg_id, {"isError": True, "content": [{"type": "text", "text": "it broke"}]})
        elif name == "hang":
            pass   # never answers
        elif name == "die":
            sys.exit(3)
        else:
            reply(msg_id, error={"code": -32601, "message": "no such tool"})
