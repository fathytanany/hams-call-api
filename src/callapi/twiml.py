"""The one TwiML shape we run: <Response><Connect><Stream url><Parameter/>*</Stream></Connect>."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_TWIML = 4000  # Twilio's limit; also bounds parse cost
MAX_PARAMETER = 500  # Twilio: name + value under 500 chars


class TwimlError(ValueError):
    """The TwiML is malformed or uses something we don't support."""


@dataclass(frozen=True)
class Stream:
    url: str
    parameters: dict[str, str]


def _only_child(el: ET.Element, tag: str) -> ET.Element:
    children = list(el)
    if len(children) != 1 or children[0].tag != tag:
        found = ", ".join(f"<{c.tag}>" for c in children) or "nothing"
        raise TwimlError(f"<{el.tag}> must contain exactly one <{tag}>, found {found}")
    return children[0]


def parse(twiml: str) -> Stream:
    if len(twiml) > MAX_TWIML:
        raise TwimlError(f"Twiml must be at most {MAX_TWIML} characters")
    try:
        # Expat never fetches external entities and caps entity expansion.
        root = ET.fromstring(twiml)
    except ET.ParseError as e:
        raise TwimlError(f"Twiml is not valid XML: {e}") from e
    if root.tag != "Response":
        raise TwimlError(f"Twiml root must be <Response>, got <{root.tag}>")
    stream = _only_child(_only_child(root, "Connect"), "Stream")

    if unknown := set(stream.attrib) - {"url", "name"}:
        raise TwimlError(f"Unsupported <Stream> attribute(s): {', '.join(sorted(unknown))}")
    url = stream.get("url", "")
    parts = urlsplit(url)
    # ws:// is allowed so the local test app needs no TLS (ASSUMPTIONS 5).
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        raise TwimlError(f"<Stream> url must be an absolute ws:// or wss:// URL, got {url!r}")
    if parts.query:
        raise TwimlError("<Stream> url must not have a query string; use <Parameter>")

    parameters: dict[str, str] = {}
    for p in stream:
        if p.tag != "Parameter":
            raise TwimlError(f"<Stream> may only contain <Parameter>, found <{p.tag}>")
        name, value = p.get("name"), p.get("value")
        if not name or value is None:
            raise TwimlError("<Parameter> needs name and value attributes")
        if len(name) + len(value) >= MAX_PARAMETER:
            raise TwimlError(f"<Parameter> {name!r}: name + value must be under 500 chars")
        parameters[name] = value
    return Stream(url, parameters)
