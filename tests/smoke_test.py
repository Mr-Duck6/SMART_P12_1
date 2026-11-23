
import json
import urllib.error
import urllib.request

BASE = "http://localhost:8081"

def request(path, method="GET", body=None, content_type="application/json", accept="application/json"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": content_type, "Accept": accept}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, res.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()

assert request("/api/health")[0] == 200
assert len(json.loads(request("/api/devices")[1])) == 3
assert request("/api/devices/thermo-01/resources/temperature")[0] == 200
assert request("/api/devices/thermo-01/resources/temperature", "PUT", {"value": 26.5})[0] == 200
assert json.loads(request("/api/devices/thermo-01/resources/temperature")[1])["data"]["value"] == 26.5
assert request("/api/devices/thermo-01/resources/humidity", "PUT", {"value": 150})[0] == 400
assert request("/api/devices/nope/resources/temperature")[0] == 404
assert request("/api/devices/thermo-01/resources/temperature", "POST",
               {"value": 1}, content_type="text/plain")[0] == 415
assert request("/api/devices/thermo-01/resources/temperature",
               accept="text/plain")[0] == 406
print("All gateway smoke tests passed.")
