import json
import re
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st
from defusedxml.ElementTree import fromstring  # safer XML parsing
import xml.etree.ElementTree as ET


# ----------------------------
# Helpers: XML pretty print
# ----------------------------
def _indent_xml(elem: ET.Element, level: int = 0) -> None:
    """In-place pretty indentation for ElementTree elements."""
    i = "\n" + level * "  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
        for child in elem:
            _indent_xml(child, level + 1)
        if not elem.tail or not elem.tail.strip():
            elem.tail = i
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = i


def pretty_xml(xml_bytes: bytes) -> str:
    root = ET.fromstring(xml_bytes)
    _indent_xml(root)
    return ET.tostring(root, encoding="unicode")


# ----------------------------
# Helpers: namespaces + safe find
# ----------------------------
def extract_namespaces(xml_bytes: bytes) -> Dict[str, str]:
    """
    Extract namespace mappings from the raw XML using a regex.
    Example: xmlns:cbc="urn:oasis:names:specification:..."
    """
    text = xml_bytes.decode("utf-8", errors="replace")
    ns = dict(re.findall(r'\sxmlns:([A-Za-z0-9_]+)="([^"]+)"', text))
    # also handle default namespace xmlns="..."
    m = re.search(r'\sxmlns="([^"]+)"', text)
    if m:
        ns["default"] = m.group(1)
    return ns


def text_or_none(elem: Optional[ET.Element]) -> Optional[str]:
    if elem is None:
        return None
    t = (elem.text or "").strip()
    return t if t else None


def find_first_text(root: ET.Element, paths: List[str], ns: Dict[str, str]) -> Optional[str]:
    for p in paths:
        e = root.find(p, ns)
        t = text_or_none(e)
        if t is not None:
            return t
    return None


def find_all_text(root: ET.Element, path: str, ns: Dict[str, str]) -> List[str]:
    out = []
    for e in root.findall(path, ns):
        t = text_or_none(e)
        if t is not None:
            out.append(t)
    return out


# ----------------------------
# Generic XML -> dict (fallback)
# ----------------------------
def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def xml_to_dict(elem: ET.Element) -> Dict[str, Any]:
    """
    Convert XML to a nested dict:
    - attributes under "@attrs"
    - repeated child tags become lists
    - leaf text under "#text" if attributes exist, else plain string
    """
    node: Dict[str, Any] = {}
    attrs = {k: v for k, v in elem.attrib.items()} if elem.attrib else {}
    children = list(elem)

    # Process children
    if children:
        grouped: Dict[str, List[Any]] = {}
        for c in children:
            k = strip_ns(c.tag)
            grouped.setdefault(k, []).append(xml_to_dict(c))
        for k, v in grouped.items():
            node[k] = v[0] if len(v) == 1 else v

    # Text
    text = (elem.text or "").strip()
    if text:
        if node or attrs:
            node["#text"] = text
        else:
            return {strip_ns(elem.tag): text}

    # Attributes
    if attrs:
        node["@attrs"] = attrs

    return {strip_ns(elem.tag): node if node else (text if text else {})}


# ----------------------------
# Simple "common invoice fields" mapper
# Works for many UBL/XRechnung variants; if not found, returns what it can.
# ----------------------------
def map_invoice_common(root: ET.Element, ns: Dict[str, str]) -> Dict[str, Any]:
    # UBL/XRechnung common namespaces are often cbc/cac
    # We'll use "best effort" paths.
    invoice_id = find_first_text(
        root,
        paths=[
            ".//cbc:ID",
            ".//ID",  # fallback if no ns prefixes
        ],
        ns=ns,
    )

    issue_date = find_first_text(
        root,
        paths=[
            ".//cbc:IssueDate",
            ".//IssueDate",
            ".//cbc:IssueDateTime",
        ],
        ns=ns,
    )

    due_date = find_first_text(
        root,
        paths=[
            ".//cbc:DueDate",
            ".//DueDate",
        ],
        ns=ns,
    )

    # Supplier / Customer names (common in UBL)
    supplier_name = find_first_text(
        root,
        paths=[
            ".//cac:AccountingSupplierParty//cac:Party//cac:PartyName//cbc:Name",
            ".//cac:SellerSupplierParty//cac:Party//cac:PartyName//cbc:Name",
            ".//SellerTradeParty//Name",  # some CII/XRechnung-ish structures
            ".//cac:AccountingSupplierParty//cbc:Name",
        ],
        ns=ns,
    )

    customer_name = find_first_text(
        root,
        paths=[
            ".//cac:AccountingCustomerParty//cac:Party//cac:PartyName//cbc:Name",
            ".//BuyerTradeParty//Name",
            ".//cac:AccountingCustomerParty//cbc:Name",
        ],
        ns=ns,
    )

    currency = find_first_text(
        root,
        paths=[
            ".//cbc:DocumentCurrencyCode",
            ".//DocumentCurrencyCode",
            ".//cbc:TaxCurrencyCode",
        ],
        ns=ns,
    )

    payable_amount = find_first_text(
        root,
        paths=[
            ".//cbc:PayableAmount",
            ".//PayableAmount",
            ".//cbc:TaxInclusiveAmount",
        ],
        ns=ns,
    )

    payable_amount_currency = None
    # Try to read the currency attribute for PayableAmount if present
    pay_elem = root.find(".//cbc:PayableAmount", ns) or root.find(".//PayableAmount", ns)
    if pay_elem is not None:
        payable_amount_currency = pay_elem.attrib.get("currencyID") or pay_elem.attrib.get("currencyId")

    # Invoice lines (best effort)
    line_ids = find_all_text(root, ".//cac:InvoiceLine//cbc:ID", ns) or find_all_text(root, ".//InvoiceLine//ID", ns)
    line_descs = find_all_text(root, ".//cac:InvoiceLine//cac:Item//cbc:Description", ns)
    line_qtys = find_all_text(root, ".//cac:InvoiceLine//cbc:InvoicedQuantity", ns)

    lines: List[Dict[str, Any]] = []
    max_len = max(len(line_ids), len(line_descs), len(line_qtys), 0)
    for i in range(max_len):
        lines.append(
            {
                "lineId": line_ids[i] if i < len(line_ids) else None,
                "description": line_descs[i] if i < len(line_descs) else None,
                "quantity": line_qtys[i] if i < len(line_qtys) else None,
            }
        )

    return {
        "documentType": "invoice",
        "invoiceNumber": invoice_id,
        "issueDate": issue_date,
        "dueDate": due_date,
        "currency": currency,
        "supplier": {"name": supplier_name},
        "customer": {"name": customer_name},
        "totals": {
            "payableAmount": payable_amount,
            "payableAmountCurrency": payable_amount_currency or currency,
        },
        "lines": lines,
        "meta": {
            "rootTag": strip_ns(root.tag),
            "namespacesDetected": list(ns.keys()),
            "mappingNote": "Best-effort common-field mapping; for full fidelity use xmlAsJsonFallback.",
        },
    }


# ----------------------------
# Streamlit UI
# ----------------------------
st.set_page_config(page_title="eInvoice XML → JSON Viewer", layout="wide")
st.title("eInvoice Viewer (XML + mapped JSON)")

uploaded = st.file_uploader(
    "Upload an eInvoice XML (XRechnung / UBL / ZUGFeRD / etc.)",
    type=["xml"],
)

if not uploaded:
    st.info("Upload an XML file to begin.")
    st.stop()

xml_bytes = uploaded.read()

# Parse safely
try:
    safe_root = fromstring(xml_bytes)  # defusedxml
    # Convert defusedxml Element -> ElementTree compatible by re-parsing with ET for pretty + xpath
    root = ET.fromstring(xml_bytes)
except Exception as e:
    st.error(f"Could not parse XML: {e}")
    st.stop()

ns = extract_namespaces(xml_bytes)

# ElementTree expects prefixes to be registered in the namespace dict you pass to find/findall.
# If there is a default namespace, XPath with prefixes won't match it. We'll just keep prefixes we found.
# Common UBL prefixes are cbc/cac; if missing, many queries will fall back to non-namespaced paths.
# No further action needed.

mapped_common = map_invoice_common(root, ns)

# Full fallback representation (so you always have something to copy)
xml_as_dict = xml_to_dict(root)
mapped_common["xmlAsJsonFallback"] = xml_as_dict

pretty_xml_text = pretty_xml(xml_bytes)
pretty_json_text = json.dumps(mapped_common, indent=2, ensure_ascii=False)

tab1, tab2 = st.tabs(["XML", "Mapped JSON"])

with tab1:
    st.subheader("XML (pretty-printed)")
    st.code(pretty_xml_text, language="xml")

    st.download_button(
        "Download XML",
        data=xml_bytes,
        file_name=uploaded.name if uploaded.name.endswith(".xml") else "einvoice.xml",
        mime="application/xml",
    )

with tab2:
    st.subheader("Mapped JSON (copy or download)")
    st.code(pretty_json_text, language="json")

    st.download_button(
        "Download JSON",
        data=pretty_json_text.encode("utf-8"),
        file_name="mapped.json",
        mime="application/json",
    )

with st.expander("Debug: namespaces detected"):
    st.json(ns)

st.caption("Note: Mapping is best-effort. The JSON always includes a full XML→JSON fallback under xmlAsJsonFallback.")
