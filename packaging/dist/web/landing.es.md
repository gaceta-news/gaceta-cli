# gaceta — qué cambió en tus historias desde la última vez que las leíste

> Una página canónica, dos idiomas: [English](landing.en.md) · **Español**
> Borrador — candidato 0.3.0b4 · fuente única: `packaging/catalog.json`
> (renderizado por `scripts/build-packages.py`, verificado por `scripts/check-parity.py`)

## El trabajo de todos los días

Sigues un puñado de feeds. La misma historia llega cinco veces en una sola
mañana: dos medios la reportan, uno traduce al primero, dos la repiten. La
lees igual, porque la única que *realmente cambió* está en algún lado. gaceta
hace ese ordenado por vos: los ítems de los feeds que ya seguís se vuelven una
**historia** con sus fuentes, y cuando una historia que ya leíste cambia, el
diario dice qué cambió — y jamás inventa cuándo la leíste.

## Una salida real

Del demo offline empaquetado (datos sintéticos — historias inventadas, hosts
`*.example`; sin red, sin llamadas a modelos). Una sesión declaró una memoria
de solo versión («la última vez vi la historia `demo-trail` en su versión 1»)
y preguntó qué cambió:

```json
"skipped_editions_note": "unknown: your baseline states no edition date, so
  this surface cannot say which public editions you have no record of (trace
  times are never used as a stand-in for reading history)",
"updates": [
  {
    "story_id": "demo-trail",
    "title": "Demo Loop trail reopens after boardwalk repairs",
    "baseline": {
      "claimed_version": 1,
      "status": "known",
      "reason": "baseline version 1 of 2 matches the paper's write-up history"
    },
    "current_version": 2,
    "change": {
      "status": "material_change",
      "reason": "evidence-supported change since your baseline; each proposal
        carries the public sources behind it",
      "proposals": [
        {
          "kind": "proposed_update",
          "version": 2,
          "what_changed": "Added the walker's note; the detour sign now has a
            stated end condition.",
          "what_changed_origin": "model",
          "support": [
            {
              "title": "Demo Loop boardwalk repairs finished",
              "url": "https://trails.example/demo-loop-reopens",
              "kind": "rss", "role": "origin",
              "published": "2026-11-15T08:30:00+01:00"
            },
            {
              "title": "Walk report: Demo Loop on the first dry weekend",
              "url": "https://notes.example/notes/b66e8a0f1ec9",
              "kind": "nostr", "role": "update",
              "published": "2026-11-17T09:00:00+01:00"
            }
          ]
        }
      ]
    }
  }
]
```

Reproducilo: `python scripts/make_agent_demo.py DIR` desde un skill
empaquetado, luego `gaceta agent since --reader demo --baseline … --path DIR/export.json`.

## Cuándo NO adoptarlo

- **Si no seguís feeds.** gaceta lee los tuyos (RSS/Atom, canales de YouTube,
  Nostr); no es una app de noticias con fuentes editoriales propias.
- **Si querés un producto alojado con cuentas.** Corre en tu máquina,
  primero como CLI; no hay servicio al que iniciar sesión.
- **Si necesitás alertas de última hora.** Es un diario: las historias se
  asientan, y una re-ejecución solo reescribe lo que cambió.
- **Si querés un ranking compartido o global.** Ediciones, reacciones y gusto
  son por lector por diseño; nada te rankea contra nadie.
- **Si solo querés el resumen de un artículo suelto.** Cualquier asistente lo
  hace; esto no suma nada si el diario de un lector no alimenta tus sesiones.
- **Si no podés correr Python ≥ 3.12 o un modelo editor.** Las
  redacciones reales necesitan un modelo (Ollama local o cualquier endpoint
  compatible con OpenAI). El demo offline prueba la cañería con modelos
  falsos, nada más.
- **Un skill de agente no es el producto.** El skill de Hermes/Codex de abajo
  es una lente de solo lectura sobre un export que produce tu máquina;
  instalar solo el skill no instala ningún diario ni construye nada.

## Qué necesitás, y cuánto cuesta

- Python ≥ 3.12 + [uv](https://docs.astral.sh/uv/); los feeds que ya
  seguís; un modelo editor — todo local (Ollama: gratis, tu CPU/tiempo) o
  cualquier API compatible con OpenAI (la cobra tu proveedor).
- **Presupuestos duros, no estimaciones:** `write` acepta `--max-llm-calls` y
  `--max-change-calls`; el clustering es local con embeddings; las historias
  sin cambios no se reescriben. Un diario sin cambios no gasta llamadas.
- **La consulta del agente es gratis:** `gaceta agent edition|story|since`
  leen un solo archivo JSON — cero llamadas a modelos.

## Instalación (fijada a versión)

```bash
uv tool install git+https://github.com/gaceta-news/gaceta-cli@v0.3.0b4
gaceta --version        # debe imprimir 0.3.0b4
```

**v0.3.0b4 es el pin beta previsto**, cortado en la revisión de release —
hasta que el tag exista públicamente, esto es la forma del comando, no una
URL viva. Los tags 0.2.x publicados son reales pero previos a los assets del
demo para agentes, a `--version` y a los skills empaquetados: fijar una
instalación en 0.2.0 da un gaceta sin las funciones de este candidato.
Después, el quickstart offline de dos minutos del [README](https://github.com/gaceta-news/gaceta-cli) — feed
de muestra, modelos falsos, sin claves. Para un diario real, apuntalo a tus
feeds y a tu modelo.

## Para clientes de agentes (Hermes, Codex)

Descripción del skill: **«Find what changed in stories from the reader's feeds.»** — el runtime de solo
lectura es exactamente `gaceta agent edition`, `story` y `since`.

| Cliente | Paquete (este repo) | Ruta estándar de instalación |
|---|---|---|
| Hermes | `packaging/dist/hermes-skill/research/gaceta/` · zip en `packaging/dist/archives/` | descomprimir en `~/.hermes/skills/` (el listado en registro/hub es un paso posterior y separado) |
| Codex | `packaging/dist/codex-plugin/` (plugin de solo skills) · `packaging/dist/codex-marketplace/` para pruebas locales | `$HOME/.agents/skills/` (ámbito USER) o `codex plugin marketplace add <dir>`; rutas estándar, sin funciones experimentales |

Ambos paquetes traen el demo de arriba: un agente se puede probar sin ningún
dato. **Estado, dicho claro:** lo ejercitado hasta aquí es el empaquetado y
el demo offline. No se afirma ningún resultado de descubrimiento ciego,
indexación de catálogo ni uso configurado en ningún cliente; eso es evidencia
aparte, pre-registrada.

## Límites

- La superficie para agentes es **de solo lectura** y **pública por
  construcción**: los exports derivan de una edición `--max-privacy public` y
  se re-verifican; historias domésticas/privadas, señales de gusto y
  reacciones nunca viajan.
- Un export se niega a servirse después de 48 horas — un agente leyendo el
  export del mes pasado falla cerrado en vez de responder.
- La historia de lectura jamás se inventa: el informe califica solo lo que
  una sesión *declaró*, dice `unknown` donde falta evidencia, y las marcas
  «visto» de una persona vuelven solo vía un recibo opt-in del lado del dueño.
- Sin cuentas alojadas, sin ranking compartido, sin analítica de terceros,
  sin telemetría. Las claves se leen del entorno, nunca se guardan en configs.
- Compartir (cuando el candidato de recetas se fusione) por defecto **no
  comparte nada**; una receta lleva solo los feeds y rótulos que el dueño
  marcó explícitamente como públicos.
- Una prueba (cuando el candidato de pruebas se fusione) es acotada: a lo
  sumo tres ediciones, techos duros de llamadas/tokens, vencimiento en días
  de calendario, y nada que se renueve solo.

## Probado con (solo smoke de empaquetado)

| Qué | Resultado |
|---|---|
| Candidato | gaceta 0.3.0b4 (P4 beta candidate; the v-tag is cut at release review — published 0.2.x tags predate these features, so 0.2.0 is not a valid pin for them) — `gaceta --version`, el METADATA del wheel y cada sello de artefacto coinciden |
| Codex | codex-cli 0.171.0 — stock `codex plugin marketplace add` + `codex plugin list` on a sandboxed CODEX_HOME; no experimental features or adapters enabled |
| Hermes | Hermes Agent v0.23.2 — packaged skill validated against the stock skills conventions (frontmatter, layout, description limit); registry/hub install is exercised only once the release source is public |
| Python | 3.12 (floor of requires-python) |

La aceptación e indexación de catálogos son dependencias externas, no
supuestos de CI. La instalación fría desde GitHub y el registro de paridad
tag/wheel/docs son ítems de release, ejecutados desde la revisión pública
fijada cuando exista.

## Desinstalar

```bash
uv tool uninstall gaceta
rm -rf "$GACETA_DATA"          # el directorio de datos del diario; nada más
```

Skills: borrá `~/.hermes/skills/research/gaceta/` (Hermes) o
`~/.agents/skills/gaceta/` / `codex plugin remove gaceta`
(Codex). No hay procesos en segundo plano que detener: la superficie de
agentes no agenda nada.

## Enlaces

- Fuente e issues: https://github.com/gaceta-news/gaceta-cli · https://github.com/gaceta-news/gaceta-cli/issues
- El contrato de la superficie para agentes: `docs/agent/README.md`
- El diario demo bilingüe: `src/gaceta/assets/desk/index.html` (bilingual demo desk from the desk workstream; ships as a wheel asset — published URL pinned at release)
- Vista previa de receta portable: portable recipe (gaceta recipe export/inspect/import) is a parallel candidate (P2), not part of this candidate; the landing page links it once merged
- Prueba finita: finite trial (gaceta trial preview/start/...) is a parallel candidate (P3), not part of this candidate; the landing page links it once merged
- La fuente de esta página (un catálogo, dos renderizados): `packaging/templates/landing.en.md.tmpl`, `landing.es.md.tmpl`

*Estado de borrador: esta página se genera, no se mantiene a mano; publicarla
— y dónde — es una decisión de release posterior a las compuertas de P5. Nada
de ella se telemetra.*

