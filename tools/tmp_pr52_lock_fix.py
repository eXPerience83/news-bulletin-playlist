from pathlib import Path

runtime = Path("src/news_bulletin_playlist/engine_runtime.py")
text = runtime.read_text()
old = '''        try:
            with synchronization.hold():
                if path == "/admin/playlists/activate":
                    self._activate_managed_playlist(service, form)
                elif path == "/admin/playlists/update":
                    self._update_managed_playlist(service, form)
                else:
                    service.stop_managing(playlist_id_from_form(form))
                configured = any(playlist.enabled for playlist in service.snapshot().managed)
                lifecycle.reconcile(configured=configured)
                self.__class__.engine_scheduler = lifecycle.scheduler
'''
new = '''        try:
            with synchronization.hold():
                if path == "/admin/playlists/activate":
                    self._activate_managed_playlist(service, form)
                elif path == "/admin/playlists/update":
                    self._update_managed_playlist(service, form)
                else:
                    service.stop_managing(playlist_id_from_form(form))
                configured = any(playlist.enabled for playlist in service.snapshot().managed)
            lifecycle.reconcile(configured=configured)
            self.__class__.engine_scheduler = lifecycle.scheduler
'''
if text.count(old) != 1:
    raise SystemExit("expected managed-post lock block exactly once")
runtime.write_text(text.replace(old, new))

tests = Path("tests/test_managed_admin_runtime.py")
test_text = tests.read_text()
marker = "def test_lifecycle_reconcile_runs_after_configuration_lock_is_released()"
if marker not in test_text:
    test_text += '''\n\n\nclass _TrackingHold:\n    def __init__(self, synchronization: "_TrackingSynchronization") -> None:\n        self.synchronization = synchronization\n\n    def __enter__(self) -> None:\n        self.synchronization.depth += 1\n\n    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:\n        del exc_type, exc, traceback\n        self.synchronization.depth -= 1\n\n\nclass _TrackingSynchronization:\n    def __init__(self) -> None:\n        self.depth = 0\n\n    def hold(self) -> _TrackingHold:\n        return _TrackingHold(self)\n\n\nclass _AssertUnlockedLifecycle(_FakeLifecycle):\n    def __init__(self, synchronization: _TrackingSynchronization) -> None:\n        super().__init__()\n        self.synchronization = synchronization\n\n    def reconcile(self, *, configured: bool) -> None:\n        assert self.synchronization.depth == 0\n        super().reconcile(configured=configured)\n\n\ndef test_lifecycle_reconcile_runs_after_configuration_lock_is_released(tmp_path: Path) -> None:\n    service, _ = _service(tmp_path)\n    _activate_direct(service)\n    current = service.snapshot().managed[0]\n    synchronization = _TrackingSynchronization()\n    lifecycle = _AssertUnlockedLifecycle(synchronization)\n    security = LanAdminSecurity(_PASSWORD)\n    handler = _HandlerHarness(\n        tmp_path=tmp_path,\n        service=service,\n        lifecycle=lifecycle,\n        path="/admin/playlists/stop",\n        form={\n            "csrf_token": [security.issue_csrf_token()],\n            "playlist_id": [str(current.id)],\n        },\n        auth_provider=None,\n        security=security,\n    )\n    handler.configuration_synchronization = synchronization  # type: ignore[assignment]\n\n    handler.do_POST()\n\n    assert _response(handler).status == HTTPStatus.SEE_OTHER\n    assert synchronization.depth == 0\n    assert lifecycle.reconcile_calls == [False]\n'''
    tests.write_text(test_text)
