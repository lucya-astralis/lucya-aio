# lucya-aio

Der Helfer für einen Rechner mit dem ganzen lucya.systems-Stack. Er hält den
Docker-Socket, hat aber keine eigene Oberfläche: Lucent ist die Oberfläche
und bittet ihn über ein Netz, das nur die beiden teilen, Tools anzulegen und
zu koppeln.

```bash
docker run -d --name lucya-aio --restart always \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v lucya-aio-data:/data \
  ghcr.io/lucya-astralis/lucya-aio:stable
docker logs lucya-aio      # Adresse und Einrichtungscode
```

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

- nur die Tools aus `TOOLS` (heute arca, Fila, strata), nur ihr Image
  (`ghcr.io/lucya-astralis/<tool>:<kanal>`);
- von Lucent kommen nur Werte: Host-Port, Bind-Adresse, UI-Adresse, beim
  Koppeln Code und Rolle. Alles wird geprüft;
- jeder Container ohne Capabilities, `no-new-privileges`, Dateisystem nur
  lesbar, ein benanntes Volume für die Daten, nie `privileged`, nie das
  Host-Netz, keine Host-Ordner;
- `exec` nur für den Join-Befehl des Tools;
- er sieht und berührt nur Container mit dem Label `systems.lucya.aio`.

Die API verlangt das Token aus `/data/aio.json`, das der Helfer Lucent beim
Anlegen als `LUCENT_AIO_TOKEN` mitgibt.

## API

| | |
|---|---|
| `GET /health` | ohne Token |
| `GET /state` | Lucent, jedes Tool mit Zustand, Werten und Schritt des laufenden Auftrags, belegte Ports |
| `PUT /tools/{kind}` | `{port, bind, ui_url}` → 202; legt im Hintergrund an oder neu an (`pull` → `create` → `start` → `ready`, oder `error`). Gleiche Werte ändern nichts, neue erzeugen den Container neu, das Volume bleibt |
| `POST /tools/{kind}/join` | `{code, role}` → führt `python -m <tool> --lucent-join http://lucent:8060 CODE --ui-url … --base-url http://<tool>:<port> --role …` im Container aus |

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
