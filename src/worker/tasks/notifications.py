from src.worker.app import app


@app.task(name="src.worker.tasks.notifications.send_push_notification")
def send_push_notification(user_id: str, event: dict) -> None:
    # TODO: FCM/APNs integration (future)
    pass
