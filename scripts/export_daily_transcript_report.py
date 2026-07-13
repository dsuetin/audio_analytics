from __future__ import annotations

import argparse
import asyncio
import html
import os
import re
import socket
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
import textwrap
from zoneinfo import ZoneInfo


REPORT_COLUMNS = [
    "session_id",
    "store_id",
    "client_id",
    "seller_id",
    "recognition_text",
    "dialog_type",
    "is_sale",
    "is_alarm_triggered",
    "created_at",
]


@dataclass(frozen=True)
class ReportRow:
    session_id: str
    store_id: str | None
    client_id: str | None
    seller_id: str | None
    recognition_text: str | None
    dialog_type: str | None
    is_sale: bool
    is_alarm_triggered: bool
    created_at: datetime

    @classmethod
    def from_record(cls, record: dict) -> "ReportRow":
        return cls(
            session_id=str(record.get("session_id") or ""),
            store_id=record.get("store_id"),
            client_id=record.get("client_id"),
            seller_id=record.get("seller_id"),
            recognition_text=record.get("recognition_text"),
            dialog_type=record.get("dialog_type"),
            is_sale=bool(record.get("is_sale", False)),
            is_alarm_triggered=bool(record.get("is_alarm_triggered", False)),
            created_at=record["created_at"],
        )


@dataclass
class SheetSpec:
    name: str
    headers: list[str]
    rows: list[list[object]]
    wrap_columns: set[int] | None = None
    datetime_columns: set[int] | None = None
    freeze_panes: str = "A2"
    auto_filter: bool = True


def excel_column_name(index: int) -> str:
    result = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def escape_xml(text: str) -> str:
    return html.escape(text, quote=True)


def to_excel_serial(value: datetime) -> float:
    base = datetime(1899, 12, 30)
    naive_value = value.replace(tzinfo=None)
    return (naive_value - base).total_seconds() / 86400.0


def normalize_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def safe_sheet_name(name: str) -> str:
    invalid = '[]:*?/\\'
    cleaned = "".join("_" if char in invalid else char for char in name).strip()
    if not cleaned:
        cleaned = "Sheet"
    return cleaned[:31]


def cell_xml(row_index: int, column_index: int, value: object, style_id: int = 0) -> str:
    ref = f"{excel_column_name(column_index)}{row_index}"

    if value is None or value == "":
        return ""

    if isinstance(value, bool):
        return f'<c r="{ref}" t="inlineStr" s="{style_id}"><is><t>{escape_xml(normalize_value(value))}</t></is></c>'

    if isinstance(value, datetime):
        serial = to_excel_serial(value)
        return f'<c r="{ref}" s="{style_id}"><v>{serial:.8f}</v></c>'

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" s="{style_id}"><v>{value}</v></c>'

    text = escape_xml(str(value))
    return (
        f'<c r="{ref}" t="inlineStr" s="{style_id}">'
        f'<is><t xml:space="preserve">{text}</t></is></c>'
    )


def build_sheet_xml(sheet: SheetSpec) -> str:
    max_column = len(sheet.headers)
    max_row = len(sheet.rows) + 1
    last_ref = f"{excel_column_name(max_column)}{max_row}"

    row_xml = []

    header_cells = []
    for column_index, header in enumerate(sheet.headers, start=1):
        header_cells.append(cell_xml(1, column_index, header, style_id=1))
    row_xml.append(f'<row r="1" spans="1:{max_column}">{"".join(header_cells)}</row>')

    wrap_columns = sheet.wrap_columns or set()
    datetime_columns = sheet.datetime_columns or set()

    for row_index, row in enumerate(sheet.rows, start=2):
        cells = []
        for column_index, value in enumerate(row, start=1):
            style_id = 0
            if column_index in datetime_columns:
                style_id = 2
            elif column_index in wrap_columns:
                style_id = 3
            cell = cell_xml(row_index, column_index, value, style_id=style_id)
            if cell:
                cells.append(cell)
        row_xml.append(
            f'<row r="{row_index}" spans="1:{max_column}">{"".join(cells)}</row>'
        )

    cols = []
    for column_index in range(1, max_column + 1):
        if column_index in wrap_columns:
            width = 60
        else:
            width = 18
        if column_index in datetime_columns:
            width = 21
        cols.append(
            f'<col min="{column_index}" max="{column_index}" width="{width}" customWidth="1"/>'
        )

    freeze_xml = ""
    if sheet.freeze_panes:
        match = re.fullmatch(r"([A-Z]+)(\d+)", sheet.freeze_panes)
        split_row = 1
        top_left_cell = sheet.freeze_panes
        if match:
            split_row = max(int(match.group(2)) - 1, 0)
            top_left_cell = sheet.freeze_panes
        freeze_xml = (
            "<sheetViews><sheetView workbookViewId=\"0\">"
            f"<pane ySplit=\"{split_row}\" topLeftCell=\"{top_left_cell}\" activePane=\"bottomLeft\" state=\"frozen\"/>"
            "</sheetView></sheetViews>"
        )

    auto_filter_xml = ""
    if sheet.auto_filter:
        auto_filter_xml = f'<autoFilter ref="A1:{last_ref}"/>'

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"{freeze_xml}"
        f"<dimension ref=\"A1:{last_ref}\"/>"
        f"<sheetFormatPr defaultRowHeight=\"18\"/>"
        f"<cols>{''.join(cols)}</cols>"
        f"<sheetData>{''.join(row_xml)}</sheetData>"
        f"{auto_filter_xml}"
        "</worksheet>"
    )


def workbook_styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1">
    <numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/>
  </numFmts>
  <fonts count="3">
    <font>
      <sz val="11"/>
      <color theme="1"/>
      <name val="Calibri"/>
      <family val="2"/>
    </font>
    <font>
      <b/>
      <sz val="11"/>
      <color theme="1"/>
      <name val="Calibri"/>
      <family val="2"/>
    </font>
    <font>
      <sz val="11"/>
      <color theme="1"/>
      <name val="Calibri"/>
      <family val="2"/>
    </font>
  </fonts>
  <fills count="3">
    <fill>
      <patternFill patternType="none"/>
    </fill>
    <fill>
      <patternFill patternType="gray125"/>
    </fill>
    <fill>
      <patternFill patternType="solid">
        <fgColor rgb="FFD9EAF7"/>
        <bgColor indexed="64"/>
      </patternFill>
    </fill>
  </fills>
  <borders count="2">
    <border>
      <left/>
      <right/>
      <top/>
      <bottom/>
      <diagonal/>
    </border>
    <border>
      <left style="thin"><color rgb="FFD0D7DE"/></left>
      <right style="thin"><color rgb="FFD0D7DE"/></right>
      <top style="thin"><color rgb="FFD0D7DE"/></top>
      <bottom style="thin"><color rgb="FFD0D7DE"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
  </cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyBorder="1"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyAlignment="1" applyBorder="1">
      <alignment vertical="center" horizontal="center" wrapText="1"/>
    </xf>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1">
      <alignment vertical="center"/>
    </xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1" applyBorder="1">
      <alignment vertical="top" wrapText="1"/>
    </xf>
  </cellXfs>
  <cellStyles count="1">
    <cellStyle name="Normal" xfId="0" builtinId="0"/>
  </cellStyles>
</styleSheet>
"""


def workbook_xml(sheet_names: list[str]) -> str:
    sheets_xml = []
    for index, name in enumerate(sheet_names, start=1):
        sheets_xml.append(
            f'<sheet name="{escape_xml(name)}" sheetId="{index}" r:id="rId{index}"/>'
        )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{''.join(sheets_xml)}</sheets>"
        "</workbook>"
    )


def workbook_rels_xml(sheet_count: int) -> str:
    rels = []
    for index in range(1, sheet_count + 1):
        rels.append(
            f'<Relationship Id="rId{index}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )

    rels.append(
        f'<Relationship Id="rId{sheet_count + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{''.join(rels)}"
        "</Relationships>"
    )


def root_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="xl/workbook.xml"/>
</Relationships>
"""


def content_types_xml(sheet_count: int) -> str:
    overrides = []
    for index in range(1, sheet_count + 1):
        overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{''.join(overrides)}"
        "</Types>"
    )


def sort_rows(rows: list[ReportRow], key_order: list[str]) -> list[ReportRow]:
    def sort_key(row: ReportRow):
        values = []
        for field in key_order:
            value = getattr(row, field)
            if isinstance(value, datetime):
                values.append(value)
            elif value is None:
                values.append("")
            else:
                values.append(str(value))
        return tuple(values)

    return sorted(rows, key=sort_key)


def summary_rows(rows: list[ReportRow], group_field: str, extra_fields: list[str]) -> list[list[object]]:
    groups: dict[str, dict[str, object]] = {}

    for row in rows:
        key_value = getattr(row, group_field) or "UNKNOWN"
        key = str(key_value)
        group = groups.setdefault(
            key,
            {
                "count": 0,
                "sales": 0,
                "alarms": 0,
                "dialog_types": set(),
                "extras": {field: set() for field in extra_fields},
                "first_created_at": row.created_at,
                "last_created_at": row.created_at,
            },
        )

        group["count"] += 1
        group["sales"] += int(row.is_sale)
        group["alarms"] += int(row.is_alarm_triggered)

        dialog_type = row.dialog_type or "UNKNOWN"
        group["dialog_types"].add(dialog_type)

        for field in extra_fields:
            value = getattr(row, field) or "UNKNOWN"
            group["extras"][field].add(str(value))

        if row.created_at < group["first_created_at"]:
            group["first_created_at"] = row.created_at
        if row.created_at > group["last_created_at"]:
            group["last_created_at"] = row.created_at

    result = []
    for key in sorted(groups):
        group = groups[key]
        row = [
            key,
            group["count"],
            group["sales"],
            group["alarms"],
            len(group["dialog_types"]),
            ", ".join(sorted(group["dialog_types"])),
        ]
        for field in extra_fields:
            row.append(", ".join(sorted(group["extras"].get(field, set()))))
        row.extend([group["first_created_at"], group["last_created_at"]])
        result.append(row)

    return result


def build_sheets(rows: list[ReportRow], report_date: date, generated_at: datetime, tz_name: str) -> list[SheetSpec]:
    record_rows = [
        [
            row.session_id,
            row.store_id or "",
            row.client_id or "",
            row.seller_id or "",
            row.recognition_text or "",
            row.dialog_type or "UNKNOWN",
            row.is_sale,
            row.is_alarm_triggered,
            row.created_at.astimezone(ZoneInfo(tz_name)),
        ]
        for row in rows
    ]

    client_rows = [
        [
            row.session_id,
            row.client_id or "",
            row.store_id or "",
            row.seller_id or "",
            row.recognition_text or "",
            row.dialog_type or "UNKNOWN",
            row.is_sale,
            row.is_alarm_triggered,
            row.created_at.astimezone(ZoneInfo(tz_name)),
        ]
        for row in sort_rows(rows, ["client_id", "store_id", "seller_id", "created_at"])
    ]

    store_summary = summary_rows(rows, "store_id", ["client_id", "seller_id"])
    seller_summary = summary_rows(rows, "seller_id", ["store_id", "client_id"])
    client_summary = summary_rows(rows, "client_id", ["store_id", "seller_id"])

    store_headers = [
        "store_id",
        "sessions_count",
        "sales_count",
        "alarm_count",
        "dialog_types_count",
        "dialog_types",
        "client_ids",
        "seller_ids",
        "first_created_at",
        "last_created_at",
    ]
    seller_headers = [
        "seller_id",
        "sessions_count",
        "sales_count",
        "alarm_count",
        "dialog_types_count",
        "dialog_types",
        "store_ids",
        "client_ids",
        "first_created_at",
        "last_created_at",
    ]
    client_headers = [
        "client_id",
        "sessions_count",
        "sales_count",
        "alarm_count",
        "dialog_types_count",
        "dialog_types",
        "store_ids",
        "seller_ids",
        "first_created_at",
        "last_created_at",
    ]

    overview_rows = [
        ["Report date", report_date.isoformat()],
        ["Generated at", generated_at.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S %Z")],
        ["Timezone", tz_name],
        ["Total records", len(rows)],
        ["Total sales", sum(int(row.is_sale) for row in rows)],
        ["Total alarms", sum(int(row.is_alarm_triggered) for row in rows)],
        ["Unique stores", len({row.store_id or "UNKNOWN" for row in rows})],
        ["Unique sellers", len({row.seller_id or "UNKNOWN" for row in rows})],
        ["Unique clients", len({row.client_id or "UNKNOWN" for row in rows})],
        ["Notes", "Use the Records sheet for detailed sessions and the summary sheets for store/seller/client rollups."],
    ]

    return [
        SheetSpec(
            name="Overview",
            headers=["Metric", "Value"],
            rows=overview_rows,
            wrap_columns={2},
            auto_filter=False,
        ),
        SheetSpec(
            name="Records",
            headers=REPORT_COLUMNS,
            rows=record_rows,
            wrap_columns={5},
            datetime_columns={9},
        ),
        SheetSpec(
            name="Clients",
            headers=REPORT_COLUMNS,
            rows=client_rows,
            wrap_columns={5},
            datetime_columns={9},
        ),
        SheetSpec(
            name="Store summary",
            headers=store_headers,
            rows=store_summary,
            wrap_columns={6, 7, 8},
            datetime_columns={9, 10},
        ),
        SheetSpec(
            name="Seller summary",
            headers=seller_headers,
            rows=seller_summary,
            wrap_columns={6, 7, 8},
            datetime_columns={9, 10},
        ),
        SheetSpec(
            name="Client summary",
            headers=client_headers,
            rows=client_summary,
            wrap_columns={6, 7, 8},
            datetime_columns={9, 10},
        ),
    ]


def write_xlsx(path: Path, sheets: list[SheetSpec]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    sheet_names = [safe_sheet_name(sheet.name) for sheet in sheets]
    sheet_xmls = [build_sheet_xml(sheet) for sheet in sheets]

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml(len(sheets)))
        archive.writestr("_rels/.rels", root_rels_xml())
        archive.writestr("xl/workbook.xml", workbook_xml(sheet_names))
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml(len(sheets)))
        archive.writestr("xl/styles.xml", workbook_styles_xml())
        for index, sheet_xml in enumerate(sheet_xmls, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml)


async def fetch_rows(
    dsn: str | None,
    start_at: datetime,
    end_at: datetime,
) -> list[ReportRow]:
    import asyncpg

    if dsn:
        pool = await asyncpg.create_pool(dsn)
        try:
            async with pool.acquire() as conn:
                records = await conn.fetch(
                    """
                    SELECT
                        session_id,
                        store_id,
                        client_id,
                        seller_id,
                        recognition_text,
                        dialog_type,
                        is_sale,
                        is_alarm_triggered,
                        created_at
                    FROM transcripts
                    WHERE created_at >= $1
                      AND created_at < $2
                    ORDER BY created_at, store_id, seller_id, client_id, session_id
                    """,
                    start_at,
                    end_at,
                )
        finally:
            await pool.close()
    else:
        records = []
        conn = await asyncpg.connect(
            host=env_value("POSTGRES_HOST", "localhost"),
            port=int(env_value("POSTGRES_PORT", "5432")),
            user=env_value("POSTGRES_USER", "speech"),
            password=env_value("POSTGRES_PASSWORD", "speech"),
            database=env_value("POSTGRES_DB", "speech_db"),
        )
        try:
            records = await conn.fetch(
                """
                SELECT
                    session_id,
                    store_id,
                    client_id,
                    seller_id,
                    recognition_text,
                    dialog_type,
                    is_sale,
                    is_alarm_triggered,
                    created_at
                FROM transcripts
                WHERE created_at >= $1
                  AND created_at < $2
                ORDER BY created_at, store_id, seller_id, client_id, session_id
                """,
                start_at,
                end_at,
            )
        finally:
            await conn.close()

    return [ReportRow.from_record(dict(record)) for record in records]


def resolve_target_date(date_value: str | None, tz_name: str) -> date:
    zone = ZoneInfo(tz_name)
    if date_value:
        return datetime.strptime(date_value, "%Y-%m-%d").date()
    return datetime.now(zone).date() - timedelta(days=1)


def build_output_path(output: str | None, report_date: date) -> Path:
    if output:
        return Path(output)
    return Path(f"transcript_report_{report_date.isoformat()}.xlsx")


def build_pdf_output_path(output: str | None, report_date: date) -> Path:
    if output:
        return Path(output).with_suffix(".pdf")
    return Path(f"transcript_report_{report_date.isoformat()}.pdf")


def env_value(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def pdf_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def wrap_pdf_text(text: str, width: int) -> list[str]:
    if not text:
        return [""]
    return textwrap.wrap(
        text,
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [text]


def summary_lines(title: str, headers: list[str], rows: list[list[object]], limit: int = 12) -> list[str]:
    lines = [f"## {title}", ""]
    lines.append(" | ".join(headers))
    lines.append("-" * min(110, max(len(" | ".join(headers)), 20)))
    for row in rows[:limit]:
        joined_row = " | ".join(normalize_value(value) for value in row)
        lines.extend(wrap_pdf_text(joined_row, 110))
    if len(rows) > limit:
        lines.append(f"... and {len(rows) - limit} more rows")
    lines.append("")
    return lines


def detailed_record_lines(rows: list[ReportRow]) -> list[str]:
    lines = ["## Detailed records", ""]
    for row in rows:
        header = (
            f"{row.created_at:%Y-%m-%d %H:%M:%S} | store={row.store_id or 'UNKNOWN'} | "
            f"seller={row.seller_id or 'UNKNOWN'} | client={row.client_id or 'UNKNOWN'} | "
            f"sale={'Y' if row.is_sale else 'N'} | alarm={'Y' if row.is_alarm_triggered else 'N'} | "
            f"dialog={row.dialog_type or 'UNKNOWN'} | session={row.session_id}"
        )
        lines.extend(wrap_pdf_text(header, 110))
        for wrapped_line in wrap_pdf_text(row.recognition_text or "", 110):
            lines.append(f"  text: {wrapped_line}")
        lines.append("")
    return lines


class SimplePdfWriter:
    def __init__(self, title: str):
        self.title = title
        self.pages: list[list[str]] = []

    def add_page(self, lines: list[str]) -> None:
        self.pages.append(lines)

    def add_lines_as_pages(self, lines: list[str], lines_per_page: int = 44) -> None:
        current_page: list[str] = []
        for line in lines:
            current_page.append(line)
            if len(current_page) >= lines_per_page:
                self.add_page(current_page)
                current_page = []
        if current_page or not self.pages:
            self.add_page(current_page)

    def build(self) -> bytes:
        page_width = 595.2756
        page_height = 841.8898
        margin_left = 40
        margin_top = 48
        line_height = 13
        font_size = 10

        objects: list[bytes] = []

        def add_object(content: bytes) -> int:
            objects.append(content)
            return len(objects)

        font_regular = add_object(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        )
        font_bold = add_object(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"
        )

        page_object_ids: list[int] = []
        content_object_ids: list[int] = []

        def text_stream(lines: list[str]) -> bytes:
            content_parts = [
                "BT",
                f"/F1 {font_size} Tf",
                f"1 0 0 1 {margin_left} {page_height - margin_top} Tm",
                f"{line_height} TL",
            ]
            for line in lines:
                if line.startswith("## "):
                    content_parts.append("/F2 12 Tf")
                    content_parts.append(f"({pdf_escape(line[3:])}) Tj")
                    content_parts.append(f"/F1 {font_size} Tf")
                    content_parts.append("T*")
                    continue
                if line == "":
                    content_parts.append("T*")
                    continue
                content_parts.append(f"({pdf_escape(line)}) Tj")
                content_parts.append("T*")
            content_parts.append("ET")
            return "\n".join(content_parts).encode("latin-1", "replace")

        for page_lines in self.pages:
            content = text_stream(page_lines)
            content_object_ids.append(add_object(
                b"<< /Length "
                + str(len(content)).encode()
                + b" >>\nstream\n"
                + content
                + b"\nendstream"
            ))
            page_dict = (
                f"<< /Type /Page /Parent 0 0 R /MediaBox [0 0 {page_width:.4f} {page_height:.4f}] "
                f"/Resources << /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> >> "
                f"/Contents {content_object_ids[-1]} 0 R >>"
            ).encode()
            page_object_ids.append(add_object(page_dict))

        kids = " ".join(f"{obj_id} 0 R" for obj_id in page_object_ids)
        pages_id = add_object(
            f"<< /Type /Pages /Kids [{kids}] /Count {len(page_object_ids)} >>".encode()
        )
        catalog_id = add_object(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())

        # Fix parent references now that pages_id is known.
        for index, page_id in enumerate(page_object_ids):
            page_object = objects[page_id - 1].decode()
            page_object = page_object.replace("/Parent 0 0 R", f"/Parent {pages_id} 0 R")
            objects[page_id - 1] = page_object.encode()

        pdf = bytearray()
        pdf.extend(b"%PDF-1.4\n")
        offsets = [0]
        for index, obj in enumerate(objects, start=1):
            offsets.append(len(pdf))
            pdf.extend(f"{index} 0 obj\n".encode())
            pdf.extend(obj)
            pdf.extend(b"\nendobj\n")
        xref_start = len(pdf)
        pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode())
        pdf.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            pdf.extend(f"{offset:010d} 00000 n \n".encode())
        pdf.extend(
            (
                "trailer\n"
                f"<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
                f"startxref\n{xref_start}\n%%EOF\n"
            ).encode()
        )
        return bytes(pdf)


def write_pdf(path: Path, rows: list[ReportRow], report_date: date, generated_at: datetime, tz_name: str) -> None:
    overview_rows = [
        ["Report date", report_date.isoformat()],
        ["Generated at", generated_at.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S %Z")],
        ["Timezone", tz_name],
        ["Total records", len(rows)],
        ["Total sales", sum(int(row.is_sale) for row in rows)],
        ["Total alarms", sum(int(row.is_alarm_triggered) for row in rows)],
        ["Unique stores", len({row.store_id or "UNKNOWN" for row in rows})],
        ["Unique sellers", len({row.seller_id or "UNKNOWN" for row in rows})],
        ["Unique clients", len({row.client_id or "UNKNOWN" for row in rows})],
    ]

    store_summary = summary_rows(rows, "store_id", ["client_id", "seller_id"])
    seller_summary = summary_rows(rows, "seller_id", ["store_id", "client_id"])
    client_summary = summary_rows(rows, "client_id", ["store_id", "seller_id"])

    lines: list[str] = []
    lines.extend([f"## Transcript report for {report_date.isoformat()}", ""])
    lines.extend(
        [
            "## Overview",
            *[
                f"- {metric}: {value}"
                for metric, value in overview_rows
            ],
            "",
        ]
    )
    lines.extend(
        summary_lines(
            "Store summary",
            [
                "store_id",
                "sessions_count",
                "sales_count",
                "alarm_count",
                "dialog_types_count",
                "dialog_types",
                "client_ids",
                "seller_ids",
                "first_created_at",
                "last_created_at",
            ],
            store_summary,
        )
    )
    lines.extend(
        summary_lines(
            "Seller summary",
            [
                "seller_id",
                "sessions_count",
                "sales_count",
                "alarm_count",
                "dialog_types_count",
                "dialog_types",
                "store_ids",
                "client_ids",
                "first_created_at",
                "last_created_at",
            ],
            seller_summary,
        )
    )
    lines.extend(
        summary_lines(
            "Client summary",
            [
                "client_id",
                "sessions_count",
                "sales_count",
                "alarm_count",
                "dialog_types_count",
                "dialog_types",
                "store_ids",
                "seller_ids",
                "first_created_at",
                "last_created_at",
            ],
            client_summary,
        )
    )
    lines.extend(detailed_record_lines(rows))

    pdf = SimplePdfWriter(f"Transcript report {report_date.isoformat()}")
    pdf.add_lines_as_pages(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf.build())


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export daily transcript rows from PostgreSQL into a readable XLSX report."
    )
    parser.add_argument(
        "--date",
        help="Report date in YYYY-MM-DD format. Default: yesterday in the chosen timezone.",
    )
    parser.add_argument(
        "--timezone",
        default=os.getenv("REPORT_TIMEZONE", "Europe/Moscow"),
        help="Timezone used for the date boundary and workbook timestamps.",
    )
    parser.add_argument(
        "--output",
        help="Output .xlsx file path. Default: transcript_report_<date>.xlsx",
    )
    parser.add_argument(
        "--dsn",
        default=os.getenv("POSTGRES_DSN"),
        help="Optional PostgreSQL DSN. If omitted, uses POSTGRES_* environment variables.",
    )

    args = parser.parse_args()

    report_date = resolve_target_date(args.date, args.timezone)
    zone = ZoneInfo(args.timezone)

    start_at = datetime.combine(report_date, time.min, tzinfo=zone)
    end_at = start_at + timedelta(days=1)

    try:
        rows = await fetch_rows(args.dsn, start_at, end_at)
    except (socket.gaierror, OSError) as exc:
        host = env_value("POSTGRES_HOST", "localhost")
        port = env_value("POSTGRES_PORT", "5432")
        raise SystemExit(
            "Could not connect to PostgreSQL. "
            f"Current host is {host}:{port}. "
            "If you run the database locally, set POSTGRES_HOST=localhost. "
            "If you run the script inside Docker, set POSTGRES_HOST=postgres. "
            f"Original error: {exc}"
        ) from exc
    generated_at = datetime.now(zone)
    sheets = build_sheets(rows, report_date, generated_at, args.timezone)
    output_path = build_output_path(args.output, report_date)
    pdf_output_path = build_pdf_output_path(args.output, report_date)

    write_xlsx(output_path, sheets)
    write_pdf(pdf_output_path, rows, report_date, generated_at, args.timezone)

    print(f"Wrote {output_path.resolve()}")
    print(f"Wrote {pdf_output_path.resolve()}")
    print(f"Rows exported: {len(rows)}")


if __name__ == "__main__":
    asyncio.run(main())
