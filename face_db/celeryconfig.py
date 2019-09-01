from celery.schedules import crontab
CELERY_IMPORTS = ('face_app')
CELERYBEAT_SCHEDULE = {
    'check_face_duplicate': {
        'task': 'periodic_face_duplicate',
        # Every minute
        'schedule': crontab(minute="10"),
    }
}
