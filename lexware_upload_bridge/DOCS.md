# Bedienung

## Neu einrichten

Die App über das Repository `https://github.com/niki11euro/home-assistant-lexware-upload-app` im Home-Assistant-App-Store installieren. Eine vorhandene scanservjs-Scanner-App und ein Lexware-API-Zugang sind erforderlich.

Unter **Einstellungen → Apps → WE.R Lexware Upload → Konfiguration**:

1. **Interne Scanserver-Adresse** und **Exakte Scannerkennung** aus der eigenen Scanner-Einrichtung übernehmen.
2. **Lexware-Verbindung** auf `api` belassen und den privaten Schlüssel unter **Lexware-API-Schlüssel** eintragen.
3. Scanprofil prüfen. Neue Installationen beginnen mit 300 dpi, `Lineart`, JPG, A4 und neutraler Helligkeit/Kontrast 1000.
4. Speichern und starten. **Scannen freigeben** und **Lexware-Upload freigeben** erst nach abgeschlossener Einrichtung einschalten und die App neu starten.

MCP und Tunnel werden nicht benötigt. `mcp` bleibt als optionaler Legacy-Modus erhalten. Das Feld `api_token` ist ausschließlich ein optionaler Schlüssel für lokale Aufrufe an die Bridge; dort keinen Lexware-Schlüssel eintragen. Für die normale Oberfläche über Ingress kann es leer bleiben.

## Einen Beleg verarbeiten

WE.R-Beleg auf das Flachbett legen und **Scannen und Hochladen** anklicken. Der Button bleibt gesperrt, während die App scannt, lokal sichert und hochlädt. Keine parallelen Scans aus einer anderen Anwendung starten.

Die Historie zeigt Auftragszustand und Dateilinks. **Lexware hat den Upload angenommen** bestätigt die Dateiannahme. Verarbeitung, Erkennung oder Buchung bei Lexware können anschließend noch laufen.

Bei **Prüfung erforderlich** zuerst in Lexware nachsehen. Die App wiederholt den Upload nicht automatisch. Den Beleg nicht erneut scannen, um einen unklaren Upload zu umgehen.

## Scanprofil ändern

Die App-Konfiguration bietet 75, 100, 200, 300, 600 oder 1200 dpi, Farbe/Graustufen/Schwarzweiß, JPG/PNG und A4/A5/A6/volle Fläche. Der Scanner muss die gewählten Werte unterstützen. Nach Speichern neu starten. Die Einstellungen einer anderen Scanner-Oberfläche werden nicht übernommen.

Es wird genau eine Flachbettseite verarbeitet. Im Modus `Lineart` wendet scanservjs den Kontrastwert nicht an. Bei hohen Auflösungen Speicherbedarf und Lesbarkeit am eigenen Gerät prüfen.

## Wechsel von einer lokalen Installation

Die Store-App hat eine eigene Home-Assistant-App-ID. Das Hinzufügen des Repositorys ist kein Update der alten lokalen Installation. Beide Apps dürfen nicht gleichzeitig auf dieselben Belegdateien zugreifen.

1. Einen laufenden Auftrag zunächst beenden lassen. Unklare Uploads vor der Migration in Lexware klären. In der alten App Scan- und Uploadfreigabe ausschalten, Autostart und Watchdog ausschalten und die App stoppen.
2. **Alte App-Daten einschließlich Journal und privater Konfiguration sowie den vollständigen Share-Archivordner sichern.** Ein App-Backup allein enthält nicht automatisch `/share`. Die alte App und ihre Daten zunächst behalten.
3. Die Store-App installieren. Beide Freigaben ausgeschaltet lassen. Denselben `archive_subdirectory` wie zuvor einstellen und `import_archive_history: true` setzen. Der Ziel-Journalbestand der neuen App muss leer sein.
4. Speichern und die neue App starten. Der Import prüft alle Auftragsdateien, Annahmebestätigungen und Datei-Hashes und übernimmt die Historie gemeinsam. Er scannt nicht und ruft Lexware nicht auf. Unbekannte, unvollständige oder widersprüchliche Zustände verhindern den Import; es gibt keine teilweise Übernahme.
5. Historie und Dateizugriff kontrollieren. Danach `import_archive_history: false` setzen, private Verbindungseinstellungen und das bisherige Scanprofil übernehmen, speichern und neu starten. Scan- und Uploadfreigabe bleiben während dieser Prüfung aus.
6. Erst nach erfolgreicher Prüfung die beiden Betriebsfreigaben einschalten. Die alte App bleibt gestoppt; erst nach gesicherter Übernahme deinstallieren.

Ein vorhandenes Archiv mit leerem Journal wird ohne den kontrollierten Import abgewiesen. Bei einem Importfehler keine Archivdateien löschen oder Annahmebestätigungen verändern, um die Sperre zu umgehen. Die alte Installation und Sicherung für die Prüfung behalten.

Der Import ist keine erneute Übermittlung alter Belege. Ein älterer Journalstand darf nicht über neuere erfolgreiche Uploads geschrieben werden. Beim Rückweg nur mit einem Journal weiterarbeiten, das alle bereits gesendeten Aufträge kennt.

## Updates aus dem App-Store

Nach der einmaligen Migration wird dieselbe Store-App über **Aktualisieren** auf ihrer Home-Assistant-App-Seite aktualisiert. Repository und App-Slug bleiben gleich; vorhandene Optionen, Journal und Archiv bleiben Bestandteil der bestehenden Installation.

Vorher [Changelog](CHANGELOG.md) lesen und App-Daten sowie Share-Archiv sichern. Ein Update startet keinen Belegauftrag. Nach dem Neustart Historie, Status und Profil prüfen. Eine Wiederherstellung nimmt bereits erfolgte Uploads bei Lexware nicht zurück.

## Ablage

Das Journal liegt unter `/data/bridge/jobs.sqlite3`, die Belegdateien unter `/share/WE.R/Lexware Upload/YYYY-MM/<Auftrags-ID>/`. Der Archivpfad muss innerhalb des WE.R-Bereichs bleiben. Original und Uploadkopie bleiben auch bei Fehlern erhalten; es gibt keine automatische Aufräumfunktion.
