# WE.R Lexware Upload für Home Assistant

Belege mit einer vorhandenen scanservjs-Scanner-App scannen, Original und Uploadkopie lokal sichern und direkt an Lexware Office übergeben. Jeder Scan startet ausdrücklich per Klick. Es gibt keine automatischen Uploads oder Wiederholungen.

Die App verarbeitet eine Flachbettseite als JPG oder PNG. Sie erstellt keine Rechnungen und löst keine Zahlungen aus. „Lexware hat den Upload angenommen“ bestätigt die Dateiannahme; Lexwares weitere Verarbeitung kann noch laufen.

## Installation

1. In Home Assistant unter **Einstellungen → Apps → App-Store → Repositories** dieses Repository hinzufügen:

   ```text
   https://github.com/niki11euro/home-assistant-lexware-upload-app
   ```

2. **WE.R Lexware Upload** installieren. Veröffentlichte Versionen verwenden vorgebaute Container aus GHCR für AMD64 und ARM64.
3. Unter **Konfiguration** die interne Scanner-Adresse, die genaue Scannerkennung und den Lexware-API-Schlüssel eintragen. Zugangsdaten bleiben in den privaten App-Einstellungen.
4. Profil prüfen, speichern und die App starten. Erst danach **Scannen freigeben** und **Lexware-Upload freigeben** aktivieren und neu starten.

Neue Installationen verwenden `upload_backend: api`, 300 dpi, Schwarzweiß (`Lineart`), JPG und A4. Beide Freigaben sind zunächst aus; Zugangsschlüssel sind leer. MCP und Tunnel werden für den direkten Upload nicht benötigt. Der optionale `api_token` schützt Zugriffe auf die lokale Bridge und ist **kein Lexware-API-Schlüssel**.

Ein Beleg auf dem Scanner und ein Klick auf **Scannen und Hochladen** starten genau einen Auftrag. Nicht gleichzeitig mit einer anderen Anwendung scannen.

## Wechsel von einer lokalen Installation

Die Store-App hat eine andere Home-Assistant-App-ID als `local_lexware_upload_bridge`. Das Hinzufügen des Repositorys übernimmt deren Daten und Einstellungen nicht automatisch. Die alte App zuerst stilllegen und die Historie kontrolliert importieren; niemals beide Apps gleichzeitig auf dasselbe Archiv zugreifen lassen.

Die [Migrationsanleitung](lexware_upload_bridge/DOCS.md#wechsel-von-einer-lokalen-installation) beschreibt den Import ohne Scanner- oder Lexware-Aufrufe. Vorher **App-Daten einschließlich Journal und das Share-Archiv** sichern. Ein reines App-Backup enthält nicht automatisch das Share-Archiv.

## Updates

Nach diesem einmaligen Wechsel erscheinen neue Versionen auf der App-Seite in Home Assistant. Vor **Aktualisieren** die [Änderungen](lexware_upload_bridge/CHANGELOG.md) lesen und App-Daten sowie Share-Archiv sichern. Repository und App-Slug bleiben bei weiteren Updates gleich; dadurch bleibt der Store-Zugang bestehen. Optionen und Daten werden nicht durch die Auslieferungsdefaults ersetzt.

## Unterlagen

- [Bedienung und Migration](lexware_upload_bridge/DOCS.md)
- [Technischer Überblick](lexware_upload_bridge/README.md)
- [Sicherheit und Fehler melden](SECURITY.md)
- [Mitentwicklung](CONTRIBUTING.md)
- [MIT-Lizenz](LICENSE)

Eigenständiges Community-Projekt; kein offizielles Produkt von Home Assistant, Lexware oder scanservjs.
