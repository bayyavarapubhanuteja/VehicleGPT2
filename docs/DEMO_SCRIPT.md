# VehicleGPT 2.0 demo script (~10 min)

1. **Landing** — two portals; state that all data is synthetic and local.
2. **Driver (`alice`)** — Overview: *historical (synthetic)* badge, high-warning banner. Health Score: 65/100 “Fair”, each
   penalty explained. Assistant: “Why did my driving range decrease?” → evidence + limitations. “Explain this diagnostic
   warning in simple language.” Early Warnings: open evidence, **Acknowledge**. Maintenance: add generic suggestion
   (labelled *not manufacturer*). Reports: download PDF.
3. **Security moment** — sign out; try `alice` on the **Engineer** login → refused. (Tests also prove crafted requests fail.)
4. **Engineer (`eng.chen`)** — Dashboard filters. Technical Query: “Which vehicles experienced repeated temperature
   anomalies?” → SYN-EV-002 ×3, structured query shown. Event Detective: SIM-BMS-T01, ±15 min window, observed sequence vs
   candidates → **Create case**. Investigations: try to confirm a candidate without justification (blocked), confirm with
   justification, resolve with summary; audit trail. Diagnostic Reports: PDF. Anomaly Lab: raise a threshold, evaluate
   candidate vs current P/R/F1 without saving.
5. **Engineer (`eng.rao`)** — Data Quality on SYN-EV-003: duplicates, °F-as-°C unit error, stuck sensor, stale data.
6. **Admin** — provision an engineer, grant a vehicle, audit log shows everything above.
7. **Offline** — disable Wi-Fi, repeat a query; `scripts/offline_check.py` passes.
