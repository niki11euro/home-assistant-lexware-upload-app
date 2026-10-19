# Sicherheitsmodell

## Zugang und Geheimnisse

Die Oberfläche ist für Home-Assistant-Administratoren vorgesehen und verwendet den authentifizierten Ingress-Zugang. Schreibende Ingress-Aufrufe benötigen zusätzlich einen CSRF-Token. Forwarded-Header ersetzen die Prüfung des direkten Zugangs nicht. Ein optionaler langer `api_token` kann lokale Automatisierungsaufrufe authentifizieren; er ist bei Auslieferung leer.

`lexware_api_key` ist der Zugangsschlüssel zum Anbieter. `api_token` ist ein anderer Schlüssel für die lokale Bridge. Beide gehören ausschließlich in private App-Einstellungen. Die App gibt Zugangsschlüssel nicht in Statusantworten, Auftragsdateien oder Fehlermeldungen aus. Sie protokolliert weder Beleginhalte noch rohe Anbieterantworten.

## Funktionsgrenzen

Im Standardmodus `api` sendet die App nur die geprüfte Bilddatei an den festen HTTPS-Dateiupload von Lexware, mit `type=voucher`. TLS-Prüfung bleibt aktiv. Weiterleitungen, Umgebungs-Proxies, automatische Uploadwiederholungen und ein automatischer Wechsel zu MCP sind ausgeschaltet. Der API-Schlüssel kann beim Anbieter weitergehende Rechte haben; die Beschränkung auf den Belegupload wird im App-Code durchgesetzt.

Der optionale Legacy-Modus `mcp` verwendet einen ausdrücklich konfigurierten internen Dienst und dessen eigenen Bearer-Token. Die Bridge ruft nur das geprüfte Uploadwerkzeug auf. Das begrenzt nicht die Rechte anderer Clients am selben MCP-Server. Rechte einer anderen Anwendung steuern den eigenständigen direkten API-Zugang dieser App nicht; dafür gilt `upload_enabled` in Home Assistant.

Die App fordert keine Home-Assistant- oder Supervisor-API, keinen Docker-Socket, kein Host-Netzwerk und keinen USB-Zugriff an. Sie veröffentlicht keinen zusätzlichen Host-Port. Das Startprogramm bereitet die eigenen Speicherordner vor und wechselt vor dem Webdienst auf UID/GID 10001.

## Dateien, Historie und Wiederholungen

Das Original bleibt unverändert. Archivdateien werden mit Prüfsummen, atomaren Dateischritten und dauerhaften Journalständen gesichert. Eine bestätigte Annahme oder ein unklarer Upload sperrt eine erneute Übermittlung. Bei `needs_review` zuerst den Bestand in Lexware prüfen; ein Neustart ist kein Wiederholungsauftrag.

Der Share ist technisch als Ganzes eingebunden. Die Beschränkung auf den vorgesehenen WE.R-Unterordner erfolgt zusätzlich im Anwendungscode. Benutzer oder Prozesse mit Schreibrechten am Share können Archivdateien verändern; dies ist kein manipulationssicheres Archiv. Betriebssystemrechte und Sicherungen bleiben erforderlich.

Der einmalige Archivimport läuft nur bei ausgeschalteter Scan- und Uploadfreigabe und leerem Zieljournal. Er prüft vorhandene Auftragsdateien, Annahmebestätigungen und Datei-Hashes vor der gemeinsamen Übernahme. Unbekannte, unvollständige oder widersprüchliche Zustände werden abgewiesen. Der Import sendet keine Dateien und ruft weder Scanner noch Lexware auf. Ein vorhandenes Archiv darf nicht mit einem neuen leeren Journal für neue Uploads geöffnet werden, ohne die Historie zu übernehmen.

Für eine vollständige Sicherung werden **App-Daten mit Journal und Konfiguration sowie das Share-Archiv** benötigt. Ein App-Backup allein sichert nicht automatisch den Share. Konfiguration und Sicherungen können Zugangsdaten und Beleginhalte enthalten und gehören nicht in dieses Repository.

## Sicherheitslücken melden

Bitte GitHubs private Funktion **Report a vulnerability** im Security-Bereich dieses Repositorys verwenden, sofern sie verfügbar ist. Keine Zugangsdaten, Belege oder ausnutzbaren Details in öffentliche Issues schreiben. Falls privates Melden nicht angeboten wird, kann ein öffentliches Issue ohne technische Details um einen privaten Meldeweg bitten.

Tests, Container-Builds und Abhängigkeitsprüfungen ersetzen kein unabhängiges Sicherheitsaudit. Es gibt keine Zusage, dass externe Provider, Scanner oder jede Home-Assistant-Installation bereits geprüft sind. Bei einem Befund Versionsnummer und eine bereinigte Beschreibung angeben.
