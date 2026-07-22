import logging, time, json as jsonlib, re, requests, gzip, zlib
from urllib.parse import urlparse
from tenacity import retry, stop_after_attempt, wait_fixed
from requests.exceptions import RequestException, JSONDecodeError, ConnectionError
from lxml import html

logging.basicConfig(format='%(asctime)s severity=%(levelname)s filename=%(filename)s line=%(lineno)s message="%(message)s"', level=logging.INFO)


SENSITIVE_RE = re.compile(r"(?i)(apikey=)[^&\s]+|(?:bearer\s+)[A-Za-z0-9._~+\-/=]+|([A-Za-z0-9_-]{20,})")

def sanitize_text(value):
    if value is None:
        return value
    return SENSITIVE_RE.sub(lambda m: (m.group(1) + "<redacted>") if m.group(1) else "<redacted>", str(value))

DEFAULT_HEADERS = {
    'accept':'*/*',
    # Avoid advertising brotli unless we're sure the runtime can decode it.
    'accept-encoding':'gzip, deflate',
    'accept-language':'en-GB,en;q=0.9,en-US;q=0.8,hi;q=0.7,la;q=0.6',
    'cache-control':'no-cache',
    'dnt':'1',
    'pragma':'no-cache',
    'referer':'https',
    'sec-fetch-mode':'no-cors',
    'sec-fetch-site':'cross-site',
    'user-agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/78.0.3904.108 Safari/537.36',
}

class Connect:
    def __init__(self):
        self.session = requests.Session()
        self.trace_mode = False

    def _redact_url(self, url: str) -> str:
        try:
            p = urlparse(url)
            return f"{p.scheme}://{p.netloc}{p.path}"
        except Exception:
            return "<unparseable_url>"

    def _decode_response_bytes(self, response):
        """
        Best-effort decode for cases where the server returns compressed bytes
        but headers are missing/incorrect, so requests doesn't auto-decompress.
        """
        raw = response.content or b""
        if not raw:
            return ""

        enc = (response.headers.get("content-encoding") or "").lower().strip()
        try_order = []
        if enc:
            try_order.append(enc)

        # gzip magic bytes: 1f 8b 08
        if len(raw) >= 3 and raw[0:3] == b"\x1f\x8b\x08":
            try_order.append("gzip")

        try_order.extend(["deflate", "br"])

        data = raw
        for e in try_order:
            try:
                if e == "gzip":
                    data = gzip.decompress(raw)
                    break
                if e == "deflate":
                    try:
                        data = zlib.decompress(raw)
                    except Exception:
                        data = zlib.decompress(raw, -zlib.MAX_WBITS)
                    break
                if e == "br":
                    try:
                        import brotli  # type: ignore
                    except Exception:
                        continue
                    data = brotli.decompress(raw)
                    break
            except Exception:
                continue

        try:
            return data.decode("utf-8")
        except Exception:
            return data.decode("utf-8", errors="replace")

    def get_html(self, url, headers=DEFAULT_HEADERS, params=None, cookies=None):
        return html.fromstring(self.get(url, headers=headers, params=params, cookies=cookies).content)

    def get_json(self, url, json=None, headers=None, params=None, cookies=None):
        try:
            response = self.get(url, json=json, headers=headers, params=params, cookies=cookies)
            text = response.text or ""
            if not text.strip():
                return {
                    "error": "empty_response",
                    "status_code": response.status_code,
                    "url": self._redact_url(url),
                }

            try:
                return response.json()
            except (JSONDecodeError, ValueError):
                return {
                    "error": "invalid_json_response",
                    "status_code": response.status_code,
                    "content_type": response.headers.get("content-type"),
                    "url": self._redact_url(url),
                    "raw_response": sanitize_text(text[:500]),
                }
        except ConnectionError as e:
            return {"error": "connection_failed", "exception": sanitize_text(e), "url": self._redact_url(url)}
        except RequestException as e:
            return {"error": "request_failed", "exception": sanitize_text(e), "url": self._redact_url(url)}

    @retry(stop=stop_after_attempt(6), wait=wait_fixed(10))
    def get(self, url, json=None, headers=None, params=None, cookies=None):
        if headers is None:
            headers = DEFAULT_HEADERS
        return self.session.get(url, json=json, headers=headers, params=params, cookies=cookies)

    def get_image_encoded(self, url):
        return base64.b64encode(self.get(url).content).decode('utf-8')

    def post_html(self, url, data=None, json=None, headers=None, cookies=None):
        return html.fromstring(self.post(url, data=data, json=json, headers=headers, cookies=cookies).content)

    def _http_error_response(self, response, decoded_body=None, default_error="HTTP request failed"):
        error = default_error
        if isinstance(decoded_body, dict):
            error = decoded_body.get("error") or decoded_body.get("errorMessage") or default_error
        return {
            "error": error,
            "status_code": response.status_code,
            "decoded_response": sanitize_text(decoded_body) if decoded_body is not None else "",
        }

    def post_json(self, url, data=None, json=None, headers=None, params=None, cookies=None):
        try:
            response = self.post(url, data=data, json=json, headers=headers, params=params, cookies=cookies)
            success = 200 <= response.status_code < 300
            if not response.text.strip():
                if success:
                    return {"status": "ok", "status_code": response.status_code}
                return {"error": "Empty response from server", "status_code": response.status_code}
            try:
                data = response.json()
            except (JSONDecodeError, ValueError):
                decoded = self._decode_response_bytes(response)
                if decoded.strip():
                    try:
                        data = jsonlib.loads(decoded)
                    except Exception:
                        data = {
                            "error": "Invalid POST response",
                            "status_code": response.status_code,
                            "raw_response": sanitize_text((decoded or response.text)[:500])
                        }
                else:
                    data = {
                        "error": "Invalid POST response",
                        "status_code": response.status_code,
                        "raw_response": sanitize_text((decoded or response.text)[:500])
                    }
            if not success:
                return self._http_error_response(response, data)
            return data
        except ConnectionError as e:
            return {"error": "Connection failed", "exception": sanitize_text(e)}
        except RequestException as e:
            return {"error": "Request failed", "exception": sanitize_text(e)}

    @retry(stop=stop_after_attempt(6), wait=wait_fixed(10))
    def post(self, url, data=None, json=None, headers=None, params=None, cookies=None):
        if headers is None:
            headers = DEFAULT_HEADERS
        return self.session.post(url, data=data, json=json, params=params, headers=headers, cookies=cookies)

    def put_json(self, url, data=None, json=None, headers=None, params=None, cookies=None):
        try:
            response = self.put(url, data=data, json=json, headers=headers, params=params, cookies=cookies)
            success = 200 <= response.status_code < 300
            if not response.text.strip():
                if success:
                    return {"status": "ok", "status_code": response.status_code}
                return {"error": "Empty response from server", "status_code": response.status_code}
            try:
                data = response.json()
            except (JSONDecodeError, ValueError):
                data = {"error": "Invalid PUT response", "status_code": response.status_code, "raw_response": sanitize_text(response.text[:500])}
            if not success:
                return self._http_error_response(response, data)
            return data
        except ConnectionError as e:
            return {"error": "Connection failed", "exception": sanitize_text(e)}
        except RequestException as e:
            return {"error": "Request failed", "exception": sanitize_text(e)}

    @retry(stop=stop_after_attempt(6), wait=wait_fixed(10))
    def put(self, url, data=None, json=None, headers=None, params=None, cookies=None):
        if headers is None:
            headers = DEFAULT_HEADERS
        return self.session.put(url, data=data, json=json, params=params, headers=headers, cookies=cookies)


    def delete_json(self, url, data=None, json=None, headers=None, params=None, cookies=None):
        try:
            response = self.delete(url, data=data, json=json, headers=headers, params=params, cookies=cookies)
            success = 200 <= response.status_code < 300
            if not (response.text or '').strip():
                if success:
                    return {"status": "ok", "status_code": response.status_code}
                return {"error": "Empty response from server", "status_code": response.status_code}
            try:
                data = response.json()
            except (JSONDecodeError, ValueError):
                decoded = self._decode_response_bytes(response)
                data = {"error": "Invalid DELETE response", "status_code": response.status_code, "raw_response": sanitize_text((decoded or response.text)[:500])}
            if not success:
                return self._http_error_response(response, data, default_error="HTTP DELETE request failed")
            if isinstance(data, dict):
                data.setdefault("status_code", response.status_code)
                return data
            return {"status": "ok", "status_code": response.status_code, "json": data}
        except ConnectionError as e:
            return {"error": "Connection failed", "exception": sanitize_text(e)}
        except RequestException as e:
            return {"error": "Request failed", "exception": sanitize_text(e)}

    def delete(self, url, data=None, json=None, headers=None, params=None, cookies=None):
        if headers is None:
            headers = DEFAULT_HEADERS
        return self.session.delete(url, data=data, json=json, params=params, headers=headers, cookies=cookies)
