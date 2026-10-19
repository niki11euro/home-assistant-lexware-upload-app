# WE.R Lexware Upload 0.5.0

Home-Assistant-App für einen ausdrücklich gestarteten Ablauf: eine Flachbettseite scannen, Original und Uploadkopie lokal sichern und die Belegdatei direkt an Lexware Office übergeben.

Die [Bedienungs- und Migrationsanleitung](DOCS.md) beschreibt Einrichtung, Wechsel von einer lokalen App und Updates. Die Installation erfolgt über das [öffentliche App-Repository](https://github.com/niki11euro/home-assistant-lexware-upload-app). Veröffentlichte Versionen nutzen vorgebaute GHCR-Images für AMD64 und ARM64.

## Ablauf

1. Der Klick auf **Scannen und Hochladen** startet einen einzelnen Auftrag mit eigener Anfragekennung.
2. Genau die vom Scanauftrag zurückgemeldete Datei wird abgeholt. Die App sucht nicht nach der neuesten Datei eines gemeinsamen Ordners.
3. Das Original und die vorbereitete Uploadkopie werden dauerhaft lokal gespeichert und per SHA-256 geprüft.
4. Vor dem einzigen Uploadversuch wird der Zustand `uploading` gespeichert. Eine gültige Annahme wird mit Datei-ID journalisiert; unklare Ergebnisse bleiben `needs_review`.

Die App startet beim Hochfahren keine Scans, setzt keine alten Aufträge automatisch fort und beobachtet keine Eingangsordner. Statusabfragen und der Historienimport kontaktieren keine externen Dienste.

## Verbindungen

Standard ist `upload_backend: api`: Multipart-Dateiupload an `https://api.lexware.io/v1/files`, ausschließlich `type=voucher`. Nur HTTP 202 mit gültiger Datei-ID gilt als Annahme. Keine Weiterleitungen, Umgebungs-Proxies oder automatischen Wiederholungen; Zeitlimit 180 Sekunden. Der Schlüssel steht im Passwortfeld `lexware_api_key`.

`upload_backend: mcp` ist ein optionaler Legacy-Weg zu einem ausdrücklich konfigurierten internen MCP-Dienst. Es gibt keinen automatischen Rückfall auf diesen Weg. `api_token` authentifiziert optionale lokale Bridge-Aufrufe und ist kein Anbieter-Schlüssel; für Ingress wird er nicht benötigt.

## Speicher und Import

```text
/data/bridge/jobs.sqlite3
/share/WE.R/Lexware Upload/
  YYYY-MM/
    <Auftrags-ID>/
      original.jpg
      upload.jpg
      status.json
      lexware-response.json
```

PNG-Dateien verwenden die entsprechende Endung. Die Annahmebestätigung entsteht erst nach gültiger Uploadantwort. Archivdateien werden nicht automatisch gelöscht. Die Oberfläche zeigt lokale Browserzeit; gespeicherte Zeiten und Monatsordner verwenden UTC.

`import_archive_history` ist standardmäßig aus. Für den kontrollierten Wechsel von einer lokalen App kann es ein bereits vorhandenes, vollständig geprüftes Archiv in ein leeres Journal übernehmen. Beide Betriebsfreigaben müssen dabei aus sein. Erst wenn alle Datensätze, Bestätigungen und Datei-Hashes gültig sind, wird die Historie gemeinsam importiert. Unbekannte oder unvollständige Zustände werden abgewiesen. Der Import sendet keine Dateien. Ohne passenden Import bleibt der Start mit vorhandenem Archiv und leerem Journal gesperrt.

## Grenzen

- JPG/PNG, eine Flachbettseite; keine PDFs oder Mehrseitenverarbeitung.
- Maximal 100 MB Quelldatei und 145 Millionen Pixel. Die Uploadkopie bleibt höchstens 4,8 MB groß; nur diese Kopie wird bei Bedarf komprimiert.
- Bytegleiche Uploadkopien werden als Dubletten erkannt. Ein erneuter physischer Scan kann andere Bytes erzeugen; eine inhaltliche Dublettenerkennung ist nicht enthalten.
- Die App erstellt keine Rechnungen und löst keine Zahlungen aus. Annahme bedeutet noch keine abgeschlossene Verarbeitung oder Buchung bei Lexware.
- Der Share und das App-Journal müssen gemeinsam gesichert werden. App-Backups enthalten den Share nicht automatisch.

Das [Sicherheitsmodell](../SECURITY.md) erläutert Zugang, Geheimnisse und verbleibende Grenzen.
