import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from aiohttp import web
import aiocoap
import aiocoap.resource as resource
from aiocoap.numbers.codes import Code

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("coap-http-gateway")

DEVICES = {
    "thermo-01": {
        "name": "Temperature sensor 01",
        "temperature": {"value": 22.4, "unit": "°C"},
        "humidity": {"value": 46.0, "unit": "%"},
        "status": {"online": True, "battery": 94},
    },
    "thermo-02": {
        "name": "Temperature sensor 02",
        "temperature": {"value": 23.1, "unit": "°C"},
        "humidity": {"value": 43.5, "unit": "%"},
        "status": {"online": True, "battery": 88},
    },
    "weather-01": {
        "name": "Outdoor sensor",
        "temperature": {"value": 18.7, "unit": "°C"},
        "humidity": {"value": 61.2, "unit": "%"},
        "status": {"online": True, "battery": 76},
    },
}
RESOURCE_NAMES = ("temperature", "humidity", "status")
DEVICE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
RESOURCE_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
MAX_BODY = 64 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_json_payload(raw: bytes):
    if len(raw) > MAX_BODY:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY, actual_size=len(raw))
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Payload must be valid UTF-8 JSON")
    if not isinstance(value, dict):
        raise web.HTTPBadRequest(text="JSON payload must be an object")
    # Only allow JSON primitives/containers and reject non-finite numbers.
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Payload contains unsupported values")
    return value


class DeviceResource(resource.ObservableResource):
    """A simulated CoAP resource. aiocoap handles CoAP message framing and block-wise transfer."""

    def __init__(self, device_id, resource_name):
        super().__init__()
        self.device_id = device_id
        self.resource_name = resource_name

    def payload(self):
        value = DEVICES[self.device_id][self.resource_name]
        return json.dumps({
            "deviceId": self.device_id,
            "resource": self.resource_name,
            "data": value,
            "updatedAt": now(),
        }).encode("utf-8")

    async def render_get(self, request):
        return aiocoap.Message(code=Code.CONTENT, payload=self.payload(),
                               content_format=aiocoap.numbers.media_types_rev["application/json"])

    async def render_post(self, request):
        return await self._write(request, create_only=False)

    async def render_put(self, request):
        return await self._write(request, create_only=False)

    async def render_delete(self, request):
        if self.resource_name not in DEVICES.get(self.device_id, {}):
            return aiocoap.Message(code=Code.NOT_FOUND)
        del DEVICES[self.device_id][self.resource_name]
        self.updated_state()
        return aiocoap.Message(code=Code.DELETED)

    async def _write(self, request, create_only=False):
        try:
            incoming = json.loads(request.payload.decode("utf-8"))
            if not isinstance(incoming, dict):
                raise ValueError("JSON object required")
            if self.resource_name == "temperature":
                value = float(incoming.get("value"))
                if not -50 <= value <= 100:
                    raise ValueError("temperature must be between -50 and 100")
                clean = {"value": value, "unit": "°C"}
            elif self.resource_name == "humidity":
                value = float(incoming.get("value"))
                if not 0 <= value <= 100:
                    raise ValueError("humidity must be between 0 and 100")
                clean = {"value": value, "unit": "%"}
            elif self.resource_name == "status":
                clean = {
                    "online": bool(incoming.get("online", True)),
                    "battery": int(incoming.get("battery", 100)),
                }
                if not 0 <= clean["battery"] <= 100:
                    raise ValueError("battery must be between 0 and 100")
            else:
                raise ValueError("unknown resource")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            return aiocoap.Message(code=Code.BAD_REQUEST, payload=str(exc).encode())
        DEVICES[self.device_id][self.resource_name] = clean
        self.updated_state()
        return aiocoap.Message(code=Code.CHANGED, payload=self.payload(),
                               content_format=aiocoap.numbers.media_types_rev["application/json"])


class LargeResource(resource.ObservableResource):
    """Demonstration resource larger than one CoAP datagram; stack negotiates Block1/Block2."""
    def __init__(self):
        super().__init__()
        self.data = {"message": "Block-wise transfer demonstration", "records": [
            {"index": i, "text": "sample-data-" + str(i) + "-" + ("x" * 80)}
            for i in range(80)
        ]}

    async def render_get(self, request):
        return aiocoap.Message(code=Code.CONTENT,
            payload=json.dumps(self.data).encode(),
            content_format=aiocoap.numbers.media_types_rev["application/json"])


async def coap_call(context, method, uri, payload=None):
    request = aiocoap.Message(code=method, uri=uri, payload=payload or b"")
    if payload is not None:
        request.opt.content_format = aiocoap.numbers.media_types_rev["application/json"]
    delay = 0.25
    last_error = None
    for attempt in range(3):
        try:
            response = await asyncio.wait_for(context.request(request).response, timeout=3.0)
            return response
        except (asyncio.TimeoutError, OSError, aiocoap.error.Error) as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(delay)
                delay *= 2
    raise TimeoutError(f"CoAP request failed after 3 attempts: {last_error}")


def map_coap_code(code):
    # CoAP response class/detail -> conventional HTTP status.
    mapping = {
        Code.CONTENT: 200, Code.CREATED: 201, Code.DELETED: 200,
        Code.CHANGED: 200, Code.VALID: 304, Code.BAD_REQUEST: 400,
        Code.UNAUTHORIZED: 401, Code.FORBIDDEN: 403, Code.NOT_FOUND: 404,
        Code.METHOD_NOT_ALLOWED: 405, Code.NOT_ACCEPTABLE: 406,
        Code.REQUEST_ENTITY_INCOMPLETE: 400, Code.REQUEST_ENTITY_TOO_LARGE: 413,
        Code.UNSUPPORTED_CONTENT_FORMAT: 415, Code.INTERNAL_SERVER_ERROR: 500,
        Code.NOT_IMPLEMENTED: 501, Code.SERVICE_UNAVAILABLE: 503,
        Code.GATEWAY_TIMEOUT: 504,
    }
    return mapping.get(code, 502 if code.is_response() else 500)


def response_json(response, device_id, resource_name):
    raw = response.payload.decode("utf-8", errors="replace")
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {"raw": raw}
    return {
        "deviceId": device_id,
        "resource": resource_name,
        "coapCode": str(response.code),
        "data": body.get("data", body),
        "timestamp": now(),
    }


async def handle_devices(request):
    return web.json_response([
        {"id": device_id, "name": data["name"], "resources": list(data.keys() - {"name"})}
        for device_id, data in DEVICES.items()
    ])


async def handle_health(request):
    return web.json_response({"status": "ok", "service": "coap-http-gateway", "timestamp": now()})


async def handle_resource(request):
    device_id, resource_name = request.match_info["device_id"], request.match_info["resource"]
    if not DEVICE_ID_RE.fullmatch(device_id) or not RESOURCE_RE.fullmatch(resource_name):
        raise web.HTTPBadRequest(text="Invalid device or resource identifier")
    if device_id not in DEVICES:
        raise web.HTTPNotFound(text="Unknown device")
    if resource_name not in RESOURCE_NAMES or resource_name not in DEVICES[device_id]:
        raise web.HTTPNotFound(text="Unknown resource")

    content_type = request.headers.get("Content-Type", "application/json").split(";")[0].lower()
    accept = request.headers.get("Accept", "application/json")
    if request.method in ("POST", "PUT") and content_type not in ("application/json", "application/merge-patch+json"):
        raise web.HTTPUnsupportedMediaType(text="Use Content-Type: application/json")
    if "application/json" not in accept and "*/*" not in accept:
        raise web.HTTPNotAcceptable(text="Only application/json is available")

    method_map = {"GET": Code.GET, "POST": Code.POST, "PUT": Code.PUT, "DELETE": Code.DELETE}
    payload = None
    if request.method in ("POST", "PUT"):
        value = await request.read()
        parsed = safe_json_payload(value)
        payload = json.dumps(parsed, allow_nan=False).encode("utf-8")

    context = request.app["coap_client"]
    uri = f"coap://127.0.0.1:{os.getenv('COAP_PORT', '5683')}/devices/{device_id}/{resource_name}"
    try:
        response = await coap_call(context, method_map[request.method], uri, payload)
    except TimeoutError as exc:
        log.exception("CoAP timeout device=%s resource=%s", device_id, resource_name)
        return web.json_response({"error": "coap_timeout", "detail": str(exc)}, status=504)
    except Exception as exc:
        log.exception("CoAP transport error")
        return web.json_response({"error": "coap_transport_error", "detail": str(exc)}, status=502)

    status = map_coap_code(response.code)
    if status >= 400:
        log.warning("CoAP error device=%s resource=%s code=%s mapped_http=%s",
                    device_id, resource_name, response.code, status)
        return web.json_response({
            "error": "coap_error", "coapCode": str(response.code),
            "deviceId": device_id, "resource": resource_name,
        }, status=status)
    if request.method == "DELETE":
        return web.json_response({"deviceId": device_id, "resource": resource_name,
                                  "deleted": True, "coapCode": str(response.code)}, status=status)
    return web.json_response(response_json(response, device_id, resource_name), status=status)


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Accept"
    response.headers["Access-Control-Max-Age"] = "600"
    return response


async def start_services():
    site = resource.Site()
    for device_id in DEVICES:
        for resource_name in RESOURCE_NAMES:
            site.add_resource(["devices", device_id, resource_name],
                              DeviceResource(device_id, resource_name))
    site.add_resource(["large"], LargeResource())

    coap_port = int(os.getenv("COAP_PORT", "5683"))
    coap_server = await aiocoap.Context.create_server_context(
        site, bind=(os.getenv("COAP_HOST", "0.0.0.0"), coap_port)
    )
    coap_client = await aiocoap.Context.create_client_context()

    app = web.Application(middlewares=[cors_middleware], client_max_size=MAX_BODY)
    app["coap_client"] = coap_client
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/devices", handle_devices)
    app.router.add_route("*", "/api/devices/{device_id}/resources/{resource}", handle_resource)
    runner = web.AppRunner(app)
    await runner.setup()
    http_site = web.TCPSite(runner, os.getenv("HTTP_HOST", "0.0.0.0"),
                            int(os.getenv("HTTP_PORT", "8081")))
    await http_site.start()
    log.info("CoAP server listening on UDP %s; HTTP API listening on %s",
             coap_port, os.getenv("HTTP_PORT", "8081"))
    return coap_server, coap_client, runner


async def main():
    services = await start_services()
    try:
        await asyncio.Event().wait()
    finally:
        await services[2].cleanup()
        await services[1].shutdown()
        await services[0].shutdown()


if __name__ == "__main__":
    asyncio.run(main())
