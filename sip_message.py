from __future__ import annotations
import logging
import re

_HEADER_BODY_SEP = re.compile(r"\r?\n\r?\n")
_HEADER_LINE     = re.compile(r"^([\w-]+)\s*:\s*(.+)$")

logger = logging.getLogger(__name__)

MULTI_VALUE_HEADERS = frozenset(
    [
        "Via",
        "Route",
        "Record-Route",
        "Contact",
        "WWW-Authenticate",
        "Authorization",
    ]
)

RESPONSE_COPY_HEADERS = (
    "Via",
    "From",
    "To",
    "Call-ID",
    "CSeq",
    "Contact",
)

MANDATORY_REQUEST_HEADERS = ("Via", "From", "To", "Call-ID", "CSeq")

SINGLE_VALUE_MANDATORY = ("From", "To", "Call-ID", "CSeq")


class SIPParseError(ValueError):
    pass


class SIPMessage:
    def __init__(
        self, start_line: str, headers: dict[str, str | list[str]], body: str = ""
    ) -> None:
        self.start_line = start_line
        self.headers = headers
        self.body = body
        self._duplicate_headers: set[str] = set()

    @classmethod
    def read_raw(cls, raw: str) -> SIPMessage:
        if not raw or not raw.strip():
            raise SIPParseError("Empty SIP message")

        parts = _HEADER_BODY_SEP.split(raw, maxsplit=1)
        header_section = parts[0]
        body = parts[1] if len(parts) > 1 else ""

        lines = header_section.splitlines()
        if not lines or not lines[0].strip():
            raise SIPParseError("No start line found in SIP message")

        start_line = lines[0].strip()
        headers: dict[str, str | list[str]] = {}
        duplicate_headers: set[str] = set()

        for line in lines[1:]:
            m = _HEADER_LINE.match(line.strip())
            if not m:
                continue
            name, value = m.group(1), m.group(2).strip()
            if name in MULTI_VALUE_HEADERS:
                existing = headers.get(name)
                if existing is None:
                    headers[name] = [value]
                elif isinstance(existing, list):
                    existing.append(value)
                else:
                    headers[name] = [existing, value]
            else:
                if name in headers:
                    duplicate_headers.add(name)
                headers[name] = value

        msg = cls(start_line, headers, body)
        msg._duplicate_headers = duplicate_headers
        return msg

    def grab_header(self, name: str) -> str | None:

        val = self.headers.get(name)

        if val is None:
            return None

        if isinstance(val, list):
            return val[0]

        return val

    def put_header(self, name: str, value: str) -> None:
        self.headers[name] = value

    def tick_down_hops(self) -> None:

        raw = self.grab_header("Max-Forwards")

        if raw is None:
            return

        try:
            num = int(raw)
            num = num - 1
            self.put_header("Max-Forwards", str(num))

        except ValueError:
            logger.warning(
                "Non-integer Max-Forwards value: %r — leaving unchanged",
                raw,
            )

    def check_if_ok(self) -> str | None:
        for h in MANDATORY_REQUEST_HEADERS:
            if h not in self.headers:
                logger.warning("Validation failed: missing mandatory header %r", h)
                return "400 Bad Request"

        duplicates: set[str] = getattr(self, "_duplicate_headers", set())
        for h in SINGLE_VALUE_MANDATORY:
            if h in duplicates:
                logger.warning("Validation failed: duplicate single-value header %r", h)
                return "400 Bad Request"

        mf = self.grab_header("Max-Forwards")
        if mf is not None:
            try:
                if int(mf) == 0:
                    logger.warning("Validation failed: Max-Forwards reached 0")
                    return "483 Too Many Hops"
            except ValueError:
                logger.warning("Validation failed: non-integer Max-Forwards %r", mf)
                return "400 Bad Request"
        return None

    def make_reply(self, status: str) -> SIPMessage:

        resp_headers = {}

        for h in RESPONSE_COPY_HEADERS:
            val = self.headers.get(h)

            if val is not None:
                resp_headers[h] = val

        resp_headers["Content-Length"] = "0"
        resp_headers["Expires"] = "7200"

        return SIPMessage("SIP/2.0 " + status, resp_headers)

    def dump_bytes(self) -> bytes:
        text = self.dump_text()
        return text.encode("utf-8")

    def dump_text(self) -> str:
        lines = []

        lines.append(self.start_line)

        for name in self.headers:
            value = self.headers[name]

            if isinstance(value, list):
                for v in value:
                    line = name + ": " + v
                    lines.append(line)

            else:
                line = name + ": " + value
                lines.append(line)

        lines.append("")
        lines.append(self.body)

        result = "\r\n".join(lines)

        return result

    @property
    def is_request(self) -> bool:
        return not self.start_line.startswith("SIP/2.0")

    @property
    def is_response(self) -> bool:
        return not self.is_request

    def __repr__(self) -> str:
        return f"SIPMessage(start_line={self.start_line!r}, headers={self.headers!r})"
