"""CSV and PDF report generation (fpdf2, fully offline). Callers must pass data obtained via services.py."""
from __future__ import annotations

import io
import json
from datetime import datetime

import pandas as pd
from fpdf import FPDF

_ASCII = {"°": " deg", "–": "-", "—": "-", "→": "->", "≥": ">=", "≤": "<=", "×": "x", "Δ": "d", "²": "^2", "÷": "/",
          "…": "...", "“": '"', "”": '"', "’": "'", "‘": "'", "±": "+/-", "·": "-", "⚠️": "!", "⚠": "!", "≈": "~", "−": "-"}


def _t(s) -> str:
    s = "" if s is None else str(s)
    for k, v in _ASCII.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


def to_csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


class _PDF(FPDF):
    def __init__(self, title: str, subtitle: str):
        super().__init__()
        self.title_, self.subtitle_ = title, subtitle
        self.set_auto_page_break(True, 15)
        self.add_page()

    def header(self):
        self.set_fill_color(11, 16, 32)
        self.rect(0, 0, 210, 22, "F")
        self.set_text_color(34, 211, 238)
        self.set_font("Helvetica", "B", 14)
        self.set_xy(10, 6)
        self.cell(0, 6, _t("VehicleGPT 2.0 - " + self.title_))
        self.set_text_color(200, 205, 220)
        self.set_font("Helvetica", "", 8)
        self.set_xy(10, 13)
        self.cell(0, 5, _t(self.subtitle_))
        self.set_text_color(20, 20, 20)
        self.set_y(27)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(120)
        self.cell(0, 5, _t(f"SYNTHETIC DATA - informational prototype, not a certified diagnosis. Generated "
                           f"{datetime.now():%Y-%m-%d %H:%M}. Page {self.page_no()}"), align="C")

    def hd(self, text):
        self.ln(2)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(14, 116, 144)
        self.cell(0, 7, _t(text), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(20, 20, 20)

    def para(self, text, size=9):
        self.set_font("Helvetica", "", size)
        self.multi_cell(0, 4.6, _t(text), new_x="LMARGIN", new_y="NEXT")

    def table(self, df: pd.DataFrame, widths: list[float] | None = None, max_rows: int = 40):
        if df is None or df.empty:
            self.para("(none)")
            return
        df = df.head(max_rows)
        cols = list(df.columns)
        widths = widths or [190 / len(cols)] * len(cols)
        self.set_font("Helvetica", "B", 7.5)
        self.set_fill_color(225, 232, 240)
        for c, w in zip(cols, widths):
            self.cell(w, 5, _t(c)[:int(w / 1.6)], border=1, fill=True)
        self.ln()
        self.set_font("Helvetica", "", 7.5)
        for _, r in df.iterrows():
            for c, w in zip(cols, widths):
                v = r[c]
                v = f"{v:.2f}" if isinstance(v, float) else v
                self.cell(w, 4.8, _t(v)[:int(w / 1.55)], border=1)
            self.ln()

    def line_chart(self, x: list, series: dict[str, list], title: str, marker_idx: int | None = None, h: float = 55):
        """Minimal vector line chart (no external rendering engine)."""
        if not x or not series:
            return
        if self.get_y() + h + 12 > 280:
            self.add_page()
        x0, y0, w = 22, self.get_y() + 6, 175
        self.set_font("Helvetica", "B", 8)
        self.cell(0, 5, _t(title), new_x="LMARGIN", new_y="NEXT")
        vals = [v for s in series.values() for v in s if v is not None and pd.notna(v)]
        if not vals:
            return
        lo, hi = min(vals), max(vals)
        hi = hi if hi > lo else lo + 1
        self.set_draw_color(180)
        self.rect(x0, y0, w, h)
        self.set_font("Helvetica", "", 6.5)
        for k in range(5):
            yv = lo + (hi - lo) * k / 4
            yy = y0 + h - h * k / 4
            self.set_xy(4, yy - 2)
            self.cell(17, 4, f"{yv:.1f}", align="R")
        palette = [(14, 116, 144), (234, 88, 12), (124, 58, 237), (22, 163, 74)]
        n = len(x)
        for si, (name, ys) in enumerate(series.items()):
            self.set_draw_color(*palette[si % 4])
            self.set_line_width(0.4)
            pts = [(x0 + w * i / max(n - 1, 1), y0 + h - h * (y - lo) / (hi - lo)) for i, y in enumerate(ys)
                   if y is not None and pd.notna(y)]
            for a, b in zip(pts, pts[1:]):
                self.line(a[0], a[1], b[0], b[1])
            self.set_text_color(*palette[si % 4])
            self.set_xy(x0 + 2 + si * 45, y0 + h + 1)
            self.cell(44, 4, _t(name))
        self.set_text_color(20, 20, 20)
        if marker_idx is not None:
            self.set_draw_color(220, 38, 38)
            mx = x0 + w * marker_idx / max(n - 1, 1)
            self.line(mx, y0, mx, y0 + h)
        self.set_line_width(0.2)
        self.set_xy(x0, y0 + h + 5)
        self.set_font("Helvetica", "", 6.5)
        self.cell(w / 2, 4, _t(str(x[0])))
        self.cell(w / 2, 4, _t(str(x[-1])), align="R")
        self.set_y(y0 + h + 11)

    def bytes(self) -> bytes:
        return bytes(self.output())


def driver_health_pdf(user: dict, vehicle: dict, hs: dict, alerts: pd.DataFrame, eff: dict) -> bytes:
    pdf = _PDF("Vehicle Health Summary", f"{vehicle['nickname']} ({vehicle['vehicle_id']}) - prepared for {user['display_name']}")
    pdf.hd(f"Health indicator: {hs['score']}/100 ({hs['band']})")
    pdf.para(f"As of {hs['as_of'][:16]}. Data completeness {hs['completeness_pct']}%, confidence {hs['confidence']}. "
          "This indicator is explainable and NOT a manufacturer rating or certified diagnosis.")
    pdf.table(pd.DataFrame(hs["factors"])[["factor", "points", "detail"]] if hs["factors"] else pd.DataFrame(),
              [60, 20, 110])
    pdf.hd("Recent alerts (last 7 days)")
    pdf.table(alerts[["ts", "severity", "subsystem", "message"]].astype(str) if len(alerts) else alerts, [32, 20, 20, 118], 20)
    d = eff["daily"]
    pdf.hd("Energy consumption trend (calculated)")
    pdf.line_chart(d["date"].dt.strftime("%m-%d").tolist(), {"kWh/100 km": d["kwh_per_100km"].round(2).tolist()},
                   "Daily consumption, kWh/100 km")
    c = eff["comparison"]["diff"]["consumption_kwh_per_100km"]
    pdf.para(f"Last 7 days {c['new']} vs previous 7 days {c['old']} kWh/100 km ({c['pct']}%).")
    pdf.hd("Limitations")
    for l in hs["limitations"]:
        pdf.para("- " + l)
    return pdf.bytes()


def engineer_case_pdf(engineer: dict, vehicle: dict, rep: dict, rules: pd.DataFrame) -> bytes:
    c = rep["case"]
    pdf = _PDF(f"Diagnostic Report - Case #{c['id']}",
               f"Vehicle {vehicle['vehicle_id']} / VIN {vehicle['vin']} / {vehicle['name']} - engineer {engineer['display_name']}")
    pdf.hd("Incident summary")
    pdf.para(f"Title: {c['title']}\nStatus: {c['status']}   Priority: {c['priority']}   Root cause: {c['root_cause_status']}\n"
          f"Created: {c['created_at']}   Event: {c['event_key'] or '-'}\n\n{c['summary'] or ''}")
    pdf.para(rep["root_cause_statement"], 9)
    inv = rep["investigation"]
    if inv:
        pdf.hd("Event timeline")
        tl = pd.DataFrame(inv["timeline"])
        pdf.table(tl[["ts", "type", "text", "record_id", "source"]] if len(tl) else tl, [34, 22, 92, 20, 22])
        tel = inv["telemetry"]
        tv = tel[tel["valid"] == 1]
        ch = inv["focus_channel"] or "battery_temp_c"
        xs = tv["ts"].dt.strftime("%m-%d %H:%M").tolist()
        ev_ts = pd.Timestamp(inv["event"]["ts"])
        mi = int((tv["ts"] - ev_ts).abs().argmin()) if len(tv) else None
        pdf.line_chart(xs, {ch: tv[ch].tolist(), "ambient_temp_c": tv["ambient_temp_c"].tolist()},
                       f"Sensor chart: {ch} around event (red line = event)", mi)
        pdf.hd("Before / after comparison (valid samples)")
        pdf.table(pd.DataFrame(inv["comparison"]), [46, 36, 36, 36, 36])
        pdf.hd("Confirmed observations")
        for o in inv["confirmed"]:
            pdf.para("- " + o)
    pdf.hd("Candidate explanations (evidence strength / review status)")
    hy = pd.DataFrame(rep["hypotheses"])
    pdf.table(hy[["id", "text", "evidence_strength", "status", "review_note"]].astype(str) if len(hy) else hy, [10, 105, 22, 20, 33])
    pdf.hd("Missing information")
    for m in rep["missing_information"]:
        pdf.para("- " + m)
    pdf.hd("Recommended diagnostic checks")
    for m in rep["recommended_checks"]:
        pdf.para("- " + m)
    pdf.hd("Detection rules and versions in effect")
    pdf.table(rules, [40, 150])
    pdf.hd("Investigation notes")
    nt = pd.DataFrame(rep["notes"])
    pdf.table(nt[["created_at", "author", "body"]] if len(nt) else nt, [32, 30, 128])
    return pdf.bytes()


def json_bytes(obj) -> bytes:
    return json.dumps(obj, indent=2, default=str).encode()
