# #453 — SQLite durante un ciclo despertado desde Admin

## Punto de partida y límites de la evidencia

La investigación parte de `origin/main` en
`ec46d4171aba3cd78b34b11c265e20693912a354`, sin commits posteriores al SHA indicado.
Se leyeron `AGENTS.md`, #453 y el contexto necesario de aceptación en #362/#449.
La reproducción usa CPython 3.14.7 y SQLite 3.53.1, una base temporal con el nombre
de producción, formato 3, WAL y las conexiones independientes reales del runtime.
Los proveedores son fakes; no se hizo Spotify/OAuth real ni se accedió a producción.

Producción sólo conservó `exception_class=sqlite_error`, no el código extendido ni
el mensaje original. Por tanto, el código 517 que se identifica aquí pertenece a
la reproducción local determinista. La ruta real, el fallo inmediato anterior al
provider y el diagnóstico reproducido coinciden con la evidencia de #453; la
aceptación de #362/#449 sigue requiriendo comprobación tras el despliegue.

## Operación y conexión que fallan

`ReloadingEngineCycleRunner` construye un `EngineRunner` por ciclo. Su constructor
crea un `SpotifyRateLimitJournal`; `_build_diagnostics()` abre un nuevo
`DiagnosticEventStore` e invoca `initialize()` sobre la misma base del runtime.

Esta inicialización abre una conexión `_DiagnosticConnection` y, antes del arreglo,
ejecuta `BEGIN`, seguido de `CREATE TABLE/INDEX IF NOT EXISTS`. Con el esquema ya
existente, esas comprobaciones establecen un snapshot de lectura. Su primera
escritura posterior es exactamente:

```sql
DELETE FROM diagnostic_events
WHERE event_name = 'duration_profile'
  AND instr(details_json, '"under_10m":') = 0
```

La excepción observada en esa sentencia es:

```text
sqlite3.OperationalError: database is locked
sqlite_errorcode=517
sqlite_errorname=SQLITE_BUSY_SNAPSHOT
```

El error sale del cuerpo del contexto SQLite. `_build_diagnostics()` captura
`PersistenceError` y `ValueError`, pero no este `sqlite3.OperationalError` original.
Así llega al wrapper `InstrumentedEngineCycleRunner`, que registra accounting cero
y `cycle_unexpected_failure`, y al scheduler, que termina el ciclo como fallido.

## Secuencia concurrente demostrada

1. El handler autenticado persiste Enable/Disable o Save en `managed-state.json`,
   bajo `ConfigurationSynchronization`, y libera ese lock.
2. Invoca `EngineLifecycleController.reconcile(configured=True)` sobre el scheduler
   ya existente. `scheduler.wake()` activa el Event antes de emitir
   `scheduler_wake_requested`.
3. El scheduler inicia el ciclo, persiste `cycle_started`, recarga el nuevo estado
   y entra en la inicialización de diagnostics descrita arriba.
4. La prueba mantiene abierta esa transacción después de sus comprobaciones del
   esquema. Otra conexión del hilo Admin intenta insertar un diagnóstico real:
   `scheduler_wake_requested`, o el evento de configuración Admin posterior.
5. Antes del arreglo, la prueba mantiene abierta la transacción competidora tras
   su `INSERT`, la libera y espera a que haga `COMMIT`. La inicialización todavía
   conserva el snapshot previo.
6. El `DELETE` intenta convertir ese snapshot obsoleto en escritor y falla con
   `SQLITE_BUSY_SNAPSHOT`. No ha habido provider I/O en el ciclo despertado.

El siguiente ciclo abre conexiones y snapshots nuevos, normalmente después de que
los eventos del handler hayan terminado; no necesita deshacer la mutación, que ya
era durable. La carrera depende del intercalado entre ambos hilos, lo que explica
los wakes exitosos durante la misma sesión y los fallos que duran menos del timeout.
Un `busy_timeout` mayor no rejuvenece un snapshot obsoleto.

La semántica de aislamiento y la adquisición previa del escritor están descritas
en [SQLite Isolation](https://sqlite.org/isolation.html) y
[SQLite Transactions](https://sqlite.org/lang_transaction.html).

## Componentes inspeccionados

El conflicto probado es entre las dos conexiones de diagnostics, no una escritura
de la definición de fuente a SQLite. `ConfigurationSynchronization` protege el
estado JSON y el mutation guard; `EngineCycleSynchronization` protege los ciclos y
el mantenimiento. Ninguno debe convertirse en un lock para diagnostics/network.

`SQLiteStore.initialize()` establece WAL, valida el formato y utiliza
`BEGIN IMMEDIATE` en las migraciones. `SpotifyRequestMetrics.initialize()` vuelve
a validar ese store, pero no es la operación que falló en la reproducción: ocurre
después de construir el rate-limit journal. Sus scopes permanecen separados por
contexto. Las métricas y las escrituras de eventos usan conexiones independientes.

Los journals de public detector y Spotify-first se inicializan durante el arranque,
abren/cierran conexiones por operación y empiezan su trabajo de ciclo después de
este constructor. El journal de pending snapshots participa más tarde en la
reconciliación de destinos. No fue necesario modificar ninguno de ellos, las
migraciones ni la lógica de propagación de snapshots.

## Cambio mínimo

Sólo `DiagnosticEventStore.initialize()` reserva el escritor con `BEGIN IMMEDIATE`
antes de consultar el esquema. La escritura competidora espera entonces usando el
timeout existente; no puede adelantar a la inicialización e invalidar su snapshot.
El lock termina al commit/rollback de esa operación local, antes de cualquier
provider I/O. Los métodos de lectura de diagnostics conservan `BEGIN`.

Se cierra además la conexión cuando falla la apertura de la transacción. Una prueba
con un escritor real retenido demostró que el contexto anterior dejaba esa conexión
abierta al rechazar `BEGIN IMMEDIATE`. El error sigue propagándose como
`PersistenceError`, con su causa SQLite original. No se añaden sleeps, retries,
timeouts, excepciones ignoradas ni campos nuevos de diagnostics.

## Reproducción y regresiones

Antes de modificar producción, la prueba de topología real falló en los tres casos
Enable/Disable/Save con el mismo `DELETE`, `OperationalError`, código 517, accounting
cero y `cycle_unexpected_failure / sqlite_error`: **3 failed**.

`tests/test_admin_wake_sqlite.py` cubre después del arreglo:

- las tres mutaciones, con el evento wake y con el evento Admin como competidores;
- persistencia durable y estado efectivo nuevo, incluyendo asignaciones dormant;
- mutación sin provider I/O, y provider I/O sólo dentro del hilo del ciclo;
- un provider pausado sin transacción SQLite retenida por la inicialización;
- wake retenido durante un ciclo, ausencia de solapamientos y siguiente vencimiento
  programado controlado mediante Event;
- diagnostics completos y accounting por ciclo consistente con los agregados;
- un trigger SQLite que rechaza realmente el cleanup: el ciclo falla antes de I/O,
  cuenta cero requests y conserva el destino, sin fingir éxito.

`tests/test_diagnostics_store.py` añade contención real al comienzo de la
inicialización: una lectura concurrente sigue disponible, `SQLITE_BUSY` se propaga,
la conexión se cierra y una inicialización posterior funciona al liberar el escritor.
La prueba usa timeout cero sólo en su conexión instrumentada para acortar ese caso.
Toda la coordinación de la carrera utiliza Events; no hay sleeps en la reproducción.

No cambia el formato SQLite ni el schema del estado administrado. No cambia el
contrato de #451. La aceptación operativa de #362/#449 permanece abierta.

## Validación local final

- Reproducción anterior al arreglo: 3 fallos deterministas, uno por mutación.
- Tests focalizados de runtime/Admin/diagnostics/metrics: **169 passed**.
- Estabilidad de la nueva topología: **10 ejecuciones × 7 casos = 70 passed**.
- Suite completa con CPython 3.14.7: **1803 passed**.
- `python scripts/ruff_fix.py`, `ruff check .`, `ruff format --check .`,
  `mypy src` y `git diff --check`: correctos.

Estos resultados son validación local; CI y Container son evidencias independientes.
