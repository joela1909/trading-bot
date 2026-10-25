# Bot kostenlos auf Oracle Cloud („Always Free“)

Oracle stellt einen kleinen Linux-Server dauerhaft kostenlos bereit. Darauf läuft der Bot rund um die Uhr, auch wenn
dein PC aus ist, und die App ist vom Handy aus mit Passwort erreichbar.

## 1. Konto anlegen (einmalig, ca. 10 Minuten)

1. https://signup.cloud.oracle.com öffnen und registrieren.
2. **Home Region: „Germany Central (Frankfurt)“** wählen (oder „Netherlands Northwest (Amsterdam)“).
   Die Region lässt sich später **nicht** ändern, und Binance lehnt Anfragen aus manchen Ländern ab (z. B. USA).
3. Die Kreditkarte dient nur der Prüfung. Solange du das Konto nicht auf „Pay As You Go“ umstellst, kostet nichts etwas.

## 2. Server anlegen (ca. 5 Minuten)

Im Oracle-Dashboard: **Menü › Compute › Instances › Create instance**

| Feld | Eintrag |
|---|---|
| Name | `trading-bot` |
| Image | **Canonical Ubuntu 24.04** (über „Change image“) |
| Shape | „Change shape“ › **Ampere › VM.Standard.A1.Flex**, **1 OCPU, 6 GB** (Kennzeichen „Always Free-eligible“) |
| Networking | Voreinstellung lassen, **„Assign a public IPv4 address“ an** |
| SSH keys | **„Paste public keys“** und diesen Schlüssel einfügen (eine Zeile): |

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBslUYiApj9SN/TD6egTQjt+edB/VLUrXDJA9eE2le/V trading-bot-oracle
```

Dann **Create**. Nach 1–2 Minuten steht der Server auf „Running“, daneben die **Public IP address**.

> **„Out of capacity“?** Die kostenlosen ARM-Server sind in Frankfurt oft vergriffen. Dann eine andere
> „Availability domain“ wählen, es später erneut versuchen oder die Shape **VM.Standard.E2.1.Micro** (AMD, 1 GB)
> nehmen. Das Einrichtungsskript legt bei so wenig Speicher automatisch 2 GB Auslagerungsdatei an.

## 3. Ports 80 und 443 freigeben (für das Handy)

**Menü › Networking › Virtual cloud networks** › dein Netz › **Security Lists** › „Default Security List…“ ›
**Add Ingress Rules**:

| Source CIDR | IP Protocol | Destination Port Range |
|---|---|---|
| `0.0.0.0/0` | TCP | `80,443` |

Die Firewall im Server selbst öffnet das Einrichtungsskript.

## 4. Bot hochladen

Auf dem PC im Ordner `Bot\trading-bot-main\deploy`:

```
upload-to-server.bat ubuntu@DEINE-IP -WithState
```

`-WithState` nimmt dein bisheriges Papierkonto, die Trades und die App-Einstellungen mit. Am Ende stehen die
**Handy-Adresse** (`https://…sslip.io`), Benutzer und Passwort im Fenster. Das Passwort wird nur einmal angezeigt.

Danach den Bot auf dem PC beenden, damit nicht zwei gleichzeitig laufen.

## Später

| Aufgabe | Befehl |
|---|---|
| Neue Version vom PC hochladen (Zustand auf dem Server bleibt) | `upload-to-server.bat ubuntu@DEINE-IP` |
| Logs ansehen | `ssh -i %USERPROFILE%\.ssh\trading-bot-oracle ubuntu@DEINE-IP "cd /opt/trading-bot && sudo docker compose logs --tail 50 bot"` |
| Neues Passwort fürs Handy | `ssh -i %USERPROFILE%\.ssh\trading-bot-oracle ubuntu@DEINE-IP "cd /opt/trading-bot && sudo bash deploy/public-dashboard.sh"` |

**Achtung, Leerlauf-Regel:** Oracle kann kostenlose Server anhalten, die über 7 Tage kaum ausgelastet sind
(grob unter 20 % CPU). Der Bot rechnet zwar ständig, bleibt aber womöglich darunter. Oracle schickt vorher eine E-Mail.
Abhilfe: das Konto auf „Pay As You Go“ umstellen. Die Always-Free-Grenzen bleiben dann kostenlos, und die
Leerlauf-Regel gilt nicht mehr (dafür würde Oracle Verbrauch über den Grenzen berechnen).
