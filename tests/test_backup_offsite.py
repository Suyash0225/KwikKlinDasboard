"""Optional off-host backup uploads must fail safely and alert on errors."""

from pathlib import Path

import pytest

from app.config import settings
from app.services import backup


class FakeS3Client:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.uploads: list[tuple[str, str, str]] = []

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        if self.error is not None:
            raise self.error
        self.uploads.append((filename, bucket, key))


@pytest.mark.asyncio
async def test_offsite_backup_disabled_warns_without_crashing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "BACKUP_S3_BUCKET", "")
    monkeypatch.setattr(
        backup, "_make_s3_client",
        lambda: (_ for _ in ()).throw(AssertionError("client must not be built")),
    )
    assert await backup.upload_offsite_backup(Path("kwikklin-test.dump")) is None


@pytest.mark.asyncio
async def test_offsite_upload_uses_bucket_prefix_and_filename(monkeypatch) -> None:
    client = FakeS3Client()
    monkeypatch.setattr(settings, "BACKUP_S3_BUCKET", "private-backups")
    monkeypatch.setattr(settings, "BACKUP_S3_PREFIX", "/kwikklin/nightly/")
    monkeypatch.setattr(backup, "_make_s3_client", lambda: client)
    path = Path("/tmp/kwikklin-20261017-213000.dump")

    key = await backup.upload_offsite_backup(path)

    assert key == "kwikklin/nightly/kwikklin-20261017-213000.dump"
    assert client.uploads == [
        (str(path), "private-backups", "kwikklin/nightly/kwikklin-20261017-213000.dump")
    ]


@pytest.mark.asyncio
async def test_offsite_upload_failure_alerts_owner(monkeypatch) -> None:
    client = FakeS3Client(RuntimeError("endpoint unavailable"))
    alerts: list[str] = []
    monkeypatch.setattr(settings, "BACKUP_S3_BUCKET", "private-backups")
    monkeypatch.setattr(settings, "BACKUP_S3_PREFIX", "kwikklin/backups")
    monkeypatch.setattr(backup, "_make_s3_client", lambda: client)

    async def capture_alert(message: str) -> None:
        alerts.append(message)

    monkeypatch.setattr(backup, "_tell_the_owner", capture_alert)
    result = await backup.upload_offsite_backup(Path("kwikklin-test.dump"))

    assert result is None
    assert len(alerts) == 1
    assert "Off-site backup upload FAIL" in alerts[0]
    assert "kwikklin-test.dump" in alerts[0]
