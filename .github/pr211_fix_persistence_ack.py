from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file_path = Path(path)
    text = file_path.read_text()
    if old not in text:
        raise SystemExit(f"expected text not found in {path}")
    file_path.write_text(text.replace(old, new, 1))


# 1. Make SQLiteStore.set_match_state acknowledge whether the temporal UPSERT guard
# actually applied the requested state. Existing callers may ignore the bool; the repair
# path must not publish an uncommitted outcome.
persistence_path = Path("src/news_bulletin_playlist/persistence.py")
persistence = persistence_path.read_text()
start = persistence.index("    def set_match_state(")
end = persistence.index("\n    def get_match_state(", start)
block = persistence[start:end]
block = block.replace(
    "    ) -> None:\n        \"\"\"Persist deterministic source-edition to Spotify matching state.\"\"\"",
    "    ) -> bool:\n        \"\"\"Persist matching state and report whether the temporal guard applied it.\"\"\"",
    1,
)
block = block.replace("            connection.execute(\n", "            cursor = connection.execute(\n", 1)
needle = "            )\n"
pos = block.rfind(needle)
if pos < 0:
    raise SystemExit("could not locate set_match_state execute terminator")
block = block[: pos + len(needle)] + "            return cursor.rowcount > 0\n" + block[pos + len(needle) :]
persistence_path.write_text(persistence[:start] + block + persistence[end:])


# 2. A cached repair may be rejected when the runner clock is behind the durable row.
# Keep the durable cached outcome and its fail-closed qualification in that case instead
# of publishing the fresh outcome as if it had been persisted.
replace_once(
    "src/news_bulletin_playlist/spotify/matcher.py",
    """            evaluated = assessment.evaluated
            store.set_match_state(
                outcome.edition.source_id,
                outcome.edition.source_native_id,
                status=evaluated.status,
                spotify_episode_uri=evaluated.spotify_episode_uri,
                spotify_duration_seconds=evaluated.spotify_duration_seconds,
                diagnostics=evaluated.diagnostics,
                updated_at=observed_at,
            )
            outcomes[index] = evaluated
""",
    """            evaluated = assessment.evaluated
            applied = store.set_match_state(
                outcome.edition.source_id,
                outcome.edition.source_native_id,
                status=evaluated.status,
                spotify_episode_uri=evaluated.spotify_episode_uri,
                spotify_duration_seconds=evaluated.spotify_duration_seconds,
                diagnostics=evaluated.diagnostics,
                updated_at=observed_at,
            )
            if not applied:
                # The durable row is newer than this runner observation (for example after a
                # clock rollback). Never publish a fresh repaired outcome that SQLite rejected:
                # planner evidence must remain fail-closed against the mapping still on disk.
                qualification_overrides[index] = assessment.qualification
                continue
            outcomes[index] = evaluated
""",
)


# 3. Lock the persistence acknowledgement contract itself.
replace_once(
    "tests/test_persistence_replay.py",
    """    store.set_match_state(
        SourceId("rne"),
        "rne-asset-1",
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:newer",
        diagnostics="newer match",
        updated_at=base + timedelta(minutes=20),
    )
    store.set_match_state(
        SourceId("rne"),
        "rne-asset-1",
        status=MatchStatus.PENDING,
        diagnostics="stale pending state",
        updated_at=base + timedelta(minutes=10),
    )
""",
    """    assert store.set_match_state(
        SourceId("rne"),
        "rne-asset-1",
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:newer",
        diagnostics="newer match",
        updated_at=base + timedelta(minutes=20),
    )
    assert not store.set_match_state(
        SourceId("rne"),
        "rne-asset-1",
        status=MatchStatus.PENDING,
        diagnostics="stale pending state",
        updated_at=base + timedelta(minutes=10),
    )
""",
)


# 4. Matcher-level regression for the exact clock-rollback boundary found by Codex.
regression_path = Path("tests/test_pr211_adversarial_regressions.py")
regression = regression_path.read_text()
addition = r'''


def test_rejected_clock_rollback_repair_keeps_durable_mapping_and_never_qualifies(
    tmp_path,
) -> None:
    source = _source("ser", "ser", "Europe/Madrid", "show-ser")
    edition = _edition(
        source,
        "ser-clock-rollback",
        "Las noticias de la SER, 10:00 (14/09/2026)",
        duration_seconds=60,
    )
    store = _store(tmp_path)
    _persist_cached_match(store, edition, uri=URI_309, duration=60)

    stale_cached_candidate = _candidate(
        edition,
        source,
        title="contenido que no representa la edición",
        uri=URI_309,
        duration_ms=60_000,
    )
    replacement_candidate = _candidate(
        edition,
        source,
        uri=URI_251,
        duration_ms=61_000,
    )
    client = FakeCatalogClient(
        pages=[
            {
                "items": [stale_cached_candidate, replacement_candidate],
                "next": None,
            }
        ]
    )

    result = match_source_editions(
        client,
        store,
        source,
        (edition,),
        now=NOW - timedelta(minutes=10),
        evaluation_intents={edition.identity: MatchEvaluationIntent.EVIDENCE_RECOVERY},
    )
    outcome = result.outcomes[0]

    # The fresh catalogue positively contradicts A and uniquely selects B, but the temporal
    # persistence guard rejects the 09:20 repair because A was stored at 09:30. The public
    # result must therefore stay aligned with durable SQLite and must not publish positive
    # planner evidence for the rejected repair.
    assert outcome.from_cache
    assert outcome.status is MatchStatus.MATCHED
    assert outcome.spotify_episode_uri == URI_309
    assert outcome.spotify_duration_seconds == 60
    assert outcome.planner_input_evidence_qualified is False

    durable = store.get_match_state(*edition.identity)
    assert durable is not None
    assert durable.status is MatchStatus.MATCHED
    assert durable.spotify_episode_uri == URI_309
    assert durable.spotify_duration_seconds == 60
'''
if "test_rejected_clock_rollback_repair_keeps_durable_mapping_and_never_qualifies" in regression:
    raise SystemExit("matcher rollback regression already present")
regression_path.write_text(regression + addition)


# 5. Engine-level regression: no match-input may be published for the rejected repair, and
# once wall-clock time catches up the same recovery can persist B and certify it normally.
engine_path = Path("tests/test_engine.py")
engine = engine_path.read_text()
engine_addition = r'''


def test_clock_rollback_rejected_repair_never_publishes_match_input_and_recovers_later(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    replacement_uri = "spotify:episode:" + "9" * 22

    class ReplacementSpotify(_Spotify):
        replacement_catalogue = False

        def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0):
            if not self.replacement_catalogue:
                return super().show_episodes(show_id, limit=limit, offset=offset)
            assert show_id == "ser-show"
            assert limit == 50
            assert offset == 0
            self.show_calls += 1
            return catalogue_page(
                [
                    {
                        "uri": SER_URI,
                        "name": "contenido que no representa la edición",
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 60_000,
                    },
                    {
                        "uri": replacement_uri,
                        "name": SER_TITLE,
                        "release_date": "2026-08-30",
                        "release_date_precision": "day",
                        "duration_ms": 61_000,
                    },
                ],
                offset=offset,
                limit=limit,
            )

    spotify = ReplacementSpotify()
    runner = EngineRunner(
        _config("first"),
        _store(tmp_path),
        _Auth(),
        fetcher=lambda _url: _rss(),
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )
    assert runner.run_cycle().ok
    assert runner.store.get_spotify_episode_uri(SourceId("ser"), "ser-1") == SER_URI
    assert runner.planner_journal.get_match_input("ser", "ser-1") is not None
    runner.planner_journal.clear_match_input("ser", "ser-1")
    spotify.replacement_catalogue = True
    writes_before = tuple(spotify.replacements)

    # Roll the runner clock behind the durable 10:00 match. The catalogue proves A stale and B
    # correct, but SQLite must reject a repair stamped 09:50. The engine must not publish current
    # evidence for B (or for A) and must not mutate the destination from the durable A state.
    clock[0] = NOW - timedelta(minutes=10)
    rolled_back = runner.run_cycle()
    assert rolled_back.ok
    durable = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert durable is not None
    assert durable.status is MatchStatus.MATCHED
    assert durable.spotify_episode_uri == SER_URI
    assert runner.planner_journal.get_match_input("ser", "ser-1") is None
    assert spotify.items["destination-first"] == [SER_URI]
    assert tuple(spotify.replacements) == writes_before

    # Once time is newer than the durable row, the same evidence-recovery path may persist B and
    # then publish evidence for the state that is actually on disk.
    clock[0] = NOW + timedelta(minutes=1)
    recovered = runner.run_cycle()
    assert recovered.ok
    durable = runner.store.get_match_state(SourceId("ser"), "ser-1")
    assert durable is not None
    assert durable.status is MatchStatus.MATCHED
    assert durable.spotify_episode_uri == replacement_uri
    assert durable.spotify_duration_seconds == 61
    assert runner.planner_journal.get_match_input("ser", "ser-1") is not None
    assert spotify.items["destination-first"] == [replacement_uri]
'''
if "test_clock_rollback_rejected_repair_never_publishes_match_input_and_recovers_later" in engine:
    raise SystemExit("engine rollback regression already present")
engine_path.write_text(engine + engine_addition)

print("PR #211 persistence acknowledgement fix applied")
