# Home Assistant Integration: Carbon Aware Home

## Disclaimer
Diese Integration wird nach bestem Wissen entwickelt, jedoch ohne Gewähr. Nutzung auf eigenes Risiko.

## Aktueller Funktionsumfang
- Sensor `sensor.current_co2_intensity` (minütliche Aktualisierung durch Interpolation zwischen 15‑Minuten-Rohpunkten)
- Service `carbon_aware_home.get_best_time_raw` zur Ermittlung eines optimalen Startzeitpunkts (Sliding Window auf 15‑Minuten-Raster)

Nicht vorhanden (noch nicht implementiert): Forecast-Sensor, automatische Ausführungs-/Delay-Services, minutenfeiner Forecast.

## Installation
### HACS
1. HACS öffnen → Drei Punkte → Custom Repositories → Repository hinzufügen.
2. Integration installieren, Neustart durchführen.

### Manuell
Ordner `custom_components/carbonAwareHome` nach `custom_components/carbon_aware_home` umbenennen/kopieren (Domain muss übereinstimmen).

## Konfiguration (`configuration.yaml`)
Minimal:
```yaml
carbon_aware_home:
  location: de
```
Optional mit Intervall:
```yaml
carbon_aware_home:
  location: de
  refresh_interval_minutes: 30  # Abrufintervall API (Default 60)
```
Standort-Codes (Auszug): de, fr, at, ch, nl, uk, northwales, southwales, germanywestcentral, switzerlandnorth, francecentral, uksouth, ukwest, europe-west3, europe-west6, europe-west9, northscotland, southscotland, usw.

## Sensor `sensor.current_co2_intensity`
Attribute: Quelle (actual/forecast/interpolated), Zeitstempel UTC, Cache-Alter, Serienlänge.

## Service `get_best_time_raw`
Aufruf: Entwicklerwerkzeuge → Dienste → `carbon_aware_home.get_best_time_raw`.
Parameter:
- dataStartAt (ISO 8601 mit Zeitzone)
- dataEndAt (ISO 8601 mit Zeitzone)
- expectedRuntime (Minuten, Default 60)
- allowedHours (optional, z.B. `8-21` lokal)
Antwortfelder: `status`, `best_start`, `avg_intensity`, `runtime_minutes`, `location`. Kein Persistieren – Ergebnis nur als direkte Response.
Status-Werte: OK, No Data, InvalidWindow, InvalidDatetime, Error.

## Algorithmus
Rohdaten (15‑Minuten CO₂eq) werden in Zeitfenster der gewünschten Laufzeit aggregiert; Durchschnittswerte werden verglichen; kleinstes Mittel → Startzeit. Nur Startpunkte auf Originalzeitstempeln (kein minutenfeines Sliding).

## Datenquelle & Caching
API: https://api.energy-charts.info/co2eq
Abrufintervall konfigurierbar über `refresh_interval_minutes` (Standard 60). Werte zwischen Abrufen bleiben gecached. Sensor interpoliert zwischen zwei 15‑Minuten-Stützpunkten linear für minütliche Anzeige.

## Roadmap
- Forecast-Sensor
- Automatischer Delay-/Execution Service
- Minutenfeiner Forecast (interpoliertes Fenster)
- Erweiterte Regionen-/Cloud-Mappings

## Troubleshooting
- Kein Wert: Internet/API erreichbar? Logs prüfen.
- Falscher Ordner: Muss `custom_components/carbon_aware_home` heißen.
- Intervall ignoriert: Wert >0 setzen; Log zeigt tatsächlichen Wert.

## Lizenz
Siehe Repository-Lizenzdatei.
