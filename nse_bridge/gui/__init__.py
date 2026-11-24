"""Client GUI: splash window, system tray, native window (pywebview),
robust browser auto-open, visible error dialogs — plus the local
dashboard (web) served at ``/ui``.

The bridge must never run invisibly: on a windowed exe (no console)
every startup state has a visible surface — splash first, then the
native window or the browser, with the tray icon as the persistent
presence and a dialog (never stderr-only) for fatal errors.
"""
