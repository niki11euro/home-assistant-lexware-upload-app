# Changelog

## 0.5.0

- Eigenes öffentliches Home-Assistant-App-Repository mit vorgebauten GHCR-Images für AMD64 und ARM64. Weitere Versionen können nach der einmaligen Migration über die App-Seite aktualisiert werden.
- Direkte Lexware-API als Standardverbindung. MCP und Tunnel sind dafür nicht erforderlich; der Legacy-MCP-Modus bleibt ausdrücklich wählbar, ohne automatischen Wechsel.
- Defaults für neue Installationen: 300 dpi, Schwarzweiß (`Lineart`), JPG und A4. Scan- und Uploadfreigabe bleiben aus; Zugangsschlüssel bleiben leer. Bestehende Optionen werden bei regulären Updates erhalten.
- Neue Option `import_archive_history`, standardmäßig aus: kontrollierte Übernahme eines vorhandenen Belegarchivs in ein leeres Journal, nur mit beiden Betriebsfreigaben aus. Aufträge, Annahmebestätigungen und Datei-Hashes werden vor der gemeinsamen Übernahme geprüft; unbekannte oder unvollständige Zustände werden abgewiesen. Kein Netzwerkzugriff oder erneuter Upload beim Import.
- Vorhandenes Archiv und leeres Journal ohne passenden Import werden gesperrt. Anleitung für den Wechsel von einer lokalen App mit getrennten App- und Share-Sicherungen.
- Öffentliche Dokumentation ohne installationsbezogene Gerätekennungen, Adressen, Zugangsdaten oder lokale Betriebsnachweise. MIT-Lizenz ergänzt.

## 0.4.0

- Explizite API-/MCP-Auswahl und Passwortfeld `lexware_api_key` für direkte Belegdatei-Uploads.
- Fester HTTPS-Endpunkt, Multipart mit `type=voucher`, 180 Sekunden Zeitlimit und Annahme nur bei HTTP 202 mit gültiger Datei-ID.
- Keine Weiterleitungen, Umgebungs-Proxies, automatischen Wiederholungen oder automatischen Backendwechsel. Originale, Journal, Annahmebestätigungen und `needs_review` bleiben erhalten.

## 0.3.0

- Scanprofil vollständig aus der Home-Assistant-App-Konfiguration.
- Ein Button für Scannen, lokales Sichern und Hochladen; Fortschrittsphasen und Dateihistorie.
- Eindeutige Anfragekennung und serverseitige Sperre gegen parallele Aufträge.

## 0.2.0

- Annahmebestätigungen nach Abbruch wiederherstellen, unklare Uploads und Dubletten sperren.
- Erweiterte Prüfungen für Scannerprofile, MCP-Protokoll, Uploadschema und sichere Fehlerbehandlung.

## 0.1.0

- Erste lokale App für ausdrücklich gestartete einseitige Belegscans, lokale Archivierung und MCP-Belegupload.
