# Diagnostic imagery

The four rendered diagnostic frames from the chamber node, as shipped with the scenario:

- `diagnostics/01_grafana_biometrics.jpg` — EEG / pulse correlation
- `diagnostics/02_rack_camera_live.jpg` — rack B7 chamber view
- `diagnostics/03_surgical_consent_form.jpg` — intake consent form
- `diagnostics/04_agent_84_mirror.jpg` — agent #84 mirror frame

These are presentation copies of the generated in-world assets. The engine regenerates the
live frames into the node at `/var/www/html/diagnostics/`; the plate artwork lives in
`wetware-honeypot/assets/plates/`.
