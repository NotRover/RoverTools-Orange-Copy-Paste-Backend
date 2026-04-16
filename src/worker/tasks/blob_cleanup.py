from src.worker.app import app


@app.task(name="src.worker.tasks.blob_cleanup.cleanup_orphan_blobs")
def cleanup_orphan_blobs() -> None:
    # TODO: delete unconfirmed blobs older than 1 hour (Phase 4)
    pass
