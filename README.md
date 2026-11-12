# lucya-aio

Der Helfer für einen Rechner mit dem ganzen lucya.systems-Stack. Er hält den
Docker-Socket, hat aber keine eigene Oberfläche: Lucent ist die Oberfläche
und bittet ihn über ein Netz, das nur die beiden teilen, Tools anzulegen und
zu koppeln.

```bash
docker run -d --name lucya-aio --restart always \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v lucya-aio-data:/data \
  -v lucya-backups:/backups \
  -e LUCYA_ALLOWED_PATHS=/srv/fotos \
  ghcr.io/lucya-astralis/lucya-aio:stable
docker logs lucya-aio      # Adresse und Einrichtungscode
```

Die Images baut GitHub (`.github/workflows/image.yml` in jedem Repo):
`main` → `:stable`, `beta/*` → `:beta`. Solange die Lucent-Anbindung der
Tools nur auf ihren beta-Branches liegt, mit `-e LUCYA_CHANNEL=beta`
starten; der Helfer selbst kommt als `lucya-aio:stable`.

Beim Start legt er an, was fehlt:

- das Netz `lucya` (Lucent und Tools) und das interne Netz `lucya-aio`
  (nur Lucent und Helfer, ohne Weg nach draußen);
- den Container `lucya-lucent` mit den Volumes `lucya-lucent-data` und
  `lucya-lucent-site`, Port 8060 auf allen Schnittstellen.

Danach legt er Tools nur auf Lucents Bitte an.

## Was er darf

Wer den Docker-Socket hat, ist root auf dem Rechner. Das gilt auch für
diesen Helfer, deshalb läuft er offen als root. Begrenzt ist, was seine API
annimmt ([aio/templates.py](aio/templates.py)):

- nur die Tools aus `TOOLS` (arca, Fila, strata, Aperture), nur ihr Image
  (`ghcr.io/lucya-astralis/<tool>:<kanal>`);
- von Lucent kommen nur Werte: Host-Port, Bind-Adresse, UI-Adresse, beim
  Koppeln Code und Rolle. Alles wird geprüft;
- jeder Container ohne Capabilities, `no-new-privileges`, Dateisystem nur
  lesbar, benannte Volumes für die Daten, nie `privileged`, nie das
  Host-Netz;
- ein Host-Ordner nur für Aperture (die Fotos), und nur einer aus
  `LUCYA_ALLOWED_PATHS` oder darunter, beim `docker run` gesetzt;
- `exec` nur für den Join- und den Leave-Befehl des Tools;
- er sieht und berührt nur Container mit dem Label `systems.lucya.aio=<Präfix>`
  (Vorgabe `lucya-`): zwei Helfer auf einem Rechner kommen sich nicht in die Quere.

Die API verlangt das Token aus `/data/aio.json`, das der Helfer Lucent beim
Anlegen als `LUCENT_AIO_TOKEN` mitgibt.

## API

| | |
|---|---|
| `GET /health` | ohne Token |
| `GET /state` | Lucent, jedes Tool mit Zustand, Werten und Schritt des laufenden Auftrags, belegte Ports |
| `PUT /tools/{kind}` | `{port, bind, ui_url}` → 202; legt im Hintergrund an oder neu an (`pull` → `create` → `start` → `ready`, oder `error`). Gleiche Werte ändern nichts, neue erzeugen den Container neu, das Volume bleibt |
| `POST /tools/{kind}/join` | `{code, role, again}` → führt `python -m <tool> --lucent-join http://lucent:8060 CODE --ui-url … --base-url http://<tool>:<port> --role …` im Container aus; mit `again: true` vorher `--lucent-leave` (Fila und strata lehnen einen Join ab, solange sie gekoppelt sind) |
| `POST /tools/{kind}/restart` | Container neu starten |
| `DELETE /tools/{kind}?data=keep\|delete` | Container entfernen, das Volume nur mit `data=delete` |
| `POST /updates/check` | zieht im Hintergrund die Images aus der Registry; danach sagt `/state` je Container `update` |
| `POST /tools/{kind}/update` | auch `lucent`: sichern → Image ziehen → Container mit neuem Image neu anlegen → warten; antwortet er nicht, läuft das alte Image wieder |
| `POST /updates/all` | jedes Update nacheinander, Lucent zuletzt |
| `POST /backup` | sichert alle laufenden Container |
| `PUT /backup/schedule` | `{enabled, time: "HH:MM", keep}`: tägliche Sicherung, wie viele bleiben |

## Aperture

Aperture bekommt zwei Ports (Aperture selbst, Vorgabe 8090, und die
öffentliche Galerie, Vorgabe 8000), die Fotos aus einem freigegebenen
Host-Ordner (lesend und schreibend: Alben und Galerie haben ihre
Einstellungen als Dateien neben den Fotos) und Volumes für Daten,
Vorschaubilder und Thumbnails. Der Ordner muss dem Benutzer 10001 gehören
(`sudo chown -R 10001:10001 /srv/fotos`), sonst kann Aperture keine
Einstellungen schreiben.

Ohne Passwort startet Aperture auf einer Netzadresse nicht, und ohne
Passwort wäre es offen. Vor dem ersten Start setzt der Helfer deshalb ein
zufälliges Passwort (ein kurzer Container ohne Netz, auf Aperture's
Daten-Volume) und behält es in `/data/passwords.json`. Angemeldet wird
über Lucent; das Passwort ist der Weg hinein, wenn Lucent nicht da ist.
Lucent zeigt es Admins auf Nachfrage (`GET /tools/aperture/password`).

Gesichert wird Apertures `/data` (Index, Kopplung, Passwort). Die Fotos und
die Einstellungen daneben liegen im Host-Ordner und gehören in dessen
eigene Sicherung.

## Updates

Jedes Image, aus dem der Helfer einen Container macht, bekommt einen
eigenen Namen, `lucya-images/<tool>:current`; vor einem Update wird daraus
`:previous`. Docker mit dem containerd-Bildspeicher (Docker Desktop, neuere
Engines) vergisst ein Image, sobald kein Name mehr darauf zeigt, auch wenn
ein Container es noch nutzt, und `pull` oder `build` verschieben den Tag.
Ohne eigenen Namen gäbe es nach einem misslungenen Update kein Zurück. Hat
ein laufendes Image keinen Namen mehr, lehnt der Helfer das Update ab,
statt ohne Rückweg zu tauschen.

## Sicherungen

Unter `/backups` (was beim `docker run` dort eingehängt ist: ein benanntes
Volume oder ein Ordner, `-v /srv/backup:/backups`) liegt je Lauf ein Ordner
mit einem `<tool>.tar.gz` je Container und `backup.json`. Die Kopie
entsteht im Container selbst, als sein Benutzer: jede SQLite-Datei in
`/data`, erkannt am Dateikopf, nicht am Namen, per `VACUUM INTO`, also
vollständig auch im WAL-Modus und im laufenden Betrieb, alles andere
(Schlüssel, Kopplung) wie es ist. Ein
Archiv ist `/data`, so wie es zurück soll:

```bash
docker run --rm -v lucya-arca-data:/data -v lucya-backups:/backups alpine \
  sh -c 'cd /data && tar xzf /backups/2026-11-12_033000/arca.tar.gz'
```

Vorher das Tool stoppen (`docker stop lucya-arca`), danach wieder starten.

Ohne `/backups` gibt es keine Sicherungen, und vor einem Update wird dann
auch keine gemacht.

## Einstellungen beim `docker run`

| | Vorgabe | |
|---|---|---|
| `LUCYA_PORT` | `8060` | Lucents Port auf dem Rechner |
| `LUCYA_BIND` | `0.0.0.0` | wo Lucent zu erreichen ist |
| `LUCYA_CHANNEL` | `stable` | Image-Tag |
| `LUCYA_REGISTRY` | `ghcr.io/lucya-astralis/` | |
| `LUCYA_IMAGES` | – | lokal gebaute Images, `lucent=lucent:aio,arca=arca:aio` |
| `LUCYA_PREFIX`, `LUCYA_NETWORK`, `LUCYA_CONTROL_NETWORK` | `lucya-`, `lucya`, `lucya-aio` | Namen, zum Testen neben einem echten Stack |

Ein Stack, den man von Hand mit Compose betreibt, darf nicht am selben Netz
`lucya` hängen: zwei Container mit dem Alias `arca` im selben Netz teilen
sich den Namen.

## Tests

```bash
python -m pytest -q
```

Die Tests laufen gegen eine Docker-Attrappe ([tests/conftest.py](tests/conftest.py)).
