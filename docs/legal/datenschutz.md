<!--
SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>

SPDX-License-Identifier: GPL-2.0-only
-->

# Datenschutzerklärung für youtube-helper

Stand: 17. Oktober 2026

## 1. Verantwortlicher

Florian Weßel
E-Mail: florianwessel@gmx.net

## 2. Worum es geht

youtube-helper ist eine selbst betriebene Software. Sie läuft ausschließlich auf dem Server
dessen, der sie installiert (im Folgenden „Betreiber“), und lädt neue Videos aus den
YouTube-Abonnements des Betreibers in dessen eigene Mediathek.

Diese Erklärung gilt für den Google-OAuth-Client „youtube-helper“, den der Verantwortliche bei
Google registriert hat. Wer youtube-helper mit einem eigenen OAuth-Client betreibt, ist für
diesen selbst verantwortlich.

Der Verantwortliche betreibt keinen Server für youtube-helper und erhält keine Daten. Es gibt
keine Analyse, kein Tracking, keine Werbung und keine Weitergabe an Dritte.

## 3. Welche Daten verarbeitet werden

### Zugriff auf das Google-Konto

Bei der Anmeldung (`login`) erteilt der Nutzer youtube-helper über Google den Zugriff
`https://www.googleapis.com/auth/youtube.readonly`, also nur Lesezugriff auf das YouTube-Konto.
Genutzt wird davon ausschließlich die Liste der Abonnements
([`subscriptions.list`](https://developers.google.com/youtube/v3/docs/subscriptions/list)).
Von jedem abonnierten Kanal werden folgende Daten abgerufen:

- Kanal-ID,
- Kanalname,
- Kanalbeschreibung,
- Adresse des Kanalbilds (Avatar).

Nicht abgerufen werden unter anderem E-Mail-Adresse, Name, Wiedergabeverlauf, Playlists,
Kommentare und Bewertungen. youtube-helper ändert nichts am Konto.

### Gespeicherte Daten

Auf dem Server des Betreibers speichert youtube-helper in einer lokalen SQLite-Datenbank:

- das Refresh-Token von Google, mit dem die Abonnements ohne erneute Anmeldung gelesen werden;
- die oben genannten Daten der abonnierten Kanäle sowie den Zeitpunkt, ab dem neue Videos
  eines Kanals heruntergeladen werden;
- zu jedem neuen Video die Video-ID, den Titel, die Veröffentlichungszeit und den Status des
  Downloads (z. B. heruntergeladen, übersprungen, Fehlermeldung);
- die Nummerierung der Folgen je Kanal und Jahr.

In der Mediathek des Betreibers legt youtube-helper die heruntergeladenen Videos samt
Vorschaubild, Beschreibung und Kanalbild ab.

## 4. Zweck und Rechtsgrundlage

Die Daten dienen allein dazu, neue Videos der abonnierten Kanäle zu erkennen und in die
Mediathek des Betreibers herunterzuladen.

Rechtsgrundlage ist die Einwilligung, die der Nutzer bei der Anmeldung über Google erteilt
(Art. 6 Abs. 1 lit. a DSGVO). Nutzt der Betreiber youtube-helper nur für sich selbst, handelt es
sich um eine ausschließlich persönliche Tätigkeit (Art. 2 Abs. 2 lit. c DSGVO).

## 5. Empfänger

youtube-helper verbindet sich nur mit Google und YouTube:

- **Google OAuth und YouTube Data API** (`oauth2.googleapis.com`, `www.googleapis.com`): für die
  Anmeldung und das Lesen der Abonnements. Dabei gilt die
  [Datenschutzerklärung von Google](https://policies.google.com/privacy).
- **YouTube** (`www.youtube.com`, YouTube-Bildserver): zum Lesen der öffentlichen Upload-Feeds
  der Kanäle und zum Herunterladen der Videos und Bilder. Das geschieht anonym, ohne Anmeldung
  und ohne Cookies des Google-Kontos.

Eine Übermittlung an andere Empfänger oder in Drittländer durch youtube-helper findet nicht
statt. Google verarbeitet Daten nach eigenen Angaben auch außerhalb der EU.

## 6. Einhaltung der Google-Richtlinien

Die Nutzung und Übertragung von Informationen, die youtube-helper über Google-APIs erhält,
entspricht der
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
einschließlich der Anforderungen zur eingeschränkten Nutzung („Limited Use“). Die Daten werden
nur für die oben beschriebene Funktion verwendet, nicht verkauft, nicht für Werbung genutzt und
von Menschen nur eingesehen, wenn der Betreiber selbst in seine Datenbank schaut.

## 7. Speicherdauer und Löschung

Die Daten bleiben gespeichert, solange der Betreiber youtube-helper nutzt. Der Betreiber kann
sie jederzeit löschen, indem er die Datenbankdatei `youtube-helper.sqlite3` im
Zustandsverzeichnis entfernt.

Der Zugriff auf das Google-Konto lässt sich jederzeit widerrufen unter
[myaccount.google.com/permissions](https://myaccount.google.com/permissions). Danach kann
youtube-helper die Abonnements nicht mehr lesen; das gespeicherte Refresh-Token wird ungültig.

## 8. Rechte der Betroffenen

Betroffene haben das Recht auf Auskunft (Art. 15 DSGVO), Berichtigung (Art. 16), Löschung
(Art. 17), Einschränkung der Verarbeitung (Art. 18), Datenübertragbarkeit (Art. 20) und
Widerspruch (Art. 21) sowie das Recht, eine erteilte Einwilligung jederzeit mit Wirkung für die
Zukunft zu widerrufen (Art. 7 Abs. 3). Außerdem besteht das Recht auf Beschwerde bei einer
Datenschutz-Aufsichtsbehörde (Art. 77 DSGVO).

Da alle Daten ausschließlich auf dem Server des Betreibers liegen, kann der Verantwortliche
nur auf Daten zugreifen, wenn er selbst der Betreiber ist.

## 9. Änderungen

Diese Erklärung wird angepasst, wenn sich youtube-helper ändert. Es gilt die jeweils hier
veröffentlichte Fassung.
