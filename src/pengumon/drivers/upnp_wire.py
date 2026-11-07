"""UPnP Internet Gateway Device on the wire: what network discovery (engine/discovery/upnp.py) and the `upnp_igd` driver
(drivers/upnp_igd.py) both need, so that neither imports the other.

* the SSDP search text and the reading of an answer's headers,
* safe XML: the parser refuses any DOCTYPE or entity declaration (where "billion laughs" and external entities live)
  whatever the encoding, and nothing over XML_MAX bytes is read,
* the device description (its name and model, and the WAN services it lists) and SOAP requests and answers.

Nothing here touches the network.
"""

import re
from dataclasses import dataclass
from typing import Collection, Dict, List, Optional, Tuple
from xml.etree import ElementTree
from xml.parsers import expat

from pengumon.drivers.common import clean_text

SSDP_GROUP = ("239.255.255.250", 1900)
IGD_TYPES = ("urn:schemas-upnp-org:device:InternetGatewayDevice:1",
             "urn:schemas-upnp-org:device:InternetGatewayDevice:2")
DATAGRAM_MAX = 2048
XML_MAX = 64 * 1024

# The WAN services of a description that are kept (by type, without the version).
IP_CONNECTION = "urn:schemas-upnp-org:service:WANIPConnection:"
PPP_CONNECTION = "urn:schemas-upnp-org:service:WANPPPConnection:"
COMMON_CONFIG = "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:"
WAN_SERVICES = (IP_CONNECTION, PPP_CONNECTION, COMMON_CONFIG)

# The output arguments that are read from a SOAP answer; any other is dropped.
ANSWER_FIELDS = ("NewExternalIPAddress", "NewConnectionStatus", "NewUptime", "NewWANAccessType",
                 "NewLayer1UpstreamMaxBitRate", "NewLayer1DownstreamMaxBitRate", "NewPhysicalLinkStatus")


# --- SSDP -------------------------------------------------------------------------------------------------------

def msearch(st: str, mx: int = 2) -> bytes:
    return ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
            f"MX: {mx}\r\nST: {st}\r\n\r\n").encode("ascii")


def ssdp_location(datagram: bytes) -> Optional[str]:
    """The LOCATION of a search answer ("HTTP/1.1 200 OK") for an IGD, or None when the datagram is not one, or is
    too long. The location is not checked here: who may be asked for it is the caller's rule."""
    if not isinstance(datagram, (bytes, bytearray)) or len(datagram) > DATAGRAM_MAX:
        return None
    text = bytes(datagram).decode("latin-1")
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    if not re.match(r"^HTTP/1\.[01] 200\b", lines[0] if lines else ""):
        return None
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    if headers.get("st") not in IGD_TYPES:
        return None
    return headers.get("location", "")


# --- XML ----------------------------------------------------------------------------------------------------------

class _Refused(Exception):
    """The document declares a DTD or an entity."""


def _refuse(*_args) -> None:
    raise _Refused


def safe_xml(body: bytes) -> Optional[ElementTree.Element]:
    """The parsed document, or None for one that is too big, declares a DOCTYPE or an entity or is not well-formed.
    A first pass with an expat parser whose DOCTYPE and entity handlers raise decides on what the parser itself reads,
    whatever the encoding (a byte search for "<!DOCTYPE" misses UTF-16); only a document without a DTD is then built."""
    if not isinstance(body, (bytes, bytearray)) or len(body) > XML_MAX:
        return None
    data = bytes(body)
    guard = expat.ParserCreate()
    guard.StartDoctypeDeclHandler = _refuse
    guard.EntityDeclHandler = _refuse
    guard.UnparsedEntityDeclHandler = _refuse
    guard.ExternalEntityRefHandler = _refuse
    guard.SkippedEntityHandler = _refuse
    try:
        guard.Parse(data, True)
        return ElementTree.fromstring(data)
    except (_Refused, expat.ExpatError, ElementTree.ParseError, ValueError):
        return None


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def child_text(element: ElementTree.Element, name: str) -> Optional[str]:
    for child in element:
        if local_name(child.tag) == name:
            return clean_text(child.text or "", limit=100)
    return None


@dataclass
class Description:
    device_type: Optional[str]
    friendly_name: Optional[str]
    manufacturer: Optional[str]
    model: Optional[str]
    services: List[Tuple[str, str]]  # (service type, control URL as written)
    url_base: Optional[str] = None


def parse_description(body: bytes) -> Optional[Description]:
    """The root device and every WAN service listed anywhere in its device tree, or None when the document is not
    an IGD description (or is unsafe, see safe_xml)."""
    root = safe_xml(body)
    if root is None or local_name(root.tag) != "root":
        return None
    device = next((c for c in root if local_name(c.tag) == "device"), None)
    if device is None:
        return None
    device_type = child_text(device, "deviceType")
    if device_type not in IGD_TYPES:
        return None
    services = []
    for element in device.iter():
        if local_name(element.tag) != "service":
            continue
        st, control = child_text(element, "serviceType"), child_text(element, "controlURL")
        if st and control and any(st.startswith(prefix) for prefix in WAN_SERVICES):
            services.append((st, control))
    model = " ".join(p for p in (child_text(device, "modelName"), child_text(device, "modelNumber")) if p)
    return Description(device_type=device_type, friendly_name=child_text(device, "friendlyName"),
                       manufacturer=child_text(device, "manufacturer"), model=clean_text(model) if model else None,
                       services=services[:8], url_base=child_text(root, "URLBase"))


def soap_body(service_type: str, action: str) -> bytes:
    return ('<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{service_type}"/></s:Body></s:Envelope>').encode("utf-8")


def parse_soap_answer(body: bytes, action: str, fields: Collection[str] = ANSWER_FIELDS) -> Dict[str, str]:
    """The output arguments of an action's response that are in `fields` ({} for a fault, an unsafe or a malformed
    answer)."""
    root = safe_xml(body)
    if root is None:
        return {}
    response = next((e for e in root.iter() if local_name(e.tag) == f"{action}Response"), None)
    if response is None:
        return {}
    out = {}
    for child in response:
        name = local_name(child.tag)
        if name in fields:
            value = clean_text(child.text or "", limit=64)
            if value:
                out[name] = value
    return out
